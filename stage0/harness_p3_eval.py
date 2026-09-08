"""Harness P3 experiment: single main agent vs ordinary read-only batching vs
delegated read-only workers, on fixed synthetic scenarios.

Pre-registered acceptance criteria (P3 prompt §一.2 — stated BEFORE running):

* GAIN   : the candidate must cut planner decision calls by >=50% on the
           multi-source retrieval scenario (and >=25% on the long-label
           consistency scenario), with safety / citation conclusions
           IDENTICAL to the single-agent baseline.
* CONTEXT: the delegated worker must additionally show a planner-payload
           context advantage over plain batching on the evidence-heavy
           scenario — otherwise batching alone solves the bottleneck and
           delegation stays default-OFF ("未证实收益").
* COST   : tokens_charged overhead vs baseline <= +20%.
* SIMPLE : the simple direct query must perform ZERO delegations.

Honest-measurement contract: ALL providers are scripted/fake and the workers
are deterministic read-only pipelines (no real model inside the worker —
worker_model=None).  Wall-clock numbers are MODELED: synthetic per-decision
and per-retrieval latencies (env STAGE0_P3_SIM_PLANNER_MS / _IO_MS) stand in
for the real planner (18-60s per call measured in Stage 6) and RAG costs.
The numbers prove FLOW and resource behaviour, never real-model quality.

Usage:
    python stage0/harness_p3_eval.py --out docs/harness-upgrade/P3/experiment_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from stage0.agent import CareEvent
from stage0.harness_eval import FakeRAG, make_agent
from stage0.memory import MemoryStore

DATASET_VERSION = "p3-1.1-equivalent-coverage"
PLANNER_MS = float(os.getenv("STAGE0_P3_SIM_PLANNER_MS", "300"))
IO_MS = float(os.getenv("STAGE0_P3_SIM_IO_MS", "50"))
CHRONIC_DRUGS = ["氨氯地平", "辛伐他汀", "二甲双胍", "氯吡格雷"]
FIFTH_DRUG = "阿司匹林"
PAGE = 2000

LABEL_TEMPLATE = ("【合成说明书】{drug} 说明书。适应症部分：本品用于治疗慢性疾病，需遵医嘱使用。"
                  "药理毒理部分：合成段落，重复填充用于模拟较长标签。{filler}"
                  "不良反应部分：常见不良反应包括头晕、外周水肿、胃肠不适，出现不适应及时就医。")


def _long_chunks(drug: str) -> list[dict]:
    """~4k-char labels whose relevant paragraph sits at the END (the P1-B
    select_excerpt target case)."""
    chunks = []
    for section in ("不良反应", "禁忌", "药物相互作用"):
        filler = "。".join([f"{drug}{section}合成段落{i}：常规监测与随访事项。" for i in range(170)])
        chunks.append({
            "text": LABEL_TEMPLATE.format(drug=drug, filler=filler),
            "source_url": f"https://example.test/{drug}/{section}",
            "drug_name": drug, "section": section,
        })
    return chunks


def _sleep_ms(ms: float) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


class PayloadMeter:
    """Records planner payload sizes at each decision (context-cost metric)."""

    def __init__(self):
        self.sizes: list[int] = []
        self.reads: list[dict] = []

    def __call__(self, payload):
        try:
            self.sizes.append(len(json.dumps(payload, ensure_ascii=False, default=str)))
        except Exception:
            self.sizes.append(-1)
        return None


class _MeteredRAG:
    """FakeRAG + a real dispatch counter + modeled per-call I/O latency."""

    def __init__(self, chunks):
        self._inner = FakeRAG(chunks)
        self.dispatches = 0
        self.queries: list[str] = []

    def __call__(self, query, **kwargs):
        self.dispatches += 1
        self.queries.append(query)
        _sleep_ms(IO_MS)
        return self._inner(query, **kwargs)


# ---- scripted planner proposals ----------------------------------------------
# All scripts: consolidate FIRST (the guard blocks respond until the CareEvent
# is consolidated), then the mode's read strategy, then respond.  A scripted
# provider reading ids/claims out of its own payload mirrors what a real model
# would read from its observations.


def _observations(payload) -> list[dict]:
    return [obs for obs in payload.get("observations") or [] if isinstance(obs, dict)]


def _has_consolidation(payload) -> bool:
    return any(obs.get("tool") == "memory_write" and obs.get("ok")
               for obs in _observations(payload))


def _consolidate():
    return {"decision": "tool", "tool": "memory_write", "purpose": "consolidate",
            "arguments": {"operation": "consolidate_event"}}


def _last_evidence_ids(payload) -> list[str]:
    ids: list[str] = []
    for observation in reversed(_observations(payload)):
        result = observation.get("result")
        if isinstance(result, dict):
            for view in result.get("evidence") or []:
                if view.get("evidence_id"):
                    ids.append(view["evidence_id"])
        if ids:
            break
    return ids


def _respond():
    return {"decision": "respond", "rationale": "evidence sufficient"}


def _script_multi_single(payload, queries, **kwargs):
    """Baseline: ONE planner decision + ONE dispatch per query."""
    if not _has_consolidation(payload):
        return _consolidate()
    done = {obs.get("purpose") for obs in _observations(payload)
            if obs.get("tool") == "rag_search" and obs.get("ok")}
    remaining = [q for q in queries if q not in done]
    if remaining:
        return {"decision": "tool", "tool": "rag_search", "purpose": remaining[0],
                "arguments": {"query": remaining[0], "top_k": 3}}
    return _respond()


def _script_multi_batch(payload, queries, **kwargs):
    """Control: ONE batch proposal fans all pure reads out on the shared
    execution layer."""
    if not _has_consolidation(payload):
        return _consolidate()
    if not any(obs.get("tool") == "batch_read" and obs.get("ok")
               for obs in _observations(payload)):
        return {"decision": "tool", "tool": "batch_read", "purpose": "collect_labels",
                "arguments": {"calls": [{"tool": "rag_search",
                                         "arguments": {"query": q, "top_k": 3}}
                                        for q in queries]}}
    return _respond()


def _script_multi_delegate(payload, queries, **kwargs):
    """Treatment: ONE delegation proposal; a read-only worker collects."""
    if not _has_consolidation(payload):
        return _consolidate()
    if not any(obs.get("tool") == "delegate_task" and obs.get("ok")
               for obs in _observations(payload)):
        return {"decision": "tool", "tool": "delegate_task", "purpose": "collect_labels",
                "arguments": {"role": "evidence_retriever", "goal": "收集当前用药的标签证据",
                              "queries": queries}}
    return _respond()


def _long_calls(payload, plan_state):
    if "calls" not in plan_state:
        views = [view for obs in _observations(payload)
                 for view in (obs.get("result") or {}).get("evidence", [])]
        if views:
            plan_state["calls"] = [{"tool": "read_evidence", "arguments": {
                "evidence_id": view["evidence_id"], "offset": offset, "limit": PAGE}}
                for view in views for offset in range(0, view["total_chars"], PAGE)]
    return plan_state.get("calls", [])


def _script_long_single(payload, queries, claims, plan_state, **kwargs):
    """Baseline: read the long evidence back PAGE BY PAGE in the main loop.

    The evidence id is remembered from the first page decision (where the rag
    observation was recent enough to be un-compressed) — mirroring what a real
    model does, since its own earlier proposals carry the id."""
    if not _has_consolidation(payload):
        return _consolidate()
    if not any(obs.get("tool") == "rag_search" and obs.get("ok")
               for obs in _observations(payload)):
        return {"decision": "tool", "tool": "rag_search", "purpose": queries[0],
                "arguments": {"query": queries[0], "top_k": 3}}
    calls = _long_calls(payload, plan_state)
    cursor = plan_state.get("cursor", 0)
    if cursor < len(calls):
        plan_state["cursor"] = cursor + 1
        return {"decision": "tool", "purpose": f"page{cursor}", **calls[cursor]}
    return _respond()


def _script_long_batch(payload, queries, claims, plan_state, **kwargs):
    """Control: collect with rag, then ONE batch read-back."""
    if not _has_consolidation(payload):
        return _consolidate()
    if not any(obs.get("tool") == "rag_search" and obs.get("ok")
               for obs in _observations(payload)):
        return {"decision": "tool", "tool": "rag_search", "purpose": queries[0],
                "arguments": {"query": queries[0], "top_k": 3}}
    calls = _long_calls(payload, plan_state)
    cursor = plan_state.get("cursor", 0)
    if cursor < len(calls):
        plan_state["cursor"] = cursor + 3
        return {"decision": "tool", "tool": "batch_read", "purpose": "read_back",
                "arguments": {"calls": calls[cursor:cursor + 3]}}
    return _respond()


def _script_long_delegate(payload, queries, claims, **kwargs):
    """Treatment: ONE delegation; a read-only checker pages through the
    evidence and verifies claims verbatim, returning verdicts only."""
    if not _has_consolidation(payload):
        return _consolidate()
    if not any(obs.get("tool") == "rag_search" and obs.get("ok")
               for obs in _observations(payload)):
        return {"decision": "tool", "tool": "rag_search", "purpose": queries[0],
                "arguments": {"query": queries[0], "top_k": 3}}
    ids = _last_evidence_ids(payload)
    if ids and not any(obs.get("tool") == "delegate_task" and obs.get("ok")
                       for obs in _observations(payload)):
        return {"decision": "tool", "tool": "delegate_task", "purpose": "verify_claims",
                "arguments": {"role": "evidence_consistency_checker",
                              "goal": "核对标签证据与声明的逐字一致性",
                              "claims": claims, "evidence_refs": ids}}
    return _respond()


def _script_simple(payload, queries=None, claims=None, **kwargs):
    if not _has_consolidation(payload):
        return _consolidate()
    if not any(obs.get("tool") == "memory_read" and obs.get("ok")
               for obs in _observations(payload)):
        return {"decision": "tool", "tool": "memory_read", "purpose": "current_meds",
                "arguments": {"query": "current_medications"}}
    return _respond()


SCRIPTS = {
    ("multi_drug_labels", "single_agent"): _script_multi_single,
    ("multi_drug_labels", "batch"): _script_multi_batch,
    ("multi_drug_labels", "delegate"): _script_multi_delegate,
    ("long_label_consistency", "single_agent"): _script_long_single,
    ("long_label_consistency", "batch"): _script_long_batch,
    ("long_label_consistency", "delegate"): _script_long_delegate,
    ("simple_current_meds", "single_agent"): _script_simple,
    ("simple_current_meds", "batch"): _script_simple,
    ("simple_current_meds", "delegate"): _script_simple,
}


# ---- scenario runners -----------------------------------------------------------


def _run_turn(scenario: str, mode: str, *, rag, store: MemoryStore,
              turn_id: str, event: CareEvent, queries: list[str],
              claims: list[str] | None = None) -> tuple:
    script = SCRIPTS[(scenario, mode)]
    meter = PayloadMeter()
    plan_state = {}

    def provider(payload):
        meter(payload)
        _sleep_ms(PLANNER_MS)
        return script(payload, queries=queries, claims=claims or [], plan_state=plan_state)

    agent = make_agent(store, provider=provider, rag_tool=rag)
    read = agent.evidence_store.read

    def measured_read(*args, **kwargs):
        result = read(*args, **kwargs)
        meter.reads.append(dict(result))
        return result

    agent.evidence_store.read = measured_read
    started = time.perf_counter()
    response = agent.handle(event, session_id="s", turn_id=turn_id)
    wall = time.perf_counter() - started
    tokens = (store.workflow_run_get(turn_id) or {}).get("budget", {}).get("tokens_charged", 0)
    subtasks = [json.loads(row["result_json"]) for row in
                store.connection.execute("SELECT result_json FROM delegated_tasks").fetchall()]
    return response, meter, wall, tokens, subtasks


def _read_back_pages(response, subtasks) -> int:
    """Underlying read_evidence executions regardless of mode: main-loop acts,
    batch items (from the OBSERVE entry — act entries carry arguments only),
    and worker dispatches (from the persisted result payload)."""
    pages = 0
    for entry in response.tool_trace:
        if entry.get("phase") == "act" and entry.get("tool") == "read_evidence":
            pages += 1
        if entry.get("phase") == "observe" and entry.get("tool") == "batch_read":
            result = entry.get("observation", {}).get("result") or {}
            pages += sum(1 for item in result.get("results", [])
                         if isinstance(item, dict) and item.get("tool") == "read_evidence")
    for task in subtasks:
        # only the checker's dispatches are read_evidence page reads; the
        # retriever's are rag_search calls, counted by rag_dispatches
        if task.get("role") == "evidence_consistency_checker":
            pages += int(task.get("budget", {}).get("dispatches", 0))
    return pages


def scenario_multi_drug(mode: str) -> dict:
    """Complex #1: multi-source label re-retrieval — one query per current
    medication (4 chronic + the newly added drug)."""
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "memory.db")
        for drug in CHRONIC_DRUGS:
            store.apply_medication_change(action="add", name=drug, ingredients=None,
                                          session_id="s", turn_id=f"seed-{drug}",
                                          source="p3-eval")
        rag = _MeteredRAG(_long_chunks(FIFTH_DRUG)[:2])
        queries = [f"{drug} 不良反应 禁忌" for drug in (*CHRONIC_DRUGS, FIFTH_DRUG)]
        response, meter, wall, tokens, subtasks = _run_turn(
            "multi_drug_labels", mode, rag=rag, store=store,
            turn_id=f"p3-multi-{mode}",
            event=CareEvent("medication_change", f"新增{FIFTH_DRUG}。",
                            {"action": "add", "medication": FIFTH_DRUG}),
            queries=queries)
        store.close()
    planned_coverage = len(set(rag.queries) & set(queries)) / max(1, len(set(queries)))
    return _collect(mode, "multi_drug_labels", response, meter, wall, tokens,
                    rag.dispatches, subtasks,
                    extra={"planned_queries": len(queries),
                           "required_check_completion": round(planned_coverage, 4)})


def scenario_long_label(mode: str) -> dict:
    """Complex #2: consistency verification over LONG labels — the evidence
    must be read back (paging) and its claims verified verbatim."""
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "memory.db")
        chunks = _long_chunks(FIFTH_DRUG)
        claims = []
        for chunk in chunks:
            sentence = chunk["text"].rsplit("。", 1)[0]
            start = sentence.rfind("不良反应部分：")
            claims.append(sentence[start:] if start >= 0 else sentence[-40:])
        rag = _MeteredRAG(chunks)
        queries = [f"{FIFTH_DRUG} 不良反应 禁忌 相互作用"]
        response, meter, wall, tokens, subtasks = _run_turn(
            "long_label_consistency", mode, rag=rag, store=store,
            turn_id=f"p3-long-{mode}",
            event=CareEvent("medication_change", f"新增{FIFTH_DRUG}。",
                            {"action": "add", "medication": FIFTH_DRUG}),
            queries=queries, claims=claims)
        store.close()
    delegated = subtasks
    verdicts = [c for task in delegated for c in task.get("claims", [])]
    coverage = _full_read_coverage(meter.reads, chunks)
    verdicts_ok = coverage == 1.0 and (mode != "delegate" or (
        len(verdicts) == len(claims) and all(c.get("verdict") == "verified" for c in verdicts)))
    return _collect(mode, "long_label_consistency", response, meter, wall, tokens,
                    rag.dispatches, delegated,
                    extra={"planned_queries": len(queries),
                           "required_check_completion": 1.0 if verdicts_ok else 0.0,
                           "full_read_coverage": coverage,
                           "claims_total": len(claims),
                           "claims_verdicts_valid": verdicts_ok,
                           "claims_verified": sum(1 for c in verdicts
                                                  if c["verdict"] == "verified")})


def _full_read_coverage(reads: list[dict], chunks: list[dict]) -> float:
    """All modes must retrieve every byte of every requested synthetic label."""
    complete = 0
    for chunk in chunks:
        pages = {r["offset"]: r for r in reads if r.get("source_uri") == chunk["source_url"]}
        cursor, parts = 0, []
        while cursor in pages and pages[cursor].get("returned_chars", 0) > 0:
            page = pages[cursor]
            parts.append(page["content"])
            cursor += page["returned_chars"]
        complete += "".join(parts) == chunk["text"]
    return complete / max(1, len(chunks))


def scenario_simple(mode: str) -> dict:
    """Simple #1: a direct current-medication query — NO delegation expected."""
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "memory.db")
        rag = _MeteredRAG([])
        response, meter, wall, tokens, subtasks = _run_turn(
            "simple_current_meds", mode, rag=rag, store=store,
            turn_id=f"p3-simple-{mode}",
            event=CareEvent("register_profile", "询问当前用药。",
                            {"profile": {"age": 67}}),
            queries=[])
        store.close()
    return _collect(mode, "simple_current_meds", response, meter, wall, tokens,
                    rag.dispatches, subtasks, extra={"planned_queries": 0,
                                                     "required_check_completion": 1.0})


# ---- metric collection -----------------------------------------------------------


def _citation_validity(response) -> float:
    warnings = response.warnings or []
    if not warnings:
        return 1.0
    valid = sum(1 for w in warnings
                if w.get("citations") and (w.get("audit_trail") or {}).get("memory_refs"))
    return round(valid / len(warnings), 4)


def _collect(mode, scenario, response, meter, wall, tokens, rag_dispatches,
             subtasks, *, extra) -> dict:
    from stage0.harness_eval import MEDICAL_AUTHORITY
    plans = [e for e in response.tool_trace if e.get("phase") == "plan"]
    return {
        "mode": mode, "scenario": scenario,
        "safety_status": response.safety_status,
        "planner_decision_calls": len(plans),
        "planner_payload_chars": {
            "mean": round(statistics.mean(meter.sizes), 1) if meter.sizes else 0,
            "max": max(meter.sizes) if meter.sizes else 0,
            "decisions": len(meter.sizes)},
        "rag_dispatches": rag_dispatches,
        "read_back_pages": _read_back_pages(response, subtasks),
        "delegated_tasks": len(subtasks),
        "tokens_charged": tokens,
        "modeled_wall_seconds": round(wall, 3),
        "citation_validity": _citation_validity(response),
        "no_medical_authority": MEDICAL_AUTHORITY.search(response.text) is None,
        "open_questions": sum(len(t.get("open_questions") or []) for t in subtasks),
        **extra,
    }


# ---- acceptance evaluation (pre-registered) ---------------------------------------


def evaluate_adoption(results: list[dict]) -> dict:
    by = {(r["scenario"], r["mode"]): r for r in results}
    multi = {m: by[("multi_drug_labels", m)] for m in ("single_agent", "batch", "delegate")}
    long = {m: by[("long_label_consistency", m)] for m in ("single_agent", "batch", "delegate")}
    simple = {m: by[("simple_current_meds", m)] for m in ("single_agent", "batch", "delegate")}

    def planner_reduction(scenario_modes, mode):
        base = scenario_modes["single_agent"]["planner_decision_calls"]
        return round(1 - scenario_modes[mode]["planner_decision_calls"] / base, 4) if base else 0

    reduction = {"multi_drug_labels": {m: planner_reduction(multi, m) for m in ("batch", "delegate")},
                 "long_label_consistency": {m: planner_reduction(long, m) for m in ("batch", "delegate")}}

    safety_equal = all(
        scenario[m]["safety_status"] == "enforced" and scenario[m]["no_medical_authority"]
        and scenario[m]["citation_validity"] == 1.0
        for scenario in (multi, long, simple) for m in ("single_agent", "batch", "delegate"))
    checks_complete = all(r.get("required_check_completion") == 1.0 for r in results)
    cost_ok = all(
        scenario[m]["tokens_charged"] <= scenario["single_agent"]["tokens_charged"] * 1.2
        for scenario in (multi, long) for m in ("batch", "delegate"))
    simple_clean = all(simple[m]["delegated_tasks"] == 0 and simple[m]["safety_status"] == "enforced"
                       for m in ("single_agent", "batch", "delegate"))
    gain_ok = (all(v >= 0.5 for v in reduction["multi_drug_labels"].values())
               and all(v >= 0.25 for v in reduction["long_label_consistency"].values()))
    # CONTEXT criterion, two levels:
    # strict    — the delegated worker's planner payload is not larger than
    #             plain batching's on the evidence-heavy scenario;
    # material  — at least a 20% payload reduction vs batching.  A smaller
    #             margin does NOT justify the extra machinery: the decisive
    #             benefit of sub-agents (an independent worker LLM context
    #             window) cannot be demonstrated by a deterministic pipeline,
    #             and batching already achieves the same planner-call gain.
    payload_mean = {m: long[m]["planner_payload_chars"]["mean"]
                    for m in ("single_agent", "batch", "delegate")}
    context_strict = payload_mean["delegate"] <= payload_mean["batch"]
    context_material = payload_mean["batch"] > 0 and \
        payload_mean["delegate"] <= payload_mean["batch"] * 0.8

    verdict = {
        "gain_planner_reduction_met": gain_ok,
        "planner_reduction": reduction,
        "context_isolation_met_strict": context_strict,
        "context_isolation_material_20pct": context_material,
        "payload_mean_chars_long": payload_mean,
        "safety_citation_identical": safety_equal,
        "required_checks_complete": checks_complete,
        "cost_overhead_within_20pct": cost_ok,
        "simple_scenario_no_delegation": simple_clean,
    }
    verdict["adopt_delegation"] = bool(gain_ok and context_material and safety_equal and checks_complete
                                       and cost_ok and simple_clean)
    verdict["adopt_batching"] = bool(gain_ok and safety_equal and checks_complete and cost_ok and simple_clean)
    if verdict["adopt_delegation"]:
        verdict["delegation_conclusion"] = (
            "在等量完整证据回读的合成工作负载中达到离线候选门槛；"
            "不构成生产启用结论，STAGE0_DELEGATED_WORKERS 仍默认关闭，待真实模型与独立验证集验收。")
    if not verdict["adopt_delegation"]:
        verdict["delegation_conclusion"] = (
            "未证实相对普通批处理的实质性额外收益：STAGE0_DELEGATED_WORKERS 保持默认关闭。"
            "流程级收益（规划决策削减）与批处理相同；payload 隔离优势 "
            f"{round(1 - payload_mean['delegate'] / payload_mean['batch'], 3) if payload_mean['batch'] else 0} "
            "低于 20% 实质性阈值（P1-B 压缩已限制字符串体积）；worker 独立模型上下文这一"
            "核心卖点需要真实 worker LLM 才能验证——当前确定性流水线不声称该收益。")
    if verdict["adopt_batching"]:
        verdict["batching_conclusion"] = ("普通只读批处理满足收益标准（1 次规划决策完成多来源检索/回读），"
                                          "作为候选推荐（STAGE0_READ_BATCH 默认关闭，待 live 验证后灰度）；"
                                          "真实模型收益仍待 live 验证")
    return verdict


def run_experiment(repeat: int = 3) -> dict:
    saved = {k: os.environ.get(k) for k in ("STAGE0_DELEGATED_WORKERS", "STAGE0_READ_BATCH")}
    os.environ["STAGE0_DELEGATED_WORKERS"] = "1"
    os.environ["STAGE0_READ_BATCH"] = "1"
    try:
        scenarios = [scenario_multi_drug, scenario_long_label, scenario_simple]
        runs: dict[tuple, list[dict]] = {}
        for _ in range(max(1, repeat)):
            for scenario in scenarios:
                for mode in ("single_agent", "batch", "delegate"):
                    result = scenario(mode)
                    runs.setdefault((result["scenario"], result["mode"]), []).append(result)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    results = []
    for key, items in sorted(runs.items()):
        item = dict(items[len(items) // 2])
        # A failure in any repeat must survive aggregation.
        item["required_check_completion"] = min(i["required_check_completion"] for i in items)
        item["no_medical_authority"] = all(i["no_medical_authority"] for i in items)
        item["citation_validity"] = min(i["citation_validity"] for i in items)
        if any(i["safety_status"] != "enforced" for i in items):
            item["safety_status"] = "failed"
        item["modeled_wall_seconds_median"] = round(
            statistics.median(i["modeled_wall_seconds"] for i in items), 3)
        results.append(item)
    return {
        "dataset_version": DATASET_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modeled_latency": {"planner_ms": PLANNER_MS, "io_ms": IO_MS,
                            "note": "wall_seconds 为合成延迟模型，非真实系统耗时"},
        "pre_registered_criteria": {
            "gain": "planner 决策调用：多来源检索场景减少 >=50%，长标签一致性场景减少 >=25%；"
                    "安全/引用结论与单 Agent 基线一致",
            "context": "委派 worker 相对普通批处理的 payload 上下文优势需达到实质性水平"
                       "（>=20% 削减）才采用；低于该阈值视为'未证实相对批处理的额外收益'"
                       "（批处理已达成相同的规划决策削减）",
            "cost": "tokens_charged 开销 <= +20%",
            "simple": "简单查询零委派",
        },
        "results": results,
        "adoption": evaluate_adoption(results),
        "provider_note": ("全部 provider 为脚本化假 provider，worker 为确定性只读流水线"
                          "（worker_model=None）；仅证明流程与资源行为，不代表真实模型质量。"),
    }


def format_readable(report: dict) -> str:
    lines = [f"Harness P3 三方案对比实验（dataset {report['dataset_version']}，"
             f"{report['generated_at']}）", "=" * 76,
             "模式：single_agent=单主 Agent；batch=共享执行层只读批处理；delegate=只读专项 worker"]
    for scenario in ("multi_drug_labels", "long_label_consistency", "simple_current_meds"):
        lines.append(f"\n[场景] {scenario}")
        lines.append(f"  {'模式':<14}{'决策调用':>6}{'payload均值':>12}{'rag派发':>8}"
                     f"{'回读页':>6}{'tokens':>8}{'建模耗时s':>10}{'委派数':>6}{'安全':>10}")
        for r in report["results"]:
            if r["scenario"] != scenario:
                continue
            lines.append(f"  {r['mode']:<14}{r['planner_decision_calls']:>6}"
                         f"{r['planner_payload_chars']['mean']:>12.1f}{r['rag_dispatches']:>8}"
                         f"{r['read_back_pages']:>6}{r['tokens_charged']:>8}"
                         f"{r['modeled_wall_seconds']:>10.3f}{r['delegated_tasks']:>6}"
                         f"{r['safety_status']:>10}")
    adoption = report["adoption"]
    lines.append("\n[预注册标准判定]")
    lines.append(f"  planner 决策削减: {json.dumps(adoption['planner_reduction'], ensure_ascii=False)}")
    lines.append(f"  长标签场景 payload 均值: {adoption['payload_mean_chars_long']}")
    for key in ("gain_planner_reduction_met", "context_isolation_met_strict",
                "context_isolation_material_20pct",
                "safety_citation_identical", "cost_overhead_within_20pct",
                "required_checks_complete",
                "simple_scenario_no_delegation", "adopt_delegation", "adopt_batching"):
        lines.append(f"  {key}: {adoption[key]}")
    for key in ("delegation_conclusion", "batching_conclusion"):
        if key in adoption:
            lines.append(f"  结论: {adoption[key]}")
    lines.append("\n说明：假 provider + 确定性 worker，仅验证流程行为；耗时为合成延迟模型。")
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Harness P3 delegation experiment")
    parser.add_argument("--out", default="docs/harness-upgrade/P3/experiment_report.json")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    report = run_experiment(repeat=args.repeat)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".txt").write_text(format_readable(report), encoding="utf-8")
    print(format_readable(report))
    return 0 if (report["adoption"]["required_checks_complete"]
                 and report["adoption"]["safety_citation_identical"]
                 and report["adoption"]["simple_scenario_no_delegation"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
