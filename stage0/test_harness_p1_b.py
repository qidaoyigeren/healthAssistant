"""Harness P1-B: evidence offload, authorised read-back and semantic context.

Acceptance coverage:

* medication lists of 13+ (and 70+) stay visible or carry explicit omission
  markers — no silent [:12] cut on critical views; omissions surface in the
  planner payload and cannot pass as completed checks;
* relevant excerpts are picked by paragraph relevance (relevant text at the
  END of a long label paragraph is selected, not the first 200 chars);
* read_evidence enforces scope, existence, content hash and pagination, and
  never serves paths/URLs; cross-scope reads are indistinguishable from
  missing ids (no existence oracle);
* raw evidence is never mutated by capture; excerpts are derived views;
* structured run summaries replace hash-only digests, old observations carry
  an explicit ``not_recorded`` evidence marker;
* retention pruning protects evidence of active runs and never breaks rechecks.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stage0.agent import (
    AgentState, CareEvent, DDITool, MedicationCoordinatorAgent, ToolAction,
)
from stage0.harness.context import bounded_patient_snapshot, omissions, view_is_complete
from stage0.harness.evidence import (
    PATIENT_SPECIFIC, EvidenceStore, select_excerpt,
)
from stage0.harness.runtime import RunContext
from stage0.harness.summary import build_run_summary, summarize_observation
from stage0.memory import MemoryStore
from stage0.test_stage8_agent import _composer_unavailable, _repeat_read_provider


def make_agent(store, **kwargs):
    return MedicationCoordinatorAgent(
        store, ddi_tool=DDITool(lambda meds: []), rag_tool=kwargs.pop("rag_tool", None),
        llm_planner_enabled=True, proposal_provider=kwargs.pop("provider", _repeat_read_provider),
        response_provider=_composer_unavailable, **kwargs)


LONG_LABEL = (
    "【合成说明书】本品用于合成适应症。" * 6 + "\n" +
    "用法用量：口服，具体遵医嘱。\n" * 3 +
    "注意事项：本品与单胺氧化酶抑制剂合用可能导致严重不良反应，"
    "肾功能不全患者慎用，用药期间应监测肾功能；老年患者（60岁以上）应从低剂量起始，"
    "出现不适立即就医并咨询医生/药师。" * 2
)


class FakeRAG:
    def __init__(self, chunks):
        self.chunks = chunks

    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "fake_hybrid",
                "results": [dict(chunk, score=1.0, rank=i + 1)
                            for i, chunk in enumerate(self.chunks)]}


class SemanticContextTests(unittest.TestCase):
    def test_medication_list_13_items_fully_visible_no_marker(self):
        meds = [{"ref": f"memory:medication:{i}", "display_name": f"合成药{i}",
                 "dose": "1片", "route": "口服", "schedule": "每日", "start_at": "2026-01-01"}
                for i in range(13)]
        view = bounded_patient_snapshot({"medications": meds, "semantic": []})
        self.assertEqual(len(view["medications"]), 13)
        self.assertTrue(view_is_complete(view))
        self.assertFalse(omissions(view))

    def test_medication_list_beyond_cap_carries_explicit_omission_marker(self):
        meds = [{"ref": f"memory:medication:{i}", "display_name": f"合成药{i}"} for i in range(70)]
        view = bounded_patient_snapshot({"medications": meds, "semantic": []})
        self.assertEqual(len(view["medications"]), 65)  # 64 + marker
        self.assertFalse(view_is_complete(view))
        (marker,) = omissions(view)
        self.assertEqual(marker["omitted_count"], 6)
        self.assertIn("未显示", marker["note"])

    def test_multiple_critical_allergy_and_conflict_items_not_silently_cut(self):
        semantic = [{"ref": f"memory:semantic:{i}", "namespace": "allergy",
                     "fact_key": f"过敏原{i}", "value": {"allergen": f"过敏原{i}"},
                     "status": "active", "valid_from": "2026-01-01"}
                    for i in range(20)]
        conflicts = [{"ref": f"memory:conflict:{i}", "subject_key": "s",
                      "description": "合成矛盾", "created_at": "2026-01-01"} for i in range(5)]
        view = bounded_patient_snapshot({"semantic": semantic, "open_conflicts": conflicts})
        allergies = [item for item in view["semantic"] if isinstance(item, dict)]
        self.assertEqual(len(allergies), 20)
        self.assertEqual(len(view["open_conflicts"]), 5)
        self.assertTrue(view_is_complete(view))

    def test_planner_payload_surfaces_omissions_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            with MemoryStore(Path(directory) / "test.db") as store:
                from stage0.memory import SemanticFact
                for i in range(70):
                    store.write_semantic_fact(SemanticFact(
                        namespace="preference", key=f"pref{i}", value=f"v{i}"), source="synthetic")
                agent = make_agent(store)
                payload = agent.planner.llm_planner.prompt_payload(AgentState(
                    session_id="s", turn_id="t", event=CareEvent("user_message", "合成消息")))
        # The non-critical section is truncated but the omission is explicit,
        # so the planner cannot mistake the view for a complete read.
        self.assertTrue(payload["context_omissions"])
        self.assertGreater(payload["context_omissions"][0]["omitted_count"], 0)

    def test_excerpt_selects_relevant_paragraph_at_the_end(self):
        excerpt = select_excerpt(LONG_LABEL, "肾功能不全 60岁 慎用")
        self.assertIn("肾功能不全", excerpt["excerpt"])
        self.assertGreater(excerpt["start"], 100)  # not the head of the text
        self.assertLess(len(excerpt["excerpt"]), len(LONG_LABEL))
        self.assertGreater(excerpt["omitted_chars"], 0)


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "test.db")
        self.evidence = EvidenceStore(self.store.connection, self.store._lock)
        self.record = self.evidence.put(
            content=LONG_LABEL, source_uri="https://example.test/label/synthetic",
            run_id="r", corpus_version="fake_hybrid",
            retrieval_params={"query": "肾功能 慎用"}, patient_revision=3)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_read_back_pagination_and_out_of_range(self):
        first = self.evidence.read(self.record.evidence_id, offset=0, limit=100)
        self.assertEqual(first["returned_chars"], 100)
        self.assertTrue(first["truncated"])
        tail = self.evidence.read(self.record.evidence_id, offset=first["total_chars"] - 10, limit=2000)
        self.assertFalse(tail["truncated"])
        beyond = self.evidence.read(self.record.evidence_id, offset=10**9, limit=10)
        self.assertEqual(beyond["content"], "")
        self.assertFalse(beyond["truncated"])

    def test_pagination_bound_enforced_as_recoverable_error(self):
        from stage0.harness.errors import ToolExecutionError
        with self.assertRaises(ToolExecutionError) as caught:
            self.evidence.read(self.record.evidence_id, offset=0, limit=10**6)
        self.assertEqual(caught.exception.kind.value, "invalid_arguments")
        self.assertTrue(caught.exception.recoverable)
        with self.assertRaises(ToolExecutionError) as caught:
            self.evidence.read(self.record.evidence_id, offset=-1, limit=10)
        self.assertEqual(caught.exception.kind.value, "invalid_arguments")

    def test_cross_scope_read_is_indistinguishable_from_missing(self):
        foreign = self.evidence.put(content="其他患者专属证据", source_uri=None,
                                    access_class=PATIENT_SPECIFIC, scope_id="other-patient")
        foreign_missing = "ev-00000000000000000000"
        try:
            self.evidence.read(foreign.evidence_id)
        except Exception as exc:
            message_foreign = str(exc)
        else:
            self.fail("cross-scope read must fail")
        try:
            self.evidence.read(foreign_missing)
        except Exception as exc:
            message_missing = str(exc)
        self.assertEqual(message_foreign, message_missing)

    def test_hash_mismatch_refuses_to_serve(self):
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE evidence_records SET content='被篡改的内容' WHERE evidence_id=?",
                (self.record.evidence_id,))
        from stage0.harness.errors import ToolExecutionError
        with self.assertRaises(ToolExecutionError) as caught:
            self.evidence.read(self.record.evidence_id)
        self.assertIn("hash", caught.exception.message)

    def test_model_supplied_path_or_url_never_reaches_storage(self):
        from stage0.harness.errors import ToolExecutionError
        for probe in ("C:\\evil\\secret.txt", "https://evil.test/exfil", "/etc/passwd"):
            with self.assertRaises(ToolExecutionError):
                self.evidence.read(probe)

    def test_capture_keeps_raw_results_untouched_and_stores_full_text(self):
        chunk = {"text": LONG_LABEL, "source_url": "https://example.test/label/synthetic",
                 "drug_name": "合成药", "section": "注意事项",
                 "corpus_version": "synthetic-corpus-v1"}
        with MemoryStore(Path(self.temp.name) / "test.db") as store:
            agent = make_agent(store, rag_tool=FakeRAG([chunk]))
            state = AgentState(session_id="s", turn_id="r",
                               event=CareEvent("medication_change", "新增合成药",
                                               {"action": "add", "medication": "合成药"}))
            state.ctx = RunContext(run_id="r", turn_id="r", session_id="s")
            result = agent.executor.execute(state.ctx, "rag_search",
                                            {"query": "肾功能 慎用 注意事项"}, state=state)
            self.assertTrue(result.ok)
            # The raw result text is untouched: downstream citation checks
            # still read the ORIGINAL evidence, not the excerpt view.
            self.assertEqual(result.value["results"][0]["text"], LONG_LABEL)
            (view,) = result.value["evidence"]
            self.assertIn("肾功能不全", view["excerpt"])
            self.assertLess(len(view["excerpt"]), len(LONG_LABEL))
            self.assertEqual(result.evidence_refs, [view["evidence_id"]])
            meta = agent.evidence_store.get_meta(view["evidence_id"])
            self.assertEqual(meta["content_chars"], len(LONG_LABEL))
            self.assertEqual(meta["corpus_version"], "synthetic-corpus-v1")

    def test_retention_prune_protects_active_runs_and_dependent_records(self):
        protected = self.evidence.put(content="活跃 run 依赖的证据", source_uri=None, run_id="active-run")
        orphan = self.evidence.put(content="孤儿证据", source_uri=None, run_id="old-run")
        with self.store.connection:
            for eid in (protected.evidence_id, orphan.evidence_id):
                self.store.connection.execute(
                    "UPDATE evidence_records SET retrieved_at='2020-01-01T00:00:00+00:00' "
                    "WHERE evidence_id=?", (eid,))
        report = self.evidence.prune(older_than_days=30, active_run_ids={"active-run"},
                                     protected_evidence_ids=set())
        self.assertEqual(report["removed"], 1)
        self.assertIsNone(self.evidence.get_meta(orphan.evidence_id))
        self.assertIsNotNone(self.evidence.get_meta(protected.evidence_id))
        # Explicitly protected ids survive even without an active run.
        report2 = self.evidence.prune(older_than_days=30, active_run_ids=set(),
                                      protected_evidence_ids={protected.evidence_id})
        self.assertEqual(report2["removed"], 0)


class RunSummaryTests(unittest.TestCase):
    def _state_with_history(self):
        state = AgentState(session_id="s", turn_id="r",
                           event=CareEvent("medication_change", "新增合成药",
                                           {"action": "add", "medication": "合成药"}))
        state.observations = [
            _obs("memory_write", {"consolidation": {}, "memory_refs": []}, ok=True),
            _obs("ddi_check", {"warnings": []}, ok=False, error_kind="internal_error"),
            _obs("rag_search", {"results": []}, ok=True, evidence_refs=["ev-" + "a" * 20]),
        ]
        state.degraded_reason = "budget_exhausted:tokens"
        return state

    def test_structured_summary_replaces_hash_only_digest(self):
        state = self._state_with_history()
        summary = build_run_summary(state, fact_revision=7)
        self.assertIn("consolidate_event", summary["completed_goals"])
        self.assertIn("internal_error", summary["failure_categories"])
        self.assertTrue(any("tool_failed:ddi_check" in item for item in summary["unresolved_issues"]))
        self.assertIn("degraded:budget_exhausted:tokens", summary["unresolved_issues"])
        self.assertEqual(summary["evidence_ids"], ["ev-" + "a" * 20])
        self.assertEqual(summary["fact_revision"], 7)
        self.assertEqual(summary["summary_kind"], "deterministic_extract")

    def test_old_observation_without_evidence_marks_not_recorded(self):
        observation = _obs("ddi_check", {"warnings": []}, ok=True)
        del observation.evidence_refs[:]  # pre-P1 observations have no refs
        summary = summarize_observation(observation)
        self.assertEqual(summary["evidence"], "not_recorded")
        self.assertNotIn("evidence_ids", summary)

    def test_failed_observation_summary_carries_error_class(self):
        observation = _obs("rag_search", {"error": "internal_error"}, ok=False,
                           error_kind="retrieval_empty", recoverable=True)
        summary = summarize_observation(observation)
        self.assertEqual(summary["error_kind"], "retrieval_empty")
        self.assertTrue(summary["recoverable"])


def _obs(tool, result, *, ok=True, error_kind=None, recoverable=None, evidence_refs=None):
    from stage0.agent import Observation
    return Observation(tool=tool, purpose="p", arguments={}, result=result, ok=ok,
                       error_kind=error_kind, recoverable=recoverable,
                       evidence_refs=list(evidence_refs or []))


class ReadEvidenceThroughExecutorTests(unittest.TestCase):
    def test_read_evidence_reachable_as_registered_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            with MemoryStore(Path(directory) / "test.db") as store:
                agent = make_agent(store)
                self.assertIn("read_evidence", agent.executor.catalog())
                record = agent.evidence_store.put(content="合成证据正文" * 100,
                                                  source_uri="https://example.test/x",
                                                  run_id="r")
                state = AgentState(session_id="s", turn_id="r",
                                   event=CareEvent("user_message", "合成"))
                state.ctx = RunContext(run_id="r", turn_id="r", session_id="s")
                result = agent.executor.execute(
                    state.ctx, "read_evidence",
                    {"evidence_id": record.evidence_id, "offset": 0, "limit": 100},
                    state=state)
                self.assertTrue(result.ok)
                self.assertEqual(result.value["returned_chars"], 100)
                self.assertTrue(result.value["truncated"])
                # scope comes from the store/executor, never from the proposal
                self.assertNotIn("scope", agent.executor.catalog()["read_evidence"]["properties"])


if __name__ == "__main__":
    unittest.main()
