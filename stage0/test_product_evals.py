"""Product eval runner self-checks (P0 验收:真实失败返回非零;缺数据不输出 pass)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stage0.product_evals import run_eval


class DevSuiteTests(unittest.TestCase):
    def test_dev_suite_runs_with_real_service(self) -> None:
        report = run_eval.run_suite("dev")
        self.assertEqual(report["summary"]["failed"], 0,
                         json.dumps(report["failures"], ensure_ascii=False))
        # P2 now runs the real import endpoint and verifies candidate isolation.
        self.assertEqual(report["overall"], "pass")
        self.assertEqual(report["environment"]["provider"], "scripted")

    def test_forced_failure_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite_dir = root / "broken"
            suite_dir.mkdir()
            task = {
                "task_id": "broken-001", "phase": "P1", "capability": "evidence_readback",
                "group": "g-broken", "title": "必败样例",
                "inputs": {"seed": {"evidence": [
                    {"content": "SYNTHETIC 内容", "source_uri": None,
                     "corpus_version": "synthetic-1"}]},
                    "call": {"evidence_id": "$evidence[0].evidence_id",
                             "offset": 0, "limit": 10}},
                "expected": {"type": "read_page", "returned_chars_gt": 999999},
                "requires": ["fixture_db"],
            }
            (suite_dir / "broken-001.task.json").write_text(
                json.dumps(task, ensure_ascii=False), encoding="utf-8")
            report = run_eval.run_suite("broken", tasks_dir=root)
            self.assertEqual(report["overall"], "fail")
            self.assertEqual(report["summary"]["failed"], 1)
            self.assertEqual(report["failures"][0]["category"], "assertion_failed")
            # main() maps overall -> exit code
            self.assertEqual({"pass": 0, "fail": 1, "unavailable": 2}[report["overall"]], 1)

    def test_missing_task_data_is_unavailable_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suite_dir = root / "empty"
            suite_dir.mkdir()
            task = {
                "task_id": "nc-001", "phase": "P2", "capability": "reconciliation",
                "group": "g-p2", "title": "未实现能力",
                "inputs": {"seed": {}, "call": {"action": "stage_candidate_only"}},
                "expected": {"type": "db_state"}, "requires": ["independent_unprovided_material"],
            }
            (suite_dir / "nc-001.task.json").write_text(
                json.dumps(task, ensure_ascii=False), encoding="utf-8")
            report = run_eval.run_suite("empty", tasks_dir=root)
            self.assertEqual(report["overall"], "unavailable")
            self.assertEqual(report["failures"][0]["category"], "missing_requirement")


if __name__ == "__main__":
    unittest.main()
