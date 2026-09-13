"""必要安全检查：不经过模型、不经过 planner 的确定性执行路径。

主线要求"必要安全检查始终先执行，不能依赖 Agent 是否选中某个工具"。在改动之前，
用药变化只会*使旧结论失效*并排一条 `dependency_tasks` 重查；**第一次**出现的药物组合
没有任何检查会跑，除非模型恰好决定调用 `ddi_check`。此外重查的 hook 由
`MedicationCoordinatorAgent.__init__` 安装，agent 一旦构造不出来（provider 不可用、
配置缺失），重查就停在 `no_hook`，什么都检查不了。

本模块把检查本身变成代码：

* ``condition_warnings`` / ``recheck_ddi`` / ``recheck_condition``——从 agent 里搬出来的
  确定性检测与推导。agent 原来的方法改为委托，两条路径只有一份实现。
* ``deterministic_recheck``——不依赖 agent 实例的 recheck hook，没有模型时也能跑。
* ``run_necessary_checks``——消费 ``necessary_checks`` 队列：用药集合变化后按**当前**
  药单重新做一次相互作用检查，把新组合的提示记成结论，并更新对应安全事项。

模型在这里没有任何位置：输出是检测器的确定性结果，严重程度不会被模型改写或降低。
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from .memory import _as_utc, utc_now

ROOT = Path(__file__).resolve().parent

TRIGGER_MEDICATION_SET = 'medication_set'
TRIGGER_CONDITION_FACTS = 'condition_facts'

LEASE_TTL_SECONDS = 300
MAX_ATTEMPTS = 3
MAX_JOBS = 4

_SEVERITY_SALIENCE = {"contraindicated": 1.0, "major": 0.98, "moderate": 0.82,
                      "minor": 0.55, "unknown": 0.9}


# ---- 确定性检测与推导（自 agent.py 搬入，唯一实现） ---------------------------
def warning_text(warning: dict[str, Any]) -> str:
    """结论文本口径。``_derive_conclusion_deps_tx`` 从"："前的 ``a×b`` 解析药物对，
    所以这里不能改动分隔符。"""
    pair = f"{warning.get('drug_a')}×{warning.get('drug_b')}"
    effect = warning.get("effect") or "存在需要核实的用药风险"
    return f"{pair}：{effect}（{warning.get('severity', 'unknown')} / {warning.get('confidence', 'unknown')}）"


def warning_source_refs(warning: dict[str, Any]) -> list[dict[str, Any]]:
    """来源引用。没有 uri 的提示按既有口径标为 low confidence——安全层据此强制升级，
    绝不因为"检测到了"就当成有依据。"""
    uri = warning.get("source_url")
    if uri:
        primary = {"source_type": "drug_label_or_kegg", "uri": uri,
                   "quote": warning.get("source_text"),
                   "retrieval": warning.get("detection_path", "rag")}
        if warning.get("evidence_id"):
            primary["evidence_id"] = warning["evidence_id"]
        return [primary, *warning.get("additional_sources", [])]
    warning["confidence"] = "low"
    return [{
        "source_type": "local_detector_provenance",
        "uri": str((ROOT / "data" / "ddi_pair_index.json").resolve()),
        "quote": None,
        "retrieval": warning.get("detection_path", "local_pair_index"),
    }]


def condition_warnings(
    medication: str,
    critical_facts: Sequence[dict[str, Any]],
    current_medications: Sequence[dict[str, Any]],
    rag_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """说明书注意事项 × 结构化患者事实的确定性匹配（原 ``AgentPlanner._condition_warnings``）。"""
    warnings: list[dict[str, Any]] = []
    condition_added = False
    for result in rag_result.get("results", []):
        text = result.get("text") or ""
        if medication.lower() not in (result.get("drug_name") or "").lower():
            continue
        matched: list[str] = []
        severity = "moderate"
        for fact in critical_facts:
            namespace = fact["namespace"]
            value = fact["value"]
            if namespace == "allergy":
                allergen = value.get("allergen") if isinstance(value, dict) else str(value)
                if allergen and allergen in text and "过敏" in text:
                    matched.append(f"过敏史:{allergen}")
                    severity = "major" if "禁用" in text else "moderate"
            elif namespace == "renal_function" and any(word in text for word in ("肾功能不全", "肝肾功能不全", "肾功能受损")):
                matched.append(f"肾功能:{value}")
            elif namespace == "hepatic_function" and any(word in text for word in ("肝功能不全", "肝肾功能不全", "肝功能受损")):
                matched.append(f"肝功能:{value}")
            elif namespace == "age" and isinstance(value, (int, float)) and value >= 60 and "60岁以上" in text:
                matched.append(f"年龄:{value}岁")
        if condition_added or not matched or not re.search(r"慎用|禁用|医师指导|风险|不良反应", text):
            continue
        warnings.append({
            "drug_a": medication,
            "drug_b": "患者个体风险",
            "severity": severity,
            "mechanism": "说明书注意事项与结构化患者事实匹配",
            "effect": "；".join(matched) + "，需由医生/药师复核适用性",
            "management": None,
            "source_text": text,
            "source_url": result.get("source_url"),
            "confidence": "medium" if result.get("source_url") else "low",
            "detection_path": rag_result.get("mode", "hybrid_bm25_bge"),
        })
        condition_added = True  # 一条最贴切的原文明引用足以支撑这条个体风险发现。

    # Stage 2 检测器仍是主要药物对检测器。若它没有直接的阿司匹林×布洛芬记录，
    # 这里仍可从检索到的布洛芬说明书里注意到"与其他解热镇痛抗炎药同用"的类别警示。
    # 它被明确标为类别推断（低置信度），绝不表述成直接的药物对断言——因此会触发
    # 反思分支，而不是被当作有依据的结论。
    current_names = {item.get("display_name") for item in current_medications}
    if medication == "布洛芬" and "阿司匹林" in current_names:
        for result in rag_result.get("results", []):
            text = result.get("text") or ""
            if "其他解热、镇痛、抗炎药物同用" not in text or not any(
                    word in text for word in ("胃肠道不良反应", "溃疡", "出血")):
                continue
            warnings.append({
                "drug_a": "阿司匹林",
                "drug_b": "布洛芬",
                "severity": "major" if "出血" in text else "moderate",
                "mechanism": "布洛芬说明书类别警示与当前阿司匹林记录的保守匹配（非直接药物对证据）",
                "effect": "胃肠道不良反应/溃疡风险可能叠加；是否构成出血风险需医生/药师核实",
                "management": None,
                "source_text": text,
                "source_url": result.get("source_url"),
                "confidence": "low",
                "detection_path": f"{rag_result.get('mode', 'hybrid_bm25_bge')}+class_inference",
                "additional_sources": [{
                    "source_type": "agent_inference_disclosure",
                    "uri": str((ROOT / "agent.py").resolve()),
                    "quote": "类别匹配，不是说明书直接点名阿司匹林×布洛芬",
                    "retrieval": "reflection_required",
                }],
            })
            break
    return warnings


# ---- 重查执行器（无 agent 也能跑） -------------------------------------------
def recheck_ddi(memory, conclusion: dict[str, Any], *, detector) -> dict[str, Any] | None:
    """按**当前**药单重新运行真实检测器。

    检出与原结论同一药物对的提示 → 新的结论版本；没有检出 → 返回 None，由 memory 层
    记录保守的"未检出"结论——**绝不**表述成风险解除。
    """
    medications = [item["display_name"] for item in memory.current_medications()]
    if detector is None or not medications:
        return None
    warnings = findings_of(detector(medications))
    old_text = conclusion.get("text", "")
    related = [warning for warning in warnings
               if warning.get("drug_a") in old_text or warning.get("drug_b") in old_text]
    if not related:
        return None
    lines = [f"重查完成：按当前已记录药单（{'、'.join(medications)}）重新检查，"
             f"仍检出 {len(related)} 条与原结论相关的相互作用提示："]
    for warning in related:
        lines.append(f"- {warning.get('drug_a')} × {warning.get('drug_b')}"
                     f"（{warning.get('severity')}）：{warning.get('effect')}")
    lines.append("以上为按当前记录重新检查的结果；未检出的其他风险不因此排除，"
                 "用药调整请咨询医生/药师。")
    return {
        "text": "\n".join(lines),
        "memory_refs": list(conclusion.get("memory_refs", [])),
        "source_refs": [{"uri": warning.get("source_url"), "text": warning.get("source_text")}
                        for warning in related],
    }


def recheck_condition(memory, conclusion: dict[str, Any], *, rag_tool,
                      critical_facts_fn=None) -> dict[str, Any] | None:
    """按当前患者事实重新推导个体风险（原 ``agent._recheck_condition``）。"""
    focus: str | None = None
    for ref in conclusion.get("memory_refs", []):
        if isinstance(ref, str) and ref.startswith("memory:medication:"):
            try:
                focus = memory.resolve_ref(ref)["row"]["display_name"]
            except ValueError:
                continue
            break
    if not focus or rag_tool is None:
        return None
    snapshot = memory.snapshot()
    context = {"semantic": snapshot.get("semantic", []),
               "medications": snapshot.get("medications", [])}
    if critical_facts_fn is None:
        from .agent import PlannerPolicyGuard
        critical_facts_fn = PlannerPolicyGuard()._critical_facts
    try:
        rag_result = rag_tool(f"{focus} 注意事项 禁忌 慎用", drug_name=focus, top_k=5)
    except Exception:
        return None
    warnings = condition_warnings(focus, critical_facts_fn(context),
                                  context.get("medications", []), rag_result)
    if not warnings:
        return None
    lines = [f"重查完成：按当前已记录的患者事实重新核对 {focus} 的注意事项，"
             f"仍检出 {len(warnings)} 条与原结论相关的个体风险提示："]
    for warning in warnings:
        lines.append(f"- {warning.get('drug_a')}×{warning.get('drug_b')}"
                     f"（{warning.get('severity')}）：{warning.get('effect')}")
    lines.append("以上为按当前记录重新检查的结果；未检出的其他风险不因此排除，"
                 "用药调整请咨询医生/药师。")
    return {
        "text": "\n".join(lines),
        "memory_refs": list(conclusion.get("memory_refs", [])),
        "source_refs": [{"uri": warning.get("source_url"), "text": warning.get("source_text")}
                        for warning in warnings],
    }


def deterministic_recheck(memory, conclusion: dict[str, Any], *, detector=None,
                          rag_tool=None) -> dict[str, Any] | None:
    """不依赖 agent 实例的 recheck hook。

    与 agent 的 hook 走同一套 memory 层的租约、去重、后继结论与审计；区别只是它
    不需要一个构造好的 planner/provider——所以 provider 挂掉时检查照样执行。
    """
    if detector is None:
        from .agent import DDITool
        detector = DDITool()
    text = conclusion.get("text", "")
    if conclusion.get("kind") == "condition_warning" or (
            conclusion.get("kind") == "warning" and "患者个体风险" in text):
        return recheck_condition(memory, conclusion, rag_tool=rag_tool)
    return recheck_ddi(memory, conclusion, detector=detector)


# ---- 新组合：按当前药单做一次完整检查 ----------------------------------------
def findings_of(result: Any) -> list[dict[str, Any]]:
    """把一个检测器结果里的警示取出来，**两种口径都认**。

    检测器有两种既有形态：`DDITool(...)` 返回 `{'warnings': [...]}`，而
    `ddi_engine.detect` 风格的裸函数返回一个列表。只认前者会让一个返回列表的检测器
    被**静默**当成"没有检出"——那正是最危险的读法：把"没检查"说成"没问题"。
    认不出的形态按异常处理，绝不降级为空结果。
    """
    if result is None:
        return []
    if isinstance(result, dict):
        warnings = result.get("warnings")
        return list(warnings) if isinstance(warnings, list) else []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    raise TypeError(f'unrecognised detector result shape: {type(result).__name__}')


def current_findings(memory, *, detector) -> dict[str, Any]:
    """对当前药单运行确定性相互作用检测。返回结果与检出到的警示。"""
    medications = [item["display_name"] for item in memory.current_medications()]
    if detector is None or len(medications) < 2:
        return {"warnings": [], "medications": medications}
    return {"warnings": findings_of(detector(medications)), "medications": medications}


def pair_key(*names: str) -> str:
    """药物对的归一化身份：排序拼接，与药名在句子里出现的先后无关。"""
    return "|".join(sorted(_key(name) for name in names))


def _pair_of(text: str) -> str | None:
    head = str(text or "").split("：", 1)[0]
    if "×" not in head:
        return None
    left, _, right = head.partition("×")
    return pair_key(left, right)


def _row_to_conclusion(row) -> dict[str, Any]:
    item = dict(row)
    item["memory_refs"] = _loads(item.pop("memory_refs_json", None), [])
    item["source_refs"] = _loads(item.pop("source_refs_json", None), [])
    item["ref"] = f"memory:conclusion:{item['id']}@v1"
    return item


def current_pair_conclusions(memory, pairs: set[str]) -> list[dict[str, Any]]:
    """当前结论里表达了这些药物对的那几条。

    刻意读**全部** current 结论而不只是本次新记的：一条提示可能由交互路径（模型
    选中 ddi_check）先写下来。安全事项必须由**检查**收敛出来，不能取决于这一次是
    谁先写的结论——否则同一个风险有没有卡片，会随模型的选择而变。
    """
    out = []
    for row in memory.connection.execute(
            "SELECT * FROM conclusions WHERE status='current' AND kind IN ('warning','condition_warning')"):
        item = dict(row)
        if _pair_of(item["text"]) in pairs:
            item["memory_refs"] = _loads(item.pop("memory_refs_json", None), [])
            item["source_refs"] = _loads(item.pop("source_refs_json", None), [])
            item["ref"] = f"memory:conclusion:{item['id']}@v1"
            out.append(item)
    return out


def _loads(raw, default):
    import json
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _current_pair_keys(memory) -> set[str]:
    """已经在 `current` 结论里表达过的药物对。"""
    return {key for key in (_pair_of(row["text"]) for row in memory.connection.execute(
        "SELECT text FROM conclusions WHERE status='current' "
        "AND kind IN ('warning','condition_warning')")) if key}


def _key(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).lower()


def record_new_findings(memory, warnings: Sequence[dict[str, Any]], *,
                        session_id: str, turn_id: str,
                        detector_ref: str) -> list[dict[str, Any]]:
    """把**新**药物对的提示记成结论。

    已经表达过的药物对不重复记——重复的卡片不是"更安全"，只是更吵。每条结论都
    带来源引用与依赖行，因此后续变化仍按既有机制失效重查。
    """
    seen = _current_pair_keys(memory)
    recorded: list[dict[str, Any]] = []
    # 一条结论、它的依赖行和它的审计要么一起落盘，要么都不落——半条结论比没有
    # 结论更危险，因为界面会显示一个查不到依据的提示。
    with memory._lock, memory.connection:
        for warning in warnings:
            pair = pair_key(warning.get("drug_a"), warning.get("drug_b"))
            if pair in seen:
                continue
            seen.add(pair)
            memory_refs = [ref for ref in (warning.get("memory_refs") or []) if ref]
            if not memory_refs:
                memory_refs = [item["ref"] for item in memory.current_medications()
                               if _key(item["display_name"]) in pair.split("|")]
            if not memory_refs:
                continue
            conclusion = memory._record_conclusion_tx(
                session_id=session_id, turn_id=turn_id, kind="warning",
                text=warning_text(warning), memory_refs=memory_refs,
                source_refs=warning_source_refs(warning))
            memory._audit('necessary_check_finding', 'conclusion', conclusion['id'],
                          {'detector': detector_ref, 'pair': pair,
                           'severity': warning.get('severity')}, 'necessary_check')
            recorded.append(conclusion)
    return recorded


# ---- 队列：necessary_checks --------------------------------------------------
def _lease_ttl() -> int:
    try:
        return max(1, int(os.getenv("NECESSARY_CHECK_LEASE_TTL_SECONDS", str(LEASE_TTL_SECONDS))))
    except ValueError:
        return LEASE_TTL_SECONDS


def _recover_expired(memory) -> int:
    """崩溃的 worker 留下过期的租约：记一次尝试并放回队列（三次后判失败）。

    与 `dependency_tasks` 同一口径——失败也保持可见，不会被静默丢掉。
    """
    now = utc_now()
    rows = memory.connection.execute(
        "SELECT id, attempts FROM necessary_checks WHERE status='running' "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at<?", (now,)).fetchall()
    for row in rows:
        attempts = int(row["attempts"] or 0) + 1
        memory.connection.execute(
            "UPDATE necessary_checks SET status=?, attempts=?, lease_token=NULL, "
            "lease_expires_at=NULL, updated_at=? WHERE id=?",
            ("failed" if attempts >= MAX_ATTEMPTS else "open", attempts, now, row["id"]))
        memory._audit("necessary_check_lease_expired", "necessary_check", row["id"],
                      {"attempts": attempts}, "necessary_check")
    return len(rows)


def enqueue(memory, *, trigger_kind: str, trigger_ref: str, subject_key: str,
            reason: str) -> None:
    """登记一次必要检查。**必须在调用方的事务里**执行——事件、事实与"要检查"这件事
    要么一起提交，要么一起回滚。"""
    now = utc_now()
    memory.connection.execute(
        """INSERT INTO necessary_checks(scope_id,trigger_kind,trigger_ref,subject_key,
             status,attempts,lease_token,lease_expires_at,reason,result_json,created_at,updated_at)
           VALUES('local-demo',?,?,?, 'open',0,NULL,NULL,?,NULL,?,?)
           ON CONFLICT(trigger_kind,trigger_ref,subject_key) DO NOTHING""",
        (trigger_kind, trigger_ref, subject_key, reason, now, now))
    # 失败过的检查要能被重新排上：同样的触发再发生时不再被历史失败行挡住。
    memory.connection.execute(
        "UPDATE necessary_checks SET status='open', attempts=0, lease_token=NULL, "
        "lease_expires_at=NULL, updated_at=? "
        "WHERE trigger_kind=? AND trigger_ref=? AND subject_key=? AND status='failed'",
        (now, trigger_kind, trigger_ref, subject_key))


def pending(memory) -> dict[str, int]:
    row = memory.connection.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        "FROM necessary_checks WHERE status IN ('open','running','failed')").fetchone()
    return {"unfinished": int(row["total"] or 0), "failed": int(row["failed"] or 0)}


def run_necessary_checks(memory, *, detector=None, rag_tool=None, evidence_store=None,
                         product=None, max_jobs: int = MAX_JOBS) -> dict[str, Any]:
    """消费必要检查队列。**纯代码路径**：不需要 planner、provider 或 agent 实例。

    ``product`` 给定时，每条新结论会同时收敛到它所属的安全事项上；不给定时检查
    照常执行并落盘结论，只是不建事项（离线诊断与单元测试用）。
    """
    if detector is None:
        from .agent import DDITool
        detector = DDITool()
    lease_ttl = _lease_ttl()
    with memory._lock, memory.connection:
        _recover_expired(memory)
        rows = [dict(row) for row in memory.connection.execute(
            "SELECT * FROM necessary_checks WHERE status='open' ORDER BY id LIMIT ?",
            (max_jobs,))]
    completed: list[dict[str, Any]] = []
    for row in rows:
        token = uuid.uuid4().hex
        expires = (_as_utc(None) + timedelta(seconds=lease_ttl)).isoformat(timespec="seconds")
        with memory._lock, memory.connection:
            claimed = memory.connection.execute(
                "UPDATE necessary_checks SET status='running', lease_token=?, "
                "lease_expires_at=?, updated_at=? WHERE id=? AND status='open'",
                (token, expires, utc_now(), row["id"]))
            if claimed.rowcount != 1:
                continue
        try:
            outcome = _execute(memory, row, detector=detector, rag_tool=rag_tool,
                               product=product)
        except Exception as exc:  # 检查失败绝不伪装成"检查过了"
            with memory._lock, memory.connection:
                attempts = int(row["attempts"] or 0) + 1
                memory.connection.execute(
                    "UPDATE necessary_checks SET status=?, attempts=?, lease_token=NULL, "
                    "lease_expires_at=NULL, updated_at=? WHERE id=?",
                    ("failed" if attempts >= MAX_ATTEMPTS else "open", attempts,
                     utc_now(), row["id"]))
            completed.append({"check_id": row["id"], "status": "error",
                              "error": f"{type(exc).__name__}: {exc}"})
            continue
        with memory._lock, memory.connection:
            memory.connection.execute(
                "UPDATE necessary_checks SET status='done', lease_token=NULL, "
                "lease_expires_at=NULL, result_json=?, updated_at=? WHERE id=?",
                (_pack(outcome), utc_now(), row["id"]))
            memory._audit("necessary_check_complete", "necessary_check", row["id"],
                          {"trigger_kind": row["trigger_kind"], "outcome": outcome},
                          "necessary_check")
        completed.append({"check_id": row["id"], **outcome})
    return {"status": "ok", "completed": completed, "pending": pending(memory)}


def _execute(memory, row: dict[str, Any], *, detector, rag_tool, product) -> dict[str, Any]:
    if row["trigger_kind"] == TRIGGER_MEDICATION_SET:
        findings = current_findings(memory, detector=detector)
        warnings = findings.get("warnings") or []
        record_new_findings(memory, warnings,
                            session_id="necessary-check", turn_id=f"check-{row['id']}",
                            detector_ref="ddi_engine.detect")
        # 收敛**全部**相关 current 结论，不只本次新记的那几条。
        pairs = {pair_key(w.get("drug_a"), w.get("drug_b")) for w in warnings}
        conclusions = current_pair_conclusions(memory, pairs) if pairs else []
        return _observe(product, memory, conclusions,
                        trigger={'kind': 'necessary_check', 'check_id': row['id'],
                                 'trigger': row['trigger_kind'], 'ref': row['trigger_ref']})
    if row["trigger_kind"] == TRIGGER_CONDITION_FACTS:
        _condition_findings(memory, rag_tool=rag_tool, turn_id=f"check-{row['id']}")
        # 同一条收敛规则：当前有效的个体风险结论都归到事项上，不管是谁先写的。
        conclusions = [_row_to_conclusion(r) for r in memory.connection.execute(
            "SELECT * FROM conclusions WHERE status='current' AND kind='condition_warning'")]
        return _observe(product, memory, conclusions,
                        trigger={'kind': 'necessary_check', 'check_id': row['id'],
                                 'trigger': row['trigger_kind'], 'ref': row['trigger_ref']})
    raise ValueError(f"unknown necessary-check trigger: {row['trigger_kind']}")


def _condition_findings(memory, *, rag_tool, turn_id: str) -> list[dict[str, Any]]:
    """患者事实变化后的必要检查：对当前用药逐条重做说明书×事实匹配。

    没有可用的检索工具时**不做**推断：不跑就是没跑过，绝不产出"没发现问题"。
    """
    if rag_tool is None:
        return []
    from .agent import PlannerPolicyGuard
    guard = PlannerPolicyGuard()
    snapshot = memory.snapshot()
    context = {"semantic": snapshot.get("semantic", []),
               "medications": snapshot.get("medications", [])}
    critical = guard._critical_facts(context)
    if not critical:
        return []
    seen = _current_pair_keys(memory)
    recorded: list[dict[str, Any]] = []
    for item in memory.current_medications():
        focus = item["display_name"]
        try:
            rag_result = rag_tool(f"{focus} 注意事项 禁忌 慎用", drug_name=focus, top_k=5)
        except Exception:
            continue
        for warning in condition_warnings(focus, critical, context["medications"], rag_result):
            pair = "|".join(sorted([_key(focus), _key("患者个体风险")]))
            if pair in seen:
                continue
            with memory._lock, memory.connection:
                conclusion = memory._record_conclusion_tx(
                    session_id="necessary-check", turn_id=turn_id, kind="condition_warning",
                    text=warning_text(warning), memory_refs=[item["ref"]],
                    source_refs=warning_source_refs(warning))
                memory._audit('necessary_check_finding', 'conclusion', conclusion['id'],
                              {'detector': 'condition_warnings', 'pair': pair,
                               'severity': warning.get('severity')}, 'necessary_check')
            seen.add(pair)
            recorded.append(conclusion)
    return recorded


def _observe(product, memory, conclusions, *, trigger) -> dict[str, Any]:
    """把新结论收敛到安全事项。没有 product（离线诊断）时跳过建事项，但如实说明。"""
    if product is None:
        return {"conclusions": [c["id"] for c in conclusions],
                "cases": [], "cases_skipped": 'no_product'}
    from .safety_cases import observe_conclusion
    cases = []
    for index, conclusion in enumerate(conclusions):
        case = observe_conclusion(
            product, conclusion,
            evidence_refs=[ref.get('evidence_id') for ref in conclusion.get('source_refs') or ()
                           if isinstance(ref, dict) and ref.get('evidence_id')],
            trigger=trigger, key=f"necessary-check:{trigger['check_id']}:{index}")
        if case['id'] not in cases:
            cases.append(case['id'])
    return {"conclusions": [c["id"] for c in conclusions], "cases": cases,
            "cases_skipped": None}


def _pack(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
