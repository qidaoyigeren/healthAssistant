"""Default ToolSpec registry for the medication coordinator agent.

Harness P1-A.  The argument schemas used to live as a hand-written dict in
``agent.py`` next to the prompt; they are now the ToolSpec definitions below —
the planner prompt, the guard and the executor all read the same objects, so
prompt/schema/Python parameters cannot drift.  The lenient semantics of the
canonical proposal are unchanged (extra fields ignored, rationale optional,
no provider oneOf schemas).
"""
from __future__ import annotations

from typing import Any

try:
    from .tools import ToolExecutor, ToolSpec
    from .evidence import (EvidenceStore, capture_from_ddi_warnings,
                           capture_from_rag_result)
except ImportError:  # pragma: no cover - script-style import
    from tools import ToolExecutor, ToolSpec  # type: ignore
    from evidence import EvidenceStore, capture_from_ddi_warnings, capture_from_rag_result  # type: ignore

try:
    from . import delegation as _delegation
except ImportError:  # pragma: no cover - script-style import
    import delegation as _delegation  # type: ignore

try:
    from .errors import ToolErrorKind, ToolExecutionError
except ImportError:  # pragma: no cover - script-style import
    from errors import ToolErrorKind, ToolExecutionError  # type: ignore


MEMORY_READ_QUERIES = (
    "snapshot", "current_medications", "medication_timeline", "conflicts",
    "context_packet", "pending_rechecks",
)
MEMORY_WRITE_OPERATIONS = ("consolidate_event", "record_warnings", "create_clinical_conflict")


# The model selects a logical write only.  Warning bodies, citations, memory
# references and conflict links are executor-only fields hydrated from real
# observations; the executor rejects any model-supplied substitute.
DEFAULT_TOOL_SPECS: dict[str, ToolSpec] = {
    "ddi_check": ToolSpec(
        name="ddi_check",
        description="Run the deterministic DDI detector over the authoritative medication list.",
        argument_schema={
            "type": "object",
            "properties": {
                "medications": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "focus_medication": {"type": "string"},
            },
            "required": ["medications"],
        },
        result_shape="dict(medications_checked, focus_medication, warnings[], detector)",
        kind="read",
        required_permission="ddi:detect",
        timeout_owner="executor",
        retry_owner="none",
        idempotency="pure",
        cacheable=True,
    ),
    "rag_search": ToolSpec(
        name="rag_search",
        description="Hybrid retrieval over the local drug-label corpus with a deterministic fallback.",
        argument_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "section": {"type": "string"},
                "drug_name": {"type": "string"},
            },
            "required": ["query"],
        },
        result_shape="dict(query, mode, results[])",
        kind="read",
        required_permission="rag:search",
        idempotency="pure",
        cacheable=True,
    ),
    "memory_read": ToolSpec(
        name="memory_read",
        description="Read one bounded view of patient memory (snapshot/medications/timeline/conflicts).",
        argument_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "enum": list(MEMORY_READ_QUERIES)}},
            "required": ["query"],
        },
        result_shape="dict depending on query",
        kind="read",
        required_permission="memory:read",
        idempotency="pure",
        cacheable=True,
    ),
    "memory_write": ToolSpec(
        name="memory_write",
        description=("Persist a logical write (consolidate_event / record_warnings / "
                     "create_clinical_conflict).  Authoritative bodies and refs are injected "
                     "from real observations, never from the proposal."),
        # ``argument_schema`` describes the EXECUTOR interface (what a
        # materialized action may carry, including hydrated executor-only
        # fields); ``proposal_schema`` stays the minimal logical choice shown
        # to the model — it can only pick the operation.
        argument_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": list(MEMORY_WRITE_OPERATIONS)},
                "warnings": {"type": "array"},
                "context_refs": {"type": "array"},
                "subject_key": {"type": "string"},
                "reported_event_ref": {"type": "string"},
                "warning_ref": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["operation"],
        },
        result_shape="dict with operation outcome, memory_refs, receipt flags",
        kind="write",
        required_permission="memory:write",
        idempotency="receipt_keyed",
        proposal_schema={
            "type": "object",
            "properties": {"operation": {"type": "string", "enum": list(MEMORY_WRITE_OPERATIONS)}},
            "required": ["operation"],
        },
    ),
    "ask_clarification": ToolSpec(
        name="ask_clarification",
        description="Ask the caregiver a clarifying question; cannot diagnose or prescribe.",
        argument_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
        result_shape="dict(needs_user_input, question)",
        kind="read",
        required_permission="clarify",
        idempotency="pure",
    ),
}


# Harness P1-B: the only way back to full evidence content.  The model supplies
# an evidence id it received from a capture view — never a path or URL — and
# the executor/store enforce scope, existence, hash and pagination.
READ_EVIDENCE_SPEC = ToolSpec(
    name="read_evidence",
    description=("Read back a bounded slice of previously captured evidence by id. "
                 "Only ids obtained from tool results in this run are valid; "
                 "paths/URLs are rejected."),
    argument_schema={
        "type": "object",
        "properties": {
            "evidence_id": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["evidence_id"],
    },
    result_shape="dict(evidence_id, content, offset, returned_chars, total_chars, truncated, source_uri, content_hash)",
    kind="read",
    required_permission="evidence:read",
    idempotency="pure",
    cacheable=True,
)


# Harness P3, both default-OFF (independent flags; see delegation.py and the
# P3 report).  ``batch_read`` is the ordinary read-only batching CONTROL; the
# planner proposes it like any single tool and the executor fans the pure
# reads out through execute_read_batch (per-item checks unchanged).
BATCH_READ_SPEC = ToolSpec(
    name="batch_read",
    description=("Execute several pure read-only tool calls in one planner "
                 "decision.  Writes and non-pure tools are refused whole."),
    argument_schema={
        "type": "object",
        "properties": {
            "calls": {
                "type": "array", "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
                    "required": ["tool", "arguments"],
                },
            },
        },
        "required": ["calls"],
    },
    result_shape="dict(results[{tool, ok, value|error, evidence_refs}], partial_failures)",
    kind="read",
    required_permission="batch:read",
    idempotency="pure",
    cacheable=False,
)

# ``delegate_task`` hands a MINIMIZED read-only subtask to a fixed-role worker
# (evidence_retriever / evidence_consistency_checker).  The model proposes
# role/goal/queries/claims/refs; toolset, budget, deadline, revision and
# receipt handling are code-derived.  The result is DATA.
DELEGATE_TASK_SPEC = ToolSpec(
    name="delegate_task",
    description=("Delegate a read-only subtask to a fixed worker role "
                 "(evidence_retriever / evidence_consistency_checker; neither is a "
                 "doctor, pharmacist or human reviewer).  Returns structured "
                 "evidence refs, verbatim excerpts and verification verdicts — "
                 "never medical conclusions."),
    argument_schema={
        "type": "object",
        "properties": {
            "role": {"type": "string",
                     "enum": sorted(_delegation.WORKER_ROLES)},
            "goal": {"type": "string"},
            "queries": {"type": "array", "items": {"type": "string"}},
            "claims": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["role", "goal"],
    },
    result_shape=("dict(task_id, role, status, evidence_refs, excerpts, claims, "
                  "open_questions, failures, usage, historical_only, data_only)"),
    kind="read",
    required_permission="delegate:run",
    idempotency="pure",
    cacheable=False,
    # Proposal schema == argument schema here: every field is model-chosen
    # input, bounded and re-validated by the coordinator.  Toolset/budget/
    # deadline are NEVER model fields.
)


def batch_read_enabled() -> bool:
    import os as _os
    return _os.getenv("STAGE0_READ_BATCH", "").strip().lower() in {"1", "true", "yes", "on"}


def build_default_executor(agent: Any, *, hooks: Any = None,
                           audit_sink: Any = None,
                           evidence_store: Any = None,
                           reuse: Any = None,
                           corpus_version_fn: Any = None,
                           enable_batch: bool | None = None,
                           enable_delegation: bool | None = None) -> ToolExecutor:
    """Bind the five built-in tools of the coordinator agent to the executor.

    Handlers adapt the existing tool callables; code-derived enrichment (the
    patient-condition warning attachment for RAG results) moves here so it is
    part of the single shared execution path for both runners.  When an
    ``EvidenceStore`` is supplied (P1-B), retrieval results are captured as
    immutable evidence and the authorised ``read_evidence`` tool is
    registered — no runner or prompt code changes.  A ``ReuseCoordinator``
    (P2) enables controlled same-run/cross-run read reuse behind its flags.
    """
    executor = ToolExecutor(
        hooks=hooks,
        error_types={
            "MemoryPolicyError": ToolErrorKind.POLICY_VIOLATION,
            "OperationConflict": ToolErrorKind.POLICY_VIOLATION,
            "ReviewFactsMovedError": ToolErrorKind.POLICY_VIOLATION,
            "ReviewStateError": ToolErrorKind.POLICY_VIOLATION,
        },
        audit_sink=audit_sink,
        memory=getattr(agent, "memory", None),
        reuse=reuse,
        corpus_version_fn=corpus_version_fn,
    )

    def _capture_evidence(request, result, *, kind):
        if evidence_store is None or not isinstance(result, dict):
            return result
        ctx = request.ctx
        revision = ctx.patient_revision
        if kind == "rag":
            view = capture_from_rag_result(evidence_store, result, run_id=ctx.run_id,
                                           query=str(request.arguments.get("query", "")),
                                           patient_revision=revision)
        else:
            view = capture_from_ddi_warnings(evidence_store, result, run_id=ctx.run_id,
                                             patient_revision=revision)
        if view:
            result = dict(result)
            result["evidence"] = view
            result["evidence_refs"] = [item["evidence_id"] for item in view]
            _attach_warning_evidence_ids(result, view, kind)
        return result

    def _attach_warning_evidence_ids(result, view, kind):
        """Product P1: link each persisted warning to its captured evidence id.

        capture_from_* iterate warnings/results in order and skip the same
        entries (missing source_text / empty text), so zipping the keyed
        entries with the view reconstructs the exact warning → evidence map.
        The ids travel on the warning dicts into record_warnings → conclusion
        source_refs, making 原文 read-back addressable per warning.
        """
        if kind == "ddi":
            keyed = [w for w in result.get("warnings") or []
                     if isinstance(w, dict) and w.get("source_text")]
            for warning, item in zip(keyed, view):
                warning["evidence_id"] = item["evidence_id"]
            return
        chunks = [r for r in result.get("results") or []
                  if isinstance(r, dict) and r.get("text")]
        by_text: dict[str, str] = {}
        for chunk, item in zip(chunks, view):
            by_text.setdefault(str(chunk["text"]), str(item["evidence_id"]))
        for warning in result.get("warnings") or []:
            if isinstance(warning, dict) and warning.get("source_text") in by_text:
                warning["evidence_id"] = by_text[str(warning["source_text"])]

    for name, spec in DEFAULT_TOOL_SPECS.items():
        tool_callable = agent.tools[name]

        def handler(request, _callable=tool_callable, _name=name):
            if _name == "memory_write":
                _verify_hydrated_write(request)
                return _callable(state=request.state, **request.arguments)
            if _name == "rag_search":
                return _capture_evidence(request, agent._attach_condition_warnings(
                    request.state, _callable(**request.arguments)), kind="rag")
            if _name == "ddi_check":
                return _capture_evidence(request, _callable(**request.arguments), kind="ddi")
            return _callable(**request.arguments)

        executor.register(spec, handler)

    if evidence_store is not None:
        executor.register(READ_EVIDENCE_SPEC, lambda request: evidence_store.read(
            request.arguments["evidence_id"],
            offset=request.arguments.get("offset", 0),
            limit=request.arguments.get("limit", 2000)))

    # Harness P3: the two read-only experiment tools, each behind its own
    # default-OFF flag.  Registered here they enter the planner catalog, the
    # guard and both runners through the single source of truth — no runner
    # grows a branch.  Flags off ⇒ not registered ⇒ a proposal is unknown_tool.
    if enable_batch is None:
        enable_batch = batch_read_enabled()
    if enable_batch:
        def _batch_handler(request):
            results = executor.execute_read_batch(request.ctx, request.arguments["calls"],
                                                  state=request.state)
            value = {"results": [{"tool": r.tool, "ok": r.ok,
                                  "value": r.value if r.ok else None,
                                  "error": r.error, "evidence_refs": r.evidence_refs}
                                 for r in results],
                     "partial_failures": sum(1 for r in results if not r.ok)}
            value["evidence_refs"] = [ref for r in results for ref in r.evidence_refs]
            return value
        executor.register(BATCH_READ_SPEC, _batch_handler)

    if enable_delegation is None:
        enable_delegation = _delegation.delegation_enabled()
    if enable_delegation and evidence_store is not None:
        from .delegation import DelegationCoordinator
        coordinator = DelegationCoordinator(agent.memory, executor, evidence_store)
        executor.register(DELEGATE_TASK_SPEC, lambda request: coordinator.submit_and_run(
            request.arguments, ctx=request.ctx, state=request.state))
    return executor


def _verify_hydrated_write(request: Any) -> None:
    """Defense in depth (P1-A contract 6): even a call that bypassed the
    guard's hydration cannot persist model-authored warning bodies or
    conflict links.  The check compares against the SAME single hydration
    implementation the guard uses — never a second, diverging rule.

    raises ToolExecutionError(POLICY_VIOLATION) on a mismatch."""
    arguments = request.arguments
    operation = arguments.get("operation")
    if operation not in {"record_warnings", "create_clinical_conflict"}:
        return
    state = request.state
    try:  # local import: agent layer owns the hydration rule
        from ..agent import PlannerPolicyGuard
    except ImportError:  # pragma: no cover - script-style import
        from agent import PlannerPolicyGuard  # type: ignore
    guard = PlannerPolicyGuard()
    if operation == "record_warnings":
        source = guard._warning_source(state)
        authoritative = guard._warnings_of(source)
        if list(arguments.get("warnings") or []) != list(authoritative):
            raise ToolExecutionError(
                ToolErrorKind.POLICY_VIOLATION,
                "warning bodies must be hydrated from real observations, not proposed")
        return
    # Conflict links: recompute through the guard's own derivation.
    if not guard._clinical_conflict_ready(state):
        raise ToolExecutionError(ToolErrorKind.POLICY_VIOLATION,
                                 "clinical conflict is not grounded in this turn's observations")
    authoritative = guard._clinical_conflict_arguments(state)
    for key in ("subject_key", "reported_event_ref", "warning_ref"):
        if arguments.get(key) != authoritative.get(key):
            raise ToolExecutionError(
                ToolErrorKind.POLICY_VIOLATION,
                f"conflict field {key} must come from the observed episodic record, not the proposal")
