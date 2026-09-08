"""ToolSpec / ToolResult / ToolExecutor and the observational harness hooks.

Harness P1-A.  The two runners share one execution reality:

* the runner (legacy loop or LangGraph) owns routing, cycling and recovery;
* the ``ToolExecutor`` owns every execution constraint — identity/scope check,
  argument validation, code-derived security context, evidence/fact
  freshness capture, dispatch, result normalisation, metering and audit;
* registering a tool means registering a ``ToolSpec`` and a handler.  Neither
  runner grows a conditional branch, and the planner catalog is generated
  from the specs, so prompt, guard and executor cannot drift.

Safety boundary placement: the guard, hydration of safety-critical arguments
and the final response checks are direct code paths in the agent layer.  The
hooks here are strictly observational — they receive facts and return None;
there is no API through which a plugin could disable a safety invariant.

Retry ownership (ADR-004, unchanged): specs declare ``retry_owner='none'``;
the SDK runs with max_retries=0, the graph sets no RetryPolicy, and the only
bounded parse-retry lives with its single owner (the planner).  The executor
adds no retry of its own.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from .errors import ToolErrorKind, ToolExecutionError, classify_exception
    from .runtime import RunContext
    from .schema import schema_errors, strip_unknown_keys
except ImportError:  # pragma: no cover - script-style import
    from errors import ToolErrorKind, ToolExecutionError, classify_exception  # type: ignore
    from runtime import RunContext  # type: ignore
    from schema import schema_errors, strip_unknown_keys  # type: ignore

try:
    from ..turn_budget import check_lease
except ImportError:  # pragma: no cover - script-style import
    from turn_budget import check_lease  # type: ignore


def _args_hash(arguments: Any) -> str | None:
    """Deterministic argument fingerprint for replay dedup (P1-C)."""
    import hashlib
    import json
    try:
        return hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                                         default=str).encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None


# Permission model: a permission maps to the roles that may exercise it.  The
# principal is resolved by code; a model proposal can never widen it.
PERMISSION_ROLES: dict[str, frozenset] = {
    "memory:read": frozenset({"caregiver", "ops", "reviewer"}),
    "memory:write": frozenset({"caregiver", "ops"}),
    "rag:search": frozenset({"caregiver", "ops", "reviewer"}),
    "ddi:detect": frozenset({"caregiver", "ops", "reviewer"}),
    "evidence:read": frozenset({"caregiver", "ops", "reviewer"}),
    "clarify": frozenset({"caregiver", "ops"}),
    # Harness P3 (both default-OFF experiments): read-only batch fan-out and
    # read-only worker delegation.  Neither grants any write capability.
    "batch:read": frozenset({"caregiver", "ops"}),
    "delegate:run": frozenset({"caregiver", "ops"}),
}


@dataclass(frozen=True)
class ToolSpec:
    """Typed contract for one tool.  Generated schemas keep the planner
    prompt, the guard and the executor on the same definition.

    Two schema levels by design:

    * ``argument_schema`` — the executor-level interface, i.e. what a
      materialized action may carry, including executor-only fields the model
      never proposes (hydrated warning bodies, context refs, conflict links);
    * ``proposal_schema`` — the model-facing subset shown in the planner
      catalog and enforced on raw proposals.  Defaults to ``argument_schema``.
    """

    name: str
    description: str
    argument_schema: dict[str, Any]
    result_shape: str
    kind: str  # 'read' | 'write'
    required_permission: str
    timeout_owner: str = "executor"   # executor forwards the budget-allowed timeout
    retry_owner: str = "none"         # no layered retries (ADR-004)
    idempotency: str = "pure"         # 'pure' | 'receipt_keyed' | 'none'
    cacheable: bool = False
    parallelizable: bool = False
    proposal_schema: dict[str, Any] | None = None

    @property
    def model_schema(self) -> dict[str, Any]:
        return self.proposal_schema or self.argument_schema

    @property
    def schema_version(self) -> str:
        """Content-derived contract version: a schema/result-shape change is a
        different tool for reuse and signature purposes."""
        import hashlib
        import json
        payload = json.dumps({"schema": self.argument_schema, "shape": self.result_shape},
                             ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


@dataclass
class ToolResult:
    tool: str
    ok: bool
    value: Any = None
    error: dict[str, Any] | None = None      # ToolExecutionError.to_payload() on failure
    evidence_refs: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    receipt_replayed: bool = False

    def observation_payload(self) -> Any:
        """What the loop stores in the Observation and the planner reads."""
        if self.ok:
            return self.value
        return dict(self.error or {"error_kind": "internal_error", "error": "internal_error"})


# ---- hooks -------------------------------------------------------------------


@dataclass
class HookEvent:
    """Read-only observation handed to hooks.  No field is mutable policy."""

    phase: str
    ctx: RunContext
    kind: str | None = None        # model call source: planner|composer|verifier
    tool: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


Hook = Callable[[HookEvent], None]


class HarnessHooks:
    """A tiny observational hook bus shared by both runners.

    ``before_model``/``after_model`` fire around planner/composer/verifier
    calls; ``before_tool``/``after_tool`` around executor dispatches;
    ``before_publish`` before the final safety gate.  Hooks MUST NOT raise to
    veto (there is no veto contract): the safety boundary is code, not a hook.
    """

    def __init__(self):
        self._hooks: dict[str, list[Hook]] = {}

    def add(self, phase: str, hook: Hook) -> None:
        self._hooks.setdefault(phase, []).append(hook)

    def emit(self, phase: str, **kwargs: Any) -> None:
        for hook in self._hooks.get(phase, ()):
            try:
                hook(HookEvent(phase=phase, **kwargs))
            except Exception:
                # An observability hook must never break a turn; the safety
                # boundary does not depend on hooks running at all.
                pass


# ---- executor ----------------------------------------------------------------


class ToolExecutor:
    def __init__(self, *, hooks: HarnessHooks | None = None,
                 error_types: dict[str, ToolErrorKind] | None = None,
                 audit_sink: Callable[[dict[str, Any]], None] | None = None,
                 memory: Any = None,
                 reuse: Any = None,
                 corpus_version_fn: Callable[[], Any] | None = None):
        self.specs: dict[str, ToolSpec] = {}
        self.handlers: dict[str, Callable[[ToolRequest], Any]] = {}
        self.hooks = hooks or HarnessHooks()
        # Domain exception class names → kinds (keeps harness import-free from
        # the memory layer while classifying MemoryPolicyError distinctly).
        self.error_types = error_types or {}
        self.audit_sink = audit_sink
        # The MemoryStore, set by the agent, so write dispatches can capture
        # the current patient revision (evidence/fact freshness record).
        self.memory = memory
        # Harness P2: optional controlled read-reuse (same-run + cross-run).
        # Consulted ONLY for kind='read' + idempotency='pure' + cacheable
        # specs; failures are never stored; writes/receipts/permission checks
        # always execute for real.
        self.reuse = reuse
        self.corpus_version_fn = corpus_version_fn

    # ---- registry ----------------------------------------------------------

    def register(self, spec: ToolSpec, handler: Callable[["ToolRequest"], Any],
                 *, override: bool = False) -> None:
        if spec.name in self.specs and not override:
            raise ValueError(f"tool already registered: {spec.name}")
        self.specs[spec.name] = spec
        self.handlers[spec.name] = handler

    def spec(self, name: str) -> ToolSpec | None:
        return self.specs.get(name)

    def catalog(self) -> dict[str, dict[str, Any]]:
        """Planner/guard catalog generated from the specs — one source of
        truth for prompt, validation and execution.  Model-facing subsets
        only; executor-only fields never enter the prompt."""
        return {name: dict(spec.model_schema) for name, spec in self.specs.items()}

    def validate_arguments(self, name: str, arguments: Any) -> list[str]:
        spec = self.specs.get(name)
        if spec is None:
            return [f"unknown tool: {name}"]
        if not isinstance(arguments, dict):
            return ["arguments must be a JSON object"]
        return schema_errors(spec.argument_schema, arguments, "arguments")

    # ---- execution ---------------------------------------------------------

    def _reuse_eligible(self, spec: ToolSpec) -> bool:
        """Controlled reuse touches ONLY pure reads whose spec opts in.
        Writes, receipt-keyed operations and permission checks never hit it."""
        return (self.reuse is not None and spec.kind == "read"
                and spec.idempotency == "pure" and spec.cacheable)

    def execute(self, ctx: RunContext, tool: str, arguments: Any, *,
                state: Any = None) -> ToolResult:
        started = time.perf_counter()
        cycle = getattr(state, "cycle", None)
        spec = self.specs.get(tool)
        if spec is None:
            return self._failed(tool, ToolExecutionError(
                ToolErrorKind.UNKNOWN_TOOL, f"tool is not registered: {tool}"),
                started, corrections=[], ctx=ctx, state=state)
        # P2: the explicit refresh channel is executor-owned — popped before
        # validation so it is never treated as a model argument, and a refresh
        # request never fails schema validation.
        refresh_requested = False
        if isinstance(arguments, dict) and self._reuse_eligible(spec) and "refresh" in arguments:
            arguments = dict(arguments)
            refresh_requested = bool(arguments.pop("refresh"))
        self.hooks.emit("before_tool", ctx=ctx, tool=tool, meta={
            "arguments_keys": sorted(arguments) if isinstance(arguments, dict) else [],
            "cycle": cycle, "args_hash": _args_hash(arguments)})
        # 1) identity / scope / permission — trusted principal only.
        allowed = PERMISSION_ROLES.get(spec.required_permission, frozenset())
        if not ctx.principal.has_role(*allowed):
            return self._failed(tool, ToolExecutionError(
                ToolErrorKind.PERMISSION_DENIED,
                f"principal lacks a role for {spec.required_permission}"), started, corrections=[])
        # 2) argument validation against the spec schema.
        if not isinstance(arguments, dict):
            return self._failed(tool, ToolExecutionError(
                ToolErrorKind.INVALID_ARGUMENTS, "arguments must be a JSON object",
                recoverable=True), started, corrections=[], ctx=ctx, state=state)
        problems = schema_errors(spec.argument_schema, arguments, "arguments")
        if problems:
            return self._failed(tool, ToolExecutionError(
                ToolErrorKind.INVALID_ARGUMENTS, "; ".join(problems), recoverable=True),
                started, corrections=[], ctx=ctx, state=state)
        cleaned, dropped = strip_unknown_keys(spec.argument_schema, arguments)
        # 3) code-derived security context (event/operation keys), 4) evidence
        # and fact freshness are captured, not model-supplied.
        ctx.operation_id = ctx.operation_id or f"{ctx.run_id}:{tool}"
        if spec.kind == "write" or self._reuse_eligible(spec):
            ctx.patient_revision = self._current_revision(state)
        # 5) budget/lease gate: sync dispatch is refused once the run is
        # cancelled or the lease is lost (late results are rejected below too).
        if ctx.cancelled():
            return self._failed(tool, ToolExecutionError(
                ToolErrorKind.CANCELLED, "run is cancelled or over budget"),
                started, corrections=dropped, ctx=ctx, state=state)
        # 5b) P2 controlled reuse: identical pure read over an unchanged world
        # is served from the reuse layers; the no-progress tracker still sees
        # the repeat through its own signature.
        if self._reuse_eligible(spec):
            hit = self.reuse.lookup(
                run_id=ctx.run_id, tool=tool, arguments=arguments,
                scope_id=ctx.principal.scope_id, patient_revision=ctx.patient_revision,
                corpus_version=self._corpus_version(), tool_version=self._tool_version(spec),
                force=refresh_requested)
            if hit is not None:
                value = dict(hit["value"]) if isinstance(hit["value"], dict) else hit["value"]
                if isinstance(value, dict):
                    value["cache_hit"] = {"source": hit["source"], "refresh": False}
                result = ToolResult(tool=tool, ok=True, value=value,
                                    evidence_refs=[*value.get("evidence_refs", [])]
                                    if isinstance(value, dict) else [],
                                    metrics={"attempts": 1, "corrections": dropped,
                                             "cache_hit": hit["source"],
                                             "active_seconds": round(time.perf_counter() - started, 6)})
                self.hooks.emit("after_tool", ctx=ctx, tool=tool, meta={
                    "ok": True, "reused": True, "cache_source": hit["source"],
                    "cycle": cycle, "args_hash": _args_hash(arguments),
                    "evidence_refs": result.evidence_refs})
                self._audit(ctx, result)
                return result
        request = ToolRequest(ctx=ctx, tool=tool, spec=spec, arguments=cleaned, state=state,
                              corrections=dropped)
        # 6) dispatch (receipt semantics live inside the handler), 7) normalize.
        try:
            value = self.handlers[tool](request)
        except ToolExecutionError as exc:
            return self._failed(tool, exc, started, corrections=dropped, ctx=ctx, state=state)
        except Exception as exc:
            classified = classify_exception(exc, known_types=self.error_types)
            if classified is exc:  # passthrough re-raise (budget/lease/interrupt)
                raise
            return self._failed(tool, classified, started, corrections=dropped, ctx=ctx, state=state)
        # Late-result isolation: a lease lost during dispatch rejects the
        # projection exactly like the memory layer does inside its transactions.
        if spec.kind == "write":
            check_lease()
        result = ToolResult(tool=tool, ok=True, value=value,
                            evidence_refs=[*value.get("evidence_refs", [])]
                            if isinstance(value, dict) else [],
                            metrics={"attempts": 1, "corrections": dropped})
        if isinstance(value, dict) and value.get("operation_replayed"):
            result.receipt_replayed = True
        # P2: only successful pure reads enter the reuse layers — a failure,
        # an unknown outcome or an empty retrieval is never cached.
        if self._reuse_eligible(spec):
            try:
                self.reuse.store(run_id=ctx.run_id, tool=tool, arguments=arguments,
                                 value=value, scope_id=ctx.principal.scope_id,
                                 patient_revision=ctx.patient_revision,
                                 corpus_version=self._corpus_version(),
                                 tool_version=self._tool_version(spec))
            except Exception:
                pass
        result.metrics["active_seconds"] = round(time.perf_counter() - started, 6)
        self.hooks.emit("after_tool", ctx=ctx, tool=tool, meta={
            "ok": True, "replayed": result.receipt_replayed, "cycle": cycle,
            "args_hash": _args_hash(arguments), "evidence_refs": result.evidence_refs})
        self._audit(ctx, result)
        return result

    def execute_read_batch(self, ctx: RunContext, calls: Any, *,
                           state: Any = None, max_items: int = 8) -> list[ToolResult]:
        """Harness P3 control condition: ordinary read-only tool BATCHING on
        the shared execution layer.  Every item is dispatched through the SAME
        ``execute`` path (identity/scope/permission/schema/evidence checks per
        item — no second validation rule).  Only ``kind='read'`` +
        ``idempotency='pure'`` specs are eligible: a batch containing anything
        else is refused WHOLE, before any dispatch, so no partial side effects
        can occur.  Code-called primitive — deliberately NOT a model-callable
        tool; the planner proposes single tools (or the P3 ``batch_read``
        wrapper when its flag is on)."""
        if not isinstance(calls, list) or not (1 <= len(calls) <= max_items):
            raise ToolExecutionError(
                ToolErrorKind.INVALID_ARGUMENTS,
                f"batch must be a list of 1..{max_items} calls", recoverable=True)
        for item in calls:
            if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
                raise ToolExecutionError(ToolErrorKind.INVALID_ARGUMENTS,
                                         "batch items must be {tool, arguments}", recoverable=True)
            spec = self.specs.get(str(item.get("tool")))
            if spec is None:
                raise ToolExecutionError(ToolErrorKind.UNKNOWN_TOOL,
                                         f"batch tool is not registered: {item.get('tool')}")
            if spec.kind != "read" or spec.idempotency != "pure":
                raise ToolExecutionError(
                    ToolErrorKind.POLICY_VIOLATION,
                    f"batch is limited to pure reads; {spec.name} is {spec.kind}/{spec.idempotency}")
        return [self.execute(ctx, str(item["tool"]), item["arguments"], state=state)
                for item in calls]

    def _current_revision(self, state: Any) -> int | None:
        if self.memory is None:
            return None
        try:
            return self.memory.scope_revision("medications") + self.memory.scope_revision("semantic")
        except Exception:
            return None

    def _corpus_version(self) -> Any:
        if self.corpus_version_fn is None:
            return None
        try:
            return self.corpus_version_fn()
        except Exception:
            return None

    @staticmethod
    def _tool_version(spec: ToolSpec) -> str:
        # The executor interface itself is the tool contract version: a schema
        # or result_shape change IS a different tool for reuse purposes.
        return spec.schema_version

    def _failed(self, tool: str, exc: ToolExecutionError, started: float, *,
                corrections: list[str], ctx: RunContext | None = None,
                state: Any = None) -> ToolResult:
        result = ToolResult(tool=tool, ok=False, error=exc.to_payload(),
                            metrics={"attempts": 1, "corrections": corrections,
                                     "active_seconds": round(time.perf_counter() - started, 6)})
        self.hooks.emit("after_tool", ctx=ctx, tool=tool, meta={
            "ok": False, "error_kind": exc.kind.value, "recoverable": exc.recoverable,
            "cycle": getattr(state, "cycle", None)})
        self._audit(ctx, result)
        return result

    def _audit(self, ctx: RunContext | None, result: ToolResult) -> None:
        if self.audit_sink is None:
            return
        try:
            self.audit_sink({
                "run_id": getattr(ctx, "run_id", None), "turn_id": getattr(ctx, "turn_id", None),
                "tool": result.tool, "ok": result.ok, "error_kind": (result.error or {}).get("error_kind"),
                "replayed": result.receipt_replayed, "metrics": result.metrics,
            })
        except Exception:
            pass


@dataclass
class ToolRequest:
    ctx: RunContext
    tool: str
    spec: ToolSpec
    arguments: dict[str, Any]
    state: Any = None
    corrections: list[str] = field(default_factory=list)
