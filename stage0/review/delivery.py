"""material-review@2 —— 交付检查。

**按交付要求决定结束，不按"问题是否都有答案"。** 这里回答的是 TaskSpec 提出的
每一个问题，而不是某一种任务的固定完成条件：

* 用户指定范围是否已经核对；
* 每项必要输出是否存在；
* 事实性结论是否关联有效依据；
* 已知差异和反对证据是否遗漏；
* 未解决项是否明确说明；
* 是否存在越权或禁止输出；
* 交付是完整还是部分。

检查失败时返回**具体缺失项**，而不是要求重新执行整条调查流程。
"""
from __future__ import annotations

from ..response_safety import composed_text_prescribes
from .contract import (OUTPUT_LABELS, REQ_AWAITING_EVIDENCE, REQ_AWAITING_USER,
                       REQ_PENDING, REQ_SATISFIED, REQ_UNSATISFIED,
                       RULE_ALL_SCOPE_DISPOSITIONED, RULE_ANSWERED_FROM_SOURCE,
                       RULE_DIFFERENCES_LISTED, RULE_FIELD_DECIDED, RULE_NEVER_BLOCKS,
                       RULE_SAFETY_CLEAR, STATUS_LABELS)
from .report import SECTIONS
from .state import (CREDENTIALS_SUPPORTING, DELIVERY_COMPLETE, DELIVERY_NONE,
                    DELIVERY_PARTIAL, DIRECTION_PROFESSIONAL_REVIEW,
                    DIRECTION_USER_INPUT, FINDING_DISCREPANCY, FINDING_MISSING_FIELD,
                    FINDING_SOURCE_CONFLICT, INPUT_MATERIAL_NOTE, INPUT_USER_REPORT,
                    ISSUE_CONNECTION,
                    ISSUE_NO_MATCH, ISSUE_PERMISSION_DENIED, ISSUE_SOURCE_INVALID,
                    ORIGIN_SYSTEM, QUESTION_OPEN)

# 缺口的种类。每一条都指名道姓地说"缺哪一项"，让模型能只补那一项。
GAP_COVERAGE = 'coverage_pending'
GAP_OUTPUT = 'output_missing'
GAP_GROUNDING = 'conclusion_without_valid_evidence'
GAP_OMISSION = 'known_difference_omitted'
GAP_UNRESOLVED = 'unresolved_item_not_stated'
GAP_FORBIDDEN = 'forbidden_output'
GAP_AWAITING_INPUT = 'awaiting_user_input'
# 没有满足的**必需要求**。这是交付不完成的**唯一**出口——不是"报告缺哪一节"，
# 也不是"模型没参与"。
GAP_REQUIREMENT = 'requirement_unsatisfied'
# 可选建议还开着。它是**附注，不是缺口**：不进 BLOCKING_GAPS，不影响交付状态。
GAP_OPTIONAL_OPEN = 'optional_open'
# 没有任何依据就判"不确定"不算处置。每个 insufficient 都要说得出具体理由。
GAP_UNGROUNDED_UNCERTAINTY = 'uncertainty_without_basis'

BLOCKING_GAPS = (GAP_COVERAGE, GAP_OUTPUT, GAP_GROUNDING, GAP_OMISSION,
                 GAP_UNRESOLVED, GAP_FORBIDDEN, GAP_REQUIREMENT,
                 GAP_UNGROUNDED_UNCERTAINTY)


# ---- 交付要求的状态 -----------------------------------------------------------

def refresh_requirements(state) -> list:
    """算清每一条交付要求现在到哪一步了。**一个出口**，别处不再各算一遍。

    它同时负责**必要性**：必需要求缺了某个事实又拿不到时，系统自己建一条补充
    请求（点名它挡住哪一项要求），这样即使模型一整轮都没说话，缺项也照样会被
    准确地问出来。
    """
    for requirement in state.spec.delivery_requirements:
        _evaluate_requirement(state, requirement)
    _request_missing_facts(state)
    return state.spec.delivery_requirements


def _evaluate_requirement(state, requirement) -> None:
    rule = requirement['acceptance_rule']
    if rule == RULE_NEVER_BLOCKS:
        return
    if rule == RULE_ALL_SCOPE_DISPOSITIONED:
        # 指定范围"核对完了"= 每一条都真的比出了结果。``unreadable`` /
        # ``unmatched`` / ``insufficient`` 是**合格**的处置（它们如实记下了"这条
        # 没查成"），但它们是"没查成"，不是"核对完了"——把指定范围全部交代不了
        # 的时候算作完成，就是"把 pending 改写成 insufficient 换 complete"。
        pending = state.pending_coverage()
        if pending:
            return _set(state, requirement, REQ_PENDING,
                        reason=f'本次还有 {len(pending)} 项没有处置')
        blocked = state.blocking_requirement_ids()
        unmet = [item for item in state.spec.coverage_requirements
                 if item['disposition'] != 'covered']
        if not unmet:
            return _set(state, requirement, REQ_SATISFIED)
        reasons = '；'.join(sorted({f"{item['description']}：{item.get('disposition_reason') or item['disposition']}"
                                    for item in unmet}))
        if requirement['requirement_id'] in blocked:
            return _set(state, requirement, REQ_AWAITING_USER, reason=reasons)
        return _set(state, requirement, REQ_UNSATISFIED, reason=reasons)
    if rule == RULE_SAFETY_CLEAR:
        denied = [item for item in state.issues
                  if item['category'] == ISSUE_PERMISSION_DENIED and item['status'] == 'open']
        if denied:
            return _set(state, requirement, REQ_UNSATISFIED, reason='当前权限不允许这一步')
        return _set(state, requirement, REQ_SATISFIED,
                    reason=None if state.safety_checks else '强制安全检查未执行')
    if rule == RULE_DIFFERENCES_LISTED:
        return _evaluate_differences(state, requirement)
    if rule == RULE_FIELD_DECIDED:
        return _evaluate_field(state, requirement)
    if rule == RULE_ANSWERED_FROM_SOURCE:
        return _evaluate_answer(state, requirement)


def _set(state, requirement, status, *, reason=None, comparison_refs=None,
         evidence_refs=None) -> None:
    if requirement['status'] == status and requirement['reason'] == reason:
        return
    requirement['status'] = status
    requirement['reason'] = reason
    for ref in comparison_refs or []:
        if ref not in requirement['comparison_refs']:
            requirement['comparison_refs'].append(ref)
    for ref in evidence_refs or []:
        if ref not in requirement['evidence_refs']:
            requirement['evidence_refs'].append(ref)


def _evaluate_differences(state, requirement) -> None:
    """列出差异与缺项：**准确报告**缺项就算完成。

    它要求的不是"所有字段都有值"，而是"该列的差异与缺项都列出来了"。所以缺一项
    不是失败——没把它列出来才是。
    """
    pending = state.pending_coverage()
    if pending:
        return _set(state, requirement, REQ_PENDING,
                    reason=f'本次还有 {len(pending)} 项没有处置')
    unreported = []
    for ref, comparisons in state.field_comparisons.items():
        from .fields import STATUS_EQUAL, summarize
        summary = summarize(comparisons)
        if summary['different'] == 0 and summary['undecided'] == 0:
            continue
        row = state.material_items.get(ref) or {}
        if row.get('kind') == 'same' and summary['undecided'] == 0:
            continue
        if not _reported(state, ref):
            unreported.append(ref)
    if unreported:
        return _set(state, requirement, REQ_UNSATISFIED,
                    reason=f'有 {len(unreported)} 条材料的差异或缺项没有列进报告')
    return _set(state, requirement, REQ_SATISFIED)


def _reported(state, ref: str) -> bool:
    """这条材料的结论有没有落进报告（当前有效的发现里）。"""
    return any(ref in (finding.get('material_refs') or []) and not finding.get('stale')
               for finding in state.findings)


def _evaluate_field(state, requirement) -> None:
    """确认指定字段是否一致。**"写了未知"不算完成。**

    三种结局分开：两边都有值 → 已得出结论（相等或不等的**结论**都是结论）；
    有一边没值 → 还缺一个事实，问用户；连条目都还没比 → 还没轮到它。
    """
    from .fields import DECIDED_STATUSES, STATUS_DIFFERENT, STATUS_LABELS
    comparisons = _comparisons_for_requirement(state, requirement)
    if not comparisons:
        return _set(state, requirement, REQ_PENDING, reason='相关材料还没有完成字段比较')
    undecided = [row for row in comparisons
                 if row['comparison_status'] not in DECIDED_STATUSES]
    if undecided:
        fields = '、'.join(sorted({row['field_label'] for row in undecided}))
        detail = '、'.join(sorted({STATUS_LABELS.get(row['comparison_status'], '')
                                   for row in undecided}))
        return _set(state, requirement, REQ_AWAITING_USER,
                    reason=f'{fields}：{detail}，无法给出"是否一致"的结论',
                    comparison_refs=[row['comparison_id'] for row in undecided])
    if requirement.get('expect') == 'equal':
        differing = [row for row in comparisons if row['comparison_status'] == STATUS_DIFFERENT]
        if differing:
            pairs = '；'.join(f'{row["field_label"]} 材料 {row["left_value"]} / 记录 {row["right_value"]}'
                              for row in differing)
            return _set(state, requirement, REQ_UNSATISFIED,
                        reason=f'已确认不一致：{pairs}')
    return _set(state, requirement, REQ_SATISFIED,
                comparison_refs=[row['comparison_id'] for row in comparisons])


def _comparisons_for_requirement(state, requirement) -> list:
    field = requirement.get('field')
    if not field:
        return []
    subjects = [str(item) for item in requirement.get('subject_refs') or []]
    rows = []
    for ref, comparisons in state.field_comparisons.items():
        # 对象可以是材料条目本身，也可以是它对应的当前记录。
        if subjects and ref not in subjects and not (set(subjects) & set(state.material_subject_refs(ref))):
            continue
        rows.extend(row for row in comparisons if row['field'] == field)
    return rows


def _evaluate_answer(state, requirement) -> None:
    """依据资料回答一个问题。

    它**不能**因为"写了一条待确认"就算完成：要么有一条引用读回原文的结论，
    要么如实记下这次调查没能得到依据（失败也是结论，但要说得清）。
    """
    from .verify import STATUS_SUPPORTED
    question_refs = set(requirement.get('question_refs') or [])
    supported = [item for item in state.assertions
                 if item['verification_status'] == STATUS_SUPPORTED
                 and question_refs & set(item.get('question_refs') or [])
                 and (item.get('evidence_refs') or item.get('qualifiers', {}).get('material_refs'))]
    if supported:
        return _set(state, requirement, REQ_SATISFIED,
                    evidence_refs=[ref for item in supported
                                   for ref in (item.get('evidence_refs') or [])])
    if not requirement.get('allows_external', True) \
            or not state.spec.investigation_scope.get('allow_external_evidence', True):
        return _set(state, requirement, REQ_UNSATISFIED,
                    reason='本次任务不允许外部取证，这个问题无法从授权资料回答')
    blockers = [issue for issue in state.issues
                if issue['status'] == 'open' and issue['category'] in
                (ISSUE_NO_MATCH, ISSUE_SOURCE_INVALID, ISSUE_CONNECTION)
                and (question_refs & set(issue.get('affected_question_ids') or [])
                     or question_refs)]
    directions = {state.question(ref).get('direction') if state.question(ref) else None
                  for ref in question_refs}
    if directions and directions <= {DIRECTION_USER_INPUT}:
        return _set(state, requirement, REQ_AWAITING_USER,
                    reason='这个问题需要用户提供具体事实')
    if directions and DIRECTION_PROFESSIONAL_REVIEW in directions:
        return _set(state, requirement, REQ_UNSATISFIED,
                    reason='这个问题需要专业人员确认，本次不能代替专业判断')
    if blockers:
        return _set(state, requirement, REQ_UNSATISFIED,
                    reason=blockers[0]['user_visible_summary'])
    return _set(state, requirement, REQ_AWAITING_EVIDENCE,
                reason='还没有从授权资料取得可引用的依据')


def _request_missing_facts(state) -> None:
    """必需要求缺了事实时，**由系统**建一条补充请求——不依赖模型记得去问。

    它先看**已经开着**的请求有没有挡住这一项：基础覆盖按材料缺项建的那条请求，
    通常已经把同一批字段都问上了，再建一条只会让用户在界面上看到两遍同一件事。
    只有确实没人问过时，这里才补一条。

    请求点名它挡住哪一项要求，所以它是**阻塞**请求；而模型自己提的可选建议
    （没有挡任何必需项）不会把任务变成等待状态。
    """
    for requirement in state.spec.required_requirements():
        if requirement['status'] not in (REQ_AWAITING_USER, REQ_PENDING):
            continue
        if any(state.is_blocking_request(ref) and _is_open(state, ref)
               for ref in requirement['input_request_refs']):
            continue  # 已经有一条开着的请求挡着它了
        if _covered_by_an_open_request(state, requirement):
            continue
        if requirement['acceptance_rule'] == RULE_FIELD_DECIDED:
            missing = [row for row in _comparisons_for_requirement(state, requirement)
                       if row['comparison_status'] in ('missing_left', 'missing_right',
                                                       'not_comparable', 'invalid_value')]
            if not missing:
                continue
            subjects = _subject_names(state, requirement, missing)
            if not subjects:
                continue
            fields = list(dict.fromkeys(row['field'] for row in missing))
            # 缺的是**当前记录**那一侧时，用户说的是"记录里应该是什么"——它按
            # 用户陈述记录，用于比较，但**不会**写进权威记录（要写进去得走既有
            # 确认流程）。缺的是材料那一侧时，用户说的是"材料上写的是什么"。
            record_side = all(row['comparison_status'] == 'missing_right' for row in missing)
            _add_request(state, requirement, subjects, fields,
                         missing_fact='、'.join(dict.fromkeys(
                             row['field_label'] for row in missing)),
                         question_text='无法确认'
                                       + '、'.join(dict.fromkeys(row['field_label'] for row in missing))
                                       + f'：{missing[0]["reason"] or "缺少可比较的信息"}。'
                                       + ('请说明当前记录里这一项应当是什么（按您所说记录，不会改动记录本身）。'
                                          if record_side else '请补充实际内容。'),
                         why_needed=f'这一项挡住了"{requirement["text"]}"，缺了它无法判断是否一致。',
                         purpose=INPUT_USER_REPORT if record_side else INPUT_MATERIAL_NOTE)
            continue
        if requirement['acceptance_rule'] == RULE_ANSWERED_FROM_SOURCE:
            # 这个问题只能由人回答（用户能提供的事实 / 需要专业人员确认）。
            # 资料那一路 `_evaluate_answer` 已经判过，走到这里说明该问人。
            subjects = _subject_names(state, requirement, [])
            question = state.question((requirement['question_refs'] or [None])[0])
            _add_request(state, requirement, subjects or [requirement['text']], [],
                         missing_fact=requirement['text'],
                         question_text=f'需要您提供：{requirement["text"]}',
                         why_needed='这一项没有现成的资料可以回答，只能由您或医生确认。',
                         purpose=INPUT_USER_REPORT)


def _covered_by_an_open_request(state, requirement) -> bool:
    """已经开着的请求里，有没有一条本来就是冲这件事来的。"""
    subjects = {str(item) for item in requirement['subject_refs']}
    for item in state.open_input_requests():
        if requirement['requirement_id'] in (item.get('blocks_requirement_ids') or []):
            return True
        if subjects and subjects & {str(value) for value in item.get('subjects') or []} \
                and (requirement.get('field') or '') in (item.get('required_fields') or []):
            return True
    return False


def _add_request(state, requirement, subjects, fields, *, missing_fact, question_text,
                 why_needed, purpose) -> None:
    state.add_input_request(
        question_text=question_text, subjects=subjects, required_fields=fields,
        why_needed=why_needed, missing_fact=missing_fact,
        why_material_insufficient='材料与当前记录至少有一边没有这一项的值。',
        purpose=purpose, blocks_requirement_ids=[requirement['requirement_id']],
        target={'subjects': subjects, 'fields': fields},
        origin=ORIGIN_SYSTEM, reopen=True)


def _is_open(state, request_id) -> bool:
    return any(item['request_id'] == request_id and item['status'] == 'open'
               for item in state.input_requests)


def _subject_names(state, requirement, comparisons) -> list:
    """这条要求该**向谁**要那个事实。

    只问**材料那边真的写了这一格**的对象：材料里根本没有这条记录时（``not_listed``），
    让用户说明"记录里应该是什么"并不能让两边的值比出结果——那一格缺的是材料，
    不是记录。把它一起问上，只会让要求永远差一格。
    """
    known = state.medications_by_ref()
    fields = {row['field'] for row in (comparisons or [])}
    names = []
    for value in requirement.get('subject_refs') or []:
        medication = known.get(str(value))
        names.append(str(medication['display_name']) if medication else str(value))
    if not names:
        for ref, rows in state.field_comparisons.items():
            if fields and not any(row['field'] in fields and row['left_value'] not in (None, '')
                                  for row in rows):
                continue
            elif not fields and not any(row['left_value'] not in (None, '') for row in rows):
                continue
            for current in state.material_subject_refs(ref):
                medication = known.get(current)
                if medication:
                    names.append(str(medication['display_name']))
                    break
    return list(dict.fromkeys(names))


def check_delivery(state, *, sections=None, evidence_store=None, markdown=None) -> dict:
    """返回 ``{'ok', 'gaps', 'delivery_status', 'evidence_status', 'summary'}``。"""
    from .report import section_content
    refresh_requirements(state)
    sections = sections if sections is not None else section_content(state)
    gaps = []
    # 逐条点名的覆盖缺口与"哪一项要求没满足"是两回事：前者说哪一条材料还没碰过，
    # 后者说用户要的那件事做完了没有。两个都要，读的人才知道下一步做什么。
    gaps.extend(_coverage_gaps(state))
    gaps.extend(_requirement_gaps(state))
    gaps.extend(_output_gaps(state, sections, markdown))
    gaps.extend(_grounding_gaps(state, evidence_store))
    gaps.extend(_omission_gaps(state, sections))
    gaps.extend(_unresolved_gaps(state, sections))
    gaps.extend(_forbidden_gaps(state, sections))
    gaps.extend(_uncertainty_gaps(state))
    # **只有挡住必需要求的补充请求**才算"正在等用户"。可选建议开放着，不改变
    # 交付状态——它本来就不阻塞原始任务。
    blocking_requests = [item for item in state.input_requests
                         if item['status'] == 'open' and state.is_blocking_request(item['request_id'])]
    if blocking_requests:
        gaps.append({'code': GAP_AWAITING_INPUT, 'blocking': False,
                     'detail': f'有 {len(blocking_requests)} 条必要的补充请求还没有回答；'
                               f'报告可以作为部分结果交付。',
                     'fix': '等待用户补充；本次结果不需要重做，只需要补上缺的那部分。'})
    optional_open = [item for item in state.input_requests
                     if item['status'] == 'open' and not state.is_blocking_request(item['request_id'])]
    if optional_open:
        gaps.append({'code': GAP_OPTIONAL_OPEN, 'blocking': False,
                     'detail': f'另有 {len(optional_open)} 条可选的补充建议；'
                               f'它们不影响本次任务是否完成。',
                     'fix': '可以回答，也可以先不管——原任务的交付要求不依赖它们。'})
    blocking = [item for item in gaps if item['code'] in BLOCKING_GAPS]
    # 完成判定只有一条：**所有必需要求都满足**。模型有没有参与不再是条件之一——
    # 纯确定性的任务本来就可以在没有人分析过的情况下完整完成；反过来，任务明确
    # 要求语义解释或来源调查时，那条要求没满足就仍然不是完整交付。
    unfinished = bool(blocking)
    delivery_status = (DELIVERY_COMPLETE if not unfinished else
                       DELIVERY_PARTIAL if sections else DELIVERY_NONE)
    state.last_delivery_gaps = gaps
    return {'ok': not blocking, 'gaps': gaps, 'delivery_status': delivery_status,
            'awaiting_input': bool(blocking_requests),
            'optional_open': len(optional_open),
            'unsatisfied_required': [item['requirement_id']
                                     for item in state.unsatisfied_required()],
            'evidence_status': state.evidence_status,
            'summary': _summary(gaps, delivery_status)}


def _coverage_gaps(state):
    gaps = []
    for requirement in state.pending_coverage():
        gaps.append({'code': GAP_COVERAGE, 'blocking': True,
                     'requirement_id': requirement['requirement_id'],
                     'detail': f"未交代：{requirement['description']}",
                     'fix': '给出这条的结论，或用一条执行问题明确记录它无法读取/无法匹配/依据不足。'})
    return gaps


def _output_gaps(state, sections, markdown=None):
    """每项交付要求都要在报告里**有位置**。

    "这一节是空的"本身不是缺陷：材料全部一致时，"未发现差异"就是正确答案，
    而且必须写得出来。真正会失败的是这一节根本没有交付——所以这条检查问的是
    报告里**有没有这一节**，而不是这一节够不够热闹。至于内容该不该有，由各自的
    专项检查负责（覆盖、依据、遗漏、未决），它们都不是标题匹配。
    """
    gaps = []
    for key in state.spec.requested_outputs:
        title = _section_for(key)
        if key not in OUTPUT_LABELS or title is None:
            continue
        present = ('## ' + title) in markdown if markdown is not None else title in sections
        if not present:
            gaps.append({'code': GAP_OUTPUT, 'blocking': True, 'output': key,
                         'detail': f"缺少交付内容：{OUTPUT_LABELS[key]}",
                         'fix': '在报告里补上这一节，或写明本次这一节为空的原因。'})
    return gaps


def _section_for(key):
    return {'matched_summary': '2. 一致项与主要变化', 'differences': '3. 差异双方的记录及来源',
            'missing_fields': '3. 差异双方的记录及来源', 'confirm_questions': '4. 仍待确认的问题',
            'sources': '2. 一致项与主要变化', 'coverage': '1. 本次核对目标和覆盖范围'}.get(key)


def _grounding_gaps(state, evidence_store):
    """作为结论出现的事实，必须关联仍然有效的依据。

    "来源已有效读取"由**读取凭据**证明，不由某个工具被调用过证明：系统在同样的
    权限与完整性校验下读过的材料同样支撑结论。但**解释**仍然要独立验证——系统读
    过字段不等于模型的语义判断成立，这一层由 ``verify`` 负责，不在这里放宽。
    """
    gaps = []
    for finding in state.findings:
        if finding.get('stale'):
            continue
        if finding['finding_type'] not in (FINDING_DISCREPANCY, FINDING_MISSING_FIELD,
                                           FINDING_SOURCE_CONFLICT, 'matched'):
            continue
        refs = list(finding.get('evidence_refs') or [])
        materials = list(finding.get('material_refs') or [])
        if materials:
            ungrounded = [ref for ref in materials
                          if state.read_credential(str(ref)) not in CREDENTIALS_SUPPORTING]
            if ungrounded:
                gaps.append({'code': GAP_GROUNDING, 'blocking': True,
                             'finding_id': finding['finding_id'],
                             'detail': f'结论所依据的材料没有被有效读取：{finding["statement"][:60]}',
                             'fix': '先让这条材料被实际读取并通过校验；索引里出现过不算。'})
                continue
        if refs:
            if any(ref not in state.read_evidence_refs for ref in refs):
                gaps.append({'code': GAP_GROUNDING, 'blocking': True,
                             'finding_id': finding['finding_id'],
                             'detail': f'结论引用了尚未读回原文的依据：{finding["statement"][:60]}',
                             'fix': '先用 research_evidence 读回原文再引用。'})
            elif evidence_store is not None and not _all_valid(state, refs, evidence_store):
                gaps.append({'code': GAP_GROUNDING, 'blocking': True,
                             'finding_id': finding['finding_id'],
                             'detail': f'结论引用的依据已失效：{finding["statement"][:60]}',
                             'fix': '失效来源不能继续支持结论；重新取证或把这条降级为待确认。'})
        elif not materials:
            gaps.append({'code': GAP_GROUNDING, 'blocking': True,
                         'finding_id': finding['finding_id'],
                         'detail': f'结论没有任何来源：{finding["statement"][:60]}',
                         'fix': '补上材料或证据来源，或把这条改成待确认项。'})
    for assertion in state.assertions:
        if assertion['verification_status'] == 'supported':
            if not assertion.get('evidence_refs') and not assertion.get('qualifiers', {}).get('material_refs'):
                gaps.append({'code': GAP_GROUNDING, 'blocking': True,
                             'assertion_id': assertion['assertion_id'],
                             'detail': '断言被判为有支持，却没有关联任何来源。',
                             'fix': '补上来源，或重新核查这条断言。'})
    return gaps


def _all_valid(state, refs, evidence_store):
    for ref in refs:
        if ref not in state.read_evidence_refs:
            return False
        try:
            evidence_store.read(ref, scope_id=state.spec.scope_id, limit=1)
        except Exception:
            return False
    return True


def _omission_gaps(state, sections):
    """已知差异与反对证据不能被漏掉。"""
    gaps = []
    difference_lines = '\n'.join(sections.get('3. 差异双方的记录及来源') or [])
    unresolved_lines = '\n'.join(sections.get('4. 仍待确认的问题') or [])
    rendered = difference_lines + '\n' + unresolved_lines
    for finding in state.findings:
        if finding['finding_type'] not in (FINDING_DISCREPANCY, FINDING_MISSING_FIELD,
                                           FINDING_SOURCE_CONFLICT):
            continue
        if not finding.get('stale') and finding['statement'] not in rendered:
            gaps.append({'code': GAP_OMISSION, 'blocking': True,
                         'finding_id': finding['finding_id'],
                         'detail': f'已知差异没有出现在报告里：{finding["statement"][:60]}',
                         'fix': '把这条差异写进报告，或说明为什么它不再成立。'})
    for assertion in state.assertions:
        if assertion['verification_status'] in ('contradicted', 'conflicting'):
            if assertion.get('predicate') and assertion['predicate'] not in rendered \
                    and str(assertion.get('value')) not in rendered:
                gaps.append({'code': GAP_OMISSION, 'blocking': True,
                             'assertion_id': assertion['assertion_id'],
                             'detail': '反对证据没有出现在报告里。',
                             'fix': '把反对证据与被反驳的说法一并列出。'})
    return gaps


def _unresolved_gaps(state, sections):
    gaps = []
    rendered = '\n'.join(sections.get('4. 仍待确认的问题') or [])
    for question in state.questions:
        if question['status'] == QUESTION_OPEN and question['text'] not in rendered:
            gaps.append({'code': GAP_UNRESOLVED, 'blocking': True,
                         'question_id': question['question_id'],
                         'detail': f'未解决问题没有写进报告：{question["text"][:60]}',
                         'fix': '未解决的问题只能被解决或被明确写出，不能删除。'})
    return gaps


# 报告里**由代码生成**的提问与请求：它们是在问，不是在主张。问句不可能构成
# "未经验证的用药建议"，把它们判成越权输出会让"请确认这个药还在不在吃"这种
# 最该问出口的话反而发不出去——与"疑问句不是断言"是同一条规则。
REQUEST_PREFIXES = ('待核实：', '需要您补充：', '可以向医生或药师确认：')


def _forbidden_gaps(state, sections):
    from ..response_safety import asks_without_asserting
    gaps = []
    for title in SECTIONS:
        for line in sections.get(title) or []:
            body = str(line).lstrip('- ').strip()
            if body.startswith(REQUEST_PREFIXES) or asks_without_asserting(body):
                continue
            if composed_text_prescribes(body):
                gaps.append({'code': GAP_FORBIDDEN, 'blocking': True,
                             'detail': f'报告中含有诊断或用药调整措辞：{body[:60]}',
                             'fix': '改成陈述已核对的事实，或把它变成一条待确认的问题。'})
    return gaps


def _requirement_gaps(state):
    """**没有满足的必需要求**——交付不完成的唯一出口。

    每一条都点名是哪项要求、卡在什么上、下一步需要什么。可选项不在这里出现。
    """
    gaps = []
    for requirement in state.spec.required_requirements():
        if requirement['status'] == REQ_SATISFIED:
            continue
        gaps.append({
            'code': GAP_REQUIREMENT, 'blocking': True,
            'requirement_id': requirement['requirement_id'],
            'detail': f"{requirement['text']}：{STATUS_LABELS.get(requirement['status'], requirement['status'])}"
                      + (f"（{requirement['reason']}）" if requirement.get('reason') else ''),
            'fix': {'awaiting_user': '请补充上面缺的具体事实，然后继续核对。',
                    'awaiting_evidence': '需要从授权资料中取得可引用的依据。',
                    'unsatisfied': '这一项本次没有满足；报告会写明它没满足以及为什么。',
                    'pending': '这一项还没有轮到处理。',
                    }.get(requirement['status'], '继续处理这一项。')})
    return gaps


def _uncertainty_gaps(state):
    """每一个"不确定"都要有具体依据**和**对应的任务处置。

    把一条查不出结果的条目一律写成 ``insufficient``，可以让覆盖看起来交代完了，
    但既没有说明**为什么**不确定，也没有请谁去做点什么——那正是"用措辞换完成"。
    所以两条都要：

    * **依据**：一条具体原因，或一条执行问题（读不到 / 无法匹配）；
    * **处置**：一条待确认的问题或一条补充请求正指着它——否则这件事没有人在管。
    """
    handled = _items_with_a_follow_up(state)
    gaps = []
    for requirement in state.spec.coverage_requirements:
        if requirement['disposition'] not in ('insufficient', 'unreadable', 'unmatched'):
            continue
        ref = requirement['ref']
        if not (requirement.get('disposition_reason') or requirement.get('issue_refs')):
            gaps.append({'code': GAP_UNGROUNDED_UNCERTAINTY, 'blocking': True,
                         'requirement_id': requirement['requirement_id'],
                         'detail': '这一条只被标成「没查成」，没有说明依据：'
                                   + requirement['description'],
                         'fix': '说明它为什么没有结论（读不到 / 无法匹配 / 字段不足），或补上依据。'})
            continue
        if requirement['disposition'] == 'insufficient' \
                and requirement['kind'] == 'material_item' and ref not in handled:
            gaps.append({'code': GAP_UNGROUNDED_UNCERTAINTY, 'blocking': True,
                         'requirement_id': requirement['requirement_id'],
                         'detail': f"这一条被标成「依据不足」，但没有任何待确认的问题或补充请求"
                                   f"在跟进它：{requirement['description']}",
                         'fix': '为它建立一条待确认的问题，或建一条补充请求请用户补上缺的字段。'})
    return gaps


def _items_with_a_follow_up(state) -> set:
    """**有后续动作**的材料条目：有一条未决问题或补充请求正指着它。"""
    refs = set()
    for request in state.open_input_requests():
        target = request.get('target') or {}
        if target.get('material_ref'):
            refs.add(str(target['material_ref']))
    for question in state.open_questions():
        refs.update(str(ref) for ref in (question.get('related_material_refs') or []))
    return refs


def _summary(gaps, delivery_status):
    """给模型的**具体**反馈：缺哪几项，不要重做整条流程。"""
    blocking = [item for item in gaps if item['code'] in BLOCKING_GAPS]
    if not blocking:
        return ('交付检查通过：本次承诺的交付要求都已满足。' if delivery_status == DELIVERY_COMPLETE
                else '本次没有需要补的内容。')
    lines = [f'交付检查未通过，尚缺 {len(blocking)} 项（只需补这几项，不必重做整条调查）：']
    for item in blocking[:12]:
        lines.append(f"· {item['detail']} → {item['fix']}")
    if len(blocking) > 12:
        lines.append(f'· 另有 {len(blocking) - 12} 项。')
    return '\n'.join(lines)


def is_deliverable(state) -> bool:
    return not state.pending_coverage() and not state.open_questions()


def evidence_axis(state) -> str:
    """证据轴：由支持判断本身决定，不由交付是否完成为难。

    **只有一处实现**（``state.refresh_axes``）：如果这里再算一遍，两条口径迟早
    会分叉，而界面看到的那一条会成为"另一个答案"。
    """
    state.refresh_axes()
    return state.evidence_status


def hard_stop_reason(state) -> str | None:
    """只有**权限或关键事实完整性被破坏**时才按必要范围停止。

    单个来源失效不丢弃整个任务的有效结果：它先失效相关支持关系、更新受影响
    问题，任务继续。权限被拒则不同——继续查下去只会绕开边界。
    """
    denied = [item for item in state.issues
              if item['category'] == ISSUE_PERMISSION_DENIED and item['status'] == 'open']
    if denied:
        return 'permission_denied'
    return None
