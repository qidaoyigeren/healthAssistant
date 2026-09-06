"""Reliability P1 regression tests: LangGraph minimal migration.

Covers the P1 acceptance from docs/reliability-design/07-staged-plan.md:
legacy/graph behavioural parity on isolated databases, node-level crash
recovery with a persistent checkpointer, persisted budget (restart does not
reset it), version routing via workflow_runs, JSON-safe checkpoint state,
and the interrupt/resume wiring smoke (framework-level — no P1 node raises
interrupt() yet; human review is P2).

All tests run on temporary databases with the deterministic offline agent.
"""
from __future__ import annotations

import json
import tempfile
import typing
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from stage0.agent import CareEvent, MedicationCoordinatorAgent
from stage0.graph_runner import (
    GRAPH_VERSION,
    LegacyAgentRunner,
    LangGraphAgentRunner,
    build_workflow_state,
    make_runner,
)
from stage0.memory import MemoryStore


EVENT = {
    "event_type": "medication_change",
    "text": "新增克拉霉素",
    "payload": {"action": "add", "medication": "克拉霉素"},
    "source": "caregiver",
}
EVENT_WITH_WARNING = {
    "event_type": "medication_change",
    "text": "新增辛伐他汀",
    "payload": {"action": "add", "medication": "辛伐他汀"},
    "source": "caregiver",
}


def _make_pair(directory: Path):
    store = MemoryStore(directory / "memory.db")
    agent = MedicationCoordinatorAgent(store)
    graph = LangGraphAgentRunner(agent, checkpoint_path=str(directory / "checkpoints.db"))
    return store, agent, graph


def _run_turns(runner, store) -> dict:
    r1 = runner.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                    client_event_id="api:k1", event_id="e1", run_id="run-1")
    r2 = runner.run(event=CareEvent(**EVENT_WITH_WARNING), session_id="s1",
                    turn_id="run-2", client_event_id="api:k2", event_id="e2", run_id="run-2")
    names = [m["display_name"] for m in store.current_medications()]
    conclusions = store.connection.execute(
        "SELECT COUNT(*) FROM conclusions WHERE kind='warning'").fetchone()[0]
    return {
        "r1_text": r1.text, "r2_text": r2.text,
        "r1_warnings": r1.warnings, "r2_warnings": r2.warnings,
        "r1_safety": r1.safety_status, "r2_safety": r2.safety_status,
        "medications": sorted(names),
        "warning_conclusions": conclusions,
        "interactions": [row["event_key"] for row in
                         store.connection.execute("SELECT event_key FROM interactions ORDER BY id")],
    }


class ParityTests(unittest.TestCase):
    def test_legacy_and_graph_produce_identical_outcomes(self) -> None:
        # P1 acceptance: original regression behaviour aligned.  Two ISOLATED
        # databases — the shadow graph run never touches the legacy store.
        with tempfile.TemporaryDirectory() as directory:
            legacy_store, legacy_agent, _ = _make_pair(Path(directory) / "legacy")
            try:
                legacy = _run_turns(LegacyAgentRunner(legacy_agent), legacy_store)
            finally:
                legacy_store.close()
        with tempfile.TemporaryDirectory() as directory:
            graph_store, graph_agent, graph = _make_pair(Path(directory) / "graph")
            try:
                from_graph = _run_turns(graph, graph_store)
                run_row = graph_store.workflow_run_get("run-2")
            finally:
                graph.close()
                graph_store.close()
        self.assertEqual(legacy["r1_text"], from_graph["r1_text"])
        self.assertEqual(legacy["r2_text"], from_graph["r2_text"])
        self.assertEqual(legacy["r2_warnings"], from_graph["r2_warnings"])
        self.assertEqual(legacy["medications"], from_graph["medications"])
        self.assertEqual(legacy["warning_conclusions"], from_graph["warning_conclusions"])
        self.assertEqual(legacy["interactions"], from_graph["interactions"])
        self.assertEqual(from_graph["r2_safety"], "enforced")
        # The graph runner mapped runs and snapshotted its version.
        self.assertEqual(run_row["graph_version"], GRAPH_VERSION)
        self.assertEqual(run_row["thread_id"], "run-2")
        self.assertEqual(run_row["status"], "succeeded")


class CrashRecoveryTests(unittest.TestCase):
    def test_crash_between_nodes_resumes_from_checkpoint(self) -> None:
        # T5 (P1 form): execute node crashes after plan; the outbox-level
        # retry resumes from the last checkpoint; operation receipts keep
        # exactly one projection.
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make_pair(Path(directory) / "graph")
            try:
                original_reflect = agent._reflect
                calls = {"n": 0}

                def crashing_reflect(state, observation):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("injected crash between nodes")
                    return original_reflect(state, observation)

                agent._reflect = crashing_reflect
                with self.assertRaises(RuntimeError):
                    graph.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                              client_event_id="api:k1", event_id="e1", run_id="run-1")
                run_row = store.workflow_run_get("run-1")
                self.assertEqual(run_row["status"], "running")
                # Recovery: same run/thread resumes from the checkpoint.
                agent._reflect = original_reflect
                response = graph.run(event=CareEvent(**EVENT), session_id="s1",
                                     turn_id="run-1", client_event_id="api:k1",
                                     event_id="e1", run_id="run-1")
                self.assertIn("克拉霉素", response.text)
                names = [m["display_name"] for m in store.current_medications()]
                self.assertEqual(names.count("克拉霉素"), 1)
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM operation_receipts WHERE "
                    "operation_type='consolidate_event'").fetchone()[0], 1)
                self.assertEqual(store.workflow_run_get("run-1")["status"], "succeeded")
            finally:
                graph.close()
                store.close()

    def test_crash_after_domain_write_before_checkpoint(self) -> None:
        # The dual-write window: domain commit succeeded, checkpoint missing.
        # Replay of execute hits the P0 receipt → no duplicate effect.
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make_pair(Path(directory) / "graph")
            try:
                # First, run the turn to completion so the receipt exists.
                graph.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                          client_event_id="api:k1", event_id="e1", run_id="run-1")
                # Simulate the crash window on a NEW run whose execute node
                # dies right after the domain write (before its checkpoint).
                original_act = agent._act
                calls = {"n": 0}

                def crashing_act(state, action):
                    result = original_act(state, action)
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("crash after domain write, before checkpoint")
                    return result

                agent._act = crashing_act
                with self.assertRaises(RuntimeError):
                    graph.run(event=CareEvent(**EVENT_WITH_WARNING), session_id="s1",
                              turn_id="run-2", client_event_id="api:k2",
                              event_id="e2", run_id="run-2")
                agent._act = original_act
                graph.run(event=CareEvent(**EVENT_WITH_WARNING), session_id="s1",
                          turn_id="run-2", client_event_id="api:k2",
                          event_id="e2", run_id="run-2")
                names = [m["display_name"] for m in store.current_medications()]
                self.assertEqual(names.count("辛伐他汀"), 1)
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM operation_receipts WHERE "
                    "operation_type='consolidate_event'").fetchone()[0], 2)
            finally:
                graph.close()
                store.close()


class BudgetPersistenceTests(unittest.TestCase):
    def test_restart_does_not_reset_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make_pair(Path(directory) / "graph")
            try:
                graph.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                          client_event_id="api:k1", event_id="e1", run_id="run-1")
                stored = store.workflow_run_get("run-1")["budget"]
                self.assertGreaterEqual(stored["consumed_seconds"], 0.0)
                self.assertIsNotNone(stored["wall_clock_seconds"])
                # A resumed run merges the persisted budget (fresh state built
                # from workflow_runs keeps accumulated consumption).
                initial = build_workflow_state(
                    event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                    client_event_id="api:k1", event_id="e1", run_id="run-1",
                    budget=stored)
                self.assertEqual(initial["budget"]["consumed_seconds"],
                                 stored["consumed_seconds"])
                self.assertEqual(initial["budget"]["wall_clock_seconds"],
                                 stored["wall_clock_seconds"])
            finally:
                graph.close()
                store.close()


class VersionRoutingTests(unittest.TestCase):
    def test_inflight_legacy_run_keeps_legacy_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                store.workflow_run_start(run_id="run-old", graph_version="legacy")
                router = make_runner(store, lambda: MedicationCoordinatorAgent(store),
                                     graph_enabled=True,
                                     checkpoint_path=str(Path(directory) / "cp.db"))
                try:
                    # Even with the flag ON, the existing legacy run stays
                    # legacy: the graph runner is never engaged for it (it
                    # would otherwise compile its StateGraph on first use).
                    router.run(event=CareEvent(**EVENT), session_id="s1",
                               turn_id="run-old", client_event_id="api:k1",
                               event_id="e1", run_id="run-old")
                    self.assertEqual(store.workflow_run_get("run-old")["graph_version"],
                                     "legacy")
                    self.assertIsNone(router.graph._graph)
                    # A NEW run follows the flag to the graph.
                    router.run(event=CareEvent(**EVENT), session_id="s1",
                               turn_id="run-new", client_event_id="api:k2",
                               event_id="e2", run_id="run-new")
                    self.assertEqual(store.workflow_run_get("run-new")["graph_version"],
                                     GRAPH_VERSION)
                    self.assertIsNotNone(router.graph._graph)
                finally:
                    router.close()
            finally:
                store.close()

    def test_flag_off_routes_new_runs_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                router = make_runner(store, lambda: MedicationCoordinatorAgent(store),
                                     graph_enabled=False)
                router.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-l",
                           client_event_id="api:k1", event_id="e1", run_id="run-l")
                self.assertEqual(store.workflow_run_get("run-l")["graph_version"], "legacy")
            finally:
                store.close()


class CheckpointSafetyTests(unittest.TestCase):
    def test_checkpoint_state_is_json_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make_pair(Path(directory) / "graph")
            try:
                graph.run(event=CareEvent(**EVENT_WITH_WARNING), session_id="s1",
                          turn_id="run-1", client_event_id="api:k1",
                          event_id="e1", run_id="run-1")
                saver = graph._ensure_checkpointer()
                config = {"configurable": {"thread_id": "run-1"}}
                dumps = 0
                for snapshot in saver.list(config, limit=5):
                    # Every checkpointed channel value must be JSON-safe —
                    # no connections, clients, credentials, or callables.
                    json.dumps(snapshot.metadata)
                    values = snapshot.checkpoint.get("channel_values", {})
                    for key, value in values.items():
                        if key in {"__start__", "client_event_id", "event_id",
                                   "degraded_reason", "pending_action", "result",
                                   "error"} and value is None:
                            continue
                        json.dumps(value, ensure_ascii=False)
                        dumps += 1
                self.assertGreater(dumps, 0)
            finally:
                graph.close()
                store.close()


class InterruptWiringSmokeTests(unittest.TestCase):
    def test_interrupt_and_command_resume_with_sqlite_saver(self) -> None:
        # Framework wiring smoke ONLY (P2 will use this; no P1 node raises
        # interrupt()).  Proves: interrupt surfaces, resume value becomes the
        # interrupt() return, and the containing node re-runs from its start —
        # which is why side-effecting work must live in dedicated nodes.
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt

        with tempfile.TemporaryDirectory() as directory:
            conn = sqlite3.connect(str(Path(directory) / "cp.db"), check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            runs = {"node": 0}

            class S(typing.TypedDict, total=False):
                value: int
                approved: bool

            def wait_for_approval(state: S) -> dict:
                runs["node"] += 1
                approved = interrupt("needs approval")
                return {"approved": bool(approved), "value": runs["node"]}

            g = StateGraph(S)
            g.add_node("wait_for_approval", wait_for_approval)
            g.add_edge(START, "wait_for_approval")
            g.add_edge("wait_for_approval", END)
            app = g.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "t1"}}
            first = app.invoke({"value": 0}, config)
            self.assertTrue(first["__interrupt__"])
            # Resume: the node re-runs from its start (runs counter increments).
            final = app.invoke(Command(resume=True), config)
            self.assertTrue(final["approved"])
            self.assertEqual(final["value"], 2)
            conn.close()


class ServiceLayerFlagTests(unittest.TestCase):
    def test_graph_flag_routes_service_events(self) -> None:
        import os
        from unittest import mock

        import stage0.server as server
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"AGENT_GRAPH_RUNNER": "1"}, clear=False):
                app = server.create_app(db_path=Path(directory) / "memory.db",
                                        worker_thread=False,
                                        checkpoint_path=str(Path(directory) / "cp.db"))
                store = app.state.store
                try:
                    self.assertTrue(app.state.worker.runner.graph_enabled)
                    client = TestClient(app)
                    response = client.post("/v1/events", json={
                        "event_type": "medication_change", "text": "新增克拉霉素",
                        "payload": {"action": "add", "medication": "克拉霉素"},
                        "session_id": "s1"}, headers={"Idempotency-Key": "g1"})
                    self.assertEqual(response.status_code, 202)
                    run_id = response.json()["run_id"]
                    app.state.worker.drain_once()
                    done = client.get("/v1/events/g1")
                    self.assertEqual(done.status_code, 200)
                    self.assertEqual(done.json()["status"], "committed")
                    self.assertEqual(store.workflow_run_get(run_id)["graph_version"],
                                     GRAPH_VERSION)
                    names = [m["display_name"] for m in store.current_medications()]
                    self.assertEqual(names.count("克拉霉素"), 1)
                finally:
                    app.state.worker.runner.close()
                    store.close()


if __name__ == "__main__":
    unittest.main()
