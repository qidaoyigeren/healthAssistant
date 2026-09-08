"""Harness P1-C: call spans, replay dedup, RunManifest, eval entry.

Acceptance coverage:

* stable call/attempt spans with run/operation identity, duration, receipt
  hits, guard rejections and degradation reasons;
* checkpoint REPLAYS dedup: same logical event folds into ``replay_count``
  (no duplicate rows), genuine retries get independent attempt rows, and
  persisted turn traces are not re-appended on graph node rebuild;
* RunManifest: immutable per run, no credentials, missing fields read as
  ``unknown``, version differences locatable, restore compatibility blocked
  on semantic changes;
* OTel export strictly opt-in and never load-bearing;
* the eval dataset runs end-to-end offline and all its invariants pass.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stage0.agent import AgentState, CareEvent, DDITool, MedicationCoordinatorAgent
from stage0.graph_runner import LangGraphAgentRunner
from stage0.harness.errors import ToolErrorKind, ToolExecutionError
from stage0.harness.manifest import (
    ManifestStore, build_manifest, check_restore_compatibility, manifest_diff,
)
from stage0.harness.observability import CallSpanStore, OTelExporter, SpanRecorder
from stage0.harness.runtime import RunContext
from stage0.memory import MemoryStore
from stage0.test_stage8_agent import _composer_unavailable


def _repeat_read(payload):
    return {"decision": "tool", "tool": "memory_read", "purpose": "snapshot",
            "arguments": {"query": "snapshot"}}


def make_agent(store, **kwargs):
    return MedicationCoordinatorAgent(
        store, ddi_tool=DDITool(lambda meds: []), rag_tool=None,
        llm_planner_enabled=True, proposal_provider=kwargs.pop("provider", _repeat_read),
        response_provider=_composer_unavailable, **kwargs)


class SpanDedupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _executor_and_state(self, agent):
        state = AgentState(session_id="s", turn_id="r",
                           event=CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}}))
        state.ctx = RunContext(run_id="r", turn_id="r", session_id="s")
        return state

    def test_replay_folds_into_replay_count_no_duplicate_rows(self):
        agent = make_agent(self.store)
        state = self._executor_and_state(agent)
        state.cycle = 3
        # Same cycle + same arguments: a checkpoint replay of the same event.
        for _ in range(3):
            agent.executor.execute(state.ctx, "memory_read", {"query": "snapshot"}, state=state)
        report = agent.span_store.dedup_report("r")
        self.assertEqual(report["stored_spans"], 1)
        self.assertEqual(report["replays_folded"], 2)
        self.assertEqual(report["raw_events"], 3)

    def test_genuine_retry_gets_independent_attempt_row(self):
        agent = make_agent(self.store)
        state = self._executor_and_state(agent)
        state.cycle = 1
        agent.executor.execute(state.ctx, "memory_read", {"query": "snapshot"}, state=state)
        # A different outcome in the same cycle is a NEW attempt, not a replay.
        agent.executor.execute(state.ctx, "memory_read", {"query": "conflicts"}, state=state)
        # And a dispatch that fails is recorded with its own error kind.
        failed = agent.executor.execute(state.ctx, "read_evidence", {"evidence_id": "missing"}, state=state)
        self.assertFalse(failed.ok)
        spans = agent.span_store.spans_for_run("r")
        self.assertEqual(len(spans), 3)
        error_spans = [s for s in spans if s["status"] == "error"]
        self.assertEqual(len(error_spans), 1)
        self.assertEqual(error_spans[0]["error_kind"], "evidence_unavailable")

    def test_turn_trace_persistence_is_idempotent_under_replay(self):
        entry = {"phase": "plan", "cycle": 1, "decision": {"tool": "memory_read"}}
        for _ in range(4):
            self.store.record_turn_trace("s", "r", 1, "plan", entry)
        rows = self.store.connection.execute(
            "SELECT COUNT(*) FROM turn_traces WHERE session_id='s' AND turn_id='r'").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_graph_node_replay_does_not_duplicate_persisted_trace(self):
        # Run one turn, then re-run _flush_traces over a rebuilt AgentState
        # (exactly what a graph node replay does) — row count must not grow.
        agent = make_agent(self.store)
        response = agent.handle(CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}}),
                                session_id="s", turn_id="r")
        before = self.store.connection.execute(
            "SELECT COUNT(*) FROM turn_traces WHERE session_id='s' AND turn_id='r'").fetchone()[0]
        self.assertGreater(before, 0)
        rebuilt = AgentState(session_id="s", turn_id="r",
                             event=CareEvent("register_profile", "登记72岁", {"profile": {}}),
                             trace=json.loads(json.dumps(response.tool_trace)))
        agent._flush_traces(rebuilt)  # trace_flushed=0 on rebuild: dedup must absorb
        after = self.store.connection.execute(
            "SELECT COUNT(*) FROM turn_traces WHERE session_id='s' AND turn_id='r'").fetchone()[0]
        self.assertEqual(after, before)

    def test_otel_exporter_off_by_default_and_never_load_bearing(self):
        exporter = OTelExporter()
        if not os.getenv("STAGE0_OTEL_EXPORT"):
            self.assertFalse(exporter.enabled)
        # Exporting while disabled is a no-op, not an error.
        exporter.export_span({"kind": "tool", "source": "x", "run_id": "r"})
        self.assertEqual(exporter.dropped, 0)


class RunManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "test.db")
        self.agent = make_agent(self.store)
        self.manifest_store = ManifestStore(self.store.connection, self.store._lock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _build(self, run_id="r"):
        manifest = build_manifest(run_id=run_id, agent=self.agent,
                                  graph_version="legacy", max_cycles=16)
        self.manifest_store.save(manifest)
        return manifest

    def test_manifest_is_saved_immutable_and_credential_free(self):
        self._build()
        self._build()  # second save must not overwrite
        stored = self.manifest_store.get("r")
        self.assertIsNotNone(stored)
        self.assertIn("revision", stored["code"])
        self.assertIn("limits", stored)
        self.assertIn("langgraph", stored["dependencies"])
        raw = json.dumps(stored, ensure_ascii=False)
        for forbidden in ("api_key", "sk-", "STAGE0_AUTH_TOKEN", "secret"):
            self.assertNotIn(forbidden, raw.lower() if forbidden != "STAGE0_AUTH_TOKEN" else raw)

    def test_manifest_diff_locates_version_changes_and_unknown_fields(self):
        current = self._build().to_dict()
        older = json.loads(json.dumps(current))
        del older["models"]["planner_model"]          # pre-P1 manifest: field absent
        older["limits"]["token_budget"] = 99
        diff = manifest_diff(older, current)
        self.assertFalse(diff["identical"])
        fields = {(item["section"], item["field"]) for item in diff["differences"]}
        self.assertIn(("models", "planner_model"), fields)
        # The missing field reads as unknown — never backfilled with today's value.
        model_item = next(item for item in diff["differences"]
                          if item["section"] == "models" and item["field"] == "planner_model")
        self.assertEqual(model_item["manifest_a"], "unknown")
        limit_item = next(item for item in diff["differences"]
                          if item["section"] == "limits")
        self.assertNotEqual(limit_item["manifest_a"], limit_item["manifest_b"])

    def test_restore_compatibility_blocked_on_semantic_change(self):
        current = self._build().to_dict()
        changed = json.loads(json.dumps(current))
        changed["limits"]["call_budget"] = 1
        verdict = check_restore_compatibility(changed, current)
        self.assertFalse(verdict["compatible"])
        self.assertTrue(verdict["migration_required"])
        same = check_restore_compatibility(current, current)
        self.assertTrue(same["compatible"])

    def test_both_runners_write_manifest_at_run_start(self):
        from stage0.graph_runner import LangGraphAgentRunner, LegacyAgentRunner
        agent = make_agent(self.store)
        event = CareEvent("register_profile", "登记72岁", {"profile": {"age": 72}})
        LegacyAgentRunner(agent).run(event=event, session_id="s",
                                     turn_id="rm-legacy", run_id="rm-legacy")
        graph = LangGraphAgentRunner(agent, checkpoint_path=Path(self.temp.name) / "cp.db")
        try:
            graph.run(event=event, session_id="s", turn_id="rm-graph", run_id="rm-graph")
        finally:
            graph.close()
        legacy_manifest = self.manifest_store.get("rm-legacy")
        graph_manifest = self.manifest_store.get("rm-graph")
        self.assertEqual(legacy_manifest["graph"]["graph_version"], "legacy")
        self.assertEqual(graph_manifest["graph"]["graph_version"], "1")
        # The graph manifest records the state schema version it checkpointed.
        self.assertEqual(graph_manifest["graph"]["state_schema_version"], "2")


class EvalEntryTests(unittest.TestCase):
    def test_full_dataset_passes_offline(self):
        from stage0 import harness_eval
        report = harness_eval.run_dataset()
        self.assertEqual(report["summary"]["failed"], 0,
                         msg=json.dumps([r for r in report["scenarios"] if r["status"] == "fail"],
                                        ensure_ascii=False)[:2000])
        self.assertEqual(report["dataset_version"], harness_eval.DATASET_VERSION)
        # Safety ordering stays an explicit invariant, never a tool-order copy.
        self.assertIn("INV_WARNINGS_GROUNDED", report["summary"]["all_invariants"])
        for scenario in report["scenarios"]:
            self.assertEqual(scenario["set"], "regression")

    def test_readable_report_rendering(self):
        from stage0 import harness_eval
        report = harness_eval.run_dataset()
        text = harness_eval.format_readable(report)
        self.assertIn("Harness P1-C", text)
        self.assertIn("[PASS]", text)


if __name__ == "__main__":
    unittest.main()
