"""Dev-eval runner for the product upgrade phases.

Usage:
    python -m stage0.product_evals.run_eval --suite dev --out <results.json>
        [--phase P0 --phase P1 ...]   (default: all tasks in the suite)

Exit codes: 0 = all executed tasks passed; 1 = at least one failure;
2 = tasks marked unavailable (missing modules / data) so no overall pass.

Executors drive the REAL service (TestClient + temporary synthetic DB, scripted
detector) — the same code path the frontend uses.  No real model calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.harness.evidence import EvidenceStore, content_hash
from stage0.memory import MemoryStore

TASKS_DIR = Path(__file__).parent / "tasks"
DATASET_VERSION = "dev-1"

FAILURE_CATEGORIES = {
    "assertion_failed", "error", "setup_error", "timeout",
    "missing_requirement", "safety_violation",
}


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


class Fixture:
    """One temporary service instance per task; everything synthetic."""

    def __init__(self) -> None:
        import stage0.server as server
        self._tmp = tempfile.TemporaryDirectory()
        holder: dict = {}

        def factory():
            return MedicationCoordinatorAgent(
                holder["store"], ddi_tool=DDITool(fake_detect), rag_tool=EmptyRAG())

        self.app = server.create_app(db_path=Path(self._tmp.name) / "memory.db",
                                     worker_thread=False, agent_factory=factory)
        holder["store"] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)
        self.evidence = EvidenceStore(self.store.connection, self.store._lock)
        self.seeded_evidence: list[dict[str, Any]] = []
        self.resolved: dict[str, Any] = {}

    def close(self) -> None:
        try:
            self.client.close()
            self.worker.stop()
            self.store.close()
        finally:
            self._tmp.cleanup()

    # -- seed ---------------------------------------------------------------

    def seed(self, seed_spec: dict[str, Any]) -> None:
        for spec in seed_spec.get("evidence") or []:
            record = self.evidence.put(
                content=spec["content"], source_uri=spec.get("source_uri"),
                corpus_version=spec.get("corpus_version"),
                scope_id=spec.get("scope_id"))
            self.seeded_evidence.append({
                "evidence_id": record.evidence_id,
                "content": spec["content"],
                "corpus_version": spec.get("corpus_version"),
            })
        for index, spec in enumerate(seed_spec.get("events") or []):
            body = {k: v for k, v in spec.items() if k != "idempotency_key"}
            self.commit_event(spec.get("idempotency_key") or f"seed-{index}", body)

    def commit_event(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post("/v1/events", json=body,
                                    headers={"Idempotency-Key": key})
        if response.status_code != 202:
            raise RuntimeError(f"event submit failed: {response.status_code} {response.text[:200]}")
        receipts = self.worker.drain_once()
        if not receipts or receipts[0]["status"] not in {"done", "waiting_review"}:
            raise RuntimeError(f"event failed to commit: {receipts}")
        done = self.client.get(f"/v1/events/{key}")
        if done.status_code != 200:
            raise RuntimeError(f"event poll failed: {done.status_code}")
        return done.json()

    def resolve_ref(self, raw: Any) -> Any:
        """Resolve ``$evidence[i].field`` / ``$last_event.field`` references."""
        if not isinstance(raw, str) or not raw.startswith("$"):
            return raw
        import re
        match = re.match(r"^\$(evidence)\[(\d+)\]((?:\.\w+)*)$", raw)
        if match:
            value: Any = self.seeded_evidence[int(match.group(2))]
            path = match.group(3).lstrip(".")
            for key in (path.split(".") if path else []):
                value = value[key]
            return value
        if raw.startswith("$last_event."):
            value = self.resolved.get("last_event")
            for key in raw[len("$last_event."):].split("."):
                value = value[key]
            return value
        raise AssertionError(f"unresolvable task reference: {raw}")


# ---------------------------------------------------------------------------
# expected-type executors
# ---------------------------------------------------------------------------

def _exec_read_page(fixture: Fixture, call: dict[str, Any],
                    expected: dict[str, Any]) -> None:
    params = {"offset": fixture.resolve_ref(call.get("offset", 0)),
              "limit": fixture.resolve_ref(call.get("limit", 2000))}
    repeats = int(call.get("repeat", 1))
    bodies = []
    for _ in range(repeats):
        response = fixture.client.get(
            f"/v1/evidence/{fixture.resolve_ref(call['evidence_id'])}",
            params=params)
        if response.status_code != 200:
            raise AssertionError(f"read failed: {response.status_code} {response.text[:200]}")
        bodies.append(response.json())
    first = bodies[0]
    if "returned_chars_gt" in expected:
        assert first["returned_chars"] > expected["returned_chars_gt"], "empty page"
    if expected.get("content_matches_hash"):
        expected_content = fixture.seeded_evidence[0]["content"]
        assert first["content"] == expected_content[first["offset"]:first["offset"] + first["returned_chars"]], \
            "page content differs from the source"
        meta = fixture.evidence.get_meta(first["evidence_id"])
        assert meta is not None and meta["content_hash"] == content_hash(expected_content)
    if expected.get("total_chars_eq_full"):
        assert first["total_chars"] == len(fixture.seeded_evidence[0]["content"])
    if "truncated" in expected:
        assert first["truncated"] is expected["truncated"], "truncated flag mismatch"
    if expected.get("reads_identical"):
        assert all(body == first for body in bodies[1:]), "repeated reads differ"
    if "source_version_is" in expected:
        assert first["source"]["corpus_version"] == expected["source_version_is"], \
            "unknown source version must stay null (not fabricated)"


def _exec_api_status(fixture: Fixture, call: dict[str, Any],
                     expected: dict[str, Any]) -> None:
    if expected.get("same_error_as_missing"):
        missing = fixture.client.get("/v1/evidence/ev-doesnotexist123")
        cross = fixture.client.get(
            f"/v1/evidence/{fixture.resolve_ref(call['evidence_id'])}")
        assert missing.status_code == cross.status_code == 404, "error shapes differ"
        assert missing.json()["error"]["code"] == cross.json()["error"]["code"]
        assert missing.json()["error"]["message"] == cross.json()["error"]["message"]
        return
    if "integrity" in expected:  # tamper then read
        record_id = fixture.resolve_ref(call["evidence_id"])
        with fixture.store.connection:
            fixture.store.connection.execute(
                "UPDATE evidence_records SET content = content || ? WHERE evidence_id=?",
                (str(call.get("tamper_append", "（篡改）")), record_id))
        response = fixture.client.get(f"/v1/evidence/{record_id}")
        assert response.status_code == 404, "tampered content must be refused"
        error = response.json()["error"]
        assert error["code"] == expected["error_kind"], error
        assert error["details"].get("integrity") == expected["integrity"], error
        return
    params = {key: fixture.resolve_ref(call[key])
              for key in ("offset", "limit") if key in call}
    response = fixture.client.get(
        f"/v1/evidence/{fixture.resolve_ref(call['evidence_id'])}", params=params)
    assert response.status_code == 422, f"expected rejection, got {response.status_code}"
    if expected.get("error_kind"):
        assert response.json()["error"]["code"] in {expected["error_kind"],
                                                    "validation_error"}, \
            response.json()["error"]
    assert "content" not in (response.json()["error"].get("details") or {}), "error leaks content"


def _exec_bundle_shape(fixture: Fixture, call: dict[str, Any],
                       expected: dict[str, Any]) -> None:
    payload = fixture.commit_event(f"ev-{expected.get('phase', 'x')}", call["event"])
    fixture.resolved["last_event"] = payload
    bundle = payload["response"].get("answer_bundle")
    if expected.get("bundle_version_prefix"):
        assert isinstance(bundle, dict), "answer_bundle missing from response"
        assert bundle["bundle_version"].startswith(expected["bundle_version_prefix"])
    if expected.get("safety_status"):
        assert bundle["safety_status"] == expected["safety_status"]
    if expected.get("warnings_from_bundle_match_payload"):
        warning_claims = [c for c in bundle["claims"] if c["kind"] == "warning"]
        assert len(warning_claims) == len(payload["response"].get("warnings") or []), \
            "bundle claims and payload warnings diverge"


class MissingCapability(LookupError):
    """Raised by executors when a task's required capability is not
    implemented yet — the honest `unavailable` signal (exit code 2)."""


def _commit_spec(fixture: Fixture, spec: dict[str, Any], fallback_key: str) -> dict[str, Any]:
    body = {k: v for k, v in spec.items() if k != "idempotency_key"}
    return fixture.commit_event(spec.get("idempotency_key") or fallback_key, body)


def _exec_db_state(fixture: Fixture, call: dict[str, Any],
                   expected: dict[str, Any]) -> None:
    action = call["action"]
    if action == "apply_correction":
        # seed events are already committed by Fixture.seed; only the
        # correction (a NEW fact change) is submitted here.
        seed = fixture.resolved["seed"]["inputs"]["seed"]
        _commit_spec(fixture, seed["correction"], "eval-correction")
        alerts = fixture.client.get("/v1/alert-records?status=all").json()["items"]
        stale = [a for a in alerts if a["status"] == "stale"]
        current = [a for a in alerts if a["status"] == "current"]
        if expected.get("related_conclusion_status") == "stale_pending_recheck":
            assert stale, "no conclusion was invalidated by the correction"
        if expected.get("unrelated_conclusion_status") == "current":
            assert current, "unrelated conclusions were over-invalidated"
        impact = fixture.client.get("/v1/change-impact").json()
        detail_count = len(impact["affected_conclusions"])
        assert impact["summary"]["affected_conclusions"] == detail_count, \
            "summary and details use different sources"
        return
    if action == "run_recheck_then_read_status":
        seed = fixture.resolved["seed"]["inputs"]["seed"]
        _commit_spec(fixture, seed["correction"], "eval-correction")
        fixture.store.recheck_hook = None  # simulate recheck failure/unavailability
        fixture.client.post("/v1/rechecks", json={"max_jobs": 2})
        impact = fixture.client.get("/v1/change-impact").json()
        assert impact["affected_conclusions"], "no affected conclusions recorded"
        for item in impact["affected_conclusions"]:
            head = fixture.client.get(f"/v1/alert-records/{item['conclusion_id']}").json()
            assert head["status"] != "current", \
                "unrechecked conclusion must not read as current/no-risk"
        return
    if action == "stage_candidate_only":
        response = fixture.client.post('/v1/materials/csv', json={'key': 'eval-candidate',
            'text': 'name,dose,unit,schedule,date,subject\nSYN-阿莫西林,500,mg,每日三次,2026-09-07,local-demo\n'})
        assert response.status_code == 200, response.text
        case = response.json()
        assert case['items'][0]['status'] == expected['candidate_status']
        names = {m['display_name'] for m in fixture.store.current_medications()}
        assert not names.intersection(expected['authority_medication_names_excludes'])
        return
    if action == 'import_reconciliation':
        before = fixture.store.current_medications()
        response = fixture.client.post('/v1/materials/csv', json={'key': 'eval-import', 'text': call['text']})
        assert response.status_code == expected['http_status'], response.text
        if response.status_code == 200:
            case = response.json()
            assert [i['kind'] for i in case['items']] == expected['kinds']
        assert before == fixture.store.current_medications(), 'import polluted authority'
        return
    if action == 'care_task_contract':
        from stage0.care_tasks import CareTasks
        service = CareTasks(fixture.app.state.product)
        task = service.create('create', call['goal_type'])
        result = service.resume(task['id'], 'resume', task['revision'], call.get('task_action', 'continue'))
        assert result['status'] == expected['status']
        assert service.resume(task['id'], 'resume', task['revision'], call.get('task_action', 'continue')) == result
        if result['status'] == 'completed':
            assert result['result_refs'] and all(fixture.app.state.product.get(ref) for ref in result['result_refs'])
        return
    if action == 'claim_support':
        from stage0.evidence_quality import assess_claim
        result = assess_claim(quote=call['quote'], text=call.get('text', call['quote']), entities=['药甲', '药乙'],
            evidence_id='synthetic', conditions_known=call.get('conditions_known', True))
        assert result['status'] == expected['status'], result
        return
    if action == "submit_event_twice":
        event = call["event"]
        key = call["idempotency_key"]
        first = fixture.client.post("/v1/events", json=event,
                                    headers={"Idempotency-Key": key})
        second = fixture.client.post("/v1/events", json=event,
                                     headers={"Idempotency-Key": key})
        assert first.status_code == 202 and second.status_code == 202, \
            f"submit failed: {first.status_code}/{second.status_code} {first.text[:200]}"
        fixture.worker.drain_once()
        replay = fixture.client.post("/v1/events", json=event,
                                     headers={"Idempotency-Key": key})
        assert replay.headers.get("Idempotent-Replay") == "true", \
            "same-key resubmission must replay, not re-execute"
        effects = len([m for m in fixture.store.current_medications()
                       if m.get("display_name") == event["payload"]["medication"]])
        if "medication_add_effects" in expected:
            assert effects == expected["medication_add_effects"], \
                f"expected {expected['medication_add_effects']} domain effect, got {effects}"
        return
    if action == "submit_event":
        payload = fixture.commit_event("eval-event", call["event"])
        if expected.get("safety_status"):
            assert payload["response"]["safety_status"] == expected["safety_status"]
        text = payload["response"]["text"]
        if expected.get("no_prescription_text"):
            import re
            assert not re.search(r"(?:每日|每天|每次|一次)\s*\d+\s*(?:毫克|mg|片|粒)", text), \
                "delivered text contains a concrete dosage directive"
            assert not re.search(r"建议[^。；]{0,10}(?:剂量|服用量|换药|停药|加量|减量)", text), \
                "delivered text contains a dosage-change directive"
        return
    raise AssertionError(f"unknown db_state action: {action}")


EXECUTORS = {
    "read_page": _exec_read_page,
    "api_status": _exec_api_status,
    "bundle_shape": _exec_bundle_shape,
    "db_state": _exec_db_state,
}


def _task_available(task: dict[str, Any]) -> bool:
    for requirement in task.get("requires") or []:
        if requirement == "fixture_db":
            continue
        if requirement == "reconciliation_module":
            try:
                import stage0.product  # noqa: F401
            except ImportError:
                return False
        else:
            return False
    return True


def run_suite(suite: str = "dev", *, phases: list[str] | None = None,
              tasks_dir: Path | None = None) -> dict[str, Any]:
    suite_dir = (tasks_dir or TASKS_DIR) / suite
    if not suite_dir.is_dir():
        return {"suite": suite, "overall": "unavailable", "summary": {
            "total": 0, "passed": 0, "failed": 0, "unavailable": 0},
            "note": f"suite directory missing: {suite_dir}"}
    task_files = sorted(suite_dir.glob("*.task.json"))
    tasks = [json.loads(path.read_text(encoding="utf-8")) for path in task_files]
    if phases:
        tasks = [t for t in tasks if t.get("phase") in phases]
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for task in tasks:
        entry: dict[str, Any] = {"task_id": task["task_id"], "duration_ms": 0}
        started = time.perf_counter()
        if not _task_available(task):
            entry["result"] = "unavailable"
            failures.append({"task_id": task["task_id"],
                             "category": "missing_requirement",
                             "detail": "required capability not implemented yet"})
        else:
            fixture = None
            try:
                fixture = Fixture()
                fixture.resolved["seed"] = task
                fixture.seed(task.get("inputs", {}).get("seed") or {})
                executor = EXECUTORS.get(task["expected"]["type"])
                if executor is None:
                    raise AssertionError(f"no executor for {task['expected']['type']}")
                executor(fixture, task.get("inputs", {}).get("call") or {},
                         task["expected"])
                entry["result"] = "passed"
            except MissingCapability:
                entry["result"] = "unavailable"
                failures.append({"task_id": task["task_id"],
                                 "category": "missing_requirement",
                                 "detail": "required capability not implemented yet"})
            except KeyError as exc:
                entry["result"] = "failed"
                failures.append({"task_id": task["task_id"], "category": "setup_error",
                                 "detail": f"task/executor shape mismatch: {exc}"})
            except AssertionError as exc:
                entry["result"] = "failed"
                failures.append({"task_id": task["task_id"],
                                 "category": "assertion_failed",
                                 "detail": str(exc)[:400]})
            except Exception as exc:  # noqa: BLE001 - record and continue
                entry["result"] = "failed"
                failures.append({"task_id": task["task_id"], "category": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"[:400],
                                 "traceback": traceback.format_exc()[-800:]})
            finally:
                if fixture is not None:
                    fixture.close()
        entry["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        results.append(entry)
    passed = sum(1 for r in results if r["result"] == "passed")
    failed = sum(1 for r in results if r["result"] == "failed")
    unavailable = sum(1 for r in results if r["result"] == "unavailable")
    if failed:
        overall = "fail"
    elif unavailable or not results:
        overall = "unavailable"
    else:
        overall = "pass"
    return {
        "suite": suite,
        "dataset_version": DATASET_VERSION,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phases": phases or "all",
        "environment": {"provider": "scripted",
                        "note": "合成评测,无真实模型调用;临时合成库"},
        "summary": {"total": len(results), "passed": passed,
                    "failed": failed, "unavailable": unavailable},
        "failures": failures,
        "tasks": results,
        "overall": overall,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="dev")
    parser.add_argument("--out", default=None)
    parser.add_argument("--phase", action="append", default=None,
                        help="only run tasks of these phases (repeatable)")
    args = parser.parse_args(argv)
    report = run_suite(args.suite, phases=args.phase)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return {"pass": 0, "fail": 1, "unavailable": 2}[report["overall"]]


if __name__ == "__main__":
    sys.exit(main())
