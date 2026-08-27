from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent
from stage0.memory import EpisodicFact, MemoryStore, SemanticFact


class EmptyRAG:
    def __call__(self, query: str, **_: object) -> dict:
        return {"query": query, "mode": "test", "results": []}


def fake_detect(medications: list[str]) -> list[dict]:
    if {"氨氯地平", "克拉霉素"}.issubset(medications):
        return [{
            "drug_a": "氨氯地平",
            "drug_b": "克拉霉素",
            "severity": "moderate",
            "mechanism": "CYP3A4",
            "effect": "降压作用增强",
            "management": None,
            "source_text": "与CYP3A4抑制剂克拉霉素合用时，氨氯地平暴露量增加",
            "source_url": "https://example.test/label",
            "confidence": "medium",
            "detection_path": "test_detector",
        }]
    return []


class Stage3MemoryTests(unittest.TestCase):
    def test_dedup_conflict_and_decay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with MemoryStore(path, llm_enabled=False) as memory:
                first = memory.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, conflict_policy="conflict"),
                    source="caregiver",
                )
                duplicate = memory.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "reported"}, conflict_policy="conflict"),
                    source="caregiver",
                )
                cleared = memory.write_semantic_fact(
                    SemanticFact("allergy", "磺胺", {"status": "cleared"}, conflict_policy="conflict"),
                    source="new_note",
                )
                self.assertEqual(first["item"]["ref"], duplicate["item"]["ref"])
                self.assertEqual(duplicate["outcome"], "deduplicated")
                self.assertIsNotNone(cleared["conflict"])
                self.assertEqual(len(memory.open_conflicts()), 1)

                now = datetime.now(timezone.utc)
                memory.record_event(
                    EpisodicFact("measurement", {"bp": "130/80"}, occurred_at=(now - timedelta(days=7)).isoformat(), salience=0.7),
                    session_id="s", turn_id="new", source="caregiver",
                )
                memory.record_event(
                    EpisodicFact("measurement", {"bp": "140/90"}, occurred_at=(now - timedelta(days=365 * 3)).isoformat(), salience=0.7),
                    session_id="s", turn_id="old", source="caregiver",
                )
                ranked = memory.retrieve_episodic(event_types=["measurement"], as_of=now)
                self.assertGreater(ranked[0]["retrieval_weight"], ranked[1]["retrieval_weight"])


class Stage3AgentTests(unittest.TestCase):
    def test_proactive_warning_audit_and_cross_session_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            with MemoryStore(path, llm_enabled=False) as memory:
                agent = MedicationCoordinatorAgent(
                    memory,
                    ddi_tool=DDITool(fake_detect),
                    rag_tool=EmptyRAG(),
                )
                agent.handle(
                    CareEvent("register_profile", "母亲72岁", {"profile": {"age": 72, "sex": "女"}}),
                    session_id="s1", turn_id="profile",
                )
                agent.handle(
                    CareEvent("medication_change", "新增氨氯地平", {"action": "add", "medication": "氨氯地平"}),
                    session_id="s1", turn_id="med1",
                )
                response = agent.handle(
                    CareEvent("medication_change", "新增克拉霉素", {"action": "add", "medication": "克拉霉素"}),
                    session_id="s1", turn_id="med2",
                )
                self.assertEqual(len(response.warnings), 1)
                warning = response.warnings[0]
                self.assertTrue(warning["citations"])
                self.assertTrue(warning["audit_trail"]["memory_refs"])
                acts = [item for item in response.tool_trace if item.get("phase") == "act"]
                self.assertTrue(any(item["tool"] == "ddi_check" for item in acts))
                self.assertLess(
                    next(i for i, item in enumerate(acts) if item["tool"] == "ddi_check"),
                    next(i for i, item in enumerate(acts) if item.get("purpose") == "record_ddi_warnings"),
                )

            with MemoryStore(path, llm_enabled=False) as reopened:
                second = MedicationCoordinatorAgent(reopened, ddi_tool=DDITool(fake_detect), rag_tool=EmptyRAG())
                recall = second.handle(
                    CareEvent("query_current_medications", "我妈现在吃什么药？"),
                    session_id="s2", turn_id="recall",
                )
                self.assertIn("氨氯地平", recall.text)
                self.assertIn("克拉霉素", recall.text)
                self.assertIn("memory:medication:", recall.text + " ".join(recall.audit_trail["memory_refs"]))

    def test_safety_refuses_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with MemoryStore(Path(directory) / "memory.db", llm_enabled=False) as memory:
                agent = MedicationCoordinatorAgent(memory, ddi_tool=DDITool(fake_detect), rag_tool=EmptyRAG())
                response = agent.handle(
                    CareEvent("user_message", "帮我诊断是不是感染，再推荐药"),
                    session_id="s", turn_id="unsafe",
                )
                self.assertIn("不能诊断", response.text)
                self.assertIn("建议咨询医生/药师", response.text)


if __name__ == "__main__":
    unittest.main()
