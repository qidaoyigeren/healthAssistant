"""Reliability P2 regression tests: the human review closed loop.

Covers docs/reliability-design/06-human-handoff.md acceptance: idempotent
case creation, CAS claim, transactional decision + resume task, decision
replay (double callback), crash before resume consumption, review_stale
re-review (旧审批不授权新状态), overdue never auto-approves, role/scope
authorization, and the full service-layer cycle (submit → wait → claim →
decide → resume → resolved) against temporary databases with the
deterministic offline agent.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from stage0.agent import CareEvent, MedicationCoordinatorAgent
from stage0.graph_runner import LangGraphAgentRunner
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


def _make(directory: Path, **graph_kwargs):
    store = MemoryStore(directory / "memory.db")
    agent = MedicationCoordinatorAgent(store)
    graph = LangGraphAgentRunner(agent, checkpoint_path=str(directory / "cp.db"),
                                 review_enabled=True, **graph_kwargs)
    return store, agent, graph


class ReviewTriggerTests(unittest.TestCase):
    def test_severe_warning_creates_exactly_one_case_and_parks_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make(Path(directory))
            try:
                graph.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                          client_event_id="api:k1", event_id="e1", run_id="run-1")
                response = graph.run(event=CareEvent(**EVENT_WITH_WARNING),
                                     session_id="s1", turn_id="run-2",
                                     client_event_id="api:k2", event_id="e2",
                                     run_id="run-2")
                cases = store.review_cases()
                self.assertEqual(len(cases), 1)
                self.assertIn("severe_warning", cases[0]["reason_codes"])
                self.assertEqual(cases[0]["status"], "open")
                self.assertEqual(cases[0]["run_id"], "run-2")
                # The user-facing turn completed with the safe waiting text.
                self.assertIn("提交专业审核", response.text)
                self.assertIn("建议咨询医生/药师", response.text)
                self.assertEqual(response.audit_trail.get("run_status"), "waiting_review")
                # Run is non-terminal, worker-free (lease released upstream).
                self.assertEqual(store.workflow_run_get("run-2")["status"], "running")
                # Facts snapshot recorded for the staleness check.
                self.assertIsNotNone(cases[0]["fact_medication_set_hash"])
            finally:
                graph.close()
                store.close()

    def test_mild_turn_does_not_create_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make(Path(directory))
            try:
                graph.run(event=CareEvent(**EVENT), session_id="s1", turn_id="run-1",
                          client_event_id="api:k1", event_id="e1", run_id="run-1")
                self.assertEqual(store.review_cases(), [])
                self.assertEqual(store.workflow_run_get("run-1")["status"], "succeeded")
            finally:
                graph.close()
                store.close()

    def test_replayed_open_review_hits_logic_key(self) -> None:
        # Crash between case creation and the checkpoint → the node replays
        # and must NOT create a second ticket.
        with tempfile.TemporaryDirectory() as directory:
            store, agent, graph = _make(Path(directory))
            try:
                logic_key = "event:e2:severe_warning:r1"
                first = store.open_review_case(
                    logic_key=logic_key, event_id="e2", run_id="run-2",
                    reason_codes=["severe_warning"], summary={"x": 1},
                    due_at="2026-09-06T00:00:00+00:00")
                second = store.open_review_case(
                    logic_key=logic_key, event_id="e2", run_id="run-2",
                    reason_codes=["severe_warning"], summary={"x": 1},
                    due_at="2026-09-06T00:00:00+00:00")
                self.assertEqual(first["id"], second["id"])
                self.assertEqual(len(store.review_cases()), 1)
            finally:
                graph.close()
                store.close()


class DecisionTransactionTests(unittest.TestCase):
    def _opened_case(self, store) -> dict:
        return store.open_review_case(
            logic_key="event:e1:severe_warning:r1", event_id="e1", run_id="run-1",
            reason_codes=["severe_warning"], summary={"warnings": []},
            due_at="2099-01-01T00:00:00+00:00")

    def test_claim_cas_and_decision_idempotency(self) -> None:
        from stage0.memory import MemoryPolicyError
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                case = self._opened_case(store)
                # Claim with a wrong revision is refused.
                with self.assertRaises(MemoryPolicyError):
                    store.claim_review_case(case["id"], expected_revision=99,
                                            assignee="reviewer-1")
                claimed = store.claim_review_case(case["id"], expected_revision=1,
                                                  assignee="reviewer-1")
                self.assertEqual(claimed["status"], "assigned")
                # Re-claim by the SAME assignee is idempotent...
                again = store.claim_review_case(case["id"], expected_revision=2,
                                                assignee="reviewer-1")
                self.assertEqual(again["assignee"], "reviewer-1")
                # ...but a second reviewer cannot steal an assigned case.
                with self.assertRaises(MemoryPolicyError):
                    store.claim_review_case(case["id"], expected_revision=2,
                                            assignee="reviewer-2")
                # Decision: CAS revision mismatch refused...
                with self.assertRaises(MemoryPolicyError):
                    store.record_review_decision(
                        case_id=case["id"], expected_revision=1, action="reject_candidate",
                        payload={"basis": "x"}, idempotency_key="rk-1", actor_id="reviewer-1")
                # ...correct revision accepted...
                record = store.record_review_decision(
                    case_id=case["id"], expected_revision=2, action="reject_candidate",
                    payload={"basis": "需人工核实"}, idempotency_key="rk-1",
                    actor_id="reviewer-1")
                self.assertFalse(record["replayed"])
                # ...and the same Idempotency-Key replays (double callback).
                replay = store.record_review_decision(
                    case_id=case["id"], expected_revision=2, action="reject_candidate",
                    payload={"basis": "需人工核实"}, idempotency_key="rk-1",
                    actor_id="reviewer-1")
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["decision_id"], record["decision_id"])
                # Exactly one resume task for exactly one decision.
                self.assertEqual(len(store.pending_resume_tasks()), 1)
                self.assertEqual(store.pending_resume_tasks()[0]["operation_id"],
                                 f"resume:{record['decision_id']}")
            finally:
                store.close()

    def test_decision_on_open_case_refused(self) -> None:
        from stage0.memory import MemoryPolicyError
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                case = self._opened_case(store)
                with self.assertRaises(MemoryPolicyError):
                    store.record_review_decision(
                        case_id=case["id"], expected_revision=1,
                        action="close_with_safe_guidance", payload={},
                        idempotency_key="rk-2", actor_id="reviewer-1")
            finally:
                store.close()


class FullReviewCycleTests(unittest.TestCase):
    """Service-layer cycles via the env flags so create_app builds ONE store
    and the default routed runner (graph + review enabled)."""

    def _app(self, directory: Path):
        import os
        from unittest import mock

        from stage0.server import create_app
        ctx = mock.patch.dict(os.environ,
                              {"AGENT_GRAPH_RUNNER": "1", "STAGE0_REVIEW_ENABLED": "1"},
                              clear=False)
        ctx.start()
        app = create_app(db_path=Path(directory) / "memory.db", worker_thread=False,
                         checkpoint_path=str(Path(directory) / "cp.db"))
        self.addCleanup(ctx.stop)
        return app

    def test_claim_decide_resume_resolves_case(self) -> None:
        # Full service-layer cycle on the real graph runner.
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                            headers={"Idempotency-Key": "k1"})
                worker.drain_once()
                parked = client.post("/v1/events",
                                     json={**EVENT_WITH_WARNING, "session_id": "s1"},
                                     headers={"Idempotency-Key": "k2"}).json()
                worker.drain_once()
                done = client.get("/v1/events/k2")
                self.assertEqual(done.status_code, 200)
                self.assertEqual(done.json()["response"].get("run_status"), "waiting_review")
                run_id = parked["run_id"]
                case = store.review_cases()[0]
                self.assertEqual(store.workflow_run_get(run_id)["status"], "waiting_review")

                # Queue is visible to the (demo) reviewer; claim CAS; decide.
                listed = client.get("/v1/review-cases").json()
                self.assertIn(case["id"], [c["id"] for c in listed])
                claimed = client.post(f"/v1/review-cases/{case['id']}/claim",
                                      json={"expected_revision": case["revision"]}).json()
                self.assertEqual(claimed["status"], "assigned")
                decision = client.post(
                    f"/v1/review-cases/{case['id']}/decisions",
                    json={"action": "close_with_safe_guidance",
                          "payload": {"basis": "演示：按固定指引收尾"},
                          "expected_revision": claimed["revision"]},
                    headers={"Idempotency-Key": "rv-decide-1"})
                self.assertEqual(decision.status_code, 202)

                # The worker consumes the resume task and the run converges.
                worker.drain_resume_tasks()
                self.assertEqual(store.workflow_run_get(run_id)["status"], "succeeded")
                self.assertEqual(store.review_case(case["id"])["status"], "resolved")
                # The parked run resumed THROUGH its own final safety gates;
                # delivered text is fixed code text + the checked body.
                result = client.get("/v1/events/k2").json()["response"]
                self.assertIn("专业审核已完成", result["text"])
                # Audit record documents the applied decision.
                audits = store.connection.execute(
                    "SELECT action FROM audit_log WHERE action='review_decision_recorded'"
                ).fetchall()
                self.assertTrue(audits)
            finally:
                worker.runner.close()
                store.close()

    def test_review_stale_opens_new_round_and_keeps_run_waiting(self) -> None:
        # 旧审批不授权新状态: facts change while parked → the decision is
        # refused, the old case cancelled, a fresh round opened.
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                client.post("/v1/events", json={**EVENT, "session_id": "s1"},
                            headers={"Idempotency-Key": "k1"})
                worker.drain_once()
                parked = client.post("/v1/events",
                                     json={**EVENT_WITH_WARNING, "session_id": "s1"},
                                     headers={"Idempotency-Key": "k2"}).json()
                worker.drain_once()
                run_id = parked["run_id"]
                case = store.review_cases()[0]
                claimed = client.post(f"/v1/review-cases/{case['id']}/claim",
                                      json={"expected_revision": case["revision"]}).json()
                client.post(f"/v1/review-cases/{case['id']}/decisions",
                            json={"action": "resolve_conflict",
                                  "payload": {"conflict_ref": "conflict:1@v1",
                                              "basis": "演示"},
                                  "expected_revision": claimed["revision"]},
                            headers={"Idempotency-Key": "rv-stale-1"})
                # Facts MOVE while the case waits (new medication event).
                client.post("/v1/events", json={**EVENT, "text": "新增氨氯地平",
                                                "payload": {"action": "add",
                                                            "medication": "氨氯地平"},
                                                "session_id": "s1"},
                            headers={"Idempotency-Key": "k3"})
                worker.drain_once()  # this also consumes the stale resume task
                old_case = store.review_case(case["id"])
                self.assertEqual(old_case["status"], "cancelled")
                decisions = store.review_decisions_for(case["id"])
                self.assertEqual(decisions[0]["outcome"], "review_stale")
                # A fresh round is open; the run is still parked.
                self.assertEqual(store.workflow_run_get(run_id)["status"],
                                 "waiting_review")
                rounds = [c for c in store.review_cases() if c["round"] == 2]
                self.assertEqual(len(rounds), 1)
                self.assertIn("review_stale", rounds[0]["reason_codes"])
                # The old decision never took effect.
                names = [m["display_name"] for m in store.current_medications()]
                self.assertEqual(names.count("克拉霉素"), 1)
            finally:
                worker.runner.close()
                store.close()


class OverdueTests(unittest.TestCase):
    def test_overdue_is_operational_never_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                store.open_review_case(
                    logic_key="event:e1:severe_warning:r1", event_id="e1", run_id="run-1",
                    reason_codes=["severe_warning"], summary={},
                    due_at=(datetime.now(timezone.utc) - timedelta(seconds=1))
                    .isoformat(timespec="seconds"))
                store.mark_overdue_review_cases()
                case = store.review_cases()[0]
                self.assertEqual(case["status"], "overdue")
                # No decision, no resolution, no "confirmed" of any kind.
                self.assertEqual(store.review_decisions_for(case["id"]), [])
                # The run side is untouched: no auto-approved effect.
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM episodic_memory WHERE "
                    "event_type='clinical_review'").fetchone()[0], 0)
            finally:
                store.close()


class AuthorizationTests(unittest.TestCase):
    def test_deployment_reviewer_roles_and_object_scope(self) -> None:
        import os
        from unittest import mock

        from stage0.server import create_app
        with tempfile.TemporaryDirectory() as directory:
            env = {"STAGE0_AUTH_TOKEN": "tok-r", "STAGE0_AUTH_ROLES": "caregiver,reviewer",
                   "AGENT_GRAPH_RUNNER": "1", "STAGE0_REVIEW_ENABLED": "1"}
            with mock.patch.dict(os.environ, env, clear=False):
                app = create_app(db_path=Path(directory) / "memory.db", worker_thread=False,
                                 auth_mode="deployment",
                                 checkpoint_path=str(Path(directory) / "cp.db"))
            store = app.state.store
            try:
                store.open_review_case(
                    logic_key="event:e1:severe_warning:r1", event_id="e1", run_id="run-1",
                    reason_codes=["severe_warning"], summary={},
                    due_at="2099-01-01T00:00:00+00:00")
                client = TestClient(app)
                headers = {"X-Stage0-Token": "tok-r"}
                # reviewer-role token sees the queue; anonymous is refused.
                ok = client.get("/v1/review-cases", headers=headers)
                self.assertEqual(ok.status_code, 200)
                unauth = client.get("/v1/review-cases")
                self.assertEqual(unauth.status_code, 401)
                # Claim/decide flows authenticate; a bad revision is a 409,
                # not a crash, and leaks no patient data.
                claimed = client.post("/v1/review-cases/1/claim", headers=headers,
                                      json={"expected_revision": 1})
                self.assertEqual(claimed.status_code, 200)
                denied_revision = client.post(
                    "/v1/review-cases/1/decisions", headers=headers,
                    json={"action": "reject_candidate", "expected_revision": 1})
                # missing Idempotency-Key → 422
                self.assertEqual(denied_revision.status_code, 422)
                denied_revision2 = client.post(
                    "/v1/review-cases/1/decisions",
                    headers={**headers, "Idempotency-Key": "rv-bad-rev"},
                    json={"action": "reject_candidate", "expected_revision": 1})
                self.assertEqual(denied_revision2.status_code, 409)
            finally:
                app.state.worker.runner.close()
                store.close()


class SummaryExportTests(unittest.TestCase):
    def test_summary_endpoint_shape(self) -> None:
        import os
        from unittest import mock

        from stage0.server import create_app
        with tempfile.TemporaryDirectory() as directory:
            env = {"AGENT_GRAPH_RUNNER": "1", "STAGE0_REVIEW_ENABLED": "1"}
            with mock.patch.dict(os.environ, env, clear=False):
                app = create_app(db_path=Path(directory) / "memory.db", worker_thread=False,
                                 checkpoint_path=str(Path(directory) / "cp.db"))
            store = app.state.store
            try:
                store.open_review_case(
                    logic_key="event:e1:severe_warning:r1", event_id="e1", run_id="run-1",
                    reason_codes=["severe_warning"],
                    summary={"event_text": "新增辛伐他汀", "warnings": []},
                    due_at="2099-01-01T00:00:00+00:00")
                client = TestClient(app)
                body = client.get("/v1/review-cases/1/summary").json()
                self.assertEqual(body["case_id"], 1)
                self.assertIn("notice", body)
                self.assertIn("尚未接入真实人工服务", body["notice"])
                json.dumps(body, ensure_ascii=False)  # exportable
            finally:
                app.state.worker.runner.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
