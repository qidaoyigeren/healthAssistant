"""Conservative output checks, separate from the unchanged SafetyBoundary.

These checks are a bounded engineering defence, not a general proof that
arbitrary natural language is medically safe. Evaluation includes adversarial
examples and records rejected text rather than assuming zero unsafe output.
"""
from __future__ import annotations

import re
from typing import Any


REF = re.compile(r"memory:[a-z_]+:\d+(?:@v\d+)?")
URI = re.compile(r"https?://[^\s\]\[<>()（）“”，。；、！;,\"]+")
AUTHORITY = re.compile(
    r"(?:你|您|患者|她|他|老人).{0,4}(?:患有|得了|患上|就是|确实是|很可能是).{0,12}(?:病|炎|癌|感染|症)"
    r"|(?:诊断为|确诊为|确诊是|开具处方|开处方)"
    r"|(?:立即|马上|建议|推荐|应该|应当|可以|试试|不妨|请).{0,12}(?:停药|停用|停掉|服用|吃药|换药|换用|改用|加量|减量|加倍|减半|调整剂量|开始用药)"
    r"|(?:每天|每日|每晚|一次|一日|睡前|饭后).{0,8}(?:服|吃|用|片|粒|毫克|mg)"
    r"|(?:停用|服用|改用|换用|加量|减量|加倍|减半).{0,12}(?:药|素|片|胍|平|mg|毫克)"
    r"|\b(?:you|she|he|the patient)\s+(?:have|has|suffers from|probably has|likely has)\b"
    r"|\b(?:diagnosed with|diagnosis is|prescribe|start|stop|take|increase|decrease|double|halve|switch to)\b",
    re.I,
)


def composed_text_prescribes(text: str) -> bool:
    # Only explicit refusal/avoidance clauses are exempt. An unrelated 不 in
    # an earlier clause must not exempt a later imperative.
    text = URI.sub("", text)
    for refusal in (
        "我不能诊断、开药或自行建议停药/调整剂量",
        "我不能诊断、开药或建议停药/调整剂量",
        "我不能诊断、开药或建议调整剂量",
        "请勿自行停药或调整剂量", "不要自行停药或调整剂量",
    ):
        text = text.replace(refusal, "")
    clauses = re.split(r"[。；;!?！？\n]", text)
    for clause in clauses:
        clause = re.sub(r"(?:我)?(?:不能|无法|不会|不得)(?:诊断|确诊|开药|处方)", "", clause)
        clause = re.sub(r"(?:请勿|切勿|不要|不得|不应)(?:自行)?(?:停药|停用|调整|改变|加量|减量)", "", clause)
        clause = re.sub(r"不代表(?:必须|需要|应该)?(?:停药|停用|开始|调整)", "", clause)
        # Negated volitional statements (“也不会建议开始、停用或调整任何药物”)
        # are refusals, not directives; the negation must be adjacent to the
        # verb so a real directive keeps matching.
        clause = re.sub(r"(?:不会|不能|无法|并非)(?:建议|推荐|要求|指导)?[^。；，]{0,4}?(?:开始|停用|停药|服用|换药|改用|加量|减量|调整)[^。；]{0,12}", "", clause)
        clause = re.sub(r"\bI (?:cannot|can't|won't) diagnose or prescribe\b", "", clause, flags=re.I)
        clause = re.sub(r"\b(?:I (?:cannot|can't|won't)|do not|don't|never)\s+(?:diagnose|prescribe|start|stop|take|adjust)\b", "", clause, flags=re.I)
        if AUTHORITY.search(clause):
            return True
    return False


def check_composed_response(
    text: str, *, warnings: list[dict[str, Any]], escalation_required: bool,
    refusal_required: bool, memory_refs: list[str] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return ["empty_response"]
    if composed_text_prescribes(text):
        errors.append("medical_authority_content")
    if refusal_required and "不能诊断" not in text:
        errors.append("missing_refusal")
    if (refusal_required or escalation_required) and "建议咨询医生/药师" not in text:
        errors.append("missing_escalation")
    paragraphs = [line for line in text.splitlines() if line.strip()]
    allowed_refs = set(memory_refs or [])
    allowed_uris: set[str] = set()
    grounded: set[int] = set()
    for warning in warnings:
        citations = warning.get("citations") or []
        uris = {item["uri"] for item in citations if item.get("uri")}
        audit = warning.get("audit_trail", {})
        refs = set(audit.get("memory_refs", [])) | {audit.get("warning_memory", ""), audit.get("conclusion", "")}
        allowed_refs.update(refs)
        allowed_uris.update(uris)
        matched = False
        for index, line in enumerate(paragraphs):
            line_uris = set(URI.findall(line))
            # Local provenance paths are also supported by the existing detector.
            has_citation = bool(uris & line_uris) or any(not uri.startswith("http") and uri in line for uri in uris)
            has_pair = all(str(warning.get(key) or "<missing>") in line for key in ("drug_a", "drug_b"))
            has_ref = bool(refs & set(REF.findall(line)))
            has_effect = bool(warning.get("effect")) and warning["effect"] in line
            sanitized_summary = "：已记录警告。来源：" in line
            if has_pair and has_citation and has_ref and (has_effect or sanitized_summary):
                matched = True
                grounded.add(index)
                residue = line.replace(str(warning.get("effect", "")), "")
                for token in [*uris, *refs, str(warning.get("drug_a")), str(warning.get("drug_b"))]:
                    if token:
                        residue = residue.replace(token, "")
                if re.search(r"出血|致命|低血压|肾损伤|肝损伤|bleeding|fatal|kidney damage", residue, re.I):
                    errors.append("unsupported_warning_claim")
        if not matched:
            errors.append("warning_without_citation_or_memory:" + str(warning.get("drug_a")))
    for conflict in conflicts or []:
        refs = {conflict.get(key) for key in ("ref", "left_ref", "right_ref")} - {None}
        allowed_refs.update(refs)
        if not refs.issubset(set(REF.findall(text))):
            errors.append("conflict_missing_sides")
        if not any(word in text for word in ("未决", "待核实", "待确认")):
            errors.append("conflict_not_open")
    for index, line in enumerate(paragraphs):
        if index in grounded:
            continue
        if re.search(r"风险|警告|相互作用|出血|低血压|致命|肾损伤|肝损伤|risk|warning|interaction|bleeding", line, re.I):
            concrete_hazard = re.search(r"出血|低血压|致命|肾损伤|肝损伤|bleeding|fatal", line, re.I)
            # A reference to an already fully cited warning/conflict is not a
            # new warning: lines citing allowed memory refs summarize recorded
            # evidence, and escalation/disclaimer lines direct the user to
            # professionals instead of asserting new harm.  Concrete new harm
            # claims still need their own source.
            references_recorded_evidence = bool(allowed_refs & set(REF.findall(line)))
            summary = not concrete_hazard and (
                references_recorded_evidence
                or bool(warnings) and bool(re.search(r"(?:上述|这些)(?:风险提示|相互作用提示)|以上提示|鉴于存在需复核的风险提示", line))
                or bool(conflicts) and bool(re.search(r"说明书[^。；]{0,6}风险|风险证据|两侧|系统不判定|不静默覆盖|不会静默覆盖", line))
            )
            disclaimer = not concrete_hazard and re.search(
                r"这些是风险提示|不等于证明绝对安全|未形成带证据的新增警告|不能排除|无法排除"
                r"|需由医生/药师复核|不构成诊断|不能替代专业判断|仅基于已记录|供专业人员进行核对|并列呈现"
                r"|建议咨询医生/药师|提供给医生/药师",
                line,
            )
            if not summary and not disclaimer:
                errors.append("unrecorded_or_uncited_warning")
    if memory_refs is not None and set(REF.findall(text)) - allowed_refs:
        errors.append("fabricated_memory_ref")
    if set(URI.findall(text)) - allowed_uris:
        errors.append("fabricated_citation")
    return list(dict.fromkeys(errors))
