"""material-review@2 —— 基础材料覆盖（**运行器**的数据准备职责）。

**这一步不是模型调查，也不能冒充模型调查。** 它做的是任何一次材料核对都必须先
做完的机械工作：枚举本次选定的材料与需要比较的记录、在既有权限路径下实际读取、
校验完整性、取出具体字段与来源位置、执行**现有的确定性字段比较**，然后把结果记
成可追溯的基础发现。

它解决的问题是具体的：模型只读一条材料就停手时，其余条目会永远停在 ``pending``，
任务因此永远是"部分交付"，而报告看起来又没有错。材料覆盖**不能**取决于模型愿不
愿意逐条调用工具。

三条纪律：

* 只有**真的读过并通过校验**的内容才标记为已覆盖。材料索引里出现过一个条目
  不算核实过原文。
* 读取受现有时间、取消与批次机制约束；长材料按页读取并保存进度，截断、失败与
  未处理部分都被明确记下来，而不是被当成"没有差异"。
* 结构化的 CSV 字段可以直接比较；OCR 或自由文本解析结果只保留候选属性与置信
  信息，不因为进了覆盖阶段就升级成权威事实。
"""
from __future__ import annotations

from .fields import TERSE_FIELD_LABELS
from .state import (CREDENTIAL_MODEL, CREDENTIAL_SYSTEM, FINDING_CONTEXTUAL_NOTE,
                    FINDING_DISCREPANCY, FINDING_MATCHED, FINDING_MISSING_FIELD,
                    INPUT_MATERIAL_NOTE, INTEGRITY_FAILED, INTEGRITY_VERIFIED,
                    ISSUE_SOURCE_INVALID, KIND_LABELS, ORIGIN_SYSTEM)

# 一次 pass 处理多少条材料条目、每条读多少字、一条最多翻几页。三个上限都是为了让
# "基础覆盖"真的是有界的：它不消耗模型规划调用，但同样不能无限跑。
COVERAGE_BATCH = 12
PAGE_CHARS = 2000
MAX_PAGES_PER_ITEM = 4

# 需要逐字段比较的字段清单在 ``fields.ALL_FIELDS``；这里只保留"核心字段"的说明，
# 因为**只有核心字段比不出结果才意味着这条材料还没核对完**。


def run_coverage_pass(state, material_index, *, batch_limit=None, at=None) -> dict:
    """执行一次**有界**的基础覆盖。返回这一趟做了什么，不改交付状态。

    它是幂等的：已经读过、指纹没变的条目直接跳过；材料版本变了的条目重新读取，
    受影响的判断按既有规则失效。崩溃恢复不会重复已经完成的有效覆盖。
    """
    if material_index is None:
        return {'processed': [], 'skipped': 0, 'failed': [], 'unprocessed': [],
                'truncated': [], 'note': '没有可用的材料索引；本次没有执行基础覆盖。'}
    limit = max(1, int(batch_limit or COVERAGE_BATCH))
    cursor = state.coverage_cursor or {}
    offsets = {str(key): int(value) for key, value in (cursor.get('offsets') or {}).items()}
    case_ids = list(state.spec.selected_material_refs)
    index = material_index.index()
    materials = [material for material in (index.get('materials') or [])
                 if material.get('case_id') in set(case_ids)]

    processed, skipped, failed, unprocessed, truncated = [], 0, [], [], []
    pages_used = 0
    cases_seen = []
    for material in materials:
        case_id = material.get('case_id')
        cases_seen.append(case_id)
        _read_case(state, material_index, material, at=at)
        for item in material.get('items') or []:
            ref = f"{case_id}/{item['item_id']}"
            if not _needs_reading(state, ref, item):
                skipped += 1
                continue
            if len(processed) >= limit or pages_used >= limit * MAX_PAGES_PER_ITEM:
                # 超出本次批次：**明确记为未处理**，下一趟从这里继续——不是消失。
                unprocessed.append(ref)
                continue
            outcome = _read_item(state, material_index, material, item, ref,
                                 offsets.get(ref, 0), at=at)
            if outcome['status'] == 'unreadable':
                failed.append(ref)
                unprocessed.append(ref)
                continue
            if outcome['status'] == 'partial':
                # 长材料这一趟没读完：**把读到的位置存下来**，下一趟从这里继续。
                # 不存的话每一趟都会从第 0 页重来——看起来在"分批"，实际永远
                # 读不完，而条目会一直留在 truncated 里。
                offsets[ref] = outcome['next_offset']
                truncated.append(ref)
                unprocessed.append(ref)
                pages_used += outcome['pages']
                continue
            offsets.pop(ref, None)
            pages_used += outcome['pages']
            processed.append(ref)

    state.coverage_cursor = {'offsets': offsets, 'unprocessed': unprocessed,
                             'truncated': truncated, 'failed': failed,
                             'at': at, 'batch': len(processed)}
    run = {'at': at, 'batch': len(processed), 'skipped': skipped,
           'processed': processed, 'unprocessed': unprocessed,
           'truncated': truncated, 'failed': failed, 'cases': cases_seen}
    state.coverage_runs.append(run)
    state.sync_coverage()
    return run


# ---- 单条材料 ----------------------------------------------------------------

def _needs_reading(state, ref, item) -> bool:
    """这条还需要读吗。

    已读过、**材料版本没变**、**没有新的用户说明**的条目直接跳过——那是已经完成
    的有效覆盖。三者任意一个变了就重做：材料改版让旧凭据作废，用户补的说明让
    上一条结论过时。
    """
    from .contract import item_fingerprint
    fingerprint = item_fingerprint(item)
    if state.material_fingerprints.get(ref) != fingerprint:
        state.material_fingerprints[ref] = fingerprint
        return True
    signature = (fingerprint, tuple(sorted(answer_ids_for(state, ref))))
    if (state.material_items.get(ref) or {}).get('read_signature') != signature:
        return True
    return state.read_credential(ref) not in (CREDENTIAL_SYSTEM, CREDENTIAL_MODEL)


def user_supplied_fields(state, ref) -> dict:
    """用户对**这份材料**的说明里补上的字段。

    它们补的是"材料上写的是什么"，因此可以用于比较——但它们在报告里必须标明
    来源是用户的说明，不是材料原文。这两者效力完全不同。
    """
    supplied = {}
    for answer in state.answers:
        target = answer.get('target') or {}
        material_ref = str(target.get('material_ref') or '')
        field = answer.get('field')
        if material_ref == ref and field:
            supplied[str(field)] = {'value': answer['value'],
                                    'answer_id': answer['answer_id'],
                                    'verification_status': answer['verification_status']}
    return supplied


def user_stated_record_fields(state, ref) -> dict:
    """用户就**当前记录**里缺的那一格所作的说明。

    真实场景：材料上写了规格，而当前记录里根本没有这一列。这时"S规格是否一致"
    比不出来，除非知道记录里应该是什么。用户说了之后，比较**可以**得出结果——
    但那一格必须标明来自用户的说明，而且**当前记录分毫未动**（要真的写进去，
    得走既有的确认流程）。不这么做，"确认规格"就会变成一件永远做不完的事：
    用户答了，结论还是"未知"。
    """
    known = state.medications_by_ref()
    subject_refs = set(state.material_subject_refs(ref))
    names = {str((known.get(item) or {}).get('display_name')) for item in subject_refs}
    names.discard('None')
    supplied = {}
    for answer in state.answers:
        target = answer.get('target') or {}
        if str(target.get('material_ref') or ''):
            continue  # 那是材料侧的说明，不是记录侧的
        field = answer.get('field')
        if not field:
            continue
        if not (set(str(s) for s in answer.get('subjects') or []) & (subject_refs | names)):
            continue
        supplied[str(field)] = {'value': answer['value'],
                                'answer_id': answer['answer_id'],
                                'verification_status': answer['verification_status']}
    return supplied


def answer_ids_for(state, ref) -> list:
    wanted = str(ref)
    return [answer['answer_id'] for answer in state.answers
            if str((answer.get('target') or {}).get('material_ref') or '') == wanted]


def _effective_fields(state, ref, detail, item) -> tuple:
    """(材料字段, 字段来源)。``parsed`` 之外的部分来自**用户说明**，单独标记。"""
    parsed = dict(detail.get('fields') or item.get('fields') or {})
    sources = {field: 'material' for field in parsed if parsed.get(field)}
    for field, entry in user_supplied_fields(state, ref).items():
        if not parsed.get(field):
            parsed[field] = entry['value']
            sources[field] = 'user_statement'
    return parsed, sources


def _read_case(state, material_index, material, *, at) -> dict:
    """材料这一份文档本身：确认它被真正打开过，而不是只出现在目录里。"""
    case_id = material.get('case_id')
    ref = f'case:{case_id}'
    document_id = material.get('document_id')
    parser_version = material.get('parser_version')
    integrity = INTEGRITY_VERIFIED if document_id else INTEGRITY_FAILED
    state.record_source_read(
        ref, credential=CREDENTIAL_SYSTEM, fingerprint=(document_id, parser_version),
        integrity=integrity, document_id=document_id, parser_version=parser_version,
        item_count=material.get('item_count'), at=at)
    if integrity != INTEGRITY_VERIFIED:
        state.add_issue(operation_ref=ref, category=ISSUE_SOURCE_INVALID,
                        user_visible_summary=f'材料 {case_id} 缺少可核验的文档标识，无法确认读取的是哪一份原文。',
                        detail={'case_id': case_id})
    return {'ref': ref, 'integrity': integrity}


def _read_item(state, material_index, material, item, ref, offset, *, at) -> dict:
    """读一条材料条目：真实读取 → 校验 → 有界分页 → 确定性比较 → 落成基础发现。"""
    case_id, item_id = ref.split('/', 1)
    try:
        detail = material_index.item(case_id, item_id)
    except Exception as exc:
        state.record_source_read(ref, credential=CREDENTIAL_SYSTEM, integrity=INTEGRITY_FAILED,
                                 at=at, error=type(exc).__name__)
        state.add_issue(operation_ref=ref, category=ISSUE_SOURCE_INVALID,
                        user_visible_summary=f'材料条目 {ref} 无法读取（{type(exc).__name__}），'
                                             f'这一条没有核对到，不代表它与当前记录一致。',
                        detail={'error': type(exc).__name__})
        return {'status': 'unreadable', 'ref': ref, 'pages': 0}

    document_id = detail.get('document_id')
    integrity = INTEGRITY_VERIFIED if document_id else INTEGRITY_FAILED
    text = _source_text(detail)
    if offset >= len(text):
        offset = 0  # 位置超出了当前原文（材料换了一版）：从头读，不读到空串。
    chunk = text[offset:offset + PAGE_CHARS]
    next_offset = offset + len(chunk)
    complete = next_offset >= len(text)
    state.record_source_read(
        ref, credential=CREDENTIAL_SYSTEM,
        fingerprint=state.material_fingerprints.get(ref),
        integrity=integrity, document_id=document_id,
        parser_version=detail.get('parser_version'), at=at,
        chars_total=len(text), chars_read=next_offset, truncated=not complete,
        locations=detail.get('locations') or {}, kind=detail.get('kind'),
        pages=(state.source_reads.get(ref, {}).get('reads') or 1))
    if integrity != INTEGRITY_VERIFIED:
        state.add_issue(operation_ref=ref, category=ISSUE_SOURCE_INVALID,
                        user_visible_summary=f'材料条目 {ref} 缺少文档标识，读取未通过完整性校验。',
                        detail={'case_id': case_id, 'item_id': item_id})
        return {'status': 'unreadable', 'ref': ref, 'pages': 1}

    if not complete:
        # 这一趟只读了一部分：**不下结论、不记账**。凭据与读到的位置留着，下一趟
        # 接着读。只写了半条就落一条发现，等于拿半句话当整条材料的结论。
        return {'status': 'partial', 'ref': ref, 'pages': 1, 'next_offset': next_offset,
                'finding_id': None}
    comparison = _compare(state, item, detail, ref)
    # 字段级比较**逐条留档**：行级结论是派生出来的，原始比较才是可以复算的东西。
    state.record_comparisons(ref, comparison['comparisons'])
    previous_finding_id = (state.material_items.get(ref) or {}).get('finding_id')
    finding, question = materialize_item(state, item, detail, ref, comparison=comparison, at=at)
    if finding is not None:
        state.material_items[ref]['finding_id'] = finding['finding_id']
        _supersede(state, ref, previous_finding_id, finding['finding_id'])
    _settle(state, ref, item, detail, comparison, finding, question, at=at)
    # 记账只在**读完整条**之后：半条的签名会让下一趟以为"这条已经读过了"。
    state.material_items.setdefault(ref, {})['read_signature'] = (
        state.material_fingerprints.get(ref), tuple(sorted(answer_ids_for(state, ref))))
    return {'status': 'ok', 'ref': ref, 'pages': 1, 'next_offset': next_offset,
            'finding_id': (finding or {}).get('finding_id')}


def _source_text(detail) -> str:
    """条目的原文表示。它是**材料**，不是患者事实——未经确认不升级为权威记录。"""
    import json
    original = detail.get('original_fields') or {}
    fields = detail.get('fields') or {}
    return json.dumps({'original_fields': original, 'parsed_fields': fields,
                       'corrections': detail.get('corrections') or []}, ensure_ascii=False)


def _compare(state, item, detail, ref) -> dict:
    """**字段级**比较：材料候选字段 vs 当前记录字段。

    每个字段一条 :func:`fields.compare_field`，带双方的值、来源与版本。行级摘要
    由这些结果派生，不由 ``ProductStore.recompute`` 的 ``kind`` 决定——那个词把
    "对上了"和"根本没比过"压成了同一个意思。

    用户补充的**材料说明**也参与比较，但每个字段都带着自己的来源，报告里分得清
    哪一格是材料原文、哪一格是用户说的是什么。
    """
    from .fields import ALL_FIELDS, compare_field, summarize
    known = state.medications_by_ref()
    recorded_refs = [str(value) for value in (item.get('current') or [])]
    current = next((known.get(value) for value in recorded_refs if known.get(value)), None)
    fields, sources = _effective_fields(state, ref, detail, item)
    # 记录侧：用户就"当前记录里应该是多少"所作的说明。它只用于比较，**不写回记录**。
    stated = user_stated_record_fields(state, ref)
    versions = {'material': state.material_fingerprints.get(ref),
                'record': state.spec.input_versions.get('medications')}
    comparisons = []
    for field in ALL_FIELDS:
        if field == 'name':
            left, right = fields.get('name'), (current or {}).get('display_name')
        elif field == 'unit':
            # 记录把单位写在剂量文字里（``0.5g``）；材料把它单列一栏。
            left = fields.get('unit')
            right = _unit_of((current or {}).get('dose'))
        else:
            left = _material_value(fields, field)
            right = _recorded_value(current, field) if current else None
        right_source = 'authoritative_record'
        if right is None and field in stated:
            right, right_source = stated[field]['value'], 'user_statement'
        row = compare_field(field, left=left, right=right,
                            left_source=ref,
                            right_source=(current or {}).get('ref') or (recorded_refs[0] if recorded_refs else None),
                            left_version=versions['material'], right_version=versions['record'])
        row['subject_ref'] = (current or {}).get('ref') or (recorded_refs[0] if recorded_refs else None)
        row['value_sources'] = {'left': sources.get(field) or 'material',
                                'right': right_source}
        comparisons.append(row)
    summary = summarize(comparisons)
    return {'comparisons': comparisons, 'summary': summary,
            'parsed_by': detail.get('parser_version'),
            'material_fields': {key: value for key, value in fields.items() if value},
            'field_sources': sources, 'recorded_refs': recorded_refs,
            'derived': _derive_kind(item.get('kind'), summary)}


def _unit_of(dose_text):
    """从记录的剂量文字里取出单位：``0.5g`` → ``g``。取不到就是取不到。"""
    from .fields import normalize_field
    parsed = normalize_field('dose', dose_text) or {}
    return parsed.get('unit')


def _summary_fields(comparison) -> dict:
    """供报告与检查使用的"字段 → 结果"视图。"""
    return {row['field']: {'observed': row['comparison_status'],
                           'recorded': row['right_value'], 'material': row['left_value'],
                           'source': (row.get('value_sources') or {}).get('left')}
            for row in comparison.get('comparisons') or []}


def _recorded_value(medication, field):
    """当前记录里与材料字段**对得上**的那一格。

    剂量在权威记录里是 ``0.5g`` 这样的**带单位文字**，材料的剂量与单位是两个字段；
    开始时间在记录里叫 ``start_at``。照着字面取 ``dose``/``date`` 会让一条一致的
    记录被比成"不一致"——那是比较写错了，不是发现了差异。
    """
    if field == 'date':
        value = medication.get('start_at')
        return str(value)[:10] if value else None
    return medication.get(field)


def _material_value(fields, field):
    """材料侧的对应值：剂量在这里也要先拼回"数字+单位"。"""
    if field == 'dose':
        dose, unit = fields.get('dose'), fields.get('unit')
        return f'{dose}{unit}' if (dose and unit) else dose
    return fields.get(field)


def _derive_kind(parsed_kind, summary) -> str:
    """这一条到底一致还是不一致——**由核心字段的比较结果派生**。

    ``not_listed``（材料没写这条记录）与 ``possible_duplicate``（可能重复）不是
    字段问题，保留解析结果：前者本身就是结论，后者是身份歧义，比较再多字段也
    解决不了。
    """
    if parsed_kind in ('not_listed', 'possible_duplicate'):
        return parsed_kind
    core = summary.get('core') or {}
    if core.get('undecided'):
        return 'unresolved'
    return 'changed' if core.get('different') else 'same'


def materialize_item(state, item, detail, ref, *, comparison=None, at=None):
    """把一条材料条目落成**基础发现**（必要时附带一条系统问题）。

    这一层是代码完成的：材料与当前记录的字段比对是确定性的，让模型重算一遍只会
    引入漂移。模型真正要做的是决定**差异之外还要查什么**。

    返回值是 ``(finding, question)``。重新处理同一条时会**就地更新**
    ``material_items[ref]``，不整块替换——它的 ``read_signature`` 和上一条发现
    的 id 都是"这一条已经处理到哪了"的进度，替换掉就等于把进度清零。
    """
    parsed_kind = item.get('kind')
    if not parsed_kind:
        return None, None
    comparison = comparison or {}
    kind = comparison.get('derived') or parsed_kind
    fields = comparison.get('material_fields') or (detail.get('fields') or {}) \
        or (item.get('fields') or {})
    current = [str(value) for value in (item.get('current') or [])]
    name = fields.get('name') or _name_from_records(state, current) or '（未命名条目）'
    state.material_items[ref] = {
        **state.material_items.get(ref, {}),
        'name': fields.get('name'), 'kind': kind, 'parsed_kind': parsed_kind,
        'current': current,
        'fields': {key: fields.get(key) for key in
                   ('dose', 'unit', 'schedule', 'date', 'route')},
        'comparison': {**_summary_fields(comparison),
                       'summary': comparison.get('summary') or {},
                       'parsed_by': comparison.get('parsed_by')},
        'read_at': at}
    label = KIND_LABELS.get(kind, kind)
    statement = _statement(name, label, fields, current, item.get('issues'),
                           supplied=comparison.get('field_sources') or {},
                           summary=comparison.get('summary') or {})
    question = None
    if kind in ('unresolved', 'possible_duplicate'):
        # 字段不足 / 可能重复**不落成结论**：它们落成一条必须被处置的待确认项，
        # 加一条点名说明的系统问题。写进"差异"一节会把猜测说成事实。
        question = state.submit_question(
            question_key='reconcile:' + kind,
            text=f'{name}：{label}' + ('（' + '、'.join(str(i) for i in item.get('issues') or []) + '）'
                                       if item.get('issues') else ''),
            subjects=[name], origin=ORIGIN_SYSTEM, related_material_refs=[ref])
        finding = state.add_finding(
            finding_type=FINDING_CONTEXTUAL_NOTE, statement=statement,
            origin=ORIGIN_SYSTEM, material_refs=[ref], subject_refs=[name],
            assessment_status='verified', question_refs=[question['question_id']])
        return finding, question
    finding = state.add_finding(
        finding_type={'same': FINDING_MATCHED, 'changed': FINDING_DISCREPANCY,
                      'new': FINDING_DISCREPANCY, 'not_listed': FINDING_MISSING_FIELD}.get(
                          kind, FINDING_CONTEXTUAL_NOTE),
        statement=statement, origin=ORIGIN_SYSTEM, material_refs=[ref],
        subject_refs=[name], assessment_status='verified', question_refs=[])
    return finding, None


def _close_reconcile_questions(state, ref, comparison, supplied) -> None:
    """这一条查清楚了，就把它留下的"需要核实"问题**关掉**。

    不关的话，一条早就被解决的事项会永远挂在"仍待确认的问题"一节里，用户每次
    打开都看到同一句问话——而它是上一次的。关闭要写清凭什么：依据是代码比出来的
    确定性结果，或者用户说明加确定性比较。
    """
    for question in state.questions:
        if question['status'] != 'open' or ref not in (question.get('related_material_refs') or []):
            continue
        if not str(question.get('question_key') or '').startswith('reconcile:'):
            continue
        kinds = '、'.join(f'{field}={entry["observed"]}'
                          for field, entry in sorted((comparison or {}).get('fields', {}).items()))
        summary = ('已按确定性字段比较得出结论（' + kinds + '）'
                   if not supplied else
                   '已按您补充的说明补上缺项，并与当前记录完成确定性比较（' + kinds + '）')
        state.submit_question(question_key=question['question_key'], text=question['text'],
                              subjects=question['subjects'], origin=question['origin'],
                              status='answered', resolution_summary=summary)


def _supersede(state, ref, previous_finding_id, current_finding_id) -> None:
    """同一条材料重新得出的结论**取代**上一条。

    不取代的话，报告里会同时出现"字段不完整"和"与当前记录不一致"两个说法，而且
    都像是当前的。被取代的那条留在历史里（可回看、可审计），但不再是当前结论。
    """
    if not previous_finding_id or previous_finding_id == current_finding_id:
        return
    previous = next((item for item in state.findings
                     if item['finding_id'] == previous_finding_id), None)
    if previous is None or previous.get('stale'):
        return
    previous['stale'] = True
    previous['superseded_by'] = current_finding_id
    state.invalidation_log.append({'reason': 'material_item_reconciled', 'ref': ref,
                                   'superseded': previous_finding_id,
                                   'superseded_by': current_finding_id})


def _settle(state, ref, item, detail, comparison, finding, question, *, at) -> None:
    """给这条覆盖要求一个**明确**处置。默认是 ``covered``；只有具体理由才降级。"""
    requirement_id = f'material_item:{ref}'
    if state.spec.requirement(requirement_id) is None:
        return
    kind = (comparison or {}).get('derived') or item.get('kind')
    supplied = {field for field, source in (comparison or {}).get('field_sources', {}).items()
                if source == 'user_statement'}
    if kind is None:
        # 读得到、但索引里没有给出比较结果：没有可比较的对象。这是 ``unmatched``
        # （正常无匹配），不是故障，也不能写成"没有差异"。
        state.set_coverage(requirement_id, 'unmatched',
                           finding_id=(finding or {}).get('finding_id'),
                           reason='这一条没有可比较的当前记录')
        return
    if kind == 'unresolved':
        from .contract import COMPARED_FIELD_LABELS
        undecided = (comparison.get('summary') or {}).get('core', {}).get('undecided_fields') or []
        detail_text = '、'.join(COMPARED_FIELD_LABELS.get(field, field) for field in undecided) \
            or '、'.join(str(value) for value in item.get('issues') or []) or '字段不足'
        state.set_coverage(requirement_id, 'insufficient',
                           finding_id=(finding or {}).get('finding_id'),
                           reason=f'{detail_text}还没有可比对的值，无法判断是否一致')
        _request_fields(state, ref, detail, item, question, at=at)
    elif kind == 'possible_duplicate':
        state.set_coverage(requirement_id, 'insufficient', finding_id=(finding or {}).get('finding_id'),
                           reason='材料中有不止一条条目可能对应同一条当前记录，无法确定对应关系')
        _request_fields(state, ref, detail, item, question, at=at)
    elif kind == 'not_listed':
        # "材料没写这个药"本身就是必须报告的结论，不是"没有差异"。
        state.set_coverage(requirement_id, 'covered', finding_id=(finding or {}).get('finding_id'),
                           reason='材料未列出这条当前记录，已作为已知差异记下')
    else:
        reason = None
        if supplied:
            # 结论**确实变了**：原来是"字段不完整"，用户说明了材料上写的是什么之后
            # 比出了结果。凭据要写清楚——一致/不一致是比出来的，材料内容那一格是
            # 用户说的。当前记录没有被改动。
            reason = ('材料这一条缺少的字段已按您的说明补上（' + '、'.join(sorted(supplied))
                      + '），比较结果据此得出；当前记录未做任何修改')
        state.set_coverage(requirement_id, 'covered',
                           finding_id=(finding or {}).get('finding_id'), reason=reason)
        _close_reconcile_questions(state, ref, comparison, supplied)


def _request_fields(state, ref, detail, item, question, *, at) -> None:
    """字段不足时，系统**自己**建一条补充请求——不依赖模型记得去问。

    请求声明目标对象与用途：前端不靠解析自然语言问句来猜要写回哪条记录。它默认是
    「材料说明」，也就是说用户补的内容会被记成**说明或候选事实**，不会隐式改动
    当前药单。
    """
    fields, _ = _effective_fields(state, ref, detail, item)
    name = fields.get('name') or _name_from_records(state, [str(v) for v in item.get('current') or []])
    # **还缺什么**要按补过之后的字段算：用户已经说明过的字段不该再问一遍。
    undecided = _undecided_fields(state, ref)
    missing = [field for field in ('dose', 'unit', 'schedule', 'date', 'form', 'strength')
               if not fields.get(field) and field in undecided]
    if not name or not missing:
        return
    # 这条请求挡住哪一项必需要求：**推导出来的**，不是白名单。挡住必需项它才会
    # 让任务进入等待；不挡任何必需项时它只是一条可选建议。
    #
    # 挡住的是**这份材料补一句就能比出结果**的那些字段，不只是名字对得上的那些：
    # 剂量比不出来是因为材料没写单位，那么"确认剂量是否一致"这一项同样被这条请求
    # 挡着。但"当前记录里根本没有这一项"（``missing_right``）不在其中——用户说明
    # 材料上写了什么，并不会让当前记录长出那一列。
    fixable = sorted(field for field in undecided
                     if _status_of(state, ref, field) != 'missing_right')
    blocks = state.requirements_blocked_by_missing(ref, fixable)
    state.add_input_request(
        question_text=f'材料里 {name} 这一条缺少' + '、'.join(_FIELD_LABELS.get(f, f) for f in missing)
                      + '，请补充材料上实际写的内容。',
        related_question_ids=[question['question_id']] if question else [],
        required_fields=missing or ['dose'],
        why_needed='缺这几项就无法判断材料与当前记录是否一致。',
        subjects=[name],
        purpose=INPUT_MATERIAL_NOTE,
        # 缺的**具体事实**说的是"这几格还没有可比对的值"，而不是只报要填的那两格：
        # 用户要知道自己补的这一项会让哪几件事有结论。
        # 缺的是"哪几项事实"，按**字段**说一次。按条目重复会说成
        # "规格、规格、规格"——三个对象各有一个规格，缺的是同一件事。
        missing_fact='、'.join(dict.fromkeys(
            _FIELD_LABELS.get(field, field) for field in sorted(undecided))),
        why_material_insufficient='材料与当前记录至少有一边没有这一项的值。',
        target={'material_ref': ref, 'subjects': [name], 'fields': missing,
                'case_id': ref.split('/', 1)[0], 'item_id': ref.split('/', 1)[1]},
        blocks_requirement_ids=blocks,
        # 材料改版之后又缺同一项时**重新问**：之前那条回答针对的是旧版本的材料，
        # 它不能替新版本作答。版本没变则不重复打扰用户。
        reopen=True, origin=ORIGIN_SYSTEM)


def _undecided_fields(state, ref) -> set:
    """这条材料**还没比出结果**的字段。空集合表示没有要问的。"""
    from .fields import UNDECIDED_STATUSES
    return {row['field'] for row in state.comparisons_for(ref)
            if row['comparison_status'] in UNDECIDED_STATUSES}


def _status_of(state, ref, field):
    for row in state.comparisons_for(ref):
        if row['field'] == field:
            return row['comparison_status']
    return None


# 字段名到用户读得懂的中文。**不能落下任何一个**：漏掉的会以原始英文键的形式
# 出现在"请补充……"这句话里（真实批次里出现过"缺 form、strength"）。
_FIELD_LABELS = dict(TERSE_FIELD_LABELS)


def _name_from_records(state, refs):
    known = state.medications_by_ref()
    for ref in refs or []:
        medication = known.get(str(ref))
        if medication and medication.get('display_name'):
            return str(medication['display_name'])
    return None


def _statement(name, label, fields, current, issues, supplied=None, summary=None):
    """一条材料的行级陈述。

    标题里的"一致/不一致"**是比出来的**，而且只在核心字段比完之后才敢这么说。
    记录里根本没有的那几项（剂型、规格…）单独写一句——"材料写了、当前记录没有
    这一项"。以前它们被塞进同一句末尾的"未决问题"里，读者看到的就是一行写着
    "一致"、末尾却问"请核实规格"的话。
    """
    parts = [f'{name}：{label}']
    if current:
        parts.append('当前记录 ' + '、'.join(str(ref) for ref in current))
    labels = {'dose': '剂量', 'schedule': '频次', 'date': '日期', 'unit': '单位'}
    supplied = supplied or {}
    order = ('dose', 'unit', 'schedule', 'date')
    from_user = [f'{labels.get(key, key)}={fields.get(key)}'
                 for key in order if fields.get(key) and supplied.get(key) == 'user_statement']
    from_material = [f'{labels.get(key, key)}={fields.get(key)}'
                     for key in order if fields.get(key) and supplied.get(key) != 'user_statement']
    if from_material:
        parts.append('材料 ' + '、'.join(from_material))
    if from_user:
        # **分得清**哪一格是材料上写的、哪一格是您说的是什么——这就是"材料说明"
        # 与"材料原文"的区别，混在一起会把用户的转述读成材料的内容。
        parts.append('按您补充的说明 ' + '、'.join(from_user))
    summary = summary or {}
    absent = summary.get('record_absent_labels') or []
    if absent:
        parts.append('当前记录里没有' + '、'.join(absent) + '可供比较')
    undecided = (summary.get('core') or {}).get('undecided_fields') or []
    if undecided:
        # 用**短标签**（频次/途径），不用完整标签（服用频次/给药途径）：这一行把
        # 几个字段名连在一起写，"服用…给药"这样的相邻字样会撞上交付前的处方措辞
        # 检查，于是"材料未列出这条当前记录"这种**最该被读到**的发现反而被整句
        # 撤下。换的是标签，**检查本身没有放宽**——值仍然原样参与那道检查。
        from .fields import TERSE_FIELD_LABELS
        parts.append('还缺 ' + '、'.join(TERSE_FIELD_LABELS.get(f, f) for f in undecided)
                     + '，无法判断是否一致')
    if issues:
        parts.append('未决问题：' + '、'.join(str(item) for item in issues))
    return '；'.join(parts)


def coverage_summary(state) -> str:
    """给报告与界面用的一句话覆盖情况。数字来自实际处理记录。"""
    progress = state.coverage_progress()
    parts = [f'本次选中并核对 {progress["total"]} 项要求，已交代 {progress["processed"]} 项']
    if progress['by_disposition'].get('covered'):
        parts.append(f'其中 {progress["by_disposition"]["covered"]} 项已核对一致/已记下差异')
    if progress['items_insufficient']:
        parts.append(f'{progress["items_insufficient"]} 项依据不足或字段不完整')
    if progress['items_unreadable']:
        parts.append(f'{progress["items_unreadable"]} 项无法读取')
    if progress['items_pending']:
        parts.append(f'{progress["items_pending"]} 项本次尚未处理')
    return '；'.join(parts) + '。'
