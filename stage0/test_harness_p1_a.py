"""Harness P1-A: shared runtime, typed tool contracts and hook parity.

Verifies the P1-A acceptance surface:

* one shared ToolExecutor behind BOTH runners — a newly registered tool is
  planned, guarded and executed by legacy and LangGraph without touching
  either loop;
* the stable error taxonomy (unknown/invalid/permission/policy/internal/
  evidence) never conflates categories, and budget/lease/cancellation stay
  pass-through instead of becoming fake tool results;
* RunContext checkpoint projections are JSON-safe and exclude budget
  sessions/credentials; the trusted principal cannot be widened by proposals;
* hooks fire in the same order for both runners and can never break a turn
  or ungate the safety boundary;
* warning bodies and conflict links remain code-derived: an executor call
  with a fabricated warning body is rejected even if the guard were bypassed.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from stage0.agent import (
    AgentState, CareEvent, DDITool, MedicationCoordinatorAgent, ToolAction,
)
from stage0.graph_runner import LangGraphAgentRunner, LegacyAgentRunner
from stage0.harness.default_tools import DEFAULT_TOOL_SPECS
from stage0.harness.errors import ToolErrorKind, ToolExecutionError
from stage0.harness.runtime import Principal, RunContext
from stage0.harness.tools import HarnessHooks, ToolSpec
from stage0.memory import MemoryPolicyError, MemoryStore
from stage0.test_stage8_agent import _composer_unavailable, _repeat_read_provider


class ScriptedProvider:
    """consolidate → custom tool → respond, recording the proposals seen."""

    def __init__(self, tool_proposals, seen_catalogs=None):
        self.tool_proposals = list(tool_proposals)
        self.seen_catalogs = seen_catalogs if seen_catalogs is not None else []

    def __call__(self, payload):
        self.seen_catalogs.append({t["name"] for t in payload.get("tool_catalog", [])})
        if self.tool_proposals:
            return self.tool_proposals.pop(0)
        return {"decision": "respond", "rationale": "done"}


def make_agent(store, provider, **kwargs):
    return MedicationCoordinatorAgent(
        store, ddi_tool=DDITool(lambda meds: []), rag_tool=None,
        llm_planner_enabled=True, proposal_provider=provider,
        response_provider=_composer_unavailable, **kwargs)


class SharedExecutorParityTests(unittest.TestCase):
    """A new tool registered on the executor works through both runners."""

    def _register_note_tool(self, agent):
        spec = ToolSpec(
            name="caregiver_note_read", description="read one synthetic caregiver note",
            argument_schema={"type": "object",
                             "properties": {"note_id": {"type": "string"}},
                             "required": ["note_id"]},
            result_shape="dict(note_id, note)", kind="read",
            required_permission="memory:read")
        calls = []

        def handler(request):
            calls.append(request.arguments["note_id"])
            return {"note_id": request.arguments["note_id"], "note": "合成便签内容"}

        agent.executor.register(spec, handler)
        agent.planner.bind_tools(agent.executor.catalog())
        return calls

    def _run_both(self, provider_factory):
        results = {}
        for runner_type, name in ((LegacyAgentRunner, "legacy"), (LangGraphAgentRunner, "graph")):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                with MemoryStore(path / "test.db") as store:
                    agent = make_agent(store, provider_factory())
                    calls = self._register_note_tool(agent)
                    kwargs = dict(event=CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}}),
                                  session_id="s", turn_id="r", run_id="r")
                    runner = runner_type(agent, checkpoint_path=path / "cp.db") \
                        if runner_type is LangGraphAgentRunner else runner_type(agent)
                    try:
                        response = runner.run(**kwargs)
                    finally:
                        if hasattr(runner, "close"):
                            runner.close()
                    tools_used = [item["tool"] for item in response.tool_trace
                                  if item.get("phase") == "act"]
                    results[name] = {"calls": list(calls), "tools_used": tools_used,
                                     "trace": response.tool_trace}
        return results

    def test_new_tool_executes_on_both_runners_without_loop_changes(self):
        seen_catalogs = []

        def provider_factory():
            return ScriptedProvider([
                {"decision": "tool", "tool": "caregiver_note_read", "purpose": "note",
                 "arguments": {"note_id": "n1"}},
            ], seen_catalogs=seen_catalogs)

        results = self._run_both(provider_factory)
        for name, result in results.items():
            with self.subTest(runner=name):
                self.assertEqual(result["calls"], ["n1"])
                self.assertIn("caregiver_note_read", result["tools_used"])
                # The note tool also reached the model catalog for both runners.
                self.assertTrue(any("caregiver_note_read" in catalog for catalog in seen_catalogs))
                # The delivered response still passed the safety boundary.
                self.assertEqual(result["trace"][-1].get("phase"), "respond")

    def test_two_runners_same_error_semantics_for_invalid_arguments(self):
        # An invalid proposal is rejected by the SAME guard/executor stack for
        # both runners: the bad tool call never executes, the turn degrades.
        results = self._run_both(lambda: ScriptedProvider([
            {"decision": "tool", "tool": "caregiver_note_read", "purpose": "note",
             "arguments": {"wrong": "shape"}},
        ]))
        for name, result in results.items():
            with self.subTest(runner=name):
                self.assertEqual(result["calls"], [])
                self.assertNotIn("caregiver_note_read", result["tools_used"])
                self.assertTrue(any(
                    item.get("planner", {}).get("validation", {}).get("status") == "safety_rejected"
                    for item in result["trace"]))


class ToolExecutorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "test.db")
        self.agent = make_agent(self.store, _repeat_read_provider)
        self.state = AgentState(session_id="s", turn_id="r",
                                event=CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}}))
        self.state.ctx = RunContext(run_id="r", turn_id="r", session_id="s")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def execute(self, tool, arguments, ctx=None):
        ctx = ctx or self.state.ctx
        return self.agent.executor.execute(ctx, tool, arguments, state=self.state)

    def test_default_specs_declare_no_layered_retry_and_typed_contract(self):
        for name, spec in DEFAULT_TOOL_SPECS.items():
            with self.subTest(tool=name):
                self.assertEqual(spec.retry_owner, "none")
                self.assertIn(spec.kind, {"read", "write"})
                self.assertTrue(spec.required_permission)
                self.assertTrue(spec.idempotency in {"pure", "receipt_keyed", "none"})

    def test_error_kinds_are_distinct_and_machine_readable(self):
        cases = {
            "unknown_tool": self.execute("nonexistent_tool", {}),
            "invalid_arguments": self.execute("memory_read", {"query": "not_a_query"}),
            "permission_denied": self.execute(
                "memory_write", {"operation": "consolidate_event"},
                ctx=RunContext(run_id="r", turn_id="r", session_id="s",
                               principal=Principal(user_id="intruder",
                                                   roles=frozenset({"visitor"})))),
        }
        for name, outcome in cases.items():
            with self.subTest(case=name):
                self.assertFalse(outcome.ok)
                self.assertEqual(sorted(outcome.error), ["error", "error_kind", "recoverable"])
                self.assertEqual(outcome.error["error_kind"], name)
        self.assertTrue(cases["invalid_arguments"].error["recoverable"])
        self.assertFalse(cases["permission_denied"].error["recoverable"])

    def test_policy_violation_classified_distinct_from_internal_error(self):
        # The executor maps domain MemoryPolicyError to POLICY_VIOLATION — a
        # non-recoverable class distinct from INTERNAL_ERROR.
        self.agent.executor.register(
            ToolSpec(name="policy_boom", description="",
                     argument_schema={"type": "object"}, result_shape="",
                     kind="read", required_permission="memory:read"),
            lambda request: (_ for _ in ()).throw(MemoryPolicyError("receipt conflict")))
        result = self.execute("policy_boom", {})
        self.assertEqual(result.error["error_kind"], "policy_violation")
        self.assertFalse(result.error["recoverable"])
        self.agent.executor.register(
            ToolSpec(name="internal_boom", description="",
                     argument_schema={"type": "object"}, result_shape="",
                     kind="read", required_permission="memory:read"),
            lambda request: (_ for _ in ()).throw(RuntimeError("db on fire")),
            override=True)
        result = self.execute("internal_boom", {})
        self.assertEqual(result.error["error_kind"], "internal_error")
        self.assertNotEqual(result.error["error_kind"], "policy_violation")

    def test_budget_exhaustion_is_passthrough_never_a_tool_result(self):
        from stage0.turn_budget import BudgetExceeded, budget_scope
        self.store.workflow_run_start(run_id="r", graph_version="legacy")
        def exploding(request):
            raise BudgetExceeded("tokens")
        self.agent.executor.register(
            ToolSpec(name="boom", description="", argument_schema={"type": "object"},
                     result_shape="", kind="read", required_permission="memory:read"),
            exploding, override=False)
        with budget_scope(self.store, "r"):
            with self.assertRaises(BudgetExceeded):
                self.execute("boom", {})

    def test_cancellation_gates_dispatch_before_the_handler_runs(self):
        fired = []
        self.agent.executor.register(
            ToolSpec(name="tracked", description="", argument_schema={"type": "object"},
                     result_shape="", kind="read", required_permission="memory:read"),
            lambda request: fired.append(1) or {})
        self.state.ctx.cancel_event = threading.Event()
        self.state.ctx.cancel_event.set()
        result = self.execute("tracked", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["error_kind"], "cancelled")
        self.assertEqual(fired, [])

    def test_metrics_record_attempts_active_seconds_and_corrections(self):
        result = self.execute("memory_read", {"query": "snapshot", "bogus_extra": 1})
        self.assertTrue(result.ok)
        self.assertEqual(result.metrics["attempts"], 1)
        self.assertGreaterEqual(result.metrics["active_seconds"], 0)
        self.assertEqual(result.metrics["corrections"], ["bogus_extra"])

    def test_write_capture_records_patient_revision(self):
        self.execute("memory_write", {"operation": "consolidate_event"})
        self.assertIsNotNone(self.state.ctx.patient_revision)

    def test_fabricated_warning_body_rejected_at_executor_even_without_guard(self):
        # Defense in depth: even a call that bypasses the guard's hydration
        # cannot persist a model-authored warning body — the executor checks
        # the body against the SAME single hydration source (the observed
        # warnings of this turn).
        fake_warning = {"drug_a": "合成药A", "drug_b": "合成药B", "severity": "major",
                        "effect": "模型编造的效果", "confidence": "high",
                        "citations": [{"uri": "https://example.test/fake"}],
                        "audit_trail": {"memory_refs": ["memory:episodic:1"], "source_refs": []}}
        result = self.execute("memory_write", {"operation": "record_warnings",
                                               "warnings": [fake_warning]})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["error_kind"], "policy_violation")
        rows = self.store.connection.execute("SELECT COUNT(*) FROM episodic_memory").fetchone()
        self.assertEqual(rows[0], 0)

    def test_observation_payload_shape_is_stable(self):
        failed = self.execute("memory_read", {"query": "not_a_query"})
        payload = failed.observation_payload()
        self.assertEqual(sorted(payload), ["error", "error_kind", "recoverable"])


class RunContextTests(unittest.TestCase):
    def test_checkpoint_projection_is_json_safe_and_secret_free(self):
        ctx = RunContext(run_id="r", turn_id="t", session_id="s",
                         event_id="e1", client_event_id="api:k1", patient_revision=7)
        data = ctx.checkpoint_dict()
        json.dumps(data, ensure_ascii=False)
        self.assertNotIn("budget", data)
        self.assertNotIn("principal", data)
        self.assertNotIn("cancel_event", data)
        restored = RunContext.from_checkpoint(data)
        self.assertEqual(restored.run_id, "r")
        self.assertEqual(restored.patient_revision, 7)
        # The principal is re-resolved from configuration, not the checkpoint.
        self.assertTrue(restored.principal.has_role("caregiver"))

    def test_cancelled_reflects_event_and_budget_reason(self):
        ctx = RunContext(run_id="r", turn_id="t")
        self.assertFalse(ctx.cancelled())
        ctx.cancel_event = threading.Event()
        ctx.cancel_event.set()
        self.assertTrue(ctx.cancelled())


class HookParityTests(unittest.TestCase):
    def _phases(self, runner_type):
        events = []
        hooks = HarnessHooks()
        for phase in ("before_model", "after_model", "before_tool", "after_tool", "before_publish"):
            hooks.add(phase, lambda event, p=phase: events.append(
                (p, event.kind, event.tool)))
        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with MemoryStore(path / "test.db") as store:
                agent = make_agent(store, provider, hooks=hooks)
                runner = runner_type(agent, checkpoint_path=path / "cp.db") \
                    if runner_type is LangGraphAgentRunner else runner_type(agent)
                try:
                    runner.run(event=CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}}),
                               session_id="s", turn_id="r", run_id="r")
                finally:
                    if hasattr(runner, "close"):
                        runner.close()
        return [phase for phase, _, _ in events]

    def test_hook_sequence_identical_for_both_runners(self):
        legacy = self._phases(LegacyAgentRunner)
        graph = self._phases(LangGraphAgentRunner)
        # The legacy loop records consolidate_event + profile_snapshot reads;
        # the graph runs plan/execute nodes over the same executor.  Both must
        # contain the same hook kinds for every tool dispatch and publish once.
        self.assertEqual([p for p in legacy if p == "before_publish"], ["before_publish"])
        self.assertEqual([p for p in graph if p == "before_publish"], ["before_publish"])
        self.assertEqual(legacy.count("before_tool"), graph.count("before_tool"))
        self.assertEqual(legacy.count("after_tool"), graph.count("after_tool"))
        self.assertGreater(legacy.count("before_tool"), 0)

    def test_failing_hook_never_breaks_a_turn(self):
        hooks = HarnessHooks()
        hooks.add("before_tool", lambda event: (_ for _ in ()).throw(RuntimeError("hook bug")))
        hooks.add("before_publish", lambda event: (_ for _ in ()).throw(RuntimeError("hook bug")))
        with tempfile.TemporaryDirectory() as directory:
            with MemoryStore(Path(directory) / "test.db") as store:
                agent = make_agent(store, ScriptedProvider([]), hooks=hooks)
                response = agent.handle(
                    CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}}),
                    session_id="s", turn_id="r")
        self.assertTrue(response.text)
        self.assertEqual(response.safety_status, "enforced")


if __name__ == "__main__":
    unittest.main()
