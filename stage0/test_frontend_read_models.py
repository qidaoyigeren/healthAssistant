"""Frontend-round read-model endpoint tests (2026-09-06).

覆盖为照护前端新增的最小服务适配:预警读模型、分页历史、全量药物记录、
全状态冲突、memory ref 详情、事实核实/撤回、复查任务、会话与提交记录、
轮次 trace、总览聚合、operation_outcomes 结构化结果、导出/备份产物。
全部使用临时数据库;导出/备份产物目录被 patch 到临时目录,不污染仓库。
"""
from __future__ import annotations

import json
import tempfile
import unittest
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

    def commit_event(self, key: str = "key-1", event: dict | None = None) -> dict:
        response = self.client.post(
            "/v1/events", json=event or EVENT,
            headers={"Idempotency-Key": key})
        assert response.status_code == 202, response.text
        self.worker.drain_once()
        status = self.client.get(f"/v1/events/{key}")
        assert status.status_code == 200, status.text
        return status.json()


class OperationOutcomeTests(unittest.TestCase):
    def test_committed_response_carries_structured_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                body = api.commit_event("key-outcome")
                response = body["response"]
                outcomes = response["operation_outcomes"]
                medication_outcomes = [o for o in outcomes
                                       if o["kind"] == "medication_change"]
                self.assertEqual(len(medication_outcomes), 1)
                self.assertEqual(medication_outcomes[0]["outcome"], "add")
                self.assertTrue(medication_outcomes[0]["ref"].startswith("memory:medication:"))
            finally:
                api.close()

    def test_deduplicated_and_unresolved_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-a")
                replay_same = api.commit_event("key-b", event={
                    "event_type": "medication_change",
                    "text": "新增克拉霉素",
                    "payload": {"action": "add", "medication": "克拉霉素",
                                "session": "同药同量"},
                    "session_id": "s1",
                })
                outcomes = replay_same["response"]["operation_outcomes"]
                # 同名同量 → 后端真实去重结果,不是「已新增」
                self.assertTrue(any(o["outcome"] == "deduplicated"
                                    for o in outcomes if o["kind"] == "medication_change"))
                unresolved = api.commit_event("key-c", event={
                    "event_type": "medication_change",
                    "text": "停用阿司匹林",
                    "payload": {"action": "remove", "medication": "阿司匹林"},
                    "session_id": "s1",
                })
                self.assertTrue(any(o["outcome"] == "unresolved"
                                    for o in unresolved["response"]["operation_outcomes"]
                                    if o["kind"] == "medication_change"))
            finally:
                api.close()


class AlertRecordTests(unittest.TestCase):
    def _insert_conclusion(self, api: _App, *, kind: str, text: str,
                           status: str = "current", superseded_by: int | None = None,
                           predecessor_id: int | None = None,
                           created_at: str = "2026-01-01T00:00:00+00:00") -> int:
        cur = api.store.connection.execute(
            "INSERT INTO conclusions(session_id, turn_id, kind, text, memory_refs_json, "
            "source_refs_json, created_at, status, predecessor_id, superseded_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s1", "t1", kind, text, "[]", "[]", created_at, status,
             predecessor_id, superseded_by))
        api.store.connection.commit()
        return cur.lastrowid

    def test_list_detail_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                old_id = self._insert_conclusion(
                    api, kind="warning", text="旧结论",
                    status="stale", created_at="2026-01-01T00:00:00+00:00")
                new_id = self._insert_conclusion(
                    api, kind="warning", text="新结论",
                    predecessor_id=old_id, superseded_by=None,
                    created_at="2026-02-01T00:00:00+00:00")
                api.store.connection.execute(
                    "UPDATE conclusions SET superseded_by=? WHERE id=?", (new_id, old_id))
                api.store.connection.commit()

                listing = api.client.get("/v1/alert-records?status=all")
                self.assertEqual(listing.status_code, 200)
                body = listing.json()
                self.assertEqual(body["total"], 2)
                # 倒序:最新在前
                self.assertEqual([i["id"] for i in body["items"]], [new_id, old_id])
                item = body["items"][0]
                self.assertIsNone(item["severity"])  # 无结构化严重度就如实为 null
                self.assertTrue(item["ref"].startswith("memory:conclusion:"))

                stale = api.client.get("/v1/alert-records?status=stale")
                self.assertEqual([i["id"] for i in stale.json()["items"]], [old_id])

                detail = api.client.get(f"/v1/alert-records/{new_id}")
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()["chain"]["versions"][0]["id"], old_id)

                history = api.client.get(f"/v1/conclusions/{old_id}/history")
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json()["current_head"], new_id)

                missing = api.client.get("/v1/alert-records/999")
                self.assertEqual(missing.status_code, 404)
            finally:
                api.close()


class HistoryPaginationTests(unittest.TestCase):
    def _insert_episode(self, api: _App, index: int) -> None:
        api.store.connection.execute(
            "INSERT INTO episodic_memory(event_type, subject_key, payload_json, "
            "occurred_at, recorded_at, source, session_id, turn_id, salience, "
            "severity, version, fingerprint, needs_verification) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"note_{index}", None, json.dumps({"n": index}),
             f"2026-03-01T00:00:{index:02d}+00:00",
             f"2026-03-01T00:00:{index:02d}+00:00",
             "caregiver", "s1", f"t{index}", 0.5, None, 1,
             f"fp-{index}", 0))
        api.store.connection.commit()

    def test_cursor_pagination_covers_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                for i in range(5):
                    self._insert_episode(api, i)
                seen: list[int] = []
                cursor: str | None = None
                pages = 0
                while True:
                    url = "/v1/history/events?limit=2" + (f"&cursor={cursor}" if cursor else "")
                    page = api.client.get(url)
                    self.assertEqual(page.status_code, 200)
                    body = page.json()
                    seen.extend(item["id"] for item in body["items"])
                    pages += 1
                    if body["next_cursor"] is None:
                        break
                    cursor = body["next_cursor"]
                    self.assertLess(pages, 10)
                self.assertEqual(pages, 3)
                self.assertEqual(len(seen), 5)
                self.assertEqual(len(set(seen)), 5)  # 同秒记录不遗漏不重复
                bad = api.client.get("/v1/history/events?cursor=!!!not-a-cursor")
                self.assertEqual(bad.status_code, 422)
            finally:
                api.close()


class MedicationRecordTests(unittest.TestCase):
    def test_full_versions_including_dose_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-m1")
                api.commit_event("key-m2", event={
                    "event_type": "medication_change",
                    "text": "克拉霉素剂量改为一次 0.25g",
                    "payload": {"action": "dose_change", "medication": "克拉霉素",
                                "dose": "0.25g", "route": "口服", "schedule": "每日两次"},
                    "session_id": "s1",
                })
                listing = api.client.get("/v1/medication-records")
                self.assertEqual(listing.status_code, 200)
                body = listing.json()
                self.assertEqual(body["total"], 2)  # superseded 旧版 + active 新版
                statuses = {i["status"] for i in body["items"]}
                self.assertEqual(statuses, {"active", "superseded"})
                active = next(i for i in body["items"] if i["status"] == "active")
                detail = api.client.get(f"/v1/medication-records/{active['id']}")
                self.assertEqual(detail.status_code, 200)
                versions = detail.json()["versions"]
                self.assertEqual(len(versions), 2)
                self.assertEqual(versions[-1]["predecessor_id"], versions[0]["id"])
                self.assertEqual(versions[-1]["dose"], "0.25g")
                self.assertEqual(versions[0]["dose"], None)  # 未记录剂量如实为空
            finally:
                api.close()


class ConflictRecordTests(unittest.TestCase):
    def _make_conflict(self, api: _App) -> dict:
        from stage0.memory import SemanticFact
        api.store.write_semantic_fact(
            SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
            source="caregiver")
        api.store.write_semantic_fact(
            SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "conflict"),
            source="caregiver")
        return api.store.open_conflicts()[0]

    def test_all_statuses_and_action_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                conflict = self._make_conflict(api)
                open_list = api.client.get("/v1/conflict-records?status=open")
                self.assertEqual(open_list.json()["total"], 1)
                resolved = api.client.post(
                    f"/v1/conflicts/{conflict['id']}/actions",
                    json={"action": "resolved", "basis": "家属确认", "actor": "caregiver"})
                self.assertEqual(resolved.status_code, 200)
                # 全状态查询能看到已处理冲突(旧 open-only 端点看不到)
                resolved_list = api.client.get("/v1/conflict-records?status=resolved")
                self.assertEqual(resolved_list.json()["total"], 1)
                detail = api.client.get(f"/v1/conflict-records/{conflict['id']}")
                body = detail.json()
                self.assertEqual(body["status"], "resolved")
                self.assertEqual(len(body["actions"]), 1)
                self.assertEqual(body["actions"][0]["action"], "resolved")
                for side in ("left_ref", "right_ref"):
                    self.assertIn("item", body["sides"][side])
                history = api.client.get(f"/v1/conflicts/{conflict['id']}/history")
                self.assertEqual([h["action"] for h in history.json()], ["resolved"])
            finally:
                api.close()


class MemoryItemAndFactActionTests(unittest.TestCase):
    def test_memory_item_resolves_semantic_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-p", event={
                    "event_type": "register_profile",
                    "text": "患者 78 岁",
                    "payload": {"profile": {"age": 78}},
                    "session_id": "s1",
                })
                fact = api.store.current_semantic(["age"])[0]
                item = api.client.get(f"/v1/memory/item?ref={fact['ref']}")
                self.assertEqual(item.status_code, 200)
                body = item.json()
                self.assertEqual(body["layer"], "semantic")
                self.assertEqual(body["item"]["value"], 78)
                self.assertIsInstance(body["audit_log"], list)
            finally:
                api.close()

    def test_memory_item_rejects_non_memory_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                for bad_ref in ("C:/Windows/system32/config", "memory:episodic:1@v999",
                                "memory:unknown:1@v1"):
                    response = api.client.get(f"/v1/memory/item?ref={bad_ref}")
                    self.assertEqual(response.status_code, 422, bad_ref)
            finally:
                api.close()

    def test_verify_retract_and_blocked_by_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-p", event={
                    "event_type": "register_profile",
                    "text": "患者 78 岁",
                    "payload": {"profile": {"age": 78}},
                    "session_id": "s1",
                })
                fact = api.store.current_semantic(["age"])[0]
                missing_basis = api.client.post("/v1/memory/fact-actions", json={
                    "ref": fact["ref"], "action": "verify", "basis": "  "})
                self.assertEqual(missing_basis.status_code, 422)
                verified = api.client.post("/v1/memory/fact-actions", json={
                    "ref": fact["ref"], "action": "verify",
                    "basis": "对照出院小结核实", "actor": "caregiver"})
                self.assertEqual(verified.status_code, 200)
                self.assertEqual(verified.json()["outcome"], "verified")
                self.assertEqual(verified.json()["item"]["verification_status"], "verified")

                # 争议事实:核实被冲突阻塞,必须返回 blocked_by_conflict
                from stage0.memory import SemanticFact
                api.store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
                    source="caregiver")
                api.store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "conflict"),
                    source="caregiver")
                disputed = api.store.open_conflicts()[0]["left_ref"]
                blocked = api.client.post("/v1/memory/fact-actions", json={
                    "ref": disputed, "action": "verify", "basis": "核实尝试"})
                self.assertEqual(blocked.status_code, 200)
                self.assertEqual(blocked.json()["outcome"], "blocked_by_conflict")

                retracted = api.client.post("/v1/memory/fact-actions", json={
                    "ref": disputed, "action": "retract", "basis": "记错了,撤回"})
                self.assertEqual(retracted.status_code, 200)
                self.assertEqual(retracted.json()["outcome"], "retracted")
            finally:
                api.close()


class RecheckTaskTests(unittest.TestCase):
    def test_pending_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.store.connection.execute(
                    "INSERT INTO dependency_tasks(task_type, target_id, reason, status, created_at, updated_at) "
                    "VALUES('recheck_conclusion', 424242, 'medication set changed', 'open', "
                    "datetime('now'), datetime('now'))")
                api.store.connection.commit()
                body = api.client.get("/v1/recheck-tasks").json()
                self.assertEqual(body["pending_count"], 1)
                self.assertIsNone(body["pending"][0]["target_conclusion"])  # 引用不存在如实为 null
            finally:
                api.close()


class SessionRecordTests(unittest.TestCase):
    def test_sessions_and_submissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-s1")
                sessions = api.client.get("/v1/sessions").json()
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["session_id"], "s1")
                self.assertEqual(sessions[0]["event_count"], 1)

                events = api.client.get("/v1/sessions/s1/events").json()
                self.assertEqual(events["total"], 1)
                item = events["items"][0]
                self.assertEqual(item["idempotency_key"], "key-s1")
                self.assertEqual(item["process_status"], "committed")
                self.assertEqual(item["request"]["event_type"], "medication_change")
                self.assertIsNotNone(item["response"]["text"])

                trace = api.client.get(f"/v1/sessions/s1/turns/{item['turn_id']}/trace")
                self.assertEqual(trace.status_code, 200)
                self.assertIsInstance(trace.json()["traces"], list)

                empty = api.client.get("/v1/sessions/other/events")
                self.assertEqual(empty.json()["total"], 0)
            finally:
                api.close()


class OverviewTests(unittest.TestCase):
    def test_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-o")
                body = api.client.get("/v1/overview").json()
                self.assertEqual(body["counts"]["medications_active"], 1)
                self.assertEqual(body["counts"]["medications_records"], 1)
                self.assertEqual(body["counts"]["outbox_pending"], 0)
                self.assertIn("last_recorded", body)
            finally:
                api.close()


class DataArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        import stage0.read_models as read_models
        self._tmp = tempfile.TemporaryDirectory()
        self._original = (read_models.EXPORT_DIR, read_models.BACKUP_DIR)
        export_dir = Path(self._tmp.name) / "exports"
        backup_dir = Path(self._tmp.name) / "backups"
        read_models.EXPORT_DIR = export_dir
        read_models.BACKUP_DIR = backup_dir

    def tearDown(self) -> None:
        import stage0.read_models as read_models
        read_models.EXPORT_DIR, read_models.BACKUP_DIR = self._original
        self._tmp.cleanup()

    def test_export_backup_verify_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                api.commit_event("key-d")
                export = api.client.post("/v1/data/exports").json()
                self.assertIn("artifact_id", export)
                self.assertTrue(export["artifact_id"].endswith(".json.gz"))
                backup = api.client.post("/v1/data/backups").json()
                self.assertTrue(backup["artifact_id"].endswith(".db"))
                # 快照一致性:导出里 episodic 行数与库内一致
                self.assertEqual(export["row_counts"]["episodic_memory"],
                                 api.store.connection.execute(
                                     "SELECT COUNT(*) FROM episodic_memory").fetchone()[0])

                artifacts = api.client.get("/v1/data/artifacts").json()
                self.assertEqual(len(artifacts), 2)

                verify = api.client.get(f"/v1/data/artifacts/{export['artifact_id']}/verify")
                self.assertEqual(verify.status_code, 200)
                self.assertEqual(verify.json()["format"], "healthassistant-memory-export-v1")

                download = api.client.get(f"/v1/data/artifacts/{export['artifact_id']}/download")
                self.assertEqual(download.status_code, 200)
                self.assertGreater(len(download.content), 0)

                traversal = api.client.get("/v1/data/artifacts/..%2Fmemory.db/download")
                # 路径穿越被拒:或路由不匹配(404),或产物 ID 校验拒绝(422)。
                # 关键是不可能返回 200。
                self.assertIn(traversal.status_code, {404, 422})
                missing = api.client.get("/v1/data/artifacts/memory-20990101-000000.db/verify")
                self.assertEqual(missing.status_code, 404)
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
