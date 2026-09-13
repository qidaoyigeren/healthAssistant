"""material-review@2 —— 增量更新：材料版本 → 证据 → 断言/发现 → 问题 → 报告版本。

用户补充信息或更新材料后，**只重新处理受影响的部分**，并解释报告为什么变。
关系用普通记录维护，不引入图数据库：

* 材料条目   ``state.material_fingerprints[ref]``（候选字段的确定性指纹）
* 发现       ``finding.material_refs``
* 断言       ``assertion.qualifiers.material_refs``
* 问题       ``question.related_material_refs``
* 报告版本   ``state.reports[]``，每次修订一个新 id，**不覆盖旧报告**

规则：

* 与更新无关的有效证据保留；
* 相关字段变化使对应判断失效并重新核对；
* **来源不同不等于自动取代**——只有明确的替代记录才算取代；
* 材料里的候选信息不能未经确认变成权威事实；
* 模型只收到与本次更新相关的变化摘要。
"""
from __future__ import annotations

import json

from .state import QUESTION_OPEN

REPLACEMENT_KIND = 'source_replacement'


def fingerprints(material_index, case_ids) -> dict:
    """材料条目 → 候选字段指纹。这是"材料版本"的粒度：逐条，不是整份文件。"""
    result = {}
    if material_index is None:
        return result
    for material in (material_index.index().get('materials') or []):
        if material.get('case_id') not in set(case_ids or []):
            continue
        for item in material.get('items') or []:
            ref = f"{material['case_id']}/{item['item_id']}"
            result[ref] = _fingerprint(item)
    return result


def _fingerprint(item) -> str:
    from .contract import item_fingerprint
    return item_fingerprint(item)


def compute_change_summary(state, material_index, versions) -> dict:
    """本次更新**影响了什么**。模型只收到这一份摘要，不重读全部材料。"""
    current = fingerprints(material_index, state.spec.selected_material_refs)
    before = state.material_fingerprints or {}
    changed = sorted(ref for ref in current if ref in before and before[ref] != current[ref])
    added = sorted(ref for ref in current if ref not in before)
    removed = sorted(ref for ref in before if ref not in current)
    authoritative = sorted(key for key in (versions or {})
                           if (state.spec.input_versions or {}).get(key) != versions.get(key))
    affected_findings = [item['finding_id'] for item in state.findings
                         if set(item.get('material_refs') or []) & set(changed + removed)]
    affected_assertions = [item['assertion_id'] for item in state.assertions
                           if set((item.get('qualifiers') or {}).get('material_refs') or [])
                           & set(changed + removed)]
    affected_questions = [item['question_id'] for item in state.questions
                          if set(item.get('related_material_refs') or []) & set(changed + removed)]
    return {'material_changed': changed, 'material_added': added, 'material_removed': removed,
            'authoritative_changed': authoritative,
            'affected_findings': affected_findings, 'affected_assertions': affected_assertions,
            'affected_questions': affected_questions,
            'materials_revision': (versions or {}).get('materials'),
            'versions': dict(versions or {}),
            'note': ('只有上面这些条目发生变化；未列出的发现、证据与结论继续有效，'
                     '不需要重新核对。')}


def apply_change(state, change: dict) -> dict:
    """使受影响的判断失效，并重新核对受影响的部分。

    失效是**标记 + 重算**，不是删除：历史仍然可回看，报告版本也仍然独立。
    """
    changed = set(change['material_changed']) | set(change['material_removed'])
    invalidated = {'findings': [], 'assertions': [], 'questions': []}
    for finding in state.findings:
        if set(finding.get('material_refs') or []) & changed:
            finding['stale'] = True
            finding['assessment_status'] = 'unverified'
            invalidated['findings'].append(finding['finding_id'])
    for assertion in state.assertions:
        qualifiers = assertion.setdefault('qualifiers', {})
        if set(qualifiers.get('material_refs') or []) & changed:
            assertion['verification_status'] = 'unverified'
            assertion['verification_reasons'] = ['material_version_changed']
            invalidated['assertions'].append(assertion['assertion_id'])
    for question in state.questions:
        if set(question.get('related_material_refs') or []) & changed and question['status'] != QUESTION_OPEN:
            question.setdefault('history', []).append(
                {'revision': question['revision'], 'text': question['text'],
                 'closed_as': question['status'],
                 'resolution_summary': question.get('resolution_summary')})
            question['status'] = QUESTION_OPEN
            question['resolution_summary'] = None
            question['revision'] += 1
            invalidated['questions'].append(question['question_id'])
    state.invalidation_log.append({'reason': 'material_changed', 'at_revision': state.revision,
                                   'changed': sorted(changed), **{k: v for k, v in invalidated.items()}})
    state.change_summary = change
    return invalidated


def refresh_from_materials(state, material_index) -> dict:
    """材料版本变化后：重算指纹、丢掉已经不在材料里的条目。

    **只做版本记账**，不在这里重落发现：发现的重新生成由
    :func:`coverage.run_coverage_pass` 在**实际读过**之后完成。指纹变了但还没重读
    的条目，其读取凭据会自动作废（``read_credential`` 比对指纹），所以它支撑的
    结论不会在"读过旧版本"的状态下继续成立。
    """
    current = fingerprints(material_index, state.spec.selected_material_refs)
    for ref, value in current.items():
        state.material_fingerprints[ref] = value
    stale_reads = [ref for ref in state.source_reads
                   if ref.startswith('case:') is False and ref not in current]
    kept_items = {ref: detail for ref, detail in state.material_items.items() if ref in current}
    dropped = sorted(set(state.material_items) - set(kept_items))
    state.material_items = kept_items
    for ref in dropped:
        state.source_reads.pop(ref, None)
    return {'material_refs': len(current), 'dropped': dropped, 'stale_reads': sorted(stale_reads)}


def invalidate_sources(state, evidence_store) -> dict:
    """来源失效：**先失效相关支持关系**，而不是丢掉整个任务的结果。

    只有存在明确的替代记录（``source_replacement``）或来源已不可回读时才算失效；
    "材料不同"不构成取代的依据。
    """
    from .state import ISSUE_SOURCE_INVALID
    removed, invalid_assertions = [], []
    for ref in list(state.read_evidence_refs):
        reason = _invalid_reason(state, evidence_store, ref)
        if reason is None:
            continue
        removed.append(ref)
        state.read_evidence_refs.remove(ref)
        state.add_issue(operation_ref=ref, category=ISSUE_SOURCE_INVALID,
                        remote_outcome='executed',
                        user_visible_summary=f'来源 {ref} 已失效（{reason}），不再支持任何结论。')
        for assertion in state.assertions:
            if ref in (assertion.get('evidence_refs') or []):
                assertion['verification_status'] = 'insufficient'
                assertion['verification_reasons'] = ['source_invalid:' + reason]
                invalid_assertions.append(assertion['assertion_id'])
        for finding in state.findings:
            if ref in (finding.get('evidence_refs') or []):
                finding['assessment_status'] = 'insufficient'
    state.refresh_axes()
    return {'invalidated_sources': removed, 'invalidated_assertions': invalid_assertions}


def _invalid_reason(state, evidence_store, ref):
    try:
        evidence_store.read(ref, scope_id=state.spec.scope_id, limit=1)
    except Exception:
        return 'unreadable_or_hash_mismatch'
    try:
        with evidence_store._lock:
            has_table = evidence_store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_objects'").fetchone()
            rows = evidence_store.connection.execute(
                "SELECT body_json FROM product_objects WHERE scope_id=? AND kind=?",
                (state.spec.scope_id, REPLACEMENT_KIND)).fetchall() if has_table else []
        for row in rows:
            if json.loads(row[0]).get('old_evidence_id') == ref:
                return 'explicitly_replaced'
    except Exception:
        return None
    return None


def build_report_record(state, *, report_id, created_at, sections, markdown,
                        delivery_status, evidence_status, diff) -> dict:
    """报告版本。**新版本不覆盖旧报告**：每次修订是一个新 id。"""
    return {'report_id': report_id, 'created_at': created_at, 'revision': len(state.reports) + 1,
            'contract_version': state.contract_version, 'sections': sections,
            'markdown': markdown, 'delivery_status': delivery_status,
            'evidence_status': evidence_status, 'revision_diff': diff,
            'versions': dict(state.spec.input_versions),
            'task_id': state.spec.task_id,
            'axes': {'run': state.run_status, 'delivery': delivery_status, 'evidence': evidence_status}}
