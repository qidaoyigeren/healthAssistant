"""Product upgrade P1: controlled evidence read-back API, conclusion-evidence
linkage, explanation/change-impact read models and the minimal AnswerBundle.

Contracts: docs/product-upgrade/P0/contracts.md.  All tests run on temporary
synthetic databases; the agent runs with a scripted DDI detector (no real
model calls).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.harness.evidence import EvidenceStore
from stage0.memory import MemoryStore


def fake_detect(medications):
    warnings = []
    if {"氨氯地平", "克拉霉素"}.issubset(medications):
        warnings.append({
            "drug_a": "氨氯地平", "drug_b": "克拉霉素", "severity": "moderate",
            "mechanism": "CYP3A4", "effect": "降压作用增强", "management": None,
            "source_text": "与CYP3A4抑制剂克拉霉素合用时，氨氯地平暴露量增加，需监测血压（合成评测文本）",
            "source_url": "https://synthetic.example/label/amlodipine",
            "confidence": "medium", "detection_path": "test_detector",
        })
    if {"硝苯地平", "克拉霉素"}.issubset(medications):
        warnings.append({
            "drug_a": "硝苯地平", "drug_b": "克拉霉素", "severity": "minor",
            "mechanism": "CYP3A4", "effect": "合成无关对照警告", "management": None,
            "source_text": "硝苯地平与克拉霉素合用的合成对照文本",
            "source_url": "https://synthetic.example/label/nifedipine",
            "confidence": "low", "detection_path": "test_detector",
        })
    return warnings


class EmptyRAG:
    def __call__(self, query: str, **_: object) -> dict:
        return {"query": query, "mode": "test", "results": []}


def _agent_factory(store: MemoryStore) -> MedicationCoordinatorAgent:
    return MedicationCoordinatorAgent(store, ddi_tool=DDITool(fake_detect),
                                      rag_tool=EmptyRAG())


class _App:
    def __init__(self, directory: Path) -> None:
        import stage0.server as server
        holder: dict = {}

        def factory():
            return _agent_factory(holder["store"])

        self.app = server.create_app(
            db_path=directory / "memory.db", worker_thread=False,
            agent_factory=factory)
        holder["store"] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.client.close()
        self.worker.stop()
        self.store.close()

    def commit_event(self, key: str, body: dict) -> dict:
        """Submit an event and drive the worker; returns the final payload."""
        response = self.client.post("/v1/events", json=body,
                                    headers={"Idempotency-Key": key})
        assert response.status_code == 202, response.text
        receipts = self.worker.drain_once()
        assert receipts and receipts[0]["status"] in {"done", "waiting_review"}, receipts
        done = self.client.get(f"/v1/events/{key}")
        assert done.status_code == 200, done.text
        return done.json()


ADD_AMLODIPINE = {
    "event_type": "medication_change", "text": "新增氨氯地平",
    "payload": {"action": "add", "medication": "氨氯地平", "dose": "5mg"},
    "session_id": "s1",
}
ADD_CLARITHROMYCIN = {
    "event_type": "medication_change", "text": "新增克拉霉素",
    "payload": {"action": "add", "medication": "克拉霉素", "dose": "250mg"},
    "session_id": "s1",
}
DOSE_CHANGE_AMLODIPINE = {
    "event_type": "medication_change", "text": "更正氨氯地平剂量",
    "payload": {"action": "dose_change", "medication": "氨氯地平", "dose": "10mg"},
    "session_id": "s1",
}


def _seed_pair_state(api: _App) -> dict:
    """氨氯地平 + 克拉霉素 committed → one recorded DDI warning."""
    api.commit_event("seed-amlo", ADD_AMLODIPINE)
    return api.commit_event("seed-clari", ADD_CLARITHROMYCIN)


class EvidenceReadApiTests(unittest.TestCase):
    def test_pagination_shape_and_meta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                record = evidence.put(
                    content="第一段内容。" + "第二段内容" * 50,
                    source_uri="https://synthetic.example/label",
                    corpus_version="synthetic-1")
                first = api.client.get(
                    f"/v1/evidence/{record.evidence_id}?offset=0&limit=10")
                self.assertEqual(first.status_code, 200)
                body = first.json()
                self.assertEqual(body["evidence_id"], record.evidence_id)
                self.assertEqual(body["returned_chars"], 10)
                self.assertEqual(body["offset"], 0)
                self.assertTrue(body["truncated"])
                self.assertEqual(body["source"]["uri"], "https://synthetic.example/label")
                self.assertEqual(body["source"]["corpus_version"], "synthetic-1")
                self.assertEqual(body["source"]["source_type"], "general_label")
                self.assertEqual(body["integrity"], "verified")
                self.assertGreater(body["total_chars"], 10)
                second = api.client.get(
                    f"/v1/evidence/{record.evidence_id}?offset=10&limit=10")
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second.json()["offset"], 10)
                # stitched pages reproduce the original content
                stitched = first.json()["content"] + second.json()["content"]
                self.assertEqual(stitched, ("第一段内容。" + "第二段内容" * 50)[:20])
            finally:
                api.close()

    def test_invalid_page_params_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                record = evidence.put(content="内容", source_uri=None,
                                      corpus_version=None)
                for query in ("?offset=-1&limit=10", "?offset=0&limit=0",
                              "?offset=0&limit=99999", "?offset=abc&limit=10"):
                    response = api.client.get(f"/v1/evidence/{record.evidence_id}{query}")
                    self.assertEqual(response.status_code, 422, query)
                    # FastAPI rejects non-integer params itself; the store
                    # rejects out-of-range values with the domain code.
                    self.assertIn(response.json()["error"]["code"],
                                  {"invalid_arguments", "validation_error"}, query)
                    self.assertNotIn("content", response.json()["error"].get("details") or {})
            finally:
                api.close()

    def test_missing_and_cross_scope_share_one_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                foreign = evidence.put(content="其他患者范围的证据",
                                       source_uri="https://synthetic.example/x",
                                       corpus_version="synthetic-1",
                                       scope_id="other-scope")
                missing = api.client.get("/v1/evidence/ev-doesnotexist123")
                cross = api.client.get(f"/v1/evidence/{foreign.evidence_id}")
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(cross.status_code, 404)
                self.assertEqual(missing.json()["error"]["code"],
                                 cross.json()["error"]["code"])
                self.assertEqual(missing.json()["error"]["message"],
                                 cross.json()["error"]["message"])
            finally:
                api.close()

    def test_tampered_content_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                record = evidence.put(content="完整性受保护的正文",
                                      source_uri=None, corpus_version="synthetic-1")
                with api.store.connection:
                    api.store.connection.execute(
                        "UPDATE evidence_records SET content = content || '（篡改）' "
                        "WHERE evidence_id=?", (record.evidence_id,))
                response = api.client.get(f"/v1/evidence/{record.evidence_id}")
                self.assertEqual(response.status_code, 404)
                error = response.json()["error"]
                self.assertEqual(error["code"], "evidence_unavailable")
                self.assertEqual(error["details"].get("integrity"), "hash_mismatch")
            finally:
                api.close()

    def test_repeat_read_is_stable_and_unknown_version_stays_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                evidence = EvidenceStore(api.store.connection, api.store._lock)
                record = evidence.put(content="稳定内容", source_uri=None,
                                      corpus_version=None)
                first = api.client.get(f"/v1/evidence/{record.evidence_id}").json()
                second = api.client.get(f"/v1/evidence/{record.evidence_id}").json()
                self.assertEqual(first, second)
                self.assertIsNone(first["source"]["corpus_version"])
            finally:
                api.close()


class WarningEvidenceLinkageTests(unittest.TestCase):
    def test_recorded_warning_citation_carries_readable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                payload = _seed_pair_state(api)
                warnings = payload["response"]["warnings"]
                self.assertTrue(warnings)
                citation = warnings[0]["citations"][0]
                self.assertIn("evidence_id", citation)
                evidence_id = citation["evidence_id"]
                read = api.client.get(f"/v1/evidence/{evidence_id}")
                self.assertEqual(read.status_code, 200)
                self.assertEqual(read.json()["content"], warnings[0]["source_text"])

                alert_id = int(
                    warnings[0]["audit_trail"]["conclusion"].rsplit(":", 1)[-1].split("@")[0])
                detail = api.client.get(f"/v1/alert-records/{alert_id}").json()
                self.assertEqual(detail["status"], "current")
                refs = {r["evidence_id"]: r for r in detail.get("evidence_refs", [])}
                self.assertIn(evidence_id, refs)
                self.assertEqual(refs[evidence_id]["status"], "available")
                self.assertEqual(refs[evidence_id]["integrity"], "verified")
                self.assertEqual(refs[evidence_id]["meta"]["content_chars"],
                                 len(warnings[0]["source_text"]))
                self.assertIn("explanation", detail)
                self.assertEqual(detail["explanation"]["status"], "current")
                self.assertTrue(detail["explanation"]["fact_refs"])
            finally:
                api.close()


class LegacyEvidenceUnavailableTests(unittest.TestCase):
    def test_legacy_sources_report_unavailable_not_fabricated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                store = api.store
                conclusion = store.record_conclusion(
                    session_id="legacy", turn_id="t1", kind="warning",
                    text="氨氯地平×克拉霉素：历史记录（无 evidence 关联）",
                    memory_refs=["memory:medications:1@v1"],
                    source_refs=[{"source_type": "drug_label_or_kegg",
                                  "uri": "https://old.example/label",
                                  "quote": "历史引用文本", "retrieval": "legacy"}])
                # a source_ref pointing at a now-missing evidence id
                store.connection.execute(
                    "UPDATE conclusions SET source_refs_json=? WHERE id=?",
                    ('[{"source_type": "drug_label_or_kegg", "uri": "https://old.example/2",'
                     ' "quote": "q", "retrieval": "legacy", "evidence_id": "ev-gone123"}]',
                     conclusion["id"]))
                store.connection.commit()
                detail = api.client.get(f"/v1/alert-records/{conclusion['id']}").json()
                self.assertTrue(detail["evidence_refs"])
                statuses = {r["status"] for r in detail["evidence_refs"]}
                self.assertEqual(statuses, {"unavailable"})
                unavailable = detail["evidence_refs"][0]
                self.assertIn(unavailable["reason"], {"no_evidence_link", "evidence_missing"})
                self.assertNotIn("content", unavailable)
            finally:
                api.close()


class ChangeImpactTests(unittest.TestCase):
    def _state_with_two_warnings(self, directory: Path) -> _App:
        api = _App(directory)
        api.commit_event("imp-amlo", ADD_AMLODIPINE)
        api.commit_event("imp-nife", {
            "event_type": "medication_change", "text": "新增硝苯地平",
            "payload": {"action": "add", "medication": "硝苯地平", "dose": "30mg"},
            "session_id": "s1"})
        api.commit_event("imp-clari", ADD_CLARITHROMYCIN)
        return api

    def test_correction_marks_related_stale_only_and_counts_match_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = self._state_with_two_warnings(Path(directory))
            try:
                alerts = api.client.get("/v1/alert-records?status=all").json()["items"]
                self.assertEqual(len(alerts), 2)

                api.commit_event("imp-dose", DOSE_CHANGE_AMLODIPINE)
                impact = api.client.get("/v1/change-impact").json()
                summary = impact["summary"]
                affected_ids = {item["conclusion_id"] for item in impact["affected_conclusions"]}
                self.assertEqual(summary["affected_conclusions"], len(affected_ids))
                self.assertEqual(summary["changed_facts"], len(impact["changed_facts"]))
                self.assertGreaterEqual(summary["affected_conclusions"], 1)
                # the amlodipine×clarithromycin chain is affected
                affected_texts = " ".join(item["text"] for item in impact["affected_conclusions"])
                self.assertIn("氨氯地平", affected_texts)
                # the unrelated nifedipine×clarithromycin chain is NOT affected
                unaffected_texts = " ".join(
                    item["text"] for item in impact["unaffected_conclusions"])
                self.assertIn("硝苯地平", unaffected_texts)
                # each affected conclusion carries its REAL recheck state
                # (open/running/failed while pending; done once a recheck turn
                # produced a successor) — never a fabricated "all clear"
                for item in impact["affected_conclusions"]:
                    self.assertIn("recheck", item)
                    self.assertIn((item["recheck"] or {}).get("status"),
                                  {"open", "running", "done", "failed"})
                    if (item["recheck"] or {}).get("status") == "done":
                        self.assertIsNotNone(item["successor"])
                # after the correction the changed fact appears with a ref
                self.assertTrue(impact["changed_facts"])
                self.assertTrue(all(f.get("memory_refs") for f in impact["changed_facts"]))
                del alerts
            finally:
                api.close()

    def test_no_recheck_hook_never_reads_as_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = self._state_with_two_warnings(Path(directory))
            try:
                # Remove the agent's recheck hook BEFORE the correction: the
                # recheck runner must then leave tasks open and stale
                # conclusions stale — "not rechecked yet" must never read as
                # "risk cleared".
                api.store.recheck_hook = None
                api.commit_event("imp-dose2", DOSE_CHANGE_AMLODIPINE)
                rechecks = api.client.post("/v1/rechecks", json={"max_jobs": 2}).json()
                self.assertEqual(rechecks.get("status"), "no_hook")
                impact = api.client.get("/v1/change-impact").json()
                statuses = {item["status"] for item in impact["affected_conclusions"]}
                self.assertIn("stale", statuses)
                self.assertGreaterEqual(impact["summary"]["pending_rechecks"], 1)
                for item in impact["affected_conclusions"]:
                    head = api.client.get(f"/v1/alert-records/{item['conclusion_id']}").json()
                    self.assertNotEqual(head["status"], "current")
            finally:
                api.close()


class AnswerBundleTests(unittest.TestCase):
    def test_bundle_present_and_same_source_as_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api = _App(Path(directory))
            try:
                payload = _seed_pair_state(api)
                bundle = payload["response"].get("answer_bundle")
                self.assertIsInstance(bundle, dict)
                self.assertTrue(bundle["bundle_version"].startswith("answer-bundle@"))
                self.assertEqual(payload["response"]["safety_status"],
                                 bundle["safety_status"])
                warning_claims = [c for c in bundle["claims"] if c["kind"] == "warning"]
                self.assertEqual(len(warning_claims), len(payload["response"]["warnings"]))
                evidence_ids = {ref for claim in bundle["claims"]
                                for ref in claim.get("evidence_refs", [])}
                citation_ids = {c["evidence_id"]
                                for w in payload["response"]["warnings"]
                                for c in w["citations"] if c.get("evidence_id")}
                self.assertEqual(evidence_ids, citation_ids)
                self.assertTrue(bundle["patient_revision"].get("medications") is not None)
                self.assertTrue(bundle["fact_refs"])
            finally:
                api.close()


# Browser integration lives in run_product_acceptance, where a fresh CLI
# result and screenshots are mandatory. Unit tests never mutate an arbitrary
# service on port 8000 or silently skip the product acceptance gate.


if __name__ == "__main__":
    unittest.main()
