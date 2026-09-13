"""material-review@2 —— 报告渲染与版本间变化说明。

用户页面使用**业务语言**：gap、schema、内部枚举和调试异常留在诊断信息里。
报告分节固定，因为照护者是按这个顺序读的；内容全部来自实际读回的文字与代码
算出的确定性差异。

模型写的句子要过同一道交付前安全检查：带诊断/处方措辞的、或者对具体危害下断言
而未被逐字核实的句子**不复述**，改报它的主体。原句仍留在结构化产物里。
"""
from __future__ import annotations

from ..response_safety import composed_text_prescribes
from .contract import REQ_SATISFIED, STATUS_LABELS
from .state import (EVIDENCE_CONFLICTING, EVIDENCE_INSUFFICIENT, FINDING_CONTEXTUAL_NOTE,
                    FINDING_DISCREPANCY, FINDING_MATCHED, FINDING_MISSING_FIELD,
                    FINDING_SOURCE_CONFLICT, ISSUE_INVALID_ARGUMENTS, QUESTION_OPEN)

SECTIONS = ('1. 本次核对目标和覆盖范围', '2. 一致项与主要变化',
            '3. 差异双方的记录及来源', '4. 仍待确认的问题',
            '5. 就诊时可以讨论的事项', '6. 未覆盖范围和执行限制')
REVISION_SECTION = '7. 与上一版报告相比的变化'

EMPTY_SENTENCE = {
    '2. 一致项与主要变化': '- 本次没有在已核对范围内发现一致项；这不代表没有问题，只代表本次没有核对出可记录的条目。',
    '3. 差异双方的记录及来源': '- 已核对范围内没有发现差异；未核对的范围不在此列。',
    '4. 仍待确认的问题': '- 本次没有留下待确认的问题。',
    '5. 就诊时可以讨论的事项': '- 可将本报告的差异与未覆盖范围逐条向医生或药师确认。',
    '6. 未覆盖范围和执行限制': '- 本次没有遇到未覆盖范围或执行限制。',
}

AXIS_LABELS = {'run': {'running': '进行中', 'waiting_input': '等待补充', 'ended': '已结束',
                       'cancelled': '已取消', 'failed': '未完成'},
               'delivery': {'none': '尚无报告', 'partial': '部分报告', 'complete': '完整报告'},
               'evidence': {'verified': '已核实', 'conflicting': '存在冲突',
                            'insufficient': '不足'}}

_CONCRETE_HAZARD_WORDS = ('出血', '低血压', '致命', '肾损伤', '肝损伤', 'bleeding', 'fatal')


def safe_statement(text, subjects=()) -> str:
    """模型写的句子进入交付正文前的一道检查。**不复述**危险断言与处方措辞。"""
    text = str(text or '').strip()
    subjects = '、'.join(str(item) for item in subjects if item) or '相关记录'
    if composed_text_prescribes(text):
        return f'涉及 {subjects} 的一项说法带有诊断或用药调整措辞，本报告不复述；请与医生或药师核对。'
    lowered = text.lower()
    if any(word in lowered for word in _CONCRETE_HAZARD_WORDS):
        return f'涉及 {subjects} 的一项说法包含未经逐字核实的危害描述，本报告不复述；请与医生或药师核对。'
    return text


def question_text(state, question) -> str:
    """问题渲染：疑问句原样保留（它是在问，不是在断言）。"""
    return str(question.get('text') or '').strip()


def section_content(state) -> dict:
    """每一节的**实质**条目。空态句不在这里，渲染与交付检查读同一个集合。"""
    from .verify import STATUS_HUMAN, STATUS_SUPPORTED
    coverage = state.spec.coverage_requirements
    covered = [item for item in coverage if item['disposition'] == 'covered']
    matched, differences, unresolved, discuss, limits = [], [], [], [], []
    optional_lines: list = []

    for finding in state.findings:
        if finding.get('stale'):
            # 材料或记录变了，这条判断已被取代。它留在历史里（可回看、审计），
            # 但不再是当前报告的一条结论——留着它会和取代它的新发现互相矛盾。
            continue
        line = safe_statement(finding['statement'], finding.get('subject_refs'))
        sources = _sources(finding)
        unverified = finding.get('assessment_status') in (None, 'unverified', 'insufficient')
        if finding['finding_type'] == FINDING_MATCHED and not unverified:
            matched.append(f'- {line}{sources}')
        elif finding['finding_type'] in (FINDING_DISCREPANCY, FINDING_MISSING_FIELD,
                                         FINDING_SOURCE_CONFLICT) and not unverified:
            differences.append(f'- {line}{sources}')
        else:
            # 背景说明、以及**依据尚不充分**的发现：都只能作为待确认项出现一次。
            # 同一条在报告里写两遍，读起来像两个问题，实际只有一个。
            note = '需要核实，不是结论' if finding['finding_type'] == FINDING_CONTEXTUAL_NOTE \
                else '依据尚不充分，列为待确认'
            unresolved.append(f'- {line}（{note}）{sources}')

    for assertion in state.assertions:
        line = _assertion_line(assertion)
        if assertion['verification_status'] == STATUS_SUPPORTED:
            matched.append(f'- {line}{_sources(assertion)}')
        elif assertion['verification_status'] == STATUS_HUMAN:
            unresolved.append(f'- {line}（原文已确认存在，语义解释需人工确认）{_sources(assertion)}')
        elif assertion['verification_status'] in ('insufficient', 'contradicted'):
            unresolved.append(f'- {line}（现有依据不支持这一说法，列为待确认）{_sources(assertion)}')

    # 补充请求是同一个问题的**可操作形式**：它带字段和关闭入口。同一条文本既
    # 出现在请求里又出现在待核实清单里，读者会以为有两件事要处理。
    requested = {request['question_text'] for request in state.input_requests
                 if request['status'] == 'open'}
    # 需要用户回答的与**只是可选**的分开列。以前它们同属一张 pending 列表，
    # 于是"我还可以再查一件事"读起来和"你必须回答我"一模一样。
    for request in state.input_requests:
        if request['status'] != 'open':
            continue
        if state.is_blocking_request(request['request_id']):
            unresolved.append(f"- 需要您补充：{request['question_text']}"
                              + (f"（缺：{request['missing_fact']}）"
                                 if request.get('missing_fact') else ''))
            discuss.append(f"- 可以向医生或药师确认：{request['question_text']}")
        else:
            optional_lines.append(f"- 可选：{request['question_text']}"
                                  "（不影响本次任务是否完成）")
    for question in state.questions:
        text = question_text(state, question)
        if question['status'] == QUESTION_OPEN:
            if text in requested:
                continue
            unresolved.append(f'- 待核实：{text}')
            discuss.append(f'- 可以向医生或药师确认：{text}')
        elif question['resolution_summary']:
            matched.append(f"- 已核实：{text}（{question['resolution_summary']}）")

    for coverage_item in coverage:
        # ``pending`` 也是未覆盖范围：本次**没有**核对到它。把它写成"没有问题"
        # 才是真正危险的那种空态。
        if coverage_item['disposition'] in ('unreadable', 'unmatched', 'insufficient', 'pending'):
            limits.append(f"- {coverage_item['description']}：{_disposition_text(coverage_item['disposition'])}"
                          + (f"（{coverage_item['disposition_reason']}）" if coverage_item.get('disposition_reason') else ''))
    cursor = state.coverage_cursor or {}
    for ref in cursor.get('truncated') or []:
        limits.append(f'- 材料条目 {ref} 原文较长，本次只读到一部分，其余部分尚未核对。')
    for ref in cursor.get('failed') or []:
        limits.append(f'- 材料条目 {ref} 本次读取失败；这一条没有核对到，不代表它与当前记录一致。')
    for question in state.blocked_questions():
        # 被未回答的补充请求挡住：写出来，读者才知道是在等谁。
        limits.append(f'- 等待补充后才能继续：{question["text"]}（已向您提出补充请求）')
    # 只写**还开着、并且真的影响到结果**的执行问题。
    #
    # 两类不进正文，都留在审计记录里：
    #   · 已经被后续成功步骤处理掉的（同一个动作后来做成了）；
    #   · 根本没执行、也没动过任何远端数据的参数问题——那是模型自己的可重试笔误，
    #     循环已经处理过它。把它写成"本次执行限制"，会让一次自动改正看起来像故障，
    #     也会让照护者以为某条结论可能不可靠。
    for issue in state.open_issues():
        if issue['category'] == ISSUE_INVALID_ARGUMENTS and issue['remote_outcome'] == 'not_executed':
            continue
        limits.append(f"- {issue['user_visible_summary']}（{_operation_label(issue['operation_ref'])}）")
    if not state.model_cycles_this_delivery():
        limits.append('- 这一版没有模型参与：语义解释、需要判断的条目与"还该查什么"都没有被处理。'
                      '下面列出的结论只包含代码完成的确定性字段核对。')

    progress = state.coverage_progress()
    scope = ['- 本次要解决的问题：' + (state.spec.user_goal or '（未填写）')]
    # **本次承诺解决什么**，以及每一项目前到哪一步了。这一节是页面与报告共同的
    # 主视图：用户先看这里，再决定要不要往下读。
    scope += _requirement_lines(state)
    scope += [
             f"- 核对范围：{len(covered)}/{len(coverage)} 项已交代；"
             f"本次选中 {len(state.spec.selected_material_refs)} 份材料。"]
    for case_id in state.spec.selected_material_refs:
        scope.append(f'  · 材料 {case_id}')
    # **谁做了什么**必须写得出来：代码做的是确定性字段核对，模型做的是判断。
    # 把两者混成一句"已核查"会让"部分完成"读起来像"全部完成"。
    scope.append(f"- 基础材料覆盖：共 {progress['total']} 项要求，已处理 {progress['processed']} 项"
                 f"（已核对 {progress['items_covered']}、依据不足 {progress['items_insufficient']}、"
                 f"无法读取 {progress['items_unreadable']}、无可比较记录 {progress['items_unmatched']}、"
                 f"本次尚未处理 {progress['items_pending']}）。")
    scope.append(f"- 来源读取：系统读取 {progress['sources_read_by_system']} 条、"
                 f"模型请求读取 {progress['sources_read_by_model']} 条、"
                 f"尚未读取 {progress['sources_unread']} 条、校验未通过 {progress['sources_invalid']} 条。")
    cycles = state.model_cycles_this_delivery()
    scope.append(f"- 模型调查：这一版共完成 {cycles} 轮判断"
                 + (f"；研究中发起的检索 {len(state.research_decisions)} 次。" if state.research_decisions
                    else '；本次没有发起外部检索（普通材料核对不要求检索）。')
                 if cycles else
                 '- 这一版**没有模型参与**：以上结论全部来自代码完成的确定性字段核对，'
                 '语义解释与需要判断的条目尚未处理。')
    scope.append('- 事实版本：' + _versions_text(state))
    scope.append(f"- 本次状态：运行={_axis('run', state.run_status)}；"
                 f"交付={_axis('delivery', state.delivery_status)}；"
                 f"依据={_axis('evidence', state.evidence_status)}。")

    # 仍开着的可选问题也列在"待确认"一节里——它们确实是待确认的事，只是不阻塞。
    unresolved += optional_lines
    # 第 3 节补上**逐字段**的核对结果：行级"一致"不再是唯一说法。
    field_lines = _field_comparison_lines(state)
    if field_lines:
        differences += ['- 逐字段核对结果：'] + field_lines
    # 第 6 节把"没做到的"和"可选的"分开写，并给出当前是否已经完成的结论。
    if optional_lines:
        limits += ['- 以下为可选事项，不影响本次任务是否完成：'] + optional_lines
    unmet = [item for item in state.spec.required_requirements()
             if item['status'] != REQ_SATISFIED]
    if unmet:
        limits += ['- 本次尚未完成的必需事项：']
        for item in unmet:
            limits.append(f"  · {item['text']}（{STATUS_LABELS.get(item['status'], item['status'])}"
                          + (f'：{item["reason"]}' if item['reason'] else '') + '）')
    return {
        '1. 本次核对目标和覆盖范围': scope,
        '2. 一致项与主要变化': matched,
        '3. 差异双方的记录及来源': differences,
        '4. 仍待确认的问题': unresolved,
        '5. 就诊时可以讨论的事项': discuss,
        '6. 未覆盖范围和执行限制': limits,
    }


def _requirement_lines(state) -> list:
    """本次承诺解决什么，以及每一项目前到哪一步了。

    必需要求在前、可选建议在后——两者**不能混在一起**：一份"还有 3 项没完成"的
    报告，如果其中两项其实是可选的建议，读的人会以为整个任务都没做成。
    """
    from .contract import (KIND_LABELS, REQ_SATISFIED, STATUS_LABELS)
    required = state.spec.required_requirements()
    optional = state.spec.optional_requirements()
    lines = []
    if required:
        done = [item for item in required if item['status'] == REQ_SATISFIED]
        lines.append(f'- 本次承诺要解决 {len(required)} 项，已完成 {len(done)} 项：')
        for item in required:
            mark = '✓' if item['status'] == REQ_SATISFIED else '·'
            line = (f'  {mark} {item["text"]}（{STATUS_LABELS.get(item["status"], item["status"])}'
                    + (f'：{item["reason"]}' if item['reason'] else '') + '）')
            if item['origin'] == 'user':
                line += '〔您的要求〕'
            lines.append(line)
    if optional:
        lines.append(f'- 另有 {len(optional)} 项**可选**的进一步调查（不影响本次是否完成）：')
        for item in optional:
            lines.append(f'  · {item["text"]}'
                         + (f'（{item["reason"]}）' if item['reason'] else ''))
    return lines


def _field_comparison_lines(state) -> list:
    """逐字段的核对结果。

    "已核对"与"不可比较"分开列：一条写着"一致"的行，如果它的剂型/规格从来没比过，
    读者必须能看出来——那正是"行级一致掩盖字段未确认"的止点。
    """
    from .fields import STATUS_EQUAL, STATUS_LABELS, TERSE_FIELD_LABELS
    label = lambda row: TERSE_FIELD_LABELS.get(row['field'], row['field_label'])
    lines = []
    for ref, rows in sorted((state.field_comparisons or {}).items()):
        if not rows:
            continue
        # 名字取不到时**不要**把内部引用摆到读者面前：说清"哪一条"就够了。
        name = (state.material_items.get(ref) or {}).get('name')             or (state.material_subject_refs(ref) and '当前记录里的一条'
                or (state.spec.requirement(f'material_item:{ref}') or {}).get('description')
                or ref)
        agreed = [label(row) for row in rows if row['comparison_status'] == STATUS_EQUAL]
        parts = []
        if agreed:
            parts.append('已比对一致：' + '、'.join(agreed))
        for row in rows:
            if row['comparison_status'] == STATUS_EQUAL:
                continue
            parts.append(f"{label(row)}：{STATUS_LABELS.get(row['comparison_status'], '')}"
                         + (f'（{row["reason"]}）' if row['reason'] else ''))
        lines.append(f'- {name}：' + '；'.join(parts))
    return lines


def _assertion_line(assertion) -> str:
    subjects = '、'.join(str(item) for item in assertion.get('subject_refs') or []) or '相关记录'
    qualifiers = assertion.get('qualifiers') or {}
    detail = ''
    if qualifiers.get('excerpt'):
        detail = f"（原文：{str(qualifiers['excerpt'])[:120]}）"
    return safe_statement(f"{subjects}：{assertion.get('predicate')} = {assertion.get('value')}{detail}",
                          assertion.get('subject_refs'))


def _sources(item) -> str:
    refs = list(item.get('evidence_refs') or [])
    materials = list(item.get('material_refs') or [])
    parts = []
    if materials:
        parts.append('材料来源 ' + '、'.join(materials))
    if refs:
        parts.append('证据来源 ' + '、'.join(refs))
    return '（' + '；'.join(parts) + '）' if parts else ''


def _operation_label(operation_ref) -> str:
    return str(operation_ref)


def _disposition_text(disposition) -> str:
    return {'unreadable': '材料无法读取', 'unmatched': '没有可匹配的记录',
            'insufficient': '现有依据不足以判断',
            'pending': '本次没有核对到它（未得到结论，不代表没有问题）'}.get(disposition, disposition)


def _axis(name, value) -> str:
    return AXIS_LABELS[name].get(value, str(value))


def _versions_text(state) -> str:
    versions = state.spec.input_versions or {}
    return '、'.join(f'{key}={value}' for key, value in sorted(versions.items())) or '未记录'


def report_markdown(state, *, revision_diff=None) -> str:
    """完整报告。``revision_diff`` 非空时追加第 7 节。"""
    sections = section_content(state)
    lines = ['# 材料核对与就诊准备报告', '']
    for title in SECTIONS:
        lines += ['## ' + title, '']
        lines += sections[title] or [EMPTY_SENTENCE.get(title, '- 本次没有内容。')]
        lines += ['']
    if state.safety_checks:
        lines += ['## 强制安全检查（与本报告核对结论分别记录）', '']
        for check in state.safety_checks:
            lines.append(f"- {check['summary']}")
        lines += ['']
    if revision_diff:
        lines += ['## ' + REVISION_SECTION, '']
        lines += revision_diff + ['']
    lines += ['本报告说明本次有界核对的结果。依据不足不等于没有风险，'
              '材料未列出不代表已停用。本系统不做诊断、处方或用药调整建议；'
              '请携带本报告与医生或药师当面确认。']
    return '\n'.join(lines)


def diff_reports(previous: dict, current: dict, change: dict | None = None) -> list:
    """两份报告之间**发生了什么变化**，以及变化的依据。

    只报事实上的差异：哪些结论改变、依据是什么、哪些未决项解决了、哪些沿用、
    新增了哪些未决项。没有变化就明说没有变化——"已收到补充、相关结论未变化"是
    一个有用的答复，不是失败。

    依据里**分得清**三类输入：材料本身变了、权威记录变了、用户补充了什么。它们
    对结论的效力完全不同，混成一句"依据变了"会让"用户说了一句"看起来像"记录改了"。
    """
    if not previous:
        return []
    before, after = previous.get('sections') or {}, current.get('sections') or {}
    lines = []
    # 只比对**结论性**的几节。第 1 节是本次目标与版本，第 5 节由第 4 节派生，
    # 把它们算进"变化"只会用噪音盖住真正的变化。
    for title in ('2. 一致项与主要变化', '3. 差异双方的记录及来源',
                  '4. 仍待确认的问题', '6. 未覆盖范围和执行限制'):
        # 逐字段那几行由 `_field_changes` 单独、精确地说；在这一节里再列一遍，
        # 只会用同一批字段名把真正的结论变化淹掉。
        old = {line for line in (before.get(title) or []) if not _is_field_line(line)}
        new = {line for line in (after.get(title) or []) if not _is_field_line(line)}
        for line in sorted(new - old):
            lines.append(f'- 新增：{_plain(line)}')
        for line in sorted(old - new):
            lines.append(f'- 不再出现：{_plain(line)}')
    unchanged = sum(1 for title in ('2. 一致项与主要变化', '3. 差异双方的记录及来源',
                                    '4. 仍待确认的问题', '6. 未覆盖范围和执行限制')
                    if before.get(title) and set(before[title]) & set(after.get(title) or []))
    if unchanged:
        lines.append(f'- 沿用：{unchanged} 节中仍有条目与上一版相同。')
    if not lines:
        lines.append('- 已收到本次补充，相关结论未发生变化。')
    lines.extend(_requirement_changes(previous, current))
    lines.extend(_field_changes(previous, current))
    lines.extend(_change_basis(change, current, previous))
    return lines


def _requirement_changes(previous, current) -> list:
    """本次满足了哪一项要求、哪一项还没满足。"""
    before = {item['requirement_id']: item for item in (previous.get('requirements') or [])}
    after = {item['requirement_id']: item for item in (current.get('requirements') or [])}
    lines = []
    for requirement_id, item in sorted(after.items()):
        was = (before.get(requirement_id) or {}).get('status')
        now = item.get('status')
        if was == now:
            continue
        if now == REQ_SATISFIED:
            lines.append(f"- 本次已满足：{item['text']}（此前：{STATUS_LABELS.get(was, was or '尚未处理')}）")
        elif was == REQ_SATISFIED:
            lines.append(f"- 本次不再满足：{item['text']}（现在：{STATUS_LABELS.get(now, now)}"
                         + (f'：{item["reason"]}' if item.get('reason') else '') + '）')
    unmet = [item for item in after.values()
             if item.get('required') and item.get('status') != REQ_SATISFIED]
    optional = [item for item in after.values() if not item.get('required')]
    if unmet:
        lines.append('- 仍未完成的必需事项：' + '、'.join(item['text'] for item in unmet))
    if optional:
        lines.append('- 尚未调查的可选事项：' + '、'.join(item['text'] for item in optional)
                     + '（不影响本次是否完成）')
    return lines


def _field_changes(previous, current) -> list:
    """哪个字段的比较结果变了。"""
    from .fields import STATUS_LABELS
    def index(report):
        result = {}
        for ref, rows in (report.get('field_comparisons') or {}).items():
            for row in rows:
                result[(ref, row['field'])] = row
        return result
    before, after = index(previous), index(current)
    lines = []
    for key in sorted(after):
        now = after[key]
        was = before.get(key)
        if was is None:
            continue
        if was['comparison_status'] == now['comparison_status']:
            continue
        lines.append(f"- 字段比较变化：{now['field_label']}（{now['left_value']} / {now['right_value']}）"
                     f" 从「{STATUS_LABELS.get(was['comparison_status'], '')}」"
                     f"变为「{STATUS_LABELS.get(now['comparison_status'], '')}」")
    return lines


def _change_basis(change, current, previous) -> list:
    """变化的依据：把三类输入分开讲，谁也不冒充谁。"""
    lines = []
    change = change or {}
    material = sorted(set(change.get('material_changed') or []) |
                      set(change.get('material_added') or []) |
                      set(change.get('material_removed') or []))
    authoritative = sorted(change.get('authoritative_changed') or [])
    if material:
        # 指纹变了有两种原因：材料本身变了，或者**比较的对象**变了导致重算。
        # 说不清是哪一种就不要断言"材料改了"——那是把重算说成了新事实。
        if authoritative:
            lines.append('- 依据（比较结果）：' + '、'.join(material)
                         + ' 与当前记录的差异已按更新后的记录重新计算。')
        else:
            lines.append('- 依据（材料）：' + '、'.join(material) + ' 发生了变化。')
    if authoritative:
        lines.append('- 依据（当前记录）：' + '、'.join(authoritative)
                     + ' 发生了经确认的更新——这一项改变了比较对象本身。')
    if change.get('answers'):
        lines.append('- 依据（您的补充）：' + '；'.join(change['answers'])
                     + '（按您所说记录，**未**改变当前记录）。')
    if material or authoritative or change.get('answers'):
        lines.append(f"- 依据：本次材料/记录版本为 {_versions_text_current(current)}；"
                     f"上一版为 {_versions_text_current(previous)}。")
    else:
        lines.append(f"- 依据：材料与记录版本未变化（{_versions_text_current(current)}）。")
    return lines


def _versions_text_current(report) -> str:
    return '、'.join(f'{key}={value}' for key, value in sorted((report.get('versions') or {}).items())) or '未记录'


def _is_field_line(line) -> bool:
    """逐字段那几行(见 ``_field_comparison_lines``)在版本差异里另外处理。"""
    text = str(line)
    return (text.startswith('- 逐字段核对结果：') or '：已比对一致：' in text
            or '材料未写这一项（' in text)


def _plain(line) -> str:
    return str(line).lstrip('- ').strip()


def report_summary(state, *, delivery_status, evidence_status, gaps) -> str:
    parts = [f'交付：{_axis("delivery", delivery_status)}',
             f'依据：{_axis("evidence", evidence_status)}']
    if gaps:
        parts.append(f'尚缺 {len(gaps)} 项')
    return '；'.join(parts)


def evidence_statement(state) -> str:
    if state.evidence_status == EVIDENCE_CONFLICTING:
        return '部分依据互相冲突：报告已列出双方来源，需要人工确认，不以多数或选边的方式消除。'
    if state.evidence_status == EVIDENCE_INSUFFICIENT:
        return '部分内容依据不足：报告已明确列出哪些条目没有足够依据，依据不足不等于没有风险。'
    return '本次出现的结论都关联到仍然有效的依据。'
