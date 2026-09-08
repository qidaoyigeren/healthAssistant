"""AgentRunner abstraction and LangGraph StateGraph runner (Reliability P1).

The legacy ``MedicationCoordinatorAgent.handle`` loop is split into graph
nodes by responsibility — load_context → plan → execute → compose → publish —
so execution state is checkpointed between steps and a crashed turn resumes
from the last completed step instead of rerunning blind.  The LLM decision
semantics, guard, budget behaviour and safety boundary are the agent's own
components; the graph owns routing and recovery, never medical policy.

Identity and recovery contract:

* one run processes one event; ``thread_id`` is the server-generated
  ``run_id`` mapped in ``workflow_runs`` (P0 identity model);
* the checkpointer (SqliteSaver, own file/tables next to the domain db) is
  NOT transactional with the domain database — the "domain commit succeeded
  but checkpoint missing" window is closed by the P0 operation receipts:
  a replayed ``execute`` node hits the receipt and returns the stored result
  instead of producing a second effect;
* checkpoint state carries only JSON-safe fields (dicts/lists/scalars) —
  no connections, clients, credentials or executable objects;
* accumulated budget (active seconds, estimated tokens) is persisted to
  ``workflow_runs`` and merged on resume — a restart never resets the budget.

Retry ownership (ADR-004): the graph sets NO RetryPolicy.  Task-level
recovery belongs to the outbox lease cycle (P0); per-call retries stay with
their single owner (SDK max_retries=0). Human waiting uses interrupt().
Harness P0 shares durable attempt accounting with the legacy runner, resumes
failed nodes without restarting START, and applies review effects atomically.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict

try:
    from .agent import (
        AgentState,
        CareEvent,
        MedicationCoordinatorAgent,
        Observation,
        PlanningRejected,
        ToolAction,
    )
    from .memory import EpisodicFact, MemoryPolicyError, MemoryStore
except ImportError:  # Support ``python stage0/graph_runner.py`` imports.
    from agent import (  # type: ignore
        AgentState,
        CareEvent,
        MedicationCoordinatorAgent,
        Observation,
        PlanningRejected,
        ToolAction,
    )
    from memory import EpisodicFact, MemoryPolicyError, MemoryStore  # type: ignore


try:
    from .turn_budget import CURRENT, BudgetExceeded, budget_scope, initial_budget
    from .harness.runtime import RunContext
    from .harness.progress import is_cancel_requested, mark_cancelled
except ImportError:
    from turn_budget import CURRENT, BudgetExceeded, budget_scope, initial_budget  # type: ignore
    from harness.runtime import RunContext  # type: ignore
    from harness.progress import is_cancel_requested, mark_cancelled  # type: ignore


logger = logging.getLogger("stage0.graph_runner")


def _as_utc_now_utc() -> datetime:
    return datetime.now(timezone.utc)

GRAPH_VERSION = "1"
STATE_SCHEMA_VERSION = "2"
GRAPH_RUNNER_FLAG = "AGENT_GRAPH_RUNNER"
REVIEW_ENABLED_FLAG = "STAGE0_REVIEW_ENABLED"

# Fixed, code-owned texts for review transitions.  Reviewer decisions may NOT
# inject arbitrary delivered text (no state patches, no free-form strings) —
# their basis is recorded in the audit trail only.
WAITING_REVIEW_NOTICE = (
    "该情形按流程提交专业审核；尚未完成的检查请以正文说明为准。"
    "（当前为本地演示环境，尚未接入真实人工服务；可导出咨询摘要咨询医生/药师。）"
)
REVIEW_RESOLVED_NOTICE = "\n专业审核已完成核对，相关矛盾记录已按审核结论处理；用药调整请咨询医生/药师。"
REVIEW_REJECTED_NOTICE = "\n经专业审核，以上提示需人工进一步核实；请勿据此自行调整任何用药。"
REVIEW_CONFIRMED_NOTICE = "\n专业审核已确认本次报告记录在案。"
REVIEW_GUIDANCE_NOTICE = ("\n专业审核已完成：请携带当前用药清单咨询医生/药师，"
                          "不要自行停药或调整剂量。如出现严重不适，请立即就医。")
REVIEW_WAITING_USER_NOTICE = "\n专业审核需要您补充信息后才能继续；请按提示补充后再次记录。"

# Severity classes that route a turn into clinical review (design 8.1:
# needs_clinical_review).  Deliberately conservative and code-owned.
REVIEW_SEVERITIES = {"contraindicated", "major"}


class WorkflowState(TypedDict, total=False):
    """Graph channel schema.  EVERY value must stay JSON-safe (checkpoint
    redline): dicts/lists/scalars only — no connections, clients, credentials
    or executable objects.  Defined at module level so LangGraph's
    ``get_type_hints`` can resolve the postponed annotations."""

    session_id: str
    turn_id: str
    run_id: str
    event_id: str | None
    client_event_id: str | None
    event: dict[str, Any]
    observations: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    # Harness P1-C: carried across checkpoints so node replays never
    # re-persist already-flushed trace entries (stable-id dedup is the second
    # line of defence in record_turn_trace).
    trace_flushed: int
    reflection_notes: list[str]
    cycle: int
    degraded_reason: str | None
    route: str
    pending_action: dict[str, Any] | None
    budget: dict[str, Any]
    breaker: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    # Reliability P2: human review loop
    review_required: bool
    review_reason_codes: list[str]
    review_logic_key: str
    review_case: dict[str, Any] | None
    review_decision: dict[str, Any] | None
    review_outcome: dict[str, Any] | None


def _observation_to_dict(observation: Observation) -> dict[str, Any]:
    return {
        "tool": observation.tool,
        "purpose": observation.purpose,
        "arguments": observation.arguments,
        "result": observation.result,
        "ok": observation.ok,
        "cycle": observation.cycle,
        "error_kind": observation.error_kind,
        "recoverable": observation.recoverable,
        "evidence_refs": observation.evidence_refs,
        "no_progress": observation.no_progress,
    }


def _observation_from_dict(item: dict[str, Any]) -> Observation:
    return Observation(
        tool=item["tool"], purpose=item["purpose"], arguments=item.get("arguments") or {},
        result=item.get("result"), ok=bool(item.get("ok", True)), cycle=int(item.get("cycle") or 0),
        error_kind=item.get("error_kind"), recoverable=item.get("recoverable"),
        evidence_refs=list(item.get("evidence_refs") or []),
        no_progress=bool(item.get("no_progress")),
    )


class AgentRunner(Protocol):
    """One runner executes one event turn.  Identity comes from the service
    layer (event_id/run_id); ``turn_id`` stays the agent-facing turn key."""

    def run(self, *, event: CareEvent, session_id: str, turn_id: str,
            client_event_id: str | None = None,
            event_id: str | None = None, run_id: str | None = None) -> Any: ...


class LegacyAgentRunner:
    """Direct pass-through to ``MedicationCoordinatorAgent.handle`` — the
    unchanged pre-P1 behaviour, plus workflow_runs bookkeeping so version
    routing can see legacy runs."""

    graph_version = "legacy"

    def _save_manifest(self, run_id: str) -> None:
        """Harness P1-C: one immutable RunManifest per run (best-effort)."""
        try:
            from .harness.manifest import ManifestStore, build_manifest
            store = ManifestStore(self.agent.memory.connection, self.agent.memory._lock)
            store.save(build_manifest(run_id=run_id, agent=self.agent,
                                      graph_version=self.graph_version,
                                      max_cycles=self.agent.max_cycles))
        except Exception:
            logger.debug("manifest save failed", exc_info=True)

    def __init__(self, agent: MedicationCoordinatorAgent):
        self.agent = agent

    def run(self, *, event: CareEvent, session_id: str, turn_id: str,
            client_event_id: str | None = None,
            event_id: str | None = None, run_id: str | None = None) -> Any:
        run_id = run_id or turn_id
        existing = self.agent.memory.workflow_run_get(run_id)
        if existing and existing.get("graph_version") != "accepted":
            from .harness.manifest import enforce_restore
            enforce_restore(memory=self.agent.memory, agent=self.agent, run_id=run_id,
                            graph_version=self.graph_version)
        self.agent.memory.workflow_run_start(
            run_id=run_id, event_id=event_id, idempotency_key=client_event_id,
            thread_id=run_id, graph_version=self.graph_version)
        if not existing or existing.get("graph_version") == "accepted":
            self._save_manifest(run_id)
        try:
            with budget_scope(self.agent.memory, run_id, self.agent.max_cycles):
                response = self.agent.handle(event, session_id=session_id,
                                             turn_id=turn_id, client_event_id=client_event_id)
        except Exception as exc:
            self.agent.memory.workflow_run_update(run_id, status="failed",
                                                  error=f"{type(exc).__name__}")
            raise
        degraded = any(entry.get("phase") in {"budget", "plan"}
                       and (entry.get("exhausted") or entry.get("decision", {}).get("tool") == "circuit_break")
                       for entry in (response.tool_trace or []))
        status = "degraded" if degraded else "succeeded"
        # Harness P2: a cancellation request that landed mid-turn is the final
        # word — committed effects stay, the run is recorded as cancelled.
        if is_cancel_requested(self.agent.memory, run_id):
            status = "cancelled"
            mark_cancelled(self.agent.memory, run_id)
        self.agent.memory.workflow_run_update(
            run_id, status=status,
            result={"text": response.text, "warnings": response.warnings,
                    "conflicts": response.conflicts, "safety_status": response.safety_status,
                    "audit_trail": response.audit_trail,
                    "operation_outcomes": response.operation_outcomes,
                    "answer_bundle": getattr(response, "answer_bundle", None)})
        return response


def build_workflow_state(*, event: CareEvent, session_id: str, turn_id: str,
                         client_event_id: str | None, event_id: str | None,
                         run_id: str, budget: dict[str, Any] | None) -> dict[str, Any]:
    """Initial WorkflowState.  Every value is JSON-safe (checkpoint redline)."""
    ctx = RunContext(run_id=run_id, turn_id=turn_id, session_id=session_id,
                     event_id=event_id, client_event_id=client_event_id)
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "event_id": event_id,
        "client_event_id": client_event_id,
        "event": asdict(event),
        "observations": [],
        "trace": [],
        "trace_flushed": 0,
        "reflection_notes": [],
        "cycle": 0,
        "degraded_reason": None,
        "route": "plan",
        "pending_action": None,
        "budget": initial_budget(saved=budget),
        "breaker": {"consecutive_rejections": 0, "broken": False},
        "result": None,
        "error": None,
        "review_required": False,
        "review_reason_codes": [],
        "review_logic_key": None,
        "review_case": None,
        "review_decision": None,
        "review_outcome": None,
        # Harness P1-A: JSON-safe run context projection (ids/revisions/trace
        # correlation only — no budget session, connections or credentials).
        "ctx": ctx.checkpoint_dict(),
    }


class LangGraphAgentRunner:
    """StateGraph runner: node-level checkpoints, receipt-protected replay,
    persisted budget.  The planner/guard/tools/safety semantics are the
    wrapped agent's own."""

    graph_version = GRAPH_VERSION

    def _save_manifest(self, run_id: str) -> None:
        """Harness P1-C: one immutable RunManifest per run (best-effort)."""
        try:
            from .harness.manifest import ManifestStore, build_manifest
            store = ManifestStore(self.agent.memory.connection, self.agent.memory._lock)
            store.save(build_manifest(run_id=run_id, agent=self.agent,
                                      graph_version=self.graph_version,
                                      state_schema_version=STATE_SCHEMA_VERSION,
                                      review_enabled=self.review_enabled,
                                      max_cycles=self.agent.max_cycles))
        except Exception:
            logger.debug("manifest save failed", exc_info=True)

    def __init__(self, agent: MedicationCoordinatorAgent,
                 checkpointer: Any | None = None,
                 checkpoint_path: str | Path | None = None,
                 review_enabled: bool | None = None):
        self.agent = agent
        self.memory = agent.memory
        self._graph = None
        self._checkpointer = checkpointer
        self._checkpoint_path = str(checkpoint_path) if checkpoint_path else None
        self._checkpoint_conn: sqlite3.Connection | None = None
        if review_enabled is None:
            review_enabled = os.getenv(REVIEW_ENABLED_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}
        self.review_enabled = review_enabled
        self.review_sla_seconds = float(os.getenv("STAGE0_REVIEW_SLA_SECONDS", "600"))

    # ---- infrastructure --------------------------------------------------

    def _ensure_checkpointer(self) -> Any:
        if self._checkpointer is None:
            from langgraph.checkpoint.sqlite import SqliteSaver
            path = self._checkpoint_path or (str(self.memory.db_path) + ".checkpoints")
            self._checkpoint_conn = sqlite3.connect(path, check_same_thread=False)
            saver = SqliteSaver(self._checkpoint_conn)
            saver.setup()
            self._checkpointer = saver
        return self._checkpointer

    def close(self) -> None:
        """Release the checkpointer connection (called on worker stop)."""
        if self._checkpoint_conn is not None:
            try:
                self._checkpoint_conn.close()
            except Exception:
                logger.debug("checkpoint connection close failed", exc_info=True)
            self._checkpoint_conn = None
            self._checkpointer = None
            self._graph = None

    def _ensure_graph(self):
        if self._graph is not None:
            return self._graph
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(WorkflowState)
        graph.add_node("load_context", self._node_load_context)
        graph.add_node("plan", self._node_plan)
        graph.add_node("execute", self._node_execute)
        graph.add_node("compose", self._node_compose)
        graph.add_node("open_review", self._node_open_review)
        graph.add_node("await_review", self._node_await_review)
        graph.add_node("apply_review", self._node_apply_review)
        graph.add_node("publish", self._node_publish)

        def _after_plan(state: WorkflowState) -> str:
            route = state.get("route", "plan")
            if route == "compose":
                return "compose"
            if route == "execute":
                return "execute"
            return "plan"  # rejection loop: replan without executing

        def _after_compose(state: WorkflowState) -> str:
            if state.get("review_required") and self.review_enabled:
                return "open_review"
            return "publish"

        def _after_apply_review(state: WorkflowState) -> str:
            route = state.get("route", "publish")
            if route == "await_review":
                return "await_review"
            return "publish"

        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "plan")
        graph.add_conditional_edges("plan", _after_plan,
                                    {"plan": "plan", "execute": "execute", "compose": "compose"})
        graph.add_edge("execute", "plan")
        graph.add_conditional_edges("compose", _after_compose,
                                    {"open_review": "open_review", "publish": "publish"})
        graph.add_edge("open_review", "await_review")
        graph.add_edge("await_review", "apply_review")
        graph.add_conditional_edges("apply_review", _after_apply_review,
                                    {"await_review": "await_review", "publish": "publish"})
        graph.add_edge("publish", END)
        self._graph = graph.compile(checkpointer=self._ensure_checkpointer())
        return self._graph

    # ---- state conversion --------------------------------------------------

    def _agent_state(self, wf: dict[str, Any]) -> AgentState:
        """Build the working AgentState view for the agent's own components.
        Node functions write mutations back before the step ends."""
        event = CareEvent(**wf["event"])
        ctx = RunContext.from_checkpoint(wf.get("ctx") or {"run_id": wf["run_id"],
                                                           "turn_id": wf["turn_id"],
                                                           "session_id": wf["session_id"]})
        # Harness P2: rebind the process-wide cancellation handle on every
        # node — a cancel request raised between checkpoints must be observed
        # at the next scheduling point of the resumed graph.
        from .harness.progress import cancel_event_for
        ctx.cancel_event = cancel_event_for(wf["run_id"])
        return AgentState(
            session_id=wf["session_id"], turn_id=wf["turn_id"], event=event,
            client_event_id=wf.get("client_event_id"),
            observations=[_observation_from_dict(item) for item in wf.get("observations", [])],
            trace=wf.get("trace", []),
            trace_flushed=int(wf.get("trace_flushed") or 0),
            reflection_notes=wf.get("reflection_notes", []),
            cycle=int(wf.get("cycle", 0)),
            degraded_reason=wf.get("degraded_reason"),
            # Harness P1-A: restore the shared run context from its JSON-safe
            # checkpoint projection (principal re-resolved from configuration,
            # never from the checkpoint).
            ctx=ctx,
        )

    def _write_back(self, wf: dict[str, Any], state: AgentState,
                    node_started: float) -> dict[str, Any]:
        wf["observations"] = [_observation_to_dict(o) for o in state.observations]
        wf["trace"] = state.trace
        wf["trace_flushed"] = state.trace_flushed
        wf["reflection_notes"] = state.reflection_notes
        wf["cycle"] = state.cycle
        wf["degraded_reason"] = state.degraded_reason
        wf["budget"] = CURRENT.get().sync()
        if state.ctx is not None:
            wf["ctx"] = state.ctx.checkpoint_dict()
        return wf

    @staticmethod
    def _rejection_limit() -> int:
        try:
            return max(1, int(os.getenv("PLANNER_SAFETY_REJECTION_LIMIT", "2")))
        except ValueError:
            return 2

    # ---- nodes ------------------------------------------------------------

    def _node_load_context(self, wf: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        state = self._agent_state(wf)
        self.agent.memory.expire_working(state.session_id, except_turn=state.turn_id)
        wf = self._write_back(wf, state, started)
        wf["route"] = "plan"
        return wf

    def _node_plan(self, wf: dict[str, Any]) -> dict[str, Any]:
        """One planning half-cycle — mirrors the legacy handle() loop body:
        budget gate → cycle++ → (breaker | decide) → route."""
        agent = self.agent
        node_started = time.perf_counter()
        state = self._agent_state(wf)
        budget = CURRENT.get()
        breaker = dict(wf.get("breaker") or {})

        def _finish(route: str, *, action: Any = None) -> dict[str, Any]:
            written = self._write_back(wf, state, node_started)
            written["breaker"] = breaker
            written["route"] = route
            written["pending_action"] = asdict(action) if action is not None else None
            return written

        # Harness P2: cancellation is observed at the next scheduling point;
        # the run finishes with what it has (publish records 'cancelled').
        if state.ctx is not None and state.ctx.cancelled():
            state.degraded_reason = "cancelled"
            state.trace.append({
                "phase": "cancel", "cycle": state.cycle,
                "note": "收到取消请求；停止后续规划与工具执行，已完成操作不受影响。",
            })
            return _finish("compose")
        # Harness P2: the no-progress stop survives the unconditional
        # execute→plan edge — once the threshold tripped, no further planning
        # happens; compose finishes the run safely.
        if agent.no_progress_tracker.stop_pending(state.ctx.run_id if state.ctx else wf["run_id"]):
            if state.degraded_reason is None:
                state.degraded_reason = "no_progress:repeated_reads"
            return _finish("compose")
        if budget.gate(state):
            return _finish("compose")
        budget.cycle(state)
        if breaker.get("broken"):
            action = agent.planner._fallback(
                state, None, "circuit_break", [], "planner_circuit_break",
                time.perf_counter(), fallback_kind="circuit_break",
            )
            planner_trace = getattr(agent.planner, "last_decision_trace", None)
        else:
            try:
                action = agent.planner.decide(state)
                breaker["consecutive_rejections"] = 0
            except BudgetExceeded:
                budget.gate(state)
                return _finish("compose")
            except PlanningRejected:
                breaker["consecutive_rejections"] = int(breaker.get("consecutive_rejections") or 0) + 1
                state.trace.append({
                    "phase": "plan", "cycle": state.cycle, "decision": {"tool": "replan"},
                    "planner": agent.planner.last_decision_trace,
                })
                if breaker["consecutive_rejections"] >= self._rejection_limit():
                    breaker["broken"] = True
                    state.degraded_reason = "planner_circuit_break:safety_rejections"
                    state.trace.append({
                        "phase": "plan", "cycle": state.cycle,
                        "decision": {"tool": "circuit_break"},
                        "note": (f"连续 {breaker['consecutive_rejections']} 次安全拒绝；"
                                 "本回合剩余周期切换确定性规划，不再消耗 LLM 调用。"),
                    })
                return _finish("plan")
            planner_trace = getattr(agent.planner, "last_decision_trace", None)
            if not planner_trace:
                planner_trace = {
                    "mode": "deterministic", "source": "deterministic",
                    "proposal": asdict(action) if action else {"decision": "respond"},
                    "validation": {"status": "accepted" if action else "deterministic_terminal",
                                   "valid": True, "errors": []},
                    "fallback_reason": None, "fallback_kind": None,
                    "argument_corrections": [], "model": None, "latency_ms": 0,
                }
        state.trace.append({
            "phase": "plan", "cycle": state.cycle,
            "decision": asdict(action) if action else {"tool": "respond"},
            "planner": planner_trace,
        })
        return _finish("compose" if action is None else "execute", action=action)

    def _node_execute(self, wf: dict[str, Any]) -> dict[str, Any]:
        """Execute the planned tool, observe, reflect, flush traces.
        Domain writes run under P0 operation receipts — a replayed node that
        hits a receipt returns the stored result (no second effect)."""
        agent = self.agent
        started = time.perf_counter()
        state = self._agent_state(wf)
        action_dict = wf.get("pending_action") or {}
        action = ToolAction(tool=action_dict["tool"], purpose=action_dict.get("purpose", ""),
                            arguments=action_dict.get("arguments") or {},
                            rationale=action_dict.get("rationale", ""))
        observation = agent._act(state, action)
        observation.cycle = state.cycle
        state.observations.append(observation)
        state.trace.append({
            "phase": "observe", "cycle": state.cycle, "tool": action.tool,
            "purpose": action.purpose, "ok": observation.ok,
            "summary": agent._summarize_result(observation.result),
            "observation": asdict(observation),
        })
        agent._reflect(state, observation)
        agent._flush_traces(state)
        wf = self._write_back(wf, state, started)
        wf["pending_action"] = None
        # Harness P2: no-progress contract — a repeat feeds the planner
        # structured feedback; at the threshold the loop stops re-planning.
        route = "plan"
        if agent._progress_verdict(state, observation) == "stop":
            state.degraded_reason = "no_progress:repeated_reads"
            state.trace.append({
                "phase": "no_progress", "cycle": state.cycle,
                "note": "连续重复读取未产生新进展；停止重复规划并安全收尾，不以重复结果提高置信度。",
                "unfinished_items": agent._unfinished_items(state),
            })
            route = "compose"
        wf = self._write_back(wf, state, started)
        wf["route"] = route
        return wf

    def _node_compose(self, wf: dict[str, Any]) -> dict[str, Any]:
        agent = self.agent
        started = time.perf_counter()
        state = self._agent_state(wf)
        response = agent._respond(state)
        response = agent._finalize(state, response)
        wf = self._write_back(wf, state, started)
        wf["result"] = {
            "text": response.text,
            "warnings": response.warnings,
            "conflicts": response.conflicts,
            "audit_trail": response.audit_trail,
            "operation_outcomes": response.operation_outcomes,
            "safety_status": response.safety_status,
            "answer_bundle": getattr(response, "answer_bundle", None),
        }
        wf["route"] = "publish"
        # Reliability P2 triage: severe warnings or an open conflict route the
        # turn into clinical review (needs_clinical_review).  Code-owned rule,
        # conservative; the review_enabled flag gates the whole path.
        reason_codes = []
        for warning in response.warnings:
            if warning.get("severity") in REVIEW_SEVERITIES:
                reason_codes.append("severe_warning")
                break
        if response.conflicts:
            reason_codes.append("clinical_conflict")
        wf["review_required"] = bool(reason_codes)
        wf["review_reason_codes"] = reason_codes
        event_id = wf.get("event_id") or wf["turn_id"]
        wf["review_logic_key"] = f"event:{event_id}:{'+'.join(reason_codes) or 'none'}:r1"
        return wf

    # ---- Reliability P2: human review nodes ------------------------------

    def _node_open_review(self, wf: dict[str, Any]) -> dict[str, Any]:
        """Idempotent case creation + the safe waiting response.  No
        interrupt() here: a crash between case creation and the checkpoint
        replays this node and hits the unique logic_key."""
        result = dict(wf.get("result") or {})
        due_at = (_as_utc_now_utc() + timedelta(seconds=self.review_sla_seconds)).isoformat(timespec="seconds")
        case = self.memory.open_review_case(
            logic_key=wf.get("review_logic_key") or f"event:{wf['turn_id']}:r1",
            event_id=wf.get("event_id"), run_id=wf["run_id"], thread_id=wf["run_id"],
            reason_codes=wf.get("review_reason_codes") or ["severe_warning"],
            summary={
                "event_type": wf["event"].get("event_type"),
                "event_text": wf["event"].get("text"),
                "response_text": result.get("text", ""),
                "warnings": result.get("warnings", []),
                "conflicts": result.get("conflicts", []),
                "memory_refs": (result.get("audit_trail") or {}).get("memory_refs", []),
            },
            due_at=due_at,
            round_number=1,
        )
        wf["review_case"] = {
            "id": case["id"], "logic_key": case["logic_key"], "status": case["status"],
            "revision": case["revision"], "round": case["round"], "due_at": case["due_at"],
        }
        waiting = dict(result)
        # Fixed code-owned preamble; the composed body was already checked by
        # the final safety gate in compose, so the delivered text stays exactly
        # the checked text plus this constant.
        waiting["text"] = WAITING_REVIEW_NOTICE + "\n\n" + waiting.get("text", "")
        waiting["run_status"] = "waiting_review"
        waiting["review_case"] = wf["review_case"]
        wf["result"] = waiting
        self._validate_result(wf)
        wf["budget"] = CURRENT.get().sync()
        return wf

    def _node_await_review(self, wf: dict[str, Any]) -> dict[str, Any]:
        """Park the run.  Side-effect free by design: the framework re-runs
        this whole node on resume, so NOTHING may be written here."""
        from langgraph.types import interrupt
        case = wf.get("review_case") or {}
        decision = interrupt({
            "case_id": case.get("id"),
            "case_revision": case.get("revision"),
            "logic_key": case.get("logic_key"),
            "reason_codes": wf.get("review_reason_codes") or [],
        })
        wf["review_decision"] = decision
        return wf

    def _fresh_review_summary(self, wf, old_case):
        # Re-read current facts. Old warning text is never rebound to a new
        # hash. A bounded DDI check can contribute current candidate evidence;
        # patient-condition validation remains explicitly incomplete.
        with self.memory._lock:
            snapshot = self.memory.snapshot()
            fact_hash = self.memory.medication_set_hash()
            fact_revision = self.memory.scope_revision('medications') + self.memory.scope_revision('semantic')
        summary = {"previous_case_id": old_case["id"], "current_facts": snapshot,
            "fact_hash": fact_hash, "fact_revision": fact_revision,
            "verification_status": "incomplete", "warnings": [],
            "conflicts": snapshot.get("open_conflicts", []),
            "incomplete_checks": ["patient_condition_check"],
            "memory_refs": [x["ref"] for k in ("medications", "semantic") for x in snapshot.get(k, [])]}
        budget = CURRENT.get()
        if budget.exhausted():
            summary["incomplete_checks"].append("ddi_check")
            summary["reason"] = "budget_exhausted:" + budget.exhausted()
        else:
            result = self.agent._act(self._agent_state(wf), ToolAction(tool="ddi_check",
                purpose="review_current_facts", rationale="facts_changed", arguments={"medications": [
                    m["display_name"] for m in snapshot.get("medications", [])]}))
            budget.sync()
            if result.ok and not budget.exhausted():
                summary["current_ddi_candidates"] = result.result
            else:
                summary["incomplete_checks"].append("ddi_check")
                summary["reason"] = "check_failed_or_budget_exhausted"
        return summary

    def _node_apply_review(self, wf: dict[str, Any]) -> dict[str, Any]:
        decision = dict(wf.get("review_decision") or {})
        decision_id = decision.get("decision_id")
        record = self.memory.review_decision(decision_id) if decision_id else None
        case_id = (wf.get("review_case") or {}).get("id")
        case = self.memory.review_case(case_id) if case_id else None
        if record is None or case is None or record['case_id'] != case_id or case['run_id'] != wf['run_id']:
            raise MemoryPolicyError("review decision/case/run mismatch")
        # The database record is authoritative; the resume value is only an ID.
        action, payload, actor = record['action'], record.get('payload') or {}, record['actor_id']
        receipt = self.memory.operation_receipt('local-demo', 'review-apply:' + decision_id)
        current_rev = self.memory.scope_revision("medications") + self.memory.scope_revision("semantic")
        stale = (case['fact_medication_set_hash'] != self.memory.medication_set_hash()
                 or case['fact_scope_revision'] != current_rev)
        if not receipt and (stale or record['outcome'] == 'review_stale'):
            self.memory.set_review_decision_outcome(decision_id, outcome="review_stale")
            self.memory.close_review_case(case_id, status="cancelled",
                reason="review_stale: patient facts changed", actor="runner")
            round_number = case['round'] + 1
            key = f"event:{wf.get('event_id') or wf['turn_id']}:{'+'.join(wf.get('review_reason_codes') or ['severe_warning'])}:r{round_number}"
            fresh_case = self.memory.review_case_by_logic_key(key)
            if fresh_case is None:
                # I/O is outside the case transaction; open verifies this
                # snapshot again under the write lock before binding its hash.
                summary = self._fresh_review_summary(wf, case)
                fresh_case = self.memory.open_review_case(logic_key=key,
                    event_id=wf.get('event_id'), run_id=wf['run_id'], thread_id=wf['run_id'],
                    reason_codes=[*(wf.get('review_reason_codes') or []), 'review_stale'],
                    summary=summary, round_number=round_number,
                    due_at=(_as_utc_now_utc() + timedelta(seconds=self.review_sla_seconds)).isoformat(timespec='seconds'))
            self.memory.audit_review_stale(decision_id, case_id, fresh_case['id'])
            wf['review_case'] = {k: fresh_case[k] for k in ('id','logic_key','status','revision','round','due_at')}
            wf['review_logic_key'] = key
            wf['review_decision'] = None
            wf['route'] = 'await_review'
            wf['result'] = {'text': '患者事实已变化，旧审核依据已过期。当前事实已重新读取；患者个体风险检查尚未完成，不能据此判断无风险。建议咨询医生/药师。',
                'warnings': [], 'conflicts': [], 'audit_trail': {'memory_refs': fresh_case['summary'].get('memory_refs', [])},
                'review_case': wf['review_case'], 'run_status': 'waiting_review', 'safety_status': 'enforced'}
            self._validate_result(wf)
            wf['budget'] = CURRENT.get().sync()
            return wf
        outcome = self.memory.apply_review_effect(decision_id, session_id=wf['session_id'], turn_id=wf['turn_id'])
        notes = {'resolve_conflict': REVIEW_RESOLVED_NOTICE, 'confirm_reported_fact': REVIEW_CONFIRMED_NOTICE,
            'reject_candidate': REVIEW_REJECTED_NOTICE, 'close_with_safe_guidance': REVIEW_GUIDANCE_NOTICE,
            'request_more_info': REVIEW_WAITING_USER_NOTICE}
        wf['review_outcome'] = outcome
        result = dict(wf.get('result') or {})
        # Replayed apply starts from its prior checkpoint body and reconstructs
        # the same suffix after reading the atomic effect receipt.
        result['text'] = result.get('text', '') + notes[action]
        result['run_status'] = 'applied_review'
        result['review_case'] = self.memory.review_case(case_id)
        wf['result'] = result
        wf['route'] = 'publish'
        if action == 'request_more_info':
            wf['degraded_reason'] = 'review_waiting_user'
        self._validate_result(wf)
        wf['budget'] = CURRENT.get().sync()
        return wf

    def _validate_result(self, wf):
        result = wf.get('result') or {}
        errors = self.agent._check_response(result.get('text', ''), warnings=result.get('warnings', []),
            conflicts=result.get('conflicts', []), memory_refs=(result.get('audit_trail') or {}).get('memory_refs', []),
            escalation_required=True, refusal_required=False)
        if errors:
            raise RuntimeError('safety boundary: review response blocked: ' + ','.join(errors))

    def _node_publish(self, wf: dict[str, Any]) -> dict[str, Any]:
        status = "degraded" if wf.get("degraded_reason") else "succeeded"
        # Harness P2: a cancellation request outranks the normal terminal
        # mapping — already-committed domain effects stay, the run is recorded
        # as cancelled.  CAS on the request row keeps publish/cancel races
        # converging to exactly one final status.
        if is_cancel_requested(self.memory, wf["run_id"]):
            status = "cancelled"
            mark_cancelled(self.memory, wf["run_id"])
        self.memory.workflow_run_update(
            wf["run_id"], status=status, result=wf.get("result"),
            budget=wf.get("budget"))
        return wf

    # ---- public API -------------------------------------------------------

    def run(self, *, event: CareEvent, session_id: str, turn_id: str,
            client_event_id: str | None = None,
            event_id: str | None = None, run_id: str | None = None) -> Any:
        from .agent import AgentResponse
        run_id = run_id or turn_id
        graph = self._ensure_graph()
        config = {"configurable": {"thread_id": run_id}}
        existing = self.memory.workflow_run_get(run_id)
        if existing is not None and existing.get("graph_version") != "accepted":
            self._check_restore_manifest(run_id)
        if existing is None:
            self.memory.workflow_run_start(
                run_id=run_id, event_id=event_id, idempotency_key=client_event_id,
                thread_id=run_id, graph_version=self.graph_version,
                state_schema_version=STATE_SCHEMA_VERSION,
                model_id=getattr(getattr(self.agent, "planner", None), "model", None))
            self._save_manifest(run_id)
        elif existing.get("graph_version") == "accepted":
            # Harness P2: stamp the real runner version onto the row the API
            # acceptance pre-created ('accepted'/'queued') — an in-flight run
            # is still never silently re-routed.  Idempotent; promotion to
            # 'running' does not touch a cancelled run.
            self.memory.workflow_run_start(
                run_id=run_id, event_id=event_id, idempotency_key=client_event_id,
                thread_id=run_id, graph_version=self.graph_version,
                state_schema_version=STATE_SCHEMA_VERSION,
                model_id=getattr(getattr(self.agent, "planner", None), "model", None))
            self._save_manifest(run_id)
        if self._has_checkpoint(config) and existing is not None:
            logger.info("graph run resumed run_id=%s", run_id)
            final = self._invoke_budgeted(graph, None, config, run_id,
                                          saved=graph.get_state(config).values.get("budget"))
        else:
            final = self._invoke_fresh(graph, config, event, session_id, turn_id,
                                       client_event_id, event_id, run_id, existing)
        result = final.get("result") or {}
        if final.get("error"):
            raise RuntimeError(f"graph run failed: {final['error']}")
        audit_trail = dict(result.get("audit_trail") or {})
        if "__interrupt__" in final:
            # Parked at await_review: the waiting response is the turn result;
            # the run stays non-terminal (waiting_review).
            audit_trail["run_status"] = "waiting_review"
            audit_trail["review_case"] = final.get("review_case")
        return AgentResponse(
            text=result.get("text", ""), warnings=result.get("warnings", []),
            conflicts=result.get("conflicts", []), audit_trail=audit_trail,
            operation_outcomes=result.get("operation_outcomes", []),
            tool_trace=final.get("trace", []), safety_status=result.get("safety_status", "enforced"),
            answer_bundle=result.get("answer_bundle"))

    def _invoke_fresh(self, graph, config, event, session_id, turn_id,
                      client_event_id, event_id, run_id, existing) -> dict[str, Any]:
        initial = build_workflow_state(
            event=event, session_id=session_id, turn_id=turn_id,
            client_event_id=client_event_id, event_id=event_id, run_id=run_id,
            budget=initial_budget(self.agent.max_cycles, (existing or {}).get("budget")))
        return self._invoke_budgeted(graph, initial, config, run_id, saved=initial["budget"])

    def resume(self, run_id: str, resume_value: Any) -> dict[str, Any]:
        """Apply an authorized human decision via ``Command(resume=...)`` on
        the run's own thread.  Returns the final workflow state: either a
        terminal state (publish ran) or a re-parked interrupt state
        (``__interrupt__`` present — e.g. a review_stale round was opened)."""
        from langgraph.types import Command
        self._check_restore_manifest(run_id)
        graph = self._ensure_graph()
        config = {"configurable": {"thread_id": run_id}}
        checkpoint = graph.get_state(config)
        if not checkpoint.values:
            raise RuntimeError("missing review checkpoint")
        current_case = (checkpoint.values.get("review_case") or {}).get("id")
        # A retry after a committed checkpoint must not feed the old decision
        # into the next round's interrupt. Continue errored apply/publish nodes.
        if checkpoint.next == ("await_review",) and current_case == resume_value.get("case_id"):
            value = Command(resume=resume_value)
        else:
            value = None
        return self._invoke_budgeted(graph, value, config, run_id,
                                     saved=checkpoint.values.get("budget"))

    def _check_restore_manifest(self, run_id: str) -> None:
        from .harness.manifest import enforce_restore
        enforce_restore(memory=self.memory, agent=self.agent, run_id=run_id,
                        graph_version=self.graph_version,
                        state_schema_version=STATE_SCHEMA_VERSION,
                        review_enabled=self.review_enabled)

    def _invoke_budgeted(self, graph, value, config, run_id, saved=None):
        self.memory.activate_workflow_run(run_id)
        with budget_scope(self.memory, run_id, self.agent.max_cycles, saved) as budget:
            final = graph.invoke(value, config)
            final["budget"] = budget.sync()
            if "__interrupt__" in final:
                self.memory.workflow_run_update(run_id, status="waiting_review",
                    budget=final["budget"], result=final.get("result"))
            return final

    def _has_checkpoint(self, config: dict[str, Any]) -> bool:
        return next(iter(self._ensure_checkpointer().list(config, limit=1)), None) is not None


class RunnerRouter:
    """Version routing (Reliability P1): new runs follow the feature flag;
    a run that already has a workflow_runs row continues on the runner
    version that created it — never silently re-routed mid-flight."""

    def __init__(self, store: MemoryStore, legacy: LegacyAgentRunner,
                 graph: LangGraphAgentRunner | None, graph_enabled: bool):
        self.store = store
        self.legacy = legacy
        self.graph = graph
        self.graph_enabled = graph_enabled

    @property
    def agent(self) -> MedicationCoordinatorAgent:
        return self.legacy.agent

    def close(self) -> None:
        if self.graph is not None:
            self.graph.close()
        recorder = getattr(self.agent, "_span_recorder", None)
        if recorder is not None and recorder.exporter is not None:
            recorder.exporter.close()

    def run(self, **kwargs: Any) -> Any:
        run_id = kwargs.get("run_id") or kwargs.get("turn_id")
        existing = self.store.workflow_run_get(run_id) if run_id else None
        if existing is not None and existing["graph_version"] == "legacy":
            return self.legacy.run(**kwargs)
        if (existing is not None and existing["graph_version"] == GRAPH_VERSION
                and self.graph is not None):
            return self.graph.run(**kwargs)
        if self.graph_enabled and self.graph is not None:
            return self.graph.run(**kwargs)
        return self.legacy.run(**kwargs)

    def resume(self, run_id: str, resume_value: Any) -> dict[str, Any]:
        """Reliability P2: apply a recorded review decision to the parked
        run.  Only graph runs can resume — a legacy run has no interrupt."""
        if self.graph is None:
            raise RuntimeError("graph runner is not enabled; legacy runs cannot resume")
        existing = self.store.workflow_run_get(run_id)
        if existing is not None and existing["graph_version"] != GRAPH_VERSION:
            raise RuntimeError(f"run {run_id} is a '{existing['graph_version']}' run; cannot resume")
        return self.graph.resume(run_id, resume_value)


def make_runner(store: MemoryStore, agent_factory: Callable[[], MedicationCoordinatorAgent],
                *, graph_enabled: bool | None = None,
                checkpoint_path: str | None = None,
                review_enabled: bool | None = None) -> RunnerRouter:
    """Build the routed runner used by the service layer.  Flag defaults:
    ``AGENT_GRAPH_RUNNER`` (off — legacy stays the default path) and
    ``STAGE0_REVIEW_ENABLED`` (off — the review loop is explicit opt-in)."""
    agent = agent_factory()
    legacy = LegacyAgentRunner(agent)
    if graph_enabled is None:
        graph_enabled = os.getenv(GRAPH_RUNNER_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}
    graph = (LangGraphAgentRunner(agent, checkpoint_path=checkpoint_path,
                                  review_enabled=review_enabled)
             if graph_enabled else None)
    return RunnerRouter(store, legacy, graph, graph_enabled)
