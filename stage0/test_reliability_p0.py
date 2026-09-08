"""Reliability P0 regression tests (framework-integration upgrade round).

Covers the failure scenarios called out in docs/reliability-design/08-test-matrix.md
for the P0 stage: key-prefix collision (T1), committed-replay contract (T4),
crash after domain write (T5), stale-lease fencing (T6), classified retry
backoff and the failure queue (T7/T8), UI timeout retry key reuse (T3),
object authorization / deployment auth (T12), and log sanitization (T18).

All tests run against temporary databases with the deterministic offline
agent; no live provider, no real memory.db.
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from stage0.agent import CareEvent, MedicationCoordinatorAgent
from stage0.api_client import ApiClientError, Stage0ApiClient
from stage0.memory import LeaseRejected, MemoryStore
from stage0.server import create_app


EVENT = {
    "event_type": "medication_change",
    "text": "新增克拉霉素",
    "payload": {"action": "add", "medication": "克拉霉素"},
    "session_id": "s1",
}


class _App:
    def __init__(self, directory: Path, **kwargs):
        import stage0.server as server
        self.app = server.create_app(db_path=directory / "memory.db",
                                     worker_thread=False, **kwargs)
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.store.close()


def _expire_lease(store: MemoryStore, task_id: int) -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    store.connection.execute(
        "UPDATE outbox_tasks SET lease_expires_at=? WHERE id=?", (past, task_id))
    store.connection.commit()


class KeyPrefixCollisionTests(unittest.TestCase):
    def test_long_prefix_collision_yields_two_events(self) -> None:
        # T1: two legal keys whose first 32 chars are identical must remain
        # two independent events end to end.
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                key1, key2 = "a" * 40 + "1", "a" * 40 + "2"
                ev2 = {**EVENT, "text": "新增氨氯地平",
                       "payload": {"action": "add", "medication": "氨氯地平"}}
                r1 = api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": key1})
                r2 = api.client.post("/v1/events", json=ev2, headers={"Idempotency-Key": key2})
                self.assertEqual([r1.status_code, r2.status_code], [202, 202])
                self.assertNotEqual(r1.json()["run_id"], r2.json()["run_id"])
                self.assertNotEqual(r1.json()["event_id"], r2.json()["event_id"])
                api.worker.drain_once()
                interactions = api.store.connection.execute(
                    "SELECT event_key FROM interactions ORDER BY id").fetchall()
                event_keys = {row["event_key"] for row in interactions}
                self.assertIn(f"api:{key1}", event_keys)
                self.assertIn(f"api:{key2}", event_keys)
                self.assertEqual(len(event_keys), 2)
                names = [m["display_name"] for m in api.store.current_medications()]
                self.assertEqual(names.count("克拉霉素"), 1)
                self.assertEqual(names.count("氨氯地平"), 1)
            finally:
                api.close()

    def test_domain_event_key_is_the_api_key(self) -> None:
        # T1b: the domain projection dedups on the accepted request identity,
        # not on a turn_id derivative.
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            agent = MedicationCoordinatorAgent(store)
            try:
                agent.handle(CareEvent(**{"event_type": "medication_change",
                                          "text": "新增克拉霉素",
                                          "payload": {"action": "add", "medication": "克拉霉素"},
                                          "source": "caregiver"}),
                             session_id="s1", client_event_id="api:key-dom")
                row = store.connection.execute(
                    "SELECT event_key FROM interactions").fetchone()
                self.assertEqual(row["event_key"], "api:key-dom")
            finally:
                store.close()


class CommittedReplayContractTests(unittest.TestCase):
    def test_committed_replay_post_matches_get_shape(self) -> None:
        # T4: a successful replayed POST returns the SAME full result as the
        # status endpoint — no more response-less acceptance.
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-rp"})
                api.worker.drain_once()
                post = api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-rp"})
                get = api.client.get("/v1/events/key-rp")
                self.assertEqual(post.status_code, 202)
                self.assertEqual(post.headers.get("Idempotent-Replay"), "true")
                post_body, get_body = post.json(), get.json()
                self.assertEqual(post_body["status"], "committed")
                self.assertIn("response", post_body)
                self.assertEqual(post_body["response"], get_body["response"])
                self.assertIn("text", post_body["response"])
            finally:
                api.close()


class StaleLeaseFencingTests(unittest.TestCase):
    def test_stale_worker_writes_rejected(self) -> None:
        # T6: after lease expiry and re-claim, the OLD worker's complete,
        # fail and heartbeat are all rejected.
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-lease"})
                old = api.store.claim_outbox_task(lease_ttl_seconds=60)
                _expire_lease(api.store, old["id"])
                new = api.store.claim_outbox_task(lease_ttl_seconds=60)
                self.assertIsNotNone(new)
                self.assertEqual(new["id"], old["id"])
                self.assertNotEqual(new["lease_token"], old["lease_token"])
                with self.assertRaises(LeaseRejected):
                    api.store.complete_outbox_task(old["id"], lease_token=old["lease_token"],
                                                   result={"writer": "expired_worker"})
                with self.assertRaises(LeaseRejected):
                    api.store.fail_outbox_task(old["id"], lease_token=old["lease_token"],
                                               error="late failure")
                self.assertFalse(api.store.heartbeat_outbox_task(
                    old["id"], lease_token=old["lease_token"], lease_ttl_seconds=60))
                # The new owner is unaffected and can complete.
                api.store.complete_outbox_task(new["id"], lease_token=new["lease_token"],
                                               result={"writer": "current_worker"})
                row = api.store.outbox_task_for("api-event:key-lease")
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["result"], {"writer": "current_worker"})
            finally:
                api.close()


class ClassifiedRetryTests(unittest.TestCase):
    def test_retryable_failure_backs_off_and_caps(self) -> None:
        # T7: retryable failures wait for next_attempt_at; the attempt cap
        # (max_attempts, including the first try) terminates the loop.
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-rt"})
                task = api.store.claim_outbox_task(lease_ttl_seconds=60)
                status = api.store.fail_outbox_task(
                    task["id"], lease_token=task["lease_token"],
                    error="provider timeout", error_class="retryable")
                self.assertEqual(status, "open")
                row = api.store.outbox_task_for("api-event:key-rt")
                self.assertIsNotNone(row["next_attempt_at"])
                self.assertGreater(row["next_attempt_at"], row["updated_at"][:19])
                # Backoff gates reclaiming.
                self.assertIsNone(api.store.claim_outbox_task(lease_ttl_seconds=60))
                # Third attempt reaches the cap (attempts include the first).
                for expected_attempts in (2, 3):
                    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
                    api.store.connection.execute(
                        "UPDATE outbox_tasks SET next_attempt_at=? WHERE id=?",
                        (past, task["id"]))
                    api.store.connection.commit()
                    task = api.store.claim_outbox_task(lease_ttl_seconds=60)
                    self.assertEqual(task["attempts"], expected_attempts)
                    status = api.store.fail_outbox_task(
                        task["id"], lease_token=task["lease_token"],
                        error="provider timeout again", error_class="retryable")
                self.assertEqual(status, "failed")
                row = api.store.outbox_task_for("api-event:key-rt")
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["last_error_class"], "retryable")
            finally:
                api.close()

    def test_permanent_failure_reopened_via_explicit_retry(self) -> None:
        # T7b/T8: permanent failures skip backoff into the failure queue and
        # are recovered by the explicit retry operation — same event identity.
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-pm"})
                task = api.store.claim_outbox_task(lease_ttl_seconds=60)
                status = api.store.fail_outbox_task(
                    task["id"], lease_token=task["lease_token"],
                    error="policy violation", error_class="permanent")
                self.assertEqual(status, "failed")
                row = api.store.outbox_task_for("api-event:key-pm")
                self.assertEqual(row["last_error_class"], "permanent")
                # The API retry endpoint re-opens the SAME task.
                reopened = api.client.post("/v1/events/key-pm/retry")
                self.assertEqual(reopened.status_code, 202)
                row = api.store.outbox_task_for("api-event:key-pm")
                self.assertEqual(row["status"], "open")
                self.assertEqual(row["attempts"], 0)
                # effect_unknown must NOT be blindly retried.
                api.store.connection.execute(
                    "UPDATE outbox_tasks SET status='failed', last_error_class='effect_unknown' WHERE id=?",
                    (task["id"],))
                api.store.connection.commit()
                self.assertIsNone(api.store.reopen_failed_outbox_task("api-event:key-pm"))
                refused = api.client.post("/v1/events/key-pm/retry")
                self.assertEqual(refused.status_code, 409)
            finally:
                api.close()

    def test_worker_classifies_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-cls"})
                original = api.worker.agent.handle

                def boom(event, **kwargs):
                    raise RuntimeError("safety boundary rejected an uncited warning")

                api.worker.agent.handle = boom
                receipts = api.worker.drain_once()
                self.assertEqual(receipts[0]["error_class"], "safety")
                row = api.store.outbox_task_for("api-event:key-cls")
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["last_error_class"], "safety")
                self.assertIsNotNone(original)
            finally:
                api.close()


class CrashAfterDomainWriteTests(unittest.TestCase):
    def test_recovery_after_publish_crash_has_no_duplicate_effects(self) -> None:
        # T5: domain writes committed, then a crash before result publication.
        # Recovery replays the turn; operation receipts keep the projection
        # single (one medication version per accepted add, one receipt each).
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-c1"})
                api.worker.drain_once()
                second = {**EVENT, "text": "新增辛伐他汀",
                          "payload": {"action": "add", "medication": "辛伐他汀"}}
                api.client.post("/v1/events", json=second, headers={"Idempotency-Key": "key-c2"})
                api.worker.drain_once()  # records the contraindicated pair
                names = [m["display_name"] for m in api.store.current_medications()]
                self.assertEqual(names.count("辛伐他汀"), 1)

                # The crash candidate: accepted, then the worker turn runs and
                # dies exactly at publish_event_result (after domain writes).
                third = {**EVENT, "text": "新增克拉霉素缓释片",
                         "payload": {"action": "add", "medication": "克拉霉素缓释片"}}
                api.client.post("/v1/events", json=third, headers={"Idempotency-Key": "key-c3"})
                _expire_lease(api.store, api.store.claim_outbox_task(lease_ttl_seconds=60)["id"])
                original_publish = api.store.publish_event_result
                calls = {"n": 0}

                def crashing_publish(*args, **kwargs):
                    calls["n"] += 1
                    raise RuntimeError("injected crash before publication")

                api.worker.store.publish_event_result = crashing_publish  # type: ignore[method-assign]
                with self.assertRaises(RuntimeError):
                    api.worker.drain_once()
                self.assertGreaterEqual(calls["n"], 1)

                # Recover: patch removed, lease expired → replay.
                api.worker.store.publish_event_result = original_publish  # type: ignore[method-assign]
                crashed_task_id = api.store.outbox_task_for("api-event:key-c3")["id"]
                _expire_lease(api.store, crashed_task_id)
                api.worker.drain_once()
                names = [m["display_name"] for m in api.store.current_medications()]
                self.assertEqual(names.count("克拉霉素缓释片"), 1)
                receipts = api.store.connection.execute(
                    "SELECT operation_id FROM operation_receipts "
                    "WHERE operation_type='consolidate_event'").fetchall()
                self.assertEqual(len(receipts), 3)  # one per accepted event
                self.assertEqual(len({r["operation_id"] for r in receipts}), 3)
                status = api.client.get("/v1/events/key-c3")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["status"], "committed")
            finally:
                api.close()

    def test_warning_batch_replay_is_atomic(self) -> None:
        # T5b: record_warnings_batch replays from its receipt without
        # duplicating episodes or conclusions.
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                warning = {"drug_a": "克拉霉素", "drug_b": "辛伐他汀",
                           "severity": "contraindicated", "effect": "横纹肌溶解风险增加",
                           "confidence": "high", "source_url": "https://example.local/label"}
                items = [{
                    "warning": warning, "salience": 1.0,
                    "text": "克拉霉素×辛伐他汀：横纹肌溶解风险增加",
                    "source_refs": [{"source_type": "drug_label_or_kegg",
                                     "uri": "https://example.local/label",
                                     "quote": "禁止联用", "retrieval": "rag"}],
                    "occurred_at": None,
                }]
                kwargs = dict(
                    session_id="s1", turn_id="t1", source="agent:ddi_check",
                    items=items, context_refs=["memory:semantic:1@v1"],
                    operation_id="warnings:api:k1:abc", input_hash="hash-1")
                first = store.record_warnings_batch(**kwargs)
                self.assertFalse(first["replayed"])
                conclusions = store.connection.execute(
                    "SELECT COUNT(*) FROM conclusions WHERE kind='warning'").fetchone()[0]
                # Same id, different input → conflict, not a silent second fact.
                from stage0.memory import OperationConflict
                with self.assertRaises(OperationConflict):
                    store.record_warnings_batch(**{**kwargs, "input_hash": "hash-2"})
                replay = store.record_warnings_batch(**kwargs)
                self.assertTrue(replay["replayed"])
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM conclusions WHERE kind='warning'").fetchone()[0],
                    conclusions)
            finally:
                store.close()


class UiRetryKeyTests(unittest.TestCase):
    def test_timeout_retry_reuses_key_and_resolves(self) -> None:
        # T3: UI timeout → retry with the same fingerprint → same key, same
        # event; after commit a deliberate repeat is a NEW event.
        from stage0.api_client import SubmitKeyStore
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(db_path=Path(directory) / "memory.db", worker_thread=False)
            store = app.state.store
            worker = app.state.worker
            try:
                client = TestClient(app)
                keys = SubmitKeyStore()
                body = {"event_type": "medication_change", "text": "新增克拉霉素",
                        "payload": {"action": "add", "medication": "克拉霉素"},
                        "source": "caregiver"}
                fingerprint = Stage0ApiClient.fingerprint_event(body, session_id="ui-1")
                key1 = keys.key_for(fingerprint)
                first = client.post("/v1/events", json={**body, "session_id": "ui-1"},
                                    headers={"Idempotency-Key": key1})
                self.assertEqual(first.status_code, 202)
                # (Polling would time out here — nothing was drained yet.)
                # Retry with the same content: same key, same single event.
                key2 = keys.key_for(fingerprint)
                self.assertEqual(key1, key2)
                worker.drain_once()
                replay = client.post("/v1/events", json={**body, "session_id": "ui-1"},
                                     headers={"Idempotency-Key": key2})
                self.assertEqual(replay.status_code, 202)
                self.assertEqual(replay.json()["status"], "committed")
                self.assertIn("text", replay.json()["response"])
                keys.mark_resolved(fingerprint, event_key=replay.json()["event_key"])
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM outbox_tasks").fetchone()[0], 1)
                names = [m["display_name"] for m in store.current_medications()]
                self.assertEqual(names.count("克拉霉素"), 1)
                # Deliberate new report after success → new key, new event.
                key3 = keys.key_for(fingerprint)
                self.assertNotEqual(key3, key1)
                client.post("/v1/events", json={**body, "session_id": "ui-1"},
                            headers={"Idempotency-Key": key3})
                worker.drain_once()
                self.assertEqual(store.connection.execute(
                    "SELECT COUNT(*) FROM outbox_tasks").fetchone()[0], 2)
            finally:
                store.close()


class AuthorizationTests(unittest.TestCase):
    def test_deployment_mode_refuses_anonymous_start(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"STAGE0_AUTH_TOKEN": ""}, clear=False):
                with self.assertRaises(RuntimeError):
                    create_app(db_path=Path(directory) / "memory.db",
                               worker_thread=False, auth_mode="deployment")

    def test_deployment_mode_enforces_token_and_roles(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            env = {"STAGE0_AUTH_TOKEN": "tok-123", "STAGE0_AUTH_ROLES": "caregiver"}
            with mock.patch.dict(os.environ, env, clear=False):
                api = _App(Path(directory), auth_mode="deployment")
                try:
                    denied = api.client.get("/v1/memory/state")
                    self.assertEqual(denied.status_code, 401)
                    ok = api.client.get("/v1/memory/state", headers={"X-Stage0-Token": "tok-123"})
                    self.assertEqual(ok.status_code, 200)
                    health = api.client.get("/v1/health", headers={"X-Stage0-Token": "tok-123"})
                    self.assertEqual(health.json()["auth_mode"], "deployment")
                    # caregiver-only principal cannot run ops recovery.
                    forbidden = api.client.post("/v1/events/whatever/retry",
                                                headers={"X-Stage0-Token": "tok-123"})
                    self.assertEqual(forbidden.status_code, 403)
                finally:
                    api.close()

    def test_rate_limit_returns_429(self) -> None:
        import stage0.server as server
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                with mock.patch.object(server, "EVENT_RATE_LIMIT_PER_MINUTE", 2):
                    for _ in range(2):
                        response = api.client.post(
                            "/v1/events", json=EVENT,
                            headers={"Idempotency-Key": f"rate-{uuid.uuid4().hex[:8]}"})
                        self.assertEqual(response.status_code, 202)
                    limited = api.client.post(
                        "/v1/events", json=EVENT,
                        headers={"Idempotency-Key": f"rate-{uuid.uuid4().hex[:8]}"})
                    self.assertEqual(limited.status_code, 429)
                    self.assertEqual(limited.json()["error"]["code"], "rate_limited")
            finally:
                api.close()


class LogSanitizationTests(unittest.TestCase):
    def test_worker_failure_log_carries_no_patient_text(self) -> None:
        # T18: the failure log records exception type + ids, never the
        # exception message (which may embed patient text).
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.client.post("/v1/events", json=EVENT, headers={"Idempotency-Key": "key-log"})
                task = api.store.claim_outbox_task(lease_ttl_seconds=60)
                leak = "患者张三的私密正文泄露文本"
                with self.assertLogs("stage0.server", level="ERROR") as captured:
                    api.worker._fail_claimed(
                        task, RuntimeError(f"{leak} -> boom"), "trace-xyz")
                joined = "\n".join(captured.output)
                self.assertNotIn(leak, joined)
                self.assertIn("RuntimeError", joined)
                self.assertIn("trace-xyz", joined)  # id correlation, not content
                self.assertIn("error_class=permanent", joined)
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
