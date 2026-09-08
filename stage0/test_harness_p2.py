"""Harness P2 tests: no-progress detection, controlled read reuse, progress
events with cursor replay, and resumable cancellation.

All tests run on temporary databases with synthetic cases and scripted
providers (offline rule); every optimization flag is exercised on BOTH sides
where the contract demands identical safety conclusions.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from stage0.agent import CareEvent, MedicationCoordinatorAgent
from stage0.graph_runner import LangGraphAgentRunner
from stage0.harness.progress import (
    EVENT_CANCELLED,
    NoProgressTracker,
    ProgressEventStore,
    cancel_event_for,
    read_signature,
    request_cancel,
)
from stage0.harness.reuse import ReuseCoordinator
from stage0.harness.tools import ToolExecutor, ToolSpec
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


def _repeat_reads_provider(payload):
    return {"decision": "tool", "tool": "memory_read", "purpose": "snapshot",
            "arguments": {"query": "snapshot"}}


# ---- 一、无进展检测 ------------------------------------------------------------


class NoProgressTests(unittest.TestCase):
    """AGENT_NO_PROGRESS_LIMIT: repeats get structured feedback, the threshold
    stops re-planning, real progress (fact change) is never mis-killed, and
    the counter survives a restart."""

    def _run_repeats(self, directory: Path, limit: str):
        from stage0.harness_eval import make_agent
        with mock.patch.dict(os.environ, {"AGENT_NO_PROGRESS_LIMIT": limit}, clear=False):
            store = MemoryStore(directory / "memory.db")
            agent = make_agent(store, provider=_repeat_reads_provider)
            graph = LangGraphAgentRunner(agent, checkpoint_path=str(directory / "cp.db"))
            try:
                response = graph.run(event=CareEvent(**EVENT), session_id="s",
                                     turn_id="run-np", event_id="run-np", run_id="run-np")
            finally:
                graph.close()
            tracker_rows = store.connection.execute(
                "SELECT * FROM run_progress_state WHERE run_id='run-np'").fetchall()
            store.close()
            return response, tracker_rows

    def test_repeats_stop_at_threshold_with_structured_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response, rows = self._run_repeats(Path(directory), "2")
            acts = [e for e in response.tool_trace if e.get("phase") == "act"]
            # 1 fresh read + 2 repeats (feedback at 1, stop at the limit of 2)
            # instead of running to the max_cycles budget.
            self.assertEqual(len(acts), 3)
            feedback = [e for e in response.tool_trace if e.get("phase") == "no_progress"
                        and "重复读取" in str(e.get("note", ""))]
            self.assertGreaterEqual(len(feedback), 1)  # structured, evidence-naming
            stop = [e for e in response.tool_trace
                    if e.get("unfinished_items")]
            self.assertTrue(stop)
            # The delivered text is honest about incompleteness (this run
            # never consolidated the event) and always escalates.
            self.assertTrue("未保存" in response.text or "未完成" in response.text)
            self.assertIn("建议咨询医生/药师", response.text)
            # Persisted stop state (restart does not reset the detection).
            self.assertEqual(rows[0]["stopped_reason"], "no_progress")

    def test_repeat_is_not_a_safety_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response, _ = self._run_repeats(Path(directory), "2")
            self.assertEqual(response.safety_status, "enforced")
            # Legal reads are never reported as permission/policy errors.
            errors = [e for e in response.tool_trace if e.get("phase") == "observe"
                      and not e.get("ok")]
            self.assertEqual(errors, [])

    def test_fact_change_makes_same_query_progress(self) -> None:
        # Signature-level contract: the same arguments over a CHANGED revision
        # (write, new user information) are a different signature → progress.
        self.assertNotEqual(
            read_signature("memory_read", {"query": "snapshot"}, scope_id="s",
                           patient_revision=1),
            read_signature("memory_read", {"query": "snapshot"}, scope_id="s",
                           patient_revision=2))

    def test_tracker_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            sig = read_signature("memory_read", {"query": "snapshot"}, scope_id="s",
                                 patient_revision=0)
            first = NoProgressTracker(store.connection, store._lock)
            self.assertEqual(first.record("r", sig, limit=1)["verdict"], "progress")
            self.assertEqual(first.record("r", sig, limit=1)["verdict"], "stop")
            store.close()
            # "Restart": a fresh tracker over the same database.
            store = MemoryStore(Path(directory) / "memory.db")
            second = NoProgressTracker(store.connection, store._lock)
            self.assertEqual(second.record("r", sig, limit=1)["verdict"], "stopped")
            store.close()

    def test_happy_path_unaffected_when_enabled(self) -> None:
        # A run with real progress (writes change the revision) must not be
        # stopped by the detector even at an aggressive limit.
        from stage0.harness_eval import run_graph_scenario
        result = run_graph_scenario("happy_path_with_no_progress_limit",
                                    env={"AGENT_NO_PROGRESS_LIMIT": "1"})
        self.assertEqual(result["errors"], [])
        tools = result["metrics"]["act_tools"]
        self.assertIn("memory_write", tools)
        self.assertIn("ddi_check", tools)
        self.assertEqual(result["response"].safety_status, "enforced")


# ---- 二、受控结果复用 ------------------------------------------------------------


def _read_spec(name: str = "probe") -> ToolSpec:
    return ToolSpec(name=name, description="test read", argument_schema={
        "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
    }, result_shape="dict(q)", kind="read", required_permission="memory:read",
        idempotency="pure", cacheable=True)


class ReuseTests(unittest.TestCase):
    def _executor(self, store: MemoryStore, coordinator: ReuseCoordinator, counter: dict):
        from stage0.harness.runtime import RunContext
        executor = ToolExecutor(reuse=coordinator)
        executor.register(_read_spec(), lambda request: {"q": request.arguments["q"],
                                                         "n": counter["calls"] + 0})
        def handler(request):
            counter["calls"] += 1
            return {"q": request.arguments["q"], "call": counter["calls"]}
        executor.register(_read_spec("flaky"), handler)
        executor.register(_read_spec("boom"), handler)
        ctx = RunContext(run_id="r1", turn_id="r1")
        return executor, ctx

    def test_same_run_reuse_refresh_and_failures_never_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            counter = {"calls": 0}
            coordinator = ReuseCoordinator(same_run=True, cross_run=False)
            executor, ctx = self._executor(store, coordinator, counter)
            first = executor.execute(ctx, "flaky", {"q": "x"})
            second = executor.execute(ctx, "flaky", {"q": "x"})
            self.assertTrue(first.ok and second.ok)
            self.assertIsNone(first.metrics.get("cache_hit"))
            self.assertEqual(second.metrics.get("cache_hit"), "same_run")
            self.assertEqual(second.value["call"], first.value["call"])  # stored result
            self.assertEqual(counter["calls"], 1)
            # Refresh bypasses the READ (handler re-runs) and re-stores.
            refreshed = executor.execute(ctx, "flaky", {"q": "x", "refresh": True})
            self.assertIsNone(refreshed.metrics.get("cache_hit"))
            self.assertEqual(counter["calls"], 2)
            # Failures are never stored: the next call dispatches again.
            def failing(request):
                counter["calls"] += 1
                raise ValueError("boom")
            executor.register(_read_spec("boom"), failing, override=True)
            failed = executor.execute(ctx, "boom", {"q": "y"})
            self.assertFalse(failed.ok)
            again = executor.execute(ctx, "boom", {"q": "y"})
            self.assertFalse(again.ok)
            self.assertEqual(counter["calls"], 4)
            self.assertEqual(coordinator.stats["stores"], 2)  # only ok results
            store.close()

    def test_cross_run_cache_isolation_and_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            coordinator = ReuseCoordinator(same_run=False, cross_run=True,
                                           connection=store.connection,
                                           lock=store._lock)
            counter = {"calls": 0}
            executor, ctx = self._executor(store, coordinator, counter)
            executor.execute(ctx, "flaky", {"q": "x"})
            # A NEW run over the SAME world hits the cross-run cache.
            from stage0.harness.runtime import RunContext
            ctx2 = RunContext(run_id="r2", turn_id="r2")
            hit = executor.execute(ctx2, "flaky", {"q": "x"})
            self.assertEqual(hit.metrics.get("cache_hit"), "cross_run")
            self.assertEqual(counter["calls"], 1)
            # Scope isolation: another scope never sees this result — the
            # signature carries the scope, so a query-only key cannot match.
            self.assertIsNone(coordinator.cache.get(
                read_signature("flaky", {"q": "x"}, scope_id="other-scope",
                               patient_revision=None, corpus_version=None),
                scope_id="other-scope"))
            # Patient-specific isolation: a different revision is a new key.
            miss = coordinator.lookup(run_id="r3", tool="flaky", arguments={"q": "x"},
                                      scope_id="local-demo", patient_revision=99)
            self.assertIsNone(miss)
            self.assertEqual(counter["calls"], 1)  # lookups never dispatch
            store.close()

    def test_writes_are_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            coordinator = ReuseCoordinator(same_run=True, cross_run=False)
            executor = ToolExecutor(reuse=coordinator)
            calls = {"n": 0}

            def write_handler(request):
                calls["n"] += 1
                return {"operation": "consolidate_event", "ok": True}
            executor.register(ToolSpec(
                name="w", description="write", argument_schema={"type": "object",
                                                                "properties": {}},
                result_shape="dict", kind="write", required_permission="memory:write",
                idempotency="receipt_keyed"), write_handler)
            from stage0.harness.runtime import RunContext
            ctx = RunContext(run_id="r1", turn_id="r1")
            executor.execute(ctx, "w", {})
            executor.execute(ctx, "w", {})
            self.assertEqual(calls["n"], 2)  # dispatched for real both times
            store.close()

    def test_cache_flag_sides_agree_on_safety(self) -> None:
        from stage0.harness_eval import run_graph_scenario
        off = run_graph_scenario("cache_off")
        on = run_graph_scenario("cache_on", env={"STAGE0_RUN_REUSE": "1",
                                                 "STAGE0_READ_CACHE": "1"})
        self.assertEqual(off["errors"], [])
        self.assertEqual(on["errors"], [])
        self.assertEqual(off["response"].safety_status, "enforced")
        self.assertEqual(on["response"].safety_status, "enforced")
        self.assertEqual(len(off["warnings"]), len(on["warnings"]))
        self.assertEqual(off["metrics"]["citation_validity"],
                         on["metrics"]["citation_validity"])


# ---- 三、进度事件 ---------------------------------------------------------------


class ProgressEventTests(unittest.TestCase):
    def test_emit_idempotent_and_cursor_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            progress = ProgressEventStore(store.connection, store._lock)
            e1 = progress.emit("r", "accepted", detail={"task_id": 1})
            e1b = progress.emit("r", "accepted", detail={"task_id": 1})  # replay
            self.assertEqual(e1, e1b)
            progress.emit("r", "retrieving", cycle=1, tool="rag_search",
                          detail={"ok": True, "evidence_count": 2})
            progress.emit("r", "checking_risks", cycle=1, tool="ddi_check",
                          detail={"ok": True, "evidence_count": 1})
            page = progress.events_since("r", 0)
            self.assertEqual(len(page["events"]), 3)
            self.assertFalse(page["snapshot"])
            tail = progress.events_since("r", page["events"][1]["seq"])
            self.assertEqual([e["kind"] for e in tail["events"]], ["checking_risks"])
            self.assertEqual(tail["latest_seq"], 3)
            # Cursor older than retained history → snapshot marker: the
            # client saw up to seq 1 but seq 2 was pruned — a gap.
            store.connection.execute("DELETE FROM run_progress_events WHERE seq=2")
            store.connection.commit()
            snap = progress.events_since("r", 1)
            self.assertTrue(snap["snapshot"])
            self.assertEqual([e["seq"] for e in snap["events"]], [3])
            store.close()

    def _app(self, directory: Path):
        from stage0.server import create_app
        return create_app(db_path=Path(directory) / "memory.db", worker_thread=False)

    def test_service_progress_cursor_and_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                accepted = client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                                       headers={"Idempotency-Key": "k-prog-1"})
                self.assertEqual(accepted.status_code, 202)
                run_id = accepted.json()["run_id"]
                self.assertIsNotNone(store.workflow_run_get(run_id))
                worker.drain_once()
                page = client.get(f"/v1/runs/{run_id}/progress")
                self.assertEqual(page.status_code, 200)
                body = page.json()
                kinds = [e["kind"] for e in body["events"]]
                self.assertIn("accepted", kinds)
                self.assertIn("completed", kinds)
                self.assertEqual(body["run_status"], "succeeded")
                # Cursor replay: after the latest seq there is nothing new.
                tail = client.get(f"/v1/runs/{run_id}/progress?after={body['latest_seq']}")
                self.assertEqual(tail.json()["events"], [])
                self.assertEqual(client.get("/v1/runs/does-not-exist/progress").status_code, 404)
                # Progress events carry NO delivered/clinical text.
                blob = json.dumps(body, ensure_ascii=False)
                committed = client.get("/v1/events/k-prog-1").json()["response"]["text"]
                for sentence in committed.split("。"):
                    if len(sentence) >= 8:
                        self.assertNotIn(sentence, blob)
            finally:
                worker.runner.close()
                store.close()


# ---- 四、取消 ------------------------------------------------------------------


class CancelTests(unittest.TestCase):
    def _app(self, directory: Path, review: bool = False):
        from stage0.server import create_app
        env = {"AGENT_GRAPH_RUNNER": "1"} if review else {}
        if review:
            env["STAGE0_REVIEW_ENABLED"] = "1"
        ctx = mock.patch.dict(os.environ, env, clear=False)
        ctx.start()
        self.addCleanup(ctx.stop)
        return create_app(db_path=Path(directory) / "memory.db", worker_thread=False,
                          checkpoint_path=str(Path(directory) / "cp.db"))

    def test_cancel_queued_task_executes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                accepted = client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                                       headers={"Idempotency-Key": "k-cancel-1"}).json()
                run_id = accepted["run_id"]
                first = client.post(f"/v1/runs/{run_id}/cancel", json={"reason": "用户取消"})
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["cancel_state"], "cancelled")
                # Idempotent repeat.
                again = client.post(f"/v1/runs/{run_id}/cancel", json={})
                self.assertEqual(again.json()["cancel_state"], "cancelled")
                worker.drain_once()
                done = client.get("/v1/events/k-cancel-1")
                self.assertEqual(done.status_code, 200)  # persisted terminal state
                result = done.json()["response"]
                self.assertEqual(result["run_status"], "cancelled")
                self.assertIn("取消", result["text"])
                self.assertIn("建议咨询医生/药师", result["text"])
                # Nothing executed: no domain effect at all.
                rows = store.connection.execute(
                    "SELECT COUNT(*) FROM episodic_memory WHERE event_type='medication_add'"
                ).fetchone()[0]
                self.assertEqual(rows, 0)
                # Progress ledger records request + completion.
                page = client.get(f"/v1/runs/{run_id}/progress").json()
                kinds = [e["kind"] for e in page["events"]]
                self.assertIn("cancel_requested", kinds)
                self.assertIn("cancelled", kinds)
                self.assertEqual(store.workflow_run_get(run_id)["status"], "cancelled")
            finally:
                worker.runner.close()
                store.close()

    def test_cancel_terminal_run_is_already_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                accepted = client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                                       headers={"Idempotency-Key": "k-cancel-2"}).json()
                worker.drain_once()
                self.assertEqual(client.get("/v1/events/k-cancel-2").status_code, 200)
                outcome = client.post(f"/v1/runs/{accepted['run_id']}/cancel", json={})
                self.assertEqual(outcome.json()["cancel_state"], "already_final")
                self.assertEqual(store.workflow_run_get(accepted["run_id"])["status"],
                                 "succeeded")
            finally:
                worker.runner.close()
                store.close()

    def test_cancel_waiting_review_blocks_late_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory), review=True)
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                            headers={"Idempotency-Key": "k-rv-0"})
                worker.drain_once()
                accepted = client.post("/v1/events",
                                       json={**EVENT_WITH_WARNING, "session_id": "s1"},
                                       headers={"Idempotency-Key": "k-rv-1"}).json()
                worker.drain_once()
                run_id = accepted["run_id"]
                self.assertEqual(store.workflow_run_get(run_id)["status"], "waiting_review")
                case = store.review_cases()[0]
                cancelled = client.post(f"/v1/runs/{run_id}/cancel", json={})
                self.assertEqual(cancelled.json()["cancel_state"], "cancelled")
                self.assertEqual(store.review_case(case["id"])["status"], "cancelled")
                # A late reviewer decision cannot revive the cancelled run.
                late = client.post(f"/v1/review-cases/{case['id']}/decisions",
                                   json={"action": "close_with_safe_guidance",
                                         "payload": {"basis": "迟到决定"},
                                         "expected_revision": case["revision"]},
                                   headers={"Idempotency-Key": "k-rv-late"})
                self.assertEqual(late.status_code, 409)
                worker.drain_resume_tasks()  # nothing pending / nothing revives
                self.assertEqual(store.workflow_run_get(run_id)["status"], "cancelled")
            finally:
                worker.runner.close()
                store.close()

    def test_mid_turn_cancel_stops_planning_keeps_committed_effects(self) -> None:
        # Simulates the API thread's cancel path: request_cancel persists the
        # request, CAS-moves the run to 'cancelled' and sets the in-process
        # event the runner observes at its next scheduling point.  The
        # executor HANDLER is wrapped (the executor binds tool callables at
        # registration, so replacing agent.tools would not take effect).
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = MemoryStore(directory / "memory.db")
            agent = MedicationCoordinatorAgent(store)
            graph = LangGraphAgentRunner(agent, checkpoint_path=str(directory / "cp.db"))
            original_handler = agent.executor.handlers["ddi_check"]

            def canceling_handler(request):
                request_cancel(store, "run-mid", actor="test", reason="模拟用户取消")
                return original_handler(request)
            agent.executor.handlers["ddi_check"] = canceling_handler
            try:
                response = graph.run(event=CareEvent(**EVENT), session_id="s",
                                     turn_id="run-mid", event_id="run-mid",
                                     run_id="run-mid")
            finally:
                graph.close()
            tools = [e["tool"] for e in response.tool_trace if e.get("phase") == "act"]
            # The committed write STAYS; the loop stopped right after cancel.
            self.assertIn("memory_write", tools)
            self.assertNotIn("rag_search", tools)
            self.assertEqual(store.workflow_run_get("run-mid")["status"], "cancelled")
            self.assertEqual(response.safety_status, "enforced")
            interactions = store.connection.execute(
                "SELECT COUNT(*) FROM interactions").fetchone()[0]
            self.assertEqual(interactions, 1)
            store.close()

    def test_cancel_survives_worker_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                accepted = client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                                       headers={"Idempotency-Key": "k-cancel-3"}).json()
                client.post(f"/v1/runs/{accepted['run_id']}/cancel", json={})
            finally:
                worker.runner.close()
                store.close()
            # "Restart": a fresh app (store + worker) over the same database.
            app2 = self._app(Path(directory))
            store2 = app2.state.store
            worker2 = app2.state.worker
            try:
                worker2.drain_once()
                self.assertEqual(store2.workflow_run_get(accepted["run_id"])["status"],
                                 "cancelled")
            finally:
                worker2.runner.close()
                store2.close()


if __name__ == "__main__":
    unittest.main()
