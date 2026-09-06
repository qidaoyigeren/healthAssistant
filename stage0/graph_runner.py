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
their single owner (planner SDK max_retries=0).  Human-in-the-loop interrupts
are P2: ``resume()`` is the wiring for ``Command(resume=...)``, and no node
raises ``interrupt()`` yet.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict

try:
    from .agent import (
        PLANNER_RESERVE_SECONDS,
        AgentState,
        CareEvent,
        MedicationCoordinatorAgent,
        Observation,
        PlanningRejected,
        ToolAction,
        TurnBudget,
    )
    from .memory import EpisodicFact, MemoryPolicyError, MemoryStore
except ImportError:  # Support ``python stage0/graph_runner.py`` imports.
    from agent import (  # type: ignore
        PLANNER_RESERVE_SECONDS,
        AgentState,
        CareEvent,
        MedicationCoordinatorAgent,
        Observation,
        PlanningRejected,
        ToolAction,
        TurnBudget,
    )
    from memory import EpisodicFact, MemoryPolicyError, MemoryStore  # type: ignore


logger = logging.getLogger("stage0.graph_runner")


def _as_utc_now_utc() -> datetime:
    return datetime.now(timezone.utc)

GRAPH_VERSION = "1"
STATE_SCHEMA_VERSION = "1"
GRAPH_RUNNER_FLAG = "AGENT_GRAPH_RUNNER"
REVIEW_ENABLED_FLAG = "STAGE0_REVIEW_ENABLED"

# Fixed, code-owned texts for review transitions.  Reviewer decisions may NOT
# inject arbitrary delivered text (no state patches, no free-form strings) —
# their basis is recorded in the audit trail only.
WAITING_REVIEW_NOTICE = (
    "已记录本次报告并完成初步风险提示；该情形按流程提交专业审核。"
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
    }


def _observation_from_dict(item: dict[str, Any]) -> Observation:
    return Observation(
        tool=item["tool"], purpose=item["purpose"], arguments=item.get("arguments") or {},
        result=item.get("result"), ok=bool(item.get("ok", True)), cycle=int(item.get("cycle") or 0),
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

    def __init__(self, agent: MedicationCoordinatorAgent):
        self.agent = agent

    def run(self, *, event: CareEvent, session_id: str, turn_id: str,
            client_event_id: str | None = None,
            event_id: str | None = None, run_id: str | None = None) -> Any:
        run_id = run_id or turn_id
        self.agent.memory.workflow_run_start(
            run_id=run_id, event_id=event_id, idempotency_key=client_event_id,
            thread_id=run_id, graph_version=self.graph_version)
        try:
            response = self.agent.handle(event, session_id=session_id,
                                         turn_id=turn_id, client_event_id=client_event_id)
        except Exception as exc:
            self.agent.memory.workflow_run_update(run_id, status="failed",
                                                  error=f"{type(exc).__name__}")
            raise
        degraded = any(entry.get("phase") in {"budget", "plan"}
                       and (entry.get("exhausted") or entry.get("decision", {}).get("tool") == "circuit_break")
                       for entry in (response.tool_trace or []))
        self.agent.memory.workflow_run_update(
            run_id, status="degraded" if degraded else "succeeded",
            result={"text": response.text, "warnings": response.warnings,
                    "conflicts": response.conflicts, "safety_status": response.safety_status})
        return response


def build_workflow_state(*, event: CareEvent, session_id: str, turn_id: str,
                         client_event_id: str | None, event_id: str | None,
                         run_id: str, budget: dict[str, Any] | None) -> dict[str, Any]:
    """Initial WorkflowState.  Every value is JSON-safe (checkpoint redline)."""
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "event_id": event_id,
        "client_event_id": client_event_id,
        "event": asdict(event),
        "observations": [],
        "trace": [],
        "reflection_notes": [],
        "cycle": 0,
        "degraded_reason": None,
        "route": "plan",
        "pending_action": None,
        "budget": {"consumed_seconds": 0.0, "tokens_estimated": 0,
                   **(budget or {})},
        "breaker": {"consecutive_rejections": 0, "broken": False},
        "result": None,
        "error": None,
        "review_required": False,
        "review_reason_codes": [],
        "review_logic_key": None,
        "review_case": None,
        "review_decision": None,
        "review_outcome": None,
    }


class LangGraphAgentRunner:
    """StateGraph runner: node-level checkpoints, receipt-protected replay,
    persisted budget.  The planner/guard/tools/safety semantics are the
    wrapped agent's own."""

    graph_version = GRAPH_VERSION

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
        return AgentState(
            session_id=wf["session_id"], turn_id=wf["turn_id"], event=event,
            client_event_id=wf.get("client_event_id"),
            observations=[_observation_from_dict(item) for item in wf.get("observations", [])],
            trace=wf.get("trace", []),
            reflection_notes=wf.get("reflection_notes", []),
            cycle=int(wf.get("cycle", 0)),
            degraded_reason=wf.get("degraded_reason"),
        )

    def _write_back(self, wf: dict[str, Any], state: AgentState,
                    node_started: float) -> dict[str, Any]:
        wf["observations"] = [_observation_to_dict(o) for o in state.observations]
        wf["trace"] = state.trace
        wf["reflection_notes"] = state.reflection_notes
        wf["cycle"] = state.cycle
        wf["degraded_reason"] = state.degraded_reason
        budget = dict(wf.get("budget") or {})
        budget["consumed_seconds"] = round(
            float(budget.get("consumed_seconds") or 0.0) + (time.perf_counter() - node_started), 4)
        wf["budget"] = budget
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
        budget = dict(wf.get("budget") or {})
        wall = budget.get("wall_clock_seconds")
        if wall is None:
            configured = TurnBudget.from_env(agent.max_cycles)
            wall, max_cycles = configured.wall_clock_seconds, configured.max_cycles
            budget["wall_clock_seconds"], budget["max_cycles"] = wall, max_cycles
        max_cycles = int(budget.get("max_cycles") or agent.max_cycles)
        breaker = dict(wf.get("breaker") or {})
        elapsed = float(budget.get("consumed_seconds") or 0.0)

        def _finish(route: str, *, action: Any = None) -> dict[str, Any]:
            written = self._write_back(wf, state, node_started)
            # _write_back recomputed consumed_seconds from the persisted
            # budget; merge so this node's tokens estimate and the delta both
            # survive (the budget must never lose accumulated values).
            budget.update(written["budget"])
            budget["wall_clock_seconds"] = wall
            budget["max_cycles"] = max_cycles
            written["budget"] = budget
            written["breaker"] = breaker
            written["route"] = route
            written["pending_action"] = asdict(action) if action is not None else None
            return written

        # Loop exit (cycle reached max_cycles) — legacy 'max_cycles_exceeded'.
        if int(wf.get("cycle", 0)) >= max_cycles:
            state.degraded_reason = state.degraded_reason or "max_cycles_exceeded"
            state.trace.append({
                "phase": "reflect", "cycle": state.cycle,
                "note": f"达到 max_cycles={max_cycles}；停止工具执行并生成明确降级响应。",
            })
            return _finish("compose")

        # Budget gate (completed cycles > 0 only — exactly like the legacy loop).
        reserve = PLANNER_RESERVE_SECONDS if wall > PLANNER_RESERVE_SECONDS else 0.0
        if int(wf.get("cycle", 0)) > 0 and elapsed > wall - reserve:
            state.degraded_reason = "budget_exhausted:wall_clock"
            state.trace.append({
                "phase": "budget", "cycle": state.cycle, "exhausted": "wall_clock",
                "elapsed_seconds": round(elapsed, 3),
                "limits": {"wall_clock_seconds": wall,
                           "token_budget": budget.get("token_budget"),
                           "max_cycles": max_cycles},
                "note": "回合预算耗尽；停止工具执行并生成明确降级响应（结果可能不完整）。",
            })
            return _finish("compose")

        state.cycle += 1
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
            payload_chars = getattr(agent.planner, "last_payload_chars", 0)
            if payload_chars:
                budget["tokens_estimated"] = int(budget.get("tokens_estimated") or 0) + int(payload_chars / 1.5)
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
        wf["route"] = "plan"
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
            "safety_status": response.safety_status,
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

    def _node_apply_review(self, wf: dict[str, Any]) -> dict[str, Any]:
        """Re-validate and apply the human decision through BOUNDED writes
        only.  Freshness check first: facts moved since the case opened →
        review_stale → the decision is refused, the old case cancelled and a
        fresh round is opened (旧审批不授权新状态).  No graph goto, no state
        patch, no reviewer-authored delivered text."""
        decision = dict(wf.get("review_decision") or {})
        decision_id = decision.get("decision_id")
        action = decision.get("action")
        payload = decision.get("payload") or {}
        record = self.memory.review_decision(decision_id) if decision_id else None
        case_id = (wf.get("review_case") or {}).get("id")
        case = self.memory.review_case(case_id) if case_id is not None else None

        def _park_again(new_case: dict[str, Any] | None = None) -> dict[str, Any]:
            wf["route"] = "await_review"
            wf["review_decision"] = None
            if new_case is not None:
                waiting = dict(wf.get("result") or {})
                waiting["review_case"] = {
                    "id": new_case["id"], "logic_key": new_case["logic_key"],
                    "status": new_case["status"], "revision": new_case["revision"],
                    "round": new_case["round"], "due_at": new_case["due_at"],
                }
                waiting["run_status"] = "waiting_review"
                wf["review_case"] = waiting["review_case"]
                wf["result"] = waiting
            return wf

        if record is None or case is None:
            # Defensive: decision vanished or case closed while parked.
            wf["degraded_reason"] = "review_decision_unapplicable"
            wf["route"] = "publish"
            return wf
        if record.get("outcome") == "applied":
            # Replayed resume — idempotent no-op, finish the run.
            wf["route"] = "publish"
            return wf

        # Freshness: medication set / scope revisions must match the snapshot
        # taken when the case opened.
        current_hash = self.memory.medication_set_hash()
        current_rev = self.memory.scope_revision("medications") + self.memory.scope_revision("semantic")
        if (case.get("fact_medication_set_hash") != current_hash
                or int(case.get("fact_scope_revision") or 0) != current_rev):
            self.memory.set_review_decision_outcome(decision_id, outcome="review_stale")
            self.memory.close_review_case(
                case["id"], status="cancelled",
                reason="review_stale: patient facts changed while awaiting review",
                actor="runner")
            round_number = int(case.get("round") or 1) + 1
            base_key = f"event:{wf.get('event_id') or wf['turn_id']}"
            fresh_case = self.memory.open_review_case(
                logic_key=f"{base_key}:{'+'.join(wf.get('review_reason_codes') or ['severe_warning'])}:r{round_number}",
                event_id=wf.get("event_id"), run_id=wf["run_id"], thread_id=wf["run_id"],
                reason_codes=[*(wf.get("review_reason_codes") or []), "review_stale"],
                summary=dict(case.get("summary") or {}),
                due_at=(_as_utc_now_utc() + timedelta(seconds=self.review_sla_seconds)).isoformat(timespec="seconds"),
                round_number=round_number,
            )
            self._audit("review_decision_stale", "review_case", case["id"],
                        {"decision_id": decision_id, "new_case": fresh_case["id"]}, "runner")
            return _park_again(fresh_case)

        # Bounded effects.
        actor = decision.get("actor_id") or "reviewer"
        if action == "resolve_conflict":
            conflict_ref = payload.get("conflict_ref") or ""
            match = re.fullmatch(r"(?:memory:)?conflict:(\d+)(?:@v\d+)?", str(conflict_ref))
            if not match:
                raise MemoryPolicyError(f"invalid conflict ref in review decision: {conflict_ref!r}")
            self.memory.resolve_conflict(int(match.group(1)), action="resolved",
                                         basis=f"专业审核：{payload.get('basis', '')}",
                                         actor=actor)
            note = REVIEW_RESOLVED_NOTICE
        elif action == "confirm_reported_fact":
            note = REVIEW_CONFIRMED_NOTICE
        elif action == "reject_candidate":
            note = REVIEW_REJECTED_NOTICE
        elif action == "close_with_safe_guidance":
            note = REVIEW_GUIDANCE_NOTICE
        elif action == "request_more_info":
            note = REVIEW_WAITING_USER_NOTICE
        else:  # pragma: no cover - schema CHECK guards this
            raise MemoryPolicyError(f"unsupported review action: {action}")

        if action != "request_more_info":
            # Document the decision as an audited episodic record (bounded,
            # existing write path); it never injects clinical claims.
            self.memory.record_event(
                EpisodicFact(
                    event_type="clinical_review",
                    subject_key=f"review_case:{case['id']}",
                    payload={"decision_id": decision_id, "action": action,
                             "actor_id": actor, "basis": payload.get("basis", ""),
                             "reason_codes": wf.get("review_reason_codes") or []},
                    salience=0.95,
                ),
                session_id=wf["session_id"], turn_id=wf["turn_id"], source="reviewer",
            )
        self.memory.set_review_decision_outcome(decision_id, outcome="applied")
        wf["review_outcome"] = {"action": action, "applied": True,
                                "decision_id": decision_id}
        result = dict(wf.get("result") or {})
        result["text"] = (result.get("text", "") or WAITING_REVIEW_NOTICE) + note
        result["run_status"] = "applied_review"
        wf["result"] = result
        if action == "request_more_info":
            self.memory.close_review_case(case["id"], status="waiting_user",
                                          reason=payload.get("question", ""), actor=actor)
            wf["degraded_reason"] = "review_waiting_user"
            wf["route"] = "publish"
        else:
            wf["route"] = "publish"
        return wf

    def _node_publish(self, wf: dict[str, Any]) -> dict[str, Any]:
        status = "degraded" if wf.get("degraded_reason") else "succeeded"
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
        if existing is None:
            self.memory.workflow_run_start(
                run_id=run_id, event_id=event_id, idempotency_key=client_event_id,
                thread_id=run_id, graph_version=self.graph_version,
                state_schema_version=STATE_SCHEMA_VERSION,
                model_id=getattr(getattr(self.agent, "planner", None), "model", None))
        if self._has_checkpoint(config) and existing is not None:
            # Crash recovery: continue from the last completed step.  If the
            # framework cannot resume (e.g. the run already terminated), fall
            # back to a full re-run — P0 operation receipts make that safe.
            logger.info("graph run resumed run_id=%s", run_id)
            try:
                final = graph.invoke(None, config)
            except Exception:
                logger.warning("resume failed for run_id=%s; re-running from "
                               "start (receipt-protected)", run_id, exc_info=True)
                final = self._invoke_fresh(graph, config, event, session_id, turn_id,
                                           client_event_id, event_id, run_id, existing)
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
            tool_trace=final.get("trace", []), safety_status=result.get("safety_status", "enforced"))

    def _invoke_fresh(self, graph, config, event, session_id, turn_id,
                      client_event_id, event_id, run_id, existing) -> dict[str, Any]:
        initial = build_workflow_state(
            event=event, session_id=session_id, turn_id=turn_id,
            client_event_id=client_event_id, event_id=event_id, run_id=run_id,
            budget=(existing or {}).get("budget"))
        return graph.invoke(initial, config)

    def resume(self, run_id: str, resume_value: Any) -> dict[str, Any]:
        """Apply an authorized human decision via ``Command(resume=...)`` on
        the run's own thread.  Returns the final workflow state: either a
        terminal state (publish ran) or a re-parked interrupt state
        (``__interrupt__`` present — e.g. a review_stale round was opened)."""
        from langgraph.types import Command
        graph = self._ensure_graph()
        return graph.invoke(Command(resume=resume_value),
                            {"configurable": {"thread_id": run_id}})

    def _has_checkpoint(self, config: dict[str, Any]) -> bool:
        try:
            for _ in self._ensure_checkpointer().list(config, limit=1):
                return True
        except Exception:
            logger.debug("checkpoint listing failed", exc_info=True)
        return False


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
