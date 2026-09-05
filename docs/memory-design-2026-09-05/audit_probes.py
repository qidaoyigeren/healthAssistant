"""Design-only probes against the existing v3 store, using synthetic temp DBs.

Run from the repository root. This records observed defects; it is not a test of
the proposed v4 implementation. Never opens stage0/memory.db.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from stage0.memory import EpisodicFact, MemoryStore, SemanticFact, StructuredFactExtractor


def run() -> dict:
    findings = []
    extractor = StructuredFactExtractor(enabled=False)
    for text in ("妈妈没有糖尿病", "邻居有糖尿病", "如果妈妈有糖尿病"):
        extracted, mode, error = extractor.extract(text)
        disease = [f for f in extracted["semantic"] if f["namespace"] == "chronic_disease"]
        findings.append({"probe": "assertion_semantics", "input": text, "disease_candidates": disease,
                         "defect_observed": any(f["value"].get("present") is True for f in disease)})
    with tempfile.TemporaryDirectory(prefix="health-memory-design-") as directory:
        root = Path(directory)
        with MemoryStore(root / "policy.db", llm_enabled=False) as m:
            first = m.write_semantic_fact(SemanticFact("allergy", "synthetic-A", {"status": "reported"}), source="synthetic")
            before = m.audit_for(first["item"]["ref"])["item"]["status"]
            changed = m.write_semantic_fact(SemanticFact("allergy", "synthetic-A", {"status": "cleared"}, conflict_policy="update"), source="synthetic")
            findings.append({"probe": "critical_update_bypass", "outcome": changed["outcome"],
                             "open_conflicts": len(m.open_conflicts()), "defect_observed": changed["conflict"] is None})
            invalid = m.audit_for(first["item"]["ref"].split("@v")[0] + "@v999")
            findings.append({"probe": "invalid_ref_version", "requested": 999, "returned": invalid["item"]["version"],
                             "defect_observed": invalid["item"]["version"] != 999})
            after = m.audit_for(first["item"]["ref"])["item"]["status"]
            findings.append({"probe": "mutable_ref_status", "before": before, "after": after, "defect_observed": before != after})
        with MemoryStore(root / "atomic.db", llm_enabled=False) as m:
            error = None
            try:
                m.consolidate_interaction(session_id="s", turn_id="t", user_text="合成事务输入", semantic_hints=[
                    SemanticFact("preference", "font", "large"), SemanticFact("weight", "kg", 60, salience=2.0)
                ])
            except ValueError as exc:
                error = str(exc)
            findings.append({"probe": "event_partial_commit", "error": error, "facts_remaining": len(m.current_semantic()),
                             "defect_observed": error is not None and len(m.current_semantic()) == 1})
        with MemoryStore(root / "time.db", llm_enabled=False) as m:
            m.record_event(EpisodicFact("measurement", {"synthetic": 1}, occurred_at="2026-09-05T00:00:00Z"),
                           session_id="s", turn_id="t", source="synthetic")
            rows = m.retrieve_episodic(as_of="2026-09-03T00:00:00Z")
            findings.append({"probe": "as_of_future_event", "as_of": "2026-09-03T00:00:00Z", "returned": [r["occurred_at"] for r in rows],
                             "defect_observed": len(rows) == 1})
        with MemoryStore(root / "retry.db", llm_enabled=False) as m:
            event = EpisodicFact("measurement", {"synthetic": 1})
            for when in ("2026-09-05T00:00:00Z", "2026-09-05T00:00:01Z"):
                with patch("stage0.memory._iso", return_value=when):
                    m.record_event(event, session_id="same-session", turn_id="same-input", source="synthetic")
            count = m.connection.execute("SELECT count(*) FROM episodic_memory").fetchone()[0]
            findings.append({"probe": "same_input_retry_different_clock", "rows": count, "defect_observed": count == 2})
        with MemoryStore(root / "two-events.db", llm_enabled=False) as m:
            event = EpisodicFact("measurement", {"synthetic": 1}, occurred_at="2026-09-05T00:00:00Z")
            refs = [m.record_event(event, session_id="s", turn_id=turn, source="synthetic")["ref"] for turn in ("event-1", "event-2")]
            findings.append({"probe": "distinct_events_same_text_time", "refs": refs, "defect_observed": refs[0] == refs[1]})
        with MemoryStore(root / "late.db", llm_enabled=False) as m:
            for action, when, turn in (("remove", "2026-09-01T00:00:00Z", "stop-first"), ("add", "2026-08-20T00:00:00Z", "start-late")):
                m.apply_medication_change(action=action, name="合成药A", ingredients=[], session_id="s", turn_id=turn, source="synthetic", occurred_at=when)
            findings.append({"probe": "out_of_order_stop_start", "current_count": len(m.current_medications()),
                             "defect_observed": len(m.current_medications()) == 1})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "memory_py_sha256": hashlib.sha256((ROOT / "stage0/memory.py").read_bytes()).hexdigest(),
        "synthetic_only": True, "opened_user_database": False, "findings": findings,
        "note": "Defect witnesses for existing v3 only. No v4 correctness or medical safety claim."
    }


if __name__ == "__main__":
    result = run()
    output = Path(__file__).with_name("audit_results.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
