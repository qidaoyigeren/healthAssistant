"""P0 memory guarantees: policy guard, idempotency, atomicity, strict refs.

Every test runs against a temporary synthetic database; the user's
``stage0/memory.db`` is never touched.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stage0.memory import (
    EpisodicFact,
    IdempotencyKeyReused,
    MemoryStore,
    SemanticFact,
)


def make_store(directory: Path) -> MemoryStore:
    return MemoryStore(Path(directory) / "memory.db", llm_enabled=False)


class OfflineSemanticsTests(unittest.TestCase):
    """Negated / hypothetical / other-subject / uncertain mentions."""

    def test_negated_disease_is_not_patient_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            result = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="妈妈没有糖尿病", source="caregiver",
            )
            diseases = [f for f in result.semantic if f["namespace"] == "chronic_disease"]
            self.assertEqual(diseases, [])
            deferred = [e for e in result.episodic if e["event_type"] == "caregiver_message"]
            self.assertEqual(len(deferred), 1)
            self.assertEqual(deferred[0]["payload"]["mode"], "negated")
            self.assertEqual(deferred[0]["needs_verification"], 1)
            self.assertEqual(store.current_semantic(["chronic_disease"]), [])

    def test_other_subject_disease_is_excluded_from_patient_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            result = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="邻居有糖尿病", source="caregiver",
            )
            self.assertEqual([f for f in result.semantic if f["namespace"] == "chronic_disease"], [])
            deferred = [e for e in result.episodic if e["event_type"] == "caregiver_message"]
            self.assertEqual(deferred[0]["payload"]["mode"], "other_subject")
            self.assertEqual(deferred[0]["payload"]["subject_mention"], "邻居")

    def test_hypothetical_disease_is_not_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            result = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="如果妈妈有糖尿病要怎么办", source="caregiver",
            )
            self.assertEqual([f for f in result.semantic if f["namespace"] == "chronic_disease"], [])
            self.assertEqual(result.episodic[0]["payload"]["mode"], "hypothetical")

    def test_uncertain_allergy_stays_pending_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            result = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="我记得她可能对青霉素过敏", source="caregiver",
            )
            self.assertEqual([f for f in result.semantic if f["namespace"] == "allergy"], [])
            deferred = [e for e in result.episodic if e["event_type"] == "caregiver_message"]
            self.assertEqual(deferred[0]["payload"]["mode"], "uncertain")
            self.assertEqual(deferred[0]["needs_verification"], 1)
            self.assertEqual(deferred[0]["payload"]["mention"], "青霉素")

    def test_affirmed_reports_still_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            result = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="妈妈有糖尿病", source="caregiver",
            )
            diseases = [f for f in result.semantic if f["namespace"] == "chronic_disease"]
            self.assertEqual(len(diseases), 1)
            self.assertTrue(diseases[0]["value"]["present"])


class StoragePolicyGuardTests(unittest.TestCase):
    """Direct storage API calls get the same critical-fact policy."""

    def test_critical_fact_cannot_be_updated_via_policy_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            first = store.write_semantic_fact(
                SemanticFact("allergy", "磺胺", {"status": "reported"}, conflict_policy="conflict"),
                source="caregiver",
            )
            second = store.write_semantic_fact(
                SemanticFact("allergy", "磺胺", {"status": "cleared"}, conflict_policy="update"),
                source="llm_candidate",
            )
            # The relaxation is refused: this became a conflict, not an update.
            self.assertEqual(second["outcome"], "conflict")
            self.assertIsNotNone(second["conflict"])
            prior = store.connection.execute(
                "SELECT status FROM semantic_memory WHERE id=?", (first["item"]["id"],)
            ).fetchone()
            self.assertEqual(prior["status"], "disputed")
            self.assertEqual(len(store.open_conflicts()), 1)

    def test_low_risk_fact_still_allows_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            store.write_semantic_fact(
                SemanticFact("weight", "patient_weight_kg", 60.0, conflict_policy="update"),
                source="caregiver",
            )
            second = store.write_semantic_fact(
                SemanticFact("weight", "patient_weight_kg", 62.0, conflict_policy="update"),
                source="caregiver",
            )
            self.assertEqual(second["outcome"], "update")
            self.assertEqual(second["conflict"], None)


class StrictRefTests(unittest.TestCase):
    def test_ref_version_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            written = store.write_semantic_fact(
                SemanticFact("allergy", "磺胺", {"status": "reported"}, conflict_policy="conflict"),
                source="caregiver",
            )
            ref = written["item"]["ref"]
            self.assertEqual(store.audit_for(ref)["item"]["namespace"], "allergy")
            forged = ref.rsplit("@v", 1)[0] + "@v999"
            with self.assertRaisesRegex(ValueError, "version mismatch"):
                store.audit_for(forged)
            with self.assertRaisesRegex(ValueError, "unknown memory item"):
                store.audit_for("memory:semantic:424242@v1")
            with self.assertRaisesRegex(ValueError, "invalid memory ref"):
                store.audit_for("not-a-ref")

    def test_conclusion_ref_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            conclusion = store.record_conclusion(
                session_id="s", turn_id="t", kind="note", text="测试",
                memory_refs=["memory:semantic:1@v1"], source_refs=[],
            )
            resolved = store.audit_for(conclusion["ref"])
            self.assertEqual(resolved["item"]["kind"], "note")


class IdempotencyTests(unittest.TestCase):
    def test_same_event_id_replays_committed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            first = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="妈妈有高血压",
                source="caregiver", client_event_id="evt-1",
            )
            second = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="妈妈有高血压",
                source="caregiver", client_event_id="evt-1",
            )
            self.assertTrue(second.replayed)
            self.assertEqual(first.memory_refs, second.memory_refs)
            rows = store.connection.execute("SELECT COUNT(*) FROM semantic_memory").fetchone()[0]
            self.assertEqual(rows, len(first.semantic))

    def test_same_event_id_different_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="妈妈有高血压",
                source="caregiver", client_event_id="evt-1",
            )
            with self.assertRaises(IdempotencyKeyReused):
                store.consolidate_interaction(
                    session_id="s", turn_id="t1", user_text="妈妈对青霉素过敏",
                    source="caregiver", client_event_id="evt-1",
                )

    def test_turn_owned_by_other_event_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="妈妈有高血压",
                source="caregiver", client_event_id="evt-1",
            )
            with self.assertRaises(IdempotencyKeyReused):
                store.consolidate_interaction(
                    session_id="s", turn_id="t1", user_text="妈妈有高血压",
                    source="caregiver", client_event_id="evt-2",
                )


class TransactionAtomicityTests(unittest.TestCase):
    def test_failure_after_first_fact_leaves_no_partial_state(self) -> None:
        original = MemoryStore._write_semantic_fact_tx
        calls = {"n": 0}

        def flaky(self: MemoryStore, fact: SemanticFact, *, source: str) -> dict:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected failure on second fact")
            return original(self, fact, source=source)

        with tempfile.TemporaryDirectory() as directory, make_store(Path(directory)) as store:
            hints = [
                SemanticFact("preference", "diet", "低盐", 0.5, "update"),
                SemanticFact("preference", "exercise", "散步", 0.5, "update"),
            ]
            with mock.patch.object(MemoryStore, "_write_semantic_fact_tx", flaky):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    store.consolidate_interaction(
                        session_id="s", turn_id="t1", user_text="测试输入",
                        source="caregiver", semantic_hints=hints, client_event_id="evt-1",
                    )
            # T1 rolled back: neither fact, nor episodic/working side effects.
            self.assertEqual(store.current_semantic(), [])
            counts = store.connection.execute(
                "SELECT process_status, result_json FROM interactions WHERE event_key='evt-1'"
            ).fetchone()
            self.assertEqual(counts["process_status"], "failed")
            self.assertIsNone(counts["result_json"])
            # T0 preserved the raw report for retry.
            raw = store.connection.execute(
                "SELECT user_text FROM interactions WHERE event_key='evt-1'"
            ).fetchone()
            self.assertEqual(raw["user_text"], "测试输入")

            # Retry without the fault commits the whole event.
            result = store.consolidate_interaction(
                session_id="s", turn_id="t1", user_text="测试输入",
                source="caregiver", semantic_hints=hints, client_event_id="evt-1",
            )
            self.assertFalse(result.replayed)
            self.assertEqual(len(result.semantic), 2)
            status = store.connection.execute(
                "SELECT process_status FROM interactions WHERE event_key='evt-1'"
            ).fetchone()["process_status"]
            self.assertEqual(status, "committed")


class DefaultOfflineTests(unittest.TestCase):
    def test_bare_store_is_offline_by_default(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "MEMORY_ENABLE_LLM"}
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, env, clear=True):
                store = MemoryStore(Path(directory) / "memory.db")
                self.assertFalse(store.extractor.enabled)
                store.close()
            with mock.patch.dict(os.environ, {"MEMORY_ENABLE_LLM": "1"}, clear=True):
                store = MemoryStore(Path(directory) / "memory.db")
                self.assertTrue(store.extractor.enabled)
                store.close()

    def test_explicit_argument_wins_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"MEMORY_ENABLE_LLM": "1"}, clear=True):
                store = MemoryStore(Path(directory) / "memory.db", llm_enabled=False)
                self.assertFalse(store.extractor.enabled)
                store.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_p0_columns_added_without_dropping_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with make_store(Path(directory)) as store:
                store.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, conflict_policy="conflict"),
                    source="caregiver",
                )
            # Simulate reopening a database that already has rows (the v3 path
            # is exercised by _ensure_p0_columns being idempotent).
            with make_store(Path(directory)) as reopened:
                version = reopened.connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()["value"]
                # Stage 7 (production upgrade) bumped the additive migration
                # target from 4-p1 to 4-p2; data survival is the real intent.
                self.assertEqual(version, "4-p2")
                current = reopened.current_semantic(["allergy"])
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0]["verification_status"], "recorded_as_reported")


if __name__ == "__main__":
    unittest.main()
