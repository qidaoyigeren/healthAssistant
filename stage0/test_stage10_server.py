"""Stage 10 service-layer tests.

Covers the async-first /events lifecycle (202 + polling), the four idempotency
quadrants (replay / same-key-different-payload / retry-after-failure /
concurrent same key), outbox crash recovery via lease expiry, the read
endpoints, and the unified error model.  Tests drive the worker
deterministically (``worker_thread=False`` + ``drain_once()``); one smoke test
exercises the real background thread.  All runs use temporary databases.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from stage0.server import create_app


EVENT = {
    "event_type": "medication_change",
    "text": "新增克拉霉素",
    "payload": {"action": "add", "medication": "克拉霉素"},
    "session_id": "s1",
}


class _App:
    def __init__(self, directory: Path):
        import stage0.server as server
        self.app = server.create_app(
            db_path=directory / "memory.db", worker_thread=False)
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.store.close()


class EventLifecycleTests(unittest.TestCase):
    def test_async_submit_poll_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                response = api.client.post("/v1/events", json=EVENT,
                                           headers={"Idempotency-Key": "key-1"})
                self.assertEqual(response.status_code, 202)
                body = response.json()
                self.assertEqual(body["status"], "queued")
                self.assertEqual(body["event_key"], "api:key-1")
                # Pending: polling before the worker runs.
                pending = api.client.get("/v1/events/key-1")
                self.assertEqual(pending.status_code, 202)
                self.assertIn(pending.json()["status"], {"queued", "processing"})
                receipts = api.worker.drain_once()
                self.assertEqual(receipts[0]["status"], "done")
                done = api.client.get("/v1/events/key-1")
                self.assertEqual(done.status_code, 200)
                payload = done.json()
                self.assertEqual(payload["status"], "committed")
                self.assertIn("text", payload["response"])
                self.assertEqual(payload["response"]["safety_status"], "enforced")
                # The agent turn actually projected the event into memory.
                self.assertTrue(any(
                    m["display_name"] == "克拉霉素" for m in api.store.current_medications()))
            finally:
                api.close()

    def test_worker_thread_smoke(self) -> None:
        import stage0.server as server
        with tempfile.TemporaryDirectory() as directory:
            app = server.create_app(db_path=Path(directory) / "memory.db", worker_thread=True)
            store = app.state.store
            try:
                with TestClient(app) as client:
                    response = client.post("/v1/events", json=EVENT,
                                           headers={"Idempotency-Key": "key-smoke"})
                    self.assertEqual(response.status_code, 202)
                    deadline = time.time() + 15
                    final = None
                    while time.time() < deadline:
                        final = client.get("/v1/events/key-smoke")
                        if final.status_code == 200:
                            break
                        time.sleep(0.2)
                    self.assertIsNotNone(final)
                    self.assertEqual(final.status_code, 200)
                    self.assertEqual(final.json()["status"], "committed")
                self.assertTrue(any(
                    m["display_name"] == "克拉霉素" for m in store.current_medications()))
            finally:
                store.close()


class IdempotencyTests(unittest.TestCase):
    def test_replay_same_key_same_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                first = api.client.post("/v1/events", json=EVENT,
                                        headers={"Idempotency-Key": "key-r"})
                second = api.client.post("/v1/events", json=EVENT,
                                         headers={"Idempotency-Key": "key-r"})
                self.assertEqual(first.status_code, 202)
                self.assertEqual(second.status_code, 202)
                self.assertEqual(second.headers.get("Idempotent-Replay"), "true")
                self.assertEqual(first.json()["event_key"], second.json()["event_key"])
                api.worker.drain_once()
                # Only one task ever existed: one projection, one conclusion set.
                tasks = api.store.connection.execute(
                    "SELECT COUNT(*) FROM outbox_tasks").fetchone()[0]
                self.assertEqual(tasks, 1)
                replay = api.client.post("/v1/events", json=EVENT,
                                         headers={"Idempotency-Key": "key-r"})
                self.assertEqual(replay.status_code, 202)
                self.assertEqual(replay.headers.get("Idempotent-Replay"), "true")
                self.assertEqual(replay.json()["status"], "committed")
            finally:
                api.close()

    def test_same_key_different_payload_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT,
                                headers={"Idempotency-Key": "key-x"})
                other = {**EVENT, "text": "新增氨氯地平"}
                response = api.client.post("/v1/events", json=other,
                                           headers={"Idempotency-Key": "key-x"})
                self.assertEqual(response.status_code, 422)
                error = response.json()["error"]
                self.assertEqual(error["category"], "validation")
                self.assertEqual(error["code"], "idempotency_key_reused")
            finally:
                api.close()

    def test_failed_key_returns_409_until_new_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT,
                                headers={"Idempotency-Key": "key-f"})
                # Force a permanent classified failure (Reliability P0: no
                # backoff loop needed — permanent errors go straight to the
                # failure queue).
                task = api.store.claim_outbox_task(lease_ttl_seconds=60)
                api.store.fail_outbox_task(task["id"], lease_token=task["lease_token"],
                                           error="injected failure", error_class="permanent")
                retry = api.client.post("/v1/events", json=EVENT,
                                        headers={"Idempotency-Key": "key-f"})
                self.assertEqual(retry.status_code, 409)
                self.assertEqual(retry.json()["error"]["code"], "previous_attempt_failed")
                fresh = api.client.post("/v1/events", json=EVENT,
                                        headers={"Idempotency-Key": "key-f2"})
                self.assertEqual(fresh.status_code, 202)
            finally:
                api.close()

    def test_concurrent_same_key_same_acceptance(self) -> None:
        # Two submissions before the worker runs: both get the SAME 202
        # acceptance (the task is identical), so no 409 storm and no dupes.
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                first = api.client.post("/v1/events", json=EVENT,
                                        headers={"Idempotency-Key": "key-c"})
                second = api.client.post("/v1/events", json=EVENT,
                                         headers={"Idempotency-Key": "key-c"})
                self.assertEqual(first.status_code, second.status_code, 202)
                self.assertEqual(first.json()["event_key"], second.json()["event_key"])
                api.worker.drain_once()
                medications = [m["display_name"] for m in api.store.current_medications()]
                self.assertEqual(medications.count("克拉霉素"), 1)
            finally:
                api.close()


class OutboxRecoveryTests(unittest.TestCase):
    def test_expired_lease_recovers_and_executes_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT,
                                headers={"Idempotency-Key": "key-crash"})
                # Simulate a crashed worker: claim, never complete, expire.
                claimed = api.store.claim_outbox_task(lease_ttl_seconds=60)
                self.assertIsNotNone(claimed)
                past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
                api.store.connection.execute(
                    "UPDATE outbox_tasks SET lease_expires_at=? WHERE id=?",
                    (past, claimed["id"]))
                api.store.connection.commit()
                # The next claim recovers the lease and re-executes; the
                # domain-level event_key dedup keeps the projection single.
                receipts = api.worker.drain_once()
                self.assertEqual(receipts[0]["status"], "done")
                status = api.client.get("/v1/events/key-crash")
                self.assertEqual(status.status_code, 200)
                medications = [m["display_name"] for m in api.store.current_medications()]
                self.assertEqual(medications.count("克拉霉素"), 1)
                conclusions = api.store.connection.execute(
                    "SELECT COUNT(*) FROM conclusions").fetchone()[0]
                api.worker.drain_once()  # nothing left
                self.assertEqual(api.store.connection.execute(
                    "SELECT COUNT(*) FROM conclusions").fetchone()[0], conclusions)
            finally:
                api.close()


class ReadEndpointTests(unittest.TestCase):
    def test_reads_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT,
                                headers={"Idempotency-Key": "key-read"})
                api.worker.drain_once()
                state = api.client.get("/v1/memory/state")
                self.assertEqual(state.status_code, 200)
                self.assertTrue(any(m["display_name"] == "克拉霉素"
                                    for m in state.json()["medications"]))
                timeline = api.client.get("/v1/memory/timeline?limit=10")
                self.assertEqual(timeline.status_code, 200)
                self.assertTrue(timeline.json())
                conflicts = api.client.get("/v1/memory/conflicts")
                self.assertEqual(conflicts.status_code, 200)
                alerts = api.client.get("/v1/alerts")
                self.assertEqual(alerts.status_code, 200)
                health = api.client.get("/v1/health")
                self.assertEqual(health.status_code, 200)
                body = health.json()
                self.assertEqual(body["status"], "ok")
                self.assertTrue(body["schema_version"])
                self.assertEqual(body["pending_outbox_tasks"], 0)
            finally:
                api.close()

    def test_validation_error_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                response = api.client.post("/v1/events", json={"event_type": ""},
                                           headers={"Idempotency-Key": "key-bad"})
                self.assertEqual(response.status_code, 422)
                error = response.json()["error"]
                self.assertEqual(error["category"], "validation")
                self.assertEqual(error["code"], "validation_error")
                missing_key = api.client.post("/v1/events", json=EVENT)
                self.assertEqual(missing_key.status_code, 422)
                unknown = api.client.get("/v1/events/never-submitted")
                self.assertEqual(unknown.status_code, 404)
                self.assertEqual(unknown.json()["error"]["category"], "validation")
            finally:
                api.close()

    def test_invalid_idempotency_key_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                response = api.client.post(
                    "/v1/events", json=EVENT,
                    headers={"Idempotency-Key": "bad key with spaces/slash"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "invalid_idempotency_key")
            finally:
                api.close()

    def test_conflict_action_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                from stage0.memory import SemanticFact
                api.store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
                    source="caregiver")
                api.store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "conflict"),
                    source="caregiver")
                conflict = api.store.open_conflicts()[0]
                response = api.client.post(
                    f"/v1/conflicts/{conflict['id']}/actions",
                    json={"action": "resolved", "basis": "家属确认", "actor": "caregiver"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "resolved")
                self.assertEqual(api.client.get("/v1/memory/conflicts").json(), [])
                bad = api.client.post(
                    "/v1/conflicts/999/actions",
                    json={"action": "resolved", "basis": "x"})
                self.assertEqual(bad.status_code, 422)
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
