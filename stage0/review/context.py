"""material-review@2 —— 调用模型前的可信上下文准备。

这一段此前是**模型的工作**：先 ``memory_read(snapshot)``、再 ``list_materials``、
再自己把材料候选与当前记录对齐、再判断哪些差异没处理。这些都是机械步骤，而且
每错一步都会被算成"模型的调查能力不足"。现在由代码准备：

* 完整可信事实的**版本引用**（模型不需要选择权威快照）；
* 与本次目标有关的事实摘要（有界，且**带省略标记**）；
* 已选材料的索引与确定性字段差异（含双方具名）；
* 用户明确提出的问题；
* 已完成事项与待处理事项；
* 有效证据；
* 当前约束与剩余预算。

摘要**有省略**：完整事实仍由代码保留并用于安全校验，这里给的是子集，并且明说
哪里被省略——截断后的子集不冒充全部事实。
"""
from __future__ import annotations

from .contract import OUTPUT_LABELS, item_fingerprint
from .state import CREDENTIALS_SUPPORTING, KIND_LABELS, ORIGIN_SYSTEM

FACT_ITEM_CHARS = 600
EVIDENCE_EXCERPT_CHARS = 300


def _truncate(text, limit=FACT_ITEM_CHARS):
    text = str(text or '')
    if len(text) <= limit:
        return text
    return text[:limit] + f'…[截断，原文{len(text)}字]'


def prepare_context(*, state, material_index, facts, versions, budget,
                    evidence_store=None) -> dict:
    """构造模型视图的 ``trusted_context``。

    这里不做任何安全判断，也不改变任何边界：它只是把代码已经持有的可信事实
    摆到模型面前，并明确标出省略与不确定。
    """
    spec = state.spec
    case_ids = set(spec.selected_material_refs)
    materials = []
    omissions = []
    if material_index is not None:
        index = material_index.index()
        for material in index.get('materials') or []:
            if material.get('case_id') not in case_ids:
                # 用户没选的材料不进入本次范围。自动匹配只发生在被选中的材料里。
                continue
            items = []
            for item in material.get('items') or []:
                ref = f"{material['case_id']}/{item['item_id']}"
                kind = item.get('kind')
                fields = item.get('fields') or {}
                items.append({
                    'material_ref': ref,
                    'kind': kind,
                    'kind_label': KIND_LABELS.get(kind, kind),
                    'fields': {key: _truncate(value) for key, value in fields.items() if value},
                    'issues': list(item.get('issues') or []),
                    'current': list(item.get('current') or []),
                    'confirmed': bool(item.get('confirmed')),
                    # 原文**已经被有效读过**（系统或模型）；谁读的另外标明。模型
                    # 必须能分清"这条代码已经读过"和"这条我还没看过"，否则它会
                    # 重复读一遍，或者把系统的确定性比较误当成自己的发现。
                    'read_back': state.read_credential(ref) in CREDENTIALS_SUPPORTING,
                    'read_by': {'system_read': 'system', 'model_requested_read': 'model'}
                               .get(state.read_credential(ref)),
                    'source': _locator(item.get('locations')),
                })
            materials.append({'case_id': material.get('case_id'),
                              'document_id': material.get('document_id'),
                              'status': material.get('status'),
                              'parser_version': material.get('parser_version'),
                              'item_count': material.get('item_count'),
                              'pending_count': material.get('pending_count'),
                              'items': items})
        if index.get('omitted_items'):
            omissions.append({'section': 'materials', 'omitted_count': index['omitted_items']})
    facts_view, fact_omissions = _facts_summary(facts)
    omissions.extend(fact_omissions)

    evidence = []
    if evidence_store is not None:
        for ref in state.evidence_refs[:24]:
            meta = evidence_store.get_meta(ref) or {}
            evidence.append({'evidence_id': ref,
                             'source_uri': meta.get('source_uri'),
                             'retrieved_at': meta.get('retrieved_at'),
                             'publication_date': meta.get('publication_date', 'unknown'),
                             'corpus_version': meta.get('corpus_version'),
                             'read_back': ref in state.read_evidence_refs,
                             'scope': meta.get('scope_id')})
        if len(state.evidence_refs) > 24:
            omissions.append({'section': 'evidence', 'omitted_count': len(state.evidence_refs) - 24})

    done, pending = _todo(state, spec)
    return {
        'authority_versions': dict(versions),
        'authority_snapshot_ref': dict(spec.authoritative_snapshot_ref),
        'authority_note': ('完整事实由代码保留并用于安全校验；下面是本次目标相关的摘要，'
                           '带 __omitted__ 标记的部分没有全部展示。'),
        'facts_summary': facts_view,
        'materials': materials,
        'deterministic_differences': [item for material in materials for item in material['items']
                                      if item['kind'] != 'same'],
        # 基础覆盖的**实际处理记录**：哪些条目代码已经读过并比较完、哪些读不到、
        # 哪些字段不足、哪些还没处理。模型据此决定"差异之外还要查什么"，而不是
        # 把代码做完的确定性比较再算一遍。
        'base_coverage': {
            'progress': state.coverage_progress(),
            'source_reads': state.read_attribution(),
            'note': ('以下条目由运行器实际读取并完成确定性字段比较，'
                     'read_by=system 的条目**不需要**你再用 read_material 读一遍；'
                     '系统读过不等于你已经理解原文，涉及语义解释时仍要自己读回并给出片段。'
                     'read_by 为空的条目目前还没有人读过原文。'),
        },
        'user_answers': [{'request_id': item['request_id'], 'field': item.get('field'),
                          'value': item['value'], 'kind': item['kind'],
                          'subjects': item['subjects'],
                          'verification_status': item['verification_status'],
                          'note': '用户补充**不是**权威事实，也不是材料原文；'
                                  '要采纳它必须说明依据。'}
                         for item in state.answers],
        'answering_open_requests': [item['request_id'] for item in state.open_input_requests()],
        'user_questions': [spec.user_goal] + [q['text'] for q in state.questions
                                              if q['origin'] == 'user'],
        'completed': done,
        'pending': pending,
        'coverage': [{'requirement_id': r['requirement_id'], 'kind': r['kind'], 'ref': r['ref'],
                      'description': r['description'], 'disposition': r['disposition']}
                     for r in spec.coverage_requirements],
        'requested_outputs': [{'key': key, 'label': OUTPUT_LABELS.get(key, key)}
                              for key in spec.requested_outputs],
        'evidence': evidence,
        'evidence_note': 'evidence 只是引用与元数据；原文请用 read_material / research_evidence 读取。',
        'constraints': {
            'forbidden': ['诊断', '处方', '调整用药', '把候选材料当成权威事实'],
            'candidate_material_is_not_fact': True,
            'may_not_widen_scope': True,
            'external_evidence_allowed': bool(spec.investigation_scope.get('allow_external_evidence')),
        },
        'budget': dict(budget or {}),
        'context_omissions': omissions,
        'change_summary': state.change_summary,
    }


def _facts_summary(facts):
    """本次目标相关的事实摘要，带省略标记。"""
    medications = []
    for medication in (facts or {}).get('medications') or []:
        medications.append({'ref': medication.get('ref'), 'name': medication.get('display_name'),
                            'dose': _truncate(medication.get('dose')),
                            'schedule': _truncate(medication.get('schedule')),
                            'route': medication.get('route'),
                            'start_at': medication.get('start_at'),
                            'start_at_basis': medication.get('start_at_basis'),
                            'source_uri': medication.get('source_uri')})
    semantic = [{'ref': item.get('ref'), 'namespace': item.get('namespace'),
                 'value': _truncate(item.get('value')), 'verification_status': item.get('verification_status')}
                for item in (facts or {}).get('semantic') or []]
    omissions = []
    snapshot_medication_count = len((facts or {}).get('medications') or [])
    if snapshot_medication_count > len(medications):
        omissions.append({'section': 'facts.medications',
                          'omitted_count': snapshot_medication_count - len(medications)})
    return ({'medications': medications, 'semantic': semantic,
             'open_conflicts': (facts or {}).get('open_conflicts') or [],
             'medication_count': snapshot_medication_count,
             'semantic_count': len((facts or {}).get('semantic') or [])}, omissions)


def _locator(locations):
    if not locations:
        return None
    first = next(iter(locations.values()), None)
    if isinstance(first, dict):
        return {'line': first.get('line'), 'column': first.get('column'),
                'coordinate_system': first.get('coordinate_system')}
    return None


def _todo(state, spec):
    """已完成事项与待处理事项——模型不需要自己从一堆记录里推断进度。"""
    done = []
    for finding in state.findings:
        if finding['assessment_status'] == 'verified':
            done.append({'kind': 'finding', 'ref': finding['finding_id'],
                         'statement': finding['statement']})
    for assertion in state.assertions:
        if assertion['verification_status'] == 'supported':
            done.append({'kind': 'assertion', 'ref': assertion['assertion_id'],
                         'statement': f"{assertion['predicate']} = {assertion['value']}"})
    pending = []
    for requirement in state.pending_coverage():
        pending.append({'kind': 'coverage', 'ref': requirement['requirement_id'],
                        'statement': requirement['description'],
                        'actionable': 'system'})
    # 已经被一条**未回答的补充请求**挡住的问题：模型不需要再问一遍，也不应该把
    # 它当成"还没有人管"。等用户答完，它会自己回到 open 的处理路径上。
    blocked = {item['question_id'] for item in state.blocked_questions()}
    for question in state.open_questions():
        pending.append({'kind': 'question', 'ref': question['question_id'],
                        'statement': question['text'],
                        'actionable': 'waiting_input' if question['question_id'] in blocked
                                      else 'model'})
    for issue in state.open_issues():
        pending.append({'kind': 'issue', 'ref': issue['issue_id'],
                        'statement': issue['user_visible_summary'], 'actionable': 'model'})
    return done[-24:], pending[:48]


def seed_from_deterministic_diff(state, material_index, *, origin=ORIGIN_SYSTEM) -> list:
    """把 ``ProductStore.recompute`` 已经算好的确定性差异**落成发现**。

    这不是替模型做调查，而是把机械步骤下沉：材料与当前记录的字段比对是确定性的，
    让模型重新算一遍只会引入漂移。模型真正要做的是决定**差异之外还要查什么**，
    以及哪些差异需要进一步取证。

    具体落法复用 ``coverage.materialize_item``——基础覆盖走的是同一段代码。区别
    只在于：这里**不**记读取凭据，所以它落成的发现本身还不能支撑"已覆盖"，
    真正的覆盖由 :func:`coverage.run_coverage_pass` 在读取之后给出。
    """
    if material_index is None:
        return []
    from . import coverage
    spec = state.spec
    case_ids = set(spec.selected_material_refs)
    created = []
    for material in (material_index.index().get('materials') or []):
        case_id = material.get('case_id')
        if case_id not in case_ids:
            continue
        for item in material.get('items') or []:
            ref = f"{case_id}/{item['item_id']}"
            if not item.get('kind'):
                continue
            state.material_fingerprints[ref] = item_fingerprint(item)
            finding, question = coverage.materialize_item(state, item, item, ref)
            if finding is not None:
                created.append(finding['finding_id'])
            elif question is not None:
                created.append(question['question_id'])
    state.sync_coverage()
    return created
