"""Stage 7 (production upgrade) memory-layer tests.

Covers the conclusion dependency index and its migration backfill, selective
vs full medication invalidation (ablation comparison), recheck lease recovery
and batch dedup, at-least-once/effectively-once recheck semantics, AS-OF
conflict reconstruction equivalence, the promotion state machine, and
backup/export round trips.  All tests run against temporary databases; the
live ``stage0/memory.db`` is never touched.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent
from stage0.backup import backup_database, export_database, restore_database, verify_artifact
from stage0.memory import MemoryStore, SemanticFact


def make_store(directory: Path, ablations: frozenset[str] = frozenset()) -> MemoryStore:
    return MemoryStore(Path(directory) / "memory.db", llm_enabled=False, ablations=ablations)


def fake_detect(medications):
    return []


def _add_med(store: MemoryStore, name: str, turn: str) -> None:
    store.apply_medication_change(
        action="add", name=name, ingredients=[], session_id="s", turn_id=turn, source="caregiver")


def _record_warning(store: MemoryStore, text: str, refs, turn: str):
    return store.record_conclusion(
        session_id="s", turn_id=turn, kind="warning", text=text,
        memory_refs=refs, source_refs=[{"uri": "https://example.test"}])


class DependencyIndexTests(unittest.TestCase):
    def test_record_writes_dependency_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(Path(directory)) as store:
                _add_med(store, "药A", "m1")
                _add_med(store, "药B", "m2")
                fact = store.write_semantic_fact(
                    SemanticFact("renal_function", "renal_status", "中度受损", 0.9, "update"),
                    source="caregiver")
                medications = {m["display_name"]: m for m in store.current_medications()}
                conclusion = _record_warning(
                    store, "药A×药B：测试相互作用提示（moderate / medium）",
                    [medications["药A"]["ref"], medications["药B"]["ref"]], "t1")
                rows = store.connection.execute(
                    "SELECT dep_kind, dep_key, recorded_hash FROM conclusion_dependencies "
                    "WHERE conclusion_id=?", (conclusion["id"],)).fetchall()
                kinds = {row["dep_kind"] for row in rows}
                self.assertIn("medication", kinds)
                self.assertIn("medication_pair", kinds)
                self.assertIn("medication_set", kinds)
                pair = next(row for row in rows if row["dep_kind"] == "medication_pair")
                self.assertEqual(pair["dep_key"], "药a|药b")
                set_row = next(row for row in rows if row["dep_kind"] == "medication_set")
                self.assertIsNotNone(set_row["recorded_hash"])
                # 语义引用也进入依赖索引
                condition = _record_warning(
                    store, "药B×患者个体风险：肾功能提示（moderate / medium）",
                    [medications["药B"]["ref"], fact["item"]["ref"]], "t2")
                semantic = store.connection.execute(
                    "SELECT COUNT(*) FROM conclusion_dependencies "
                    "WHERE conclusion_id=? AND dep_kind='semantic_fact' AND dep_key='renal_function:renal_status'",
                    (condition["id"],)).fetchone()[0]
                self.assertEqual(semantic, 1)
                set_deps = store.connection.execute(
                    "SELECT COUNT(*) FROM conclusion_dependencies "
                    "WHERE conclusion_id=? AND dep_kind='medication_set'",
                    (condition["id"],)).fetchone()[0]
                self.assertEqual(set_deps, 0)  # 条件结论不挂集合依赖

    def test_migration_backfill_is_deterministic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with MemoryStore(path, llm_enabled=False) as store:
                # 降级到 4-p1 行为：手动删掉依赖行与回填标记，模拟旧库
                _add_med(store, "药A", "m1")
                _add_med(store, "药B", "m2")
                medications = {m["display_name"]: m for m in store.current_medications()}
                _record_warning(store, "药A×药B：测试相互作用提示（moderate / medium）",
                                [medications["药A"]["ref"], medications["药B"]["ref"]], "t1")
                store.connection.execute("DELETE FROM conclusion_dependencies")
                store.connection.execute("DELETE FROM schema_meta WHERE key='deps_backfilled'")
                store.connection.commit()
            with MemoryStore(path, llm_enabled=False) as reopened:
                count = reopened.connection.execute(
                    "SELECT COUNT(*) FROM conclusion_dependencies").fetchone()[0]
                self.assertGreater(count, 0)
                # 回填的集合依赖是 NULL 哈希（保守：下一次药单变更必失效）
                null_hash = reopened.connection.execute(
                    "SELECT COUNT(*) FROM conclusion_dependencies "
                    "WHERE dep_kind='medication_set' AND recorded_hash IS NULL").fetchone()[0]
                self.assertGreater(null_hash, 0)
                again = reopened.connection.execute(
                    "SELECT COUNT(*) FROM conclusion_dependencies").fetchone()[0]
                audit = reopened.connection.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action='dependency_backfill'").fetchone()[0]
                # 首次开库（空回填）+ 标记删除后的重开（真回填）各审计一次
                self.assertGreaterEqual(audit, 1)
            with MemoryStore(path, llm_enabled=False) as third:
                # 重开不再回填：行数与审计数都不重复增长
                rows = third.connection.execute(
                    "SELECT COUNT(*) FROM conclusion_dependencies").fetchone()[0]
                self.assertEqual(rows, again)
                audit_stable = third.connection.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action='dependency_backfill'").fetchone()[0]
                self.assertEqual(audit_stable, audit)

    def test_selective_vs_full_invalidation_ablation(self) -> None:
        def run(ablations: frozenset[str]) -> dict[str, bool]:
            with tempfile.TemporaryDirectory() as directory:
                with make_store(Path(directory), ablations) as store:
                    _add_med(store, "药A", "m1")
                    _add_med(store, "药B", "m2")
                    medications = {m["display_name"]: m for m in store.current_medications()}
                    fact = store.write_semantic_fact(
                        SemanticFact("renal_function", "renal_status", "中度受损", 0.9, "update"),
                        source="caregiver")
                    pair = _record_warning(
                        store, "药A×药B：测试相互作用提示（moderate / medium）",
                        [medications["药A"]["ref"], medications["药B"]["ref"]], "t1")
                    condition = _record_warning(
                        store, "药B×患者个体风险：肾功能提示（moderate / medium）",
                        [medications["药B"]["ref"], fact["item"]["ref"]], "t2")
                    _add_med(store, "药E", "m3")
                    stale = {c["id"] for c in store.stale_conclusions()}
                    return {"pair": pair["id"] in stale, "condition": condition["id"] in stale}

        selective = run(frozenset())
        full = run(frozenset({"selective_invalidation"}))
        self.assertTrue(selective["pair"])       # 集合级语义保留（S14）
        self.assertFalse(selective["condition"])  # 选择性收益
        self.assertTrue(full["pair"])
        self.assertTrue(full["condition"])        # 全量失效误伤

    def test_dependency_index_equivalence(self) -> None:
        def run(ablations: frozenset[str]) -> set[int]:
            with tempfile.TemporaryDirectory() as directory:
                with make_store(Path(directory), ablations) as store:
                    fact = store.write_semantic_fact(
                        SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
                        source="caregiver")
                    _record_warning(store, "磺胺过敏提示", [fact["item"]["ref"]], "t1")
                    store.write_semantic_fact(
                        SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "update"),
                        source="caregiver")
                    return {c["id"] for c in store.stale_conclusions()}

        self.assertEqual(run(frozenset()), run(frozenset({"dependency_index"})))

    def test_transitive_invalidation_conclusion_to_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(Path(directory)) as store:
                fact = store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
                    source="caregiver")
                first = _record_warning(store, "磺胺过敏提示", [fact["item"]["ref"]], "t1")
                # C2 引用 C1：事实变更应级联失效两者
                second = store.record_conclusion(
                    session_id="s", turn_id="t2", kind="note", text="引用上条警告的备注",
                    memory_refs=[first["ref"], fact["item"]["ref"]], source_refs=[])
                store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "update"),
                    source="caregiver")
                stale = {c["id"] for c in store.stale_conclusions()}
                self.assertIn(first["id"], stale)
                self.assertIn(second["id"], stale)


class RecheckRobustnessTests(unittest.TestCase):
    def _store_with_three_stale(self, directory: Path, detector) -> tuple[MemoryStore, MedicationCoordinatorAgent]:
        store = make_store(Path(directory))
        _add_med(store, "药A", "m1")
        _add_med(store, "药B", "m2")
        medications = {m["display_name"]: m for m in store.current_medications()}
        for index, pair_text in enumerate((
            "药A×药B：提示一（moderate / medium）",
            "药A×药B：提示二（minor / medium）",
            "药A×药B：提示三（major / medium）",
        )):
            _record_warning(store, pair_text, [medications["药A"]["ref"], medications["药B"]["ref"]],
                            f"t{index}")
        agent = MedicationCoordinatorAgent(store, ddi_tool=DDITool(detector), rag_tool=None)
        _add_med(store, "药E", "m3")  # 集合哈希变化 → 3 条 stale + 3 个任务
        return store, agent

    def test_batch_dedup_runs_detector_once(self) -> None:
        calls = {"n": 0}

        def counting_detect(medications):
            calls["n"] += 1
            return []

        with tempfile.TemporaryDirectory() as directory:
            store, agent = self._store_with_three_stale(Path(directory), counting_detect)
            try:
                self.assertEqual(len(store.pending_rechecks()), 3)
                receipt = agent.run_pending_rechecks(max_jobs=5)
                self.assertEqual(receipt["status"], "ok")
                self.assertEqual(len(receipt["completed"]), 3)
                self.assertEqual(calls["n"], 1)  # 同一药单只检测一次
            finally:
                store.close()

    def test_effectively_once_dedup_after_manual_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent = self._store_with_three_stale(Path(directory), fake_detect)
            try:
                agent.run_pending_rechecks(max_jobs=5)
                successors = store.connection.execute(
                    "SELECT COUNT(*) FROM conclusions WHERE session_id='recheck'").fetchone()[0]
                self.assertEqual(successors, 3)
                # 模拟崩溃后重放：手工重开一个已完成任务
                task_id = store.connection.execute(
                    "SELECT id FROM dependency_tasks ORDER BY id LIMIT 1").fetchone()[0]
                store.connection.execute(
                    "UPDATE dependency_tasks SET status='open' WHERE id=?", (task_id,))
                store.connection.commit()
                receipt = agent.run_pending_rechecks(max_jobs=5)
                self.assertEqual(receipt["completed"][0]["status"], "deduplicated")
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM conclusions WHERE session_id='recheck'").fetchone()[0],
                    successors)  # 没有产生重复的新结论版本
            finally:
                store.close()

    def test_lease_expiry_recovery_and_failure_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(Path(directory)) as store:
                fact = store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
                    source="caregiver")
                _record_warning(store, "磺胺过敏提示", [fact["item"]["ref"]], "t1")
                store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "update"),
                    source="caregiver")
                task_id = store.pending_rechecks()[0]["id"]
                past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
                # 三次崩溃式租约过期 → attempts 递增 → failed
                for expected_attempts, expected_status in ((1, "open"), (2, "open"), (3, "failed")):
                    store.connection.execute(
                        "UPDATE dependency_tasks SET status='running', lease_expires_at=?, lease_token='x' WHERE id=?",
                        (past, task_id))
                    store.connection.commit()
                    recovered = store.recover_expired_rechecks()
                    self.assertEqual(recovered, 1)
                    row = store.connection.execute(
                        "SELECT status, attempts FROM dependency_tasks WHERE id=?", (task_id,)).fetchone()
                    self.assertEqual(row["status"], expected_status)
                    self.assertEqual(row["attempts"], expected_attempts)
                # 无租约的遗留 running 行（迁移前数据）不被误回收
                store.connection.execute(
                    "UPDATE dependency_tasks SET status='running', lease_expires_at=NULL, lease_token=NULL WHERE id=?",
                    (task_id,))
                store.connection.commit()
                self.assertEqual(store.recover_expired_rechecks(), 0)

    def test_recheck_pending_recovers_expired_lease_in_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, agent = self._store_with_three_stale(Path(directory), fake_detect)
            try:
                task_id = store.pending_rechecks()[0]["id"]
                past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
                store.connection.execute(
                    "UPDATE dependency_tasks SET status='running', lease_expires_at=?, lease_token='crashed' WHERE id=?",
                    (past, task_id))
                store.connection.commit()
                receipt = agent.run_pending_rechecks(max_jobs=5)
                self.assertEqual(receipt["status"], "ok")
                statuses = {row["status"] for row in store.connection.execute(
                    "SELECT status FROM dependency_tasks").fetchall()}
                self.assertEqual(statuses, {"done"})
            finally:
                store.close()


class AsOfConflictTests(unittest.TestCase):
    def test_fold_matches_current_open_filter(self) -> None:
        """AS-OF 折叠在 known_at=now 时必须与 status='open' 过滤等价。"""
        with tempfile.TemporaryDirectory() as directory:
            with make_store(Path(directory)) as store:
                store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"),
                    source="caregiver")
                store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "cleared"}, 1.0, "conflict"),
                    source="caregiver")
                store.write_semantic_fact(
                    SemanticFact("allergy", "青霉素", {"status": "reported"}, 1.0, "conflict"),
                    source="caregiver")
                first = store.open_conflicts()[0]
                store.resolve_conflict(first["ref"], action="resolved", basis="确认", actor="caregiver")
                store.resolve_conflict(first["ref"], action="reopened", basis="复核")
                store.resolve_conflict(first["ref"], action="undo", basis="撤销重开")
                current = store.query_state()
                folded = {c["id"] for c in current["open_conflicts"]}
                filtered = {c["id"] for c in store.open_conflicts()}
                self.assertEqual(folded, filtered)


class PromotionTests(unittest.TestCase):
    def test_replay_never_counts_toward_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(Path(directory)) as store:
                store.consolidate_interaction(session_id="s", turn_id="t1", user_text="妈妈有高血压",
                                              client_event_id="e1")
                # 同一事件键重放：不增加报告数，不触发晋升
                store.consolidate_interaction(session_id="s", turn_id="t1", user_text="妈妈有高血压",
                                              client_event_id="e1")
                current = store.current_semantic(["chronic_disease"])
                self.assertEqual(current[0]["verification_status"], "recorded_as_reported")

    def test_open_conflict_blocks_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with make_store(Path(directory)) as store:
                old_threshold = os.environ.get("MEMORY_PROMOTION_REPORTS")
                os.environ["MEMORY_PROMOTION_REPORTS"] = "3"
                try:
                    for turn in ("t1", "t2"):
                        store.consolidate_interaction(session_id="s", turn_id=turn, user_text="妈妈有高血压",
                                                      client_event_id=f"e{turn}")
                    current = store.current_semantic(["chronic_disease"])[0]
                    # 在该键上打开一个未决冲突（非 semantic_fact_conflict 类型，不触发 disputed 标记）
                    store.create_conflict(
                        conflict_type="caregiver_report_dispute",
                        subject_key="chronic_disease:高血压",
                        left_ref=current["ref"], right_ref=current["ref"],
                        description="家属对报告来源存疑，待复核", source="caregiver")
                    store.consolidate_interaction(session_id="s", turn_id="t3", user_text="妈妈有高血压",
                                                  client_event_id="e3")
                    after = store.current_semantic(["chronic_disease"])[0]
                    self.assertNotEqual(after["verification_status"], "verified")
                    blocked = store.connection.execute(
                        "SELECT COUNT(*) FROM audit_log WHERE action='promotion_blocked_by_conflict'"
                    ).fetchone()[0]
                    self.assertEqual(blocked, 1)
                finally:
                    if old_threshold is None:
                        os.environ.pop("MEMORY_PROMOTION_REPORTS", None)
                    else:
                        os.environ["MEMORY_PROMOTION_REPORTS"] = old_threshold


class BackupRoundTripTests(unittest.TestCase):
    def _populate(self, store: MemoryStore) -> None:
        store.consolidate_interaction(session_id="s", turn_id="t1", user_text="妈妈有高血压",
                                      client_event_id="e1")
        _add_med(store, "药A", "m1")
        fact = store.write_semantic_fact(
            SemanticFact("allergy", "磺胺", {"status": "reported"}, 1.0, "conflict"), source="caregiver")
        _record_warning(store, "磺胺过敏提示", [fact["item"]["ref"]], "t2")

    def test_backup_and_restore_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with make_store(root) as store:
                self._populate(store)
            report = backup_database(root / "memory.db", root / "backups")
            restored = root / "restored.db"
            restore_database(Path(report["backup"]), restored)
            with MemoryStore(restored, llm_enabled=False) as reopened:
                self.assertEqual(len(reopened.current_semantic(["chronic_disease"])), 1)
                self.assertEqual(len(reopened.current_medications()), 1)
                self.assertEqual(len(reopened.stale_conclusions()), 0)
                audit = reopened.connection.execute(
                    "SELECT COUNT(*) FROM audit_log").fetchone()[0]
                self.assertGreater(audit, 0)
            verification = verify_artifact(Path(report["backup"]))
            self.assertEqual(verification["integrity_check"], "ok")
            self.assertTrue(verification["sha256_matches"])
            self.assertTrue(verification["counts_match_sidecar"])

    def test_export_and_import_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with make_store(root) as store:
                self._populate(store)
                original_facts = len(store.current_semantic())
            report = export_database(root / "memory.db", root / "exports")
            restored = root / "imported.db"
            restore_database(Path(report["export"]), restored)
            with MemoryStore(restored, llm_enabled=False) as reopened:
                self.assertEqual(len(reopened.current_semantic()), original_facts)
                self.assertEqual(len(reopened.current_medications()), 1)
                self.assertEqual(
                    reopened.connection.execute(
                        "SELECT COUNT(*) FROM conclusions WHERE kind='warning'").fetchone()[0], 1)
            verification = verify_artifact(Path(report["export"]))
            self.assertTrue(verification["counts_match_sidecar"])

    def test_restore_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with make_store(root) as store:
                self._populate(store)
            report = backup_database(root / "memory.db", root / "backups")
            with self.assertRaises(FileExistsError):
                restore_database(Path(report["backup"]), root / "memory.db")


if __name__ == "__main__":
    unittest.main()
