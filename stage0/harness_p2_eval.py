"""Harness P2 optimization comparison on a fixed synthetic workload.

Honest-measurement contract (P2 constraint 20): the SAME fixed workload is
run twice — once with every P2 optimization flag OFF (baseline behaviour) and
once with the flags ON — over fresh temporary databases.  Reported metrics:

* tool dispatch count (real handler invocations, counted at the tool layer)
* repeated-read rate (the P1 evaluator's definition, over the act trace)
* reuse stats: same-run / cross-run hits, stores, refresh bypasses
* tokens charged (durable budget ledger) and wall-clock seconds
* task/safety/citation metrics on BOTH sides (must agree: no safety change)

All providers are fake — this proves FLOW behaviour and resource behaviour,
never real-model quality or real-money savings.

Usage:
    python stage0/harness_p2_eval.py --out docs/harness-upgrade/P2/optimization_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from stage0.agent import CareEvent, DDITool, MedicationCoordinatorAgent
from stage0.graph_runner import LangGraphAgentRunner
from stage0.memory import MemoryStore
from stage0.harness_eval import FAKE_WARNINGS, FakeRAG, make_agent

DATASET_VERSION = "p2-1.0"
P2_FLAGS = ("STAGE0_RUN_REUSE", "STAGE0_READ_CACHE", "AGENT_NO_PROGRESS_LIMIT")

# Fixed workload: an initial registration, a medication change (the check
# flow), the SAME medication change repeated on the SAME facts (the reuse
# candidate), and a repeated legal-read loop (the no-progress candidate).
WORKLOAD = [
    ("register", CareEvent("register_profile", "登记患者信息。",
                           {"profile": {"age": 67, "chronic_disease": ["高血压"]}})),
    ("med_change_1", CareEvent("medication_change", "新增氨氯地平。",
                               {"action": "add", "medication": "氨氯地平"})),
    ("med_change_1_repeat", CareEvent("medication_change", "再次报告：新增氨氯地平。",
                                      {"action": "add", "medication": "氨氯地平"})),
    ("med_change_2", CareEvent("medication_change", "新增辛伐他汀。",
                               {"action": "add", "medication": "辛伐他汀"})),
]


def _run_workload(flags: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        import os
        saved = {k: os.environ.pop(k, None) for k in P2_FLAGS}
        os.environ.update(flags)
        counter = {"ddi": 0, "rag": 0}
        try:
            store = MemoryStore(directory / "memory.db")
            agent = make_agent(store, rag_tool=FakeRAG([]),
                               ddi_tool=_CountingDDI(counter))
            graph = LangGraphAgentRunner(agent, checkpoint_path=str(directory / "cp.db"))
            started = time.perf_counter()
            responses = []
            try:
                for index, (label, event) in enumerate(WORKLOAD):
                    run_id = f"p2eval-{index}"
                    response = graph.run(event=event, session_id="s", turn_id=run_id,
                                         event_id=run_id, run_id=run_id)
                    responses.append(response)
                wall = time.perf_counter() - started
                tokens = 0
                for index in range(len(WORKLOAD)):
                    run = store.workflow_run_get(f"p2eval-{index}") or {}
                    budget = run.get("budget") or {}
                    tokens += budget.get("tokens_charged", 0)
                reuse_stats = dict(agent.reuse.stats)
                safety = {
                    "all_enforced": all(r.safety_status == "enforced" for r in responses),
                    "total_warnings": sum(len(r.warnings) for r in responses),
                    "citation_valid": all(
                        all(w.get("citations") for w in r.warnings) for r in responses),
                }
                acts = [e["tool"] for r in responses
                        for e in r.tool_trace if e.get("phase") == "act"]
                repeated = len(acts) - len({(t, json.dumps({}, sort_keys=True)) for t in acts})
                # precise repeat definition: identical (tool,args,purpose) pairs
                seen_keys = set()
                repeated = 0
                for r in responses:
                    for e in r.tool_trace:
                        if e.get("phase") != "act":
                            continue
                        key = (e.get("tool"), e.get("purpose"),
                               json.dumps(e.get("arguments", {}), sort_keys=True,
                                          ensure_ascii=False))
                        if key in seen_keys:
                            repeated += 1
                        seen_keys.add(key)
                stopped_runs = store.connection.execute(
                    "SELECT COUNT(*) FROM run_progress_state WHERE stopped_reason IS NOT NULL"
                ).fetchone()[0]
            finally:
                graph.close()
                store.close()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return {
        "tool_dispatches": {"ddi_check": counter["ddi"], "rag_search": counter["rag"]},
        "repeated_identical_reads": repeated,
        "reuse_stats": reuse_stats,
        "tokens_charged_total": tokens,
        "wall_seconds": round(wall, 3),
        "safety": safety,
        "no_progress_stopped_runs": stopped_runs,
    }


class _CountingDDI:
    """The eval detector plus a dispatch counter (fake provider — flow only)."""

    def __init__(self, counter: dict):
        self.counter = counter
        self._inner = DDITool(lambda meds: FAKE_WARNINGS)

    def __call__(self, medications, focus_medication=None):
        self.counter["ddi"] += 1
        return self._inner(medications, focus_medication=focus_medication)


def _run_repeated_reads_scenario(flags: dict[str, str]) -> dict:
    """A scripted planner that proposes the same legal read forever — the
    no-progress contract's target case.  Metric: executed read count before
    the run terminates (budget-bounded when detection is OFF)."""
    def provider(payload):
        return {"decision": "tool", "tool": "memory_read", "purpose": "snapshot",
                "arguments": {"query": "snapshot"}}
    import os
    saved = {k: os.environ.pop(k, None) for k in P2_FLAGS}
    os.environ.update(flags)
    try:
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            store = MemoryStore(directory / "memory.db")
            agent = make_agent(store, provider=provider)
            graph = LangGraphAgentRunner(agent, checkpoint_path=str(directory / "cp.db"))
            try:
                response = graph.run(event=WORKLOAD[1][1], session_id="s",
                                     turn_id="p2eval-loop", event_id="p2eval-loop",
                                     run_id="p2eval-loop")
            finally:
                graph.close()
                acts = [e["tool"] for e in response.tool_trace if e.get("phase") == "act"]
                status = response.safety_status
                text = response.text
                store.close()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {"executed_reads": len(acts), "safety_status": status,
            "honest_incomplete": ("未保存" in text or "未完成" in text)}


def _compare_repeated_reads(baseline: dict, optimized: dict) -> dict:
    return {
        "executed_reads": {"baseline": baseline["executed_reads"],
                           "optimized": optimized["executed_reads"]},
        "safety_status_identical": baseline["safety_status"] == optimized["safety_status"],
        "honest_incomplete_response_both": (baseline["honest_incomplete"]
                                            and optimized["honest_incomplete"]),
    }


def _compare(baseline: dict, optimized: dict) -> dict:
    def delta(metric):
        base, opt = metric["baseline"], metric["optimized"]
        if isinstance(base, (int, float)) and isinstance(opt, (int, float)) and base:
            return round((opt - base) / base, 4)
        return None
    return {
        "ddi_check_dispatches": {"baseline": baseline["tool_dispatches"]["ddi_check"],
                                 "optimized": optimized["tool_dispatches"]["ddi_check"]},
        "rag_search_dispatches": {"baseline": baseline["tool_dispatches"]["rag_search"],
                                  "optimized": optimized["tool_dispatches"]["rag_search"]},
        "repeated_identical_reads": {"baseline": baseline["repeated_identical_reads"],
                                     "optimized": optimized["repeated_identical_reads"]},
        "tokens_charged_total": {"baseline": baseline["tokens_charged_total"],
                                 "optimized": optimized["tokens_charged_total"]},
        "wall_seconds": {"baseline": baseline["wall_seconds"],
                         "optimized": optimized["wall_seconds"]},
        "safety_conclusions_identical": (baseline["safety"] == optimized["safety"]),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Harness P2 optimization comparison")
    parser.add_argument("--out", default="docs/harness-upgrade/P2/optimization_report.json")
    parser.add_argument("--repeat", type=int, default=3,
                        help="runs per side; medians reduce scheduler noise")
    args = parser.parse_args()

    baseline_runs, optimized_runs = [], []
    for _ in range(max(1, args.repeat)):
        baseline_runs.append(_run_workload({}))
        optimized_runs.append(_run_workload({
            "STAGE0_RUN_REUSE": "1", "STAGE0_READ_CACHE": "1",
            "AGENT_NO_PROGRESS_LIMIT": "2",
        }))

    baseline = _aggregate(baseline_runs)
    optimized = _aggregate(optimized_runs)
    loop_baseline = _run_repeated_reads_scenario({})
    loop_optimized = _run_repeated_reads_scenario({"AGENT_NO_PROGRESS_LIMIT": "2"})
    report = {
        "dataset_version": DATASET_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workload": [label for label, _ in WORKLOAD],
        "flags": {"baseline": "(all P2 flags off)",
                  "optimized": {"STAGE0_RUN_REUSE": "1", "STAGE0_READ_CACHE": "1",
                                "AGENT_NO_PROGRESS_LIMIT": "2"}},
        "provider_note": ("全部使用合成病例与假 provider/检测器；重复次数为中位数（--repeat）。"
                          "假 provider 的耗时极小，wall_seconds 不作为本评测的收益口径；"
                          "仅证明流程与资源行为，不代表真实模型质量或实际费用。"),
        "baseline_runs": baseline_runs,
        "optimized_runs": optimized_runs,
        "comparison": _compare(baseline, optimized),
        "repeated_reads_scenario": {
            "baseline": loop_baseline, "optimized": loop_optimized,
            "comparison": _compare_repeated_reads(loop_baseline, loop_optimized)},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    c = report["comparison"]
    print("Harness P2 优化对比（同工作负载，假 provider，中位数）")
    print("=" * 60)
    for key, value in c.items():
        if isinstance(value, dict):
            print(f"  {key}: baseline={value['baseline']}  optimized={value['optimized']}")
        else:
            print(f"  {key}: {value}")
    print(f"  reuse_stats(optimized): {optimized['reuse_stats']}")
    print(f"  no_progress_stopped_runs(optimized): {optimized['no_progress_stopped_runs']}")
    rl = report["repeated_reads_scenario"]
    print(f"  repeated_reads executed_reads: baseline={rl['comparison']['executed_reads']['baseline']}"
          f"  optimized={rl['comparison']['executed_reads']['optimized']}")
    return 0 if acceptance_passed(report) else 1


def _aggregate(runs: list[dict]) -> dict:
    """True per-metric medians; never select an arbitrary middle run."""
    import statistics
    result = {}
    for key, value in runs[0].items():
        if isinstance(value, dict):
            result[key] = _aggregate([r[key] for r in runs])
        elif isinstance(value, bool):
            result[key] = all(r[key] for r in runs)
        elif isinstance(value, (int, float)):
            result[key] = statistics.median(r[key] for r in runs)
        else:
            result[key] = value
    return result


def acceptance_passed(report: dict) -> bool:
    sides = zip(report["baseline_runs"], report["optimized_runs"])
    if not all(b["safety"] == o["safety"] and b["safety"]["all_enforced"]
               and b["safety"]["citation_valid"] for b, o in sides):
        return False
    loop = report["repeated_reads_scenario"]["comparison"]
    return bool(loop["safety_status_identical"] and loop["honest_incomplete_response_both"])


if __name__ == "__main__":
    raise SystemExit(main())
