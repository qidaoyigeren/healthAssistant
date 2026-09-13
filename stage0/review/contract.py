"""material-review@2 —— 任务契约。

**完成标准来源于本次用户任务，不来自统一的药物核查模板。** 旧契约
（``medication-evidence-review@1``）把 ``authority`` / ``interaction_evidence`` /
``applicability`` 三条固定条件当作所有任务的完成条件；它们只适用于"某个药的说明书
证据核查"。本契约把它们换成由 TaskSpec 逐条声明的**交付要求**与**覆盖要求**。

契约里没有"问题必须都有确定答案"这一条。报告完整指的是**完成了本次承诺的交付
要求**，不等于所有医学问题都有定论。
"""
from __future__ import annotations

import hashlib
import json

CONTRACT_VERSION = 'material-review@2'
STATE_VERSION = 'material-review-state@1'

# ---- 交付要求（requested_outputs）--------------------------------------------
# 这些是"报告里必须存在的东西"，不是"必须得到的答案"。
OUTPUT_MATCHED = 'matched_summary'
OUTPUT_DIFFERENCES = 'differences'
OUTPUT_MISSING = 'missing_fields'
OUTPUT_CONFIRM = 'confirm_questions'
OUTPUT_SOURCES = 'sources'
OUTPUT_COVERAGE = 'coverage'

REQUESTED_OUTPUTS = (OUTPUT_MATCHED, OUTPUT_DIFFERENCES, OUTPUT_MISSING,
                     OUTPUT_CONFIRM, OUTPUT_SOURCES, OUTPUT_COVERAGE)

OUTPUT_LABELS = {
    OUTPUT_MATCHED: '一致项与差异摘要',
    OUTPUT_DIFFERENCES: '差异双方的记录及来源',
    OUTPUT_MISSING: '缺失字段',
    OUTPUT_CONFIRM: '需要用户或医生确认的问题',
    OUTPUT_SOURCES: '事实来源',
    OUTPUT_COVERAGE: '本次覆盖范围与未覆盖范围',
}

# 覆盖要求的种类。每一条都必须有处置；处置为 ``pending`` 时任务不得判为完整。
REQ_MATERIAL_CASE = 'material_case'
REQ_MATERIAL_ITEM = 'material_item'
REQ_AUTHORITATIVE_RECORD = 'authoritative_record'

# 处置词表。``unreadable`` / ``unmatched`` / ``insufficient`` 是**合格**的处置：
# 它们把"这条没查成"明确记下来，而不是让它悄悄消失，从而让任务看起来更容易完成。
DISPOSITIONS = ('pending', 'covered', 'unreadable', 'unmatched', 'insufficient')

DEFAULT_RESOURCE_LIMITS = {
    'max_cycles': 24,
    'max_searches': 6,
    'max_evidence': 24,
    'max_questions': 24,
    'max_findings': 64,
    'max_assertions': 24,
}

# ---- 交付要求 -----------------------------------------------------------------
# 完成与否由**交付要求**决定，不由"报告里有哪几节"决定，也不由"模型参没参与"决定。
# 三类来源必须分开，因为它们能不能被降级完全不同：
#
#   A. ``user``     用户在这次任务里明确要求完成的内容。模型**不能**删除或降级它。
#   B. ``contract`` 当前任务契约自身必需的核对（指定范围、安全与权限）。
#   C. ``model_proposed`` 模型提出的可选补充调查。可以展示、可以建议继续，但
#      **不自动**阻塞原任务——它也不能顶替 A/B 的位置。
ORIGIN_USER = 'user'
ORIGIN_CONTRACT = 'contract'
ORIGIN_MODEL_PROPOSED = 'model_proposed'
REQUIREMENT_ORIGINS = (ORIGIN_USER, ORIGIN_CONTRACT, ORIGIN_MODEL_PROPOSED)

# 本轮支持的三种**明确**用户要求，各自有不同的满足条件。
KIND_LIST_DIFFERENCES = 'list_differences'      # 列出差异与缺项
KIND_CONFIRM_FIELD = 'confirm_field'            # 确认指定字段是否一致
KIND_ANSWER_FROM_SOURCE = 'answer_from_source'  # 依据指定资料回答一个问题
# 契约自带的两类。
KIND_SCOPE_COVERED = 'scope_covered'            # 指定范围全部得到处置
KIND_SAFETY = 'safety'                          # 安全与权限检查（不可被 required=false 绕过）
# 模型提出的可选调查。
KIND_OPTIONAL_INVESTIGATION = 'optional_investigation'

REQUIREMENT_KINDS = (KIND_LIST_DIFFERENCES, KIND_CONFIRM_FIELD, KIND_ANSWER_FROM_SOURCE,
                     KIND_SCOPE_COVERED, KIND_SAFETY, KIND_OPTIONAL_INVESTIGATION)

KIND_LABELS = {
    KIND_LIST_DIFFERENCES: '列出材料与当前记录的差异及缺项',
    KIND_CONFIRM_FIELD: '确认指定字段是否一致',
    KIND_ANSWER_FROM_SOURCE: '依据资料回答一个问题',
    KIND_SCOPE_COVERED: '指定范围全部核对',
    KIND_SAFETY: '安全与权限检查',
    KIND_OPTIONAL_INVESTIGATION: '可选的进一步调查',
}

# 满足条件。**不是一个布尔字段**：每一种要求各自说清楚"什么算完成"。
RULE_ALL_SCOPE_DISPOSITIONED = 'all_scope_dispositioned'
RULE_DIFFERENCES_LISTED = 'differences_listed'
RULE_FIELD_DECIDED = 'field_decided'
RULE_ANSWERED_FROM_SOURCE = 'answered_from_source'
RULE_SAFETY_CLEAR = 'safety_clear'
RULE_NEVER_BLOCKS = 'never_blocks'

ACCEPTANCE_RULES = (RULE_ALL_SCOPE_DISPOSITIONED, RULE_DIFFERENCES_LISTED,
                    RULE_FIELD_DECIDED, RULE_ANSWERED_FROM_SOURCE, RULE_SAFETY_CLEAR,
                    RULE_NEVER_BLOCKS)

# 状态。``satisfied`` 是唯一的"完成"；其余各自说明卡在哪一类信息来源上。
REQ_PENDING = 'pending'
REQ_SATISFIED = 'satisfied'
REQ_AWAITING_USER = 'awaiting_user'
REQ_AWAITING_EVIDENCE = 'awaiting_evidence'
REQ_UNSATISFIED = 'unsatisfied'
REQUIREMENT_STATUSES = (REQ_PENDING, REQ_SATISFIED, REQ_AWAITING_USER,
                        REQ_AWAITING_EVIDENCE, REQ_UNSATISFIED)

STATUS_LABELS = {
    REQ_PENDING: '尚未处理', REQ_SATISFIED: '已完成',
    REQ_AWAITING_USER: '等待您补充', REQ_AWAITING_EVIDENCE: '需要依据资料调查',
    REQ_UNSATISFIED: '本次未能满足',
}

# 字段级比较覆盖的字段。名称用于**找到**对象，其余是真正被比较的值。
COMPARED_FIELD_LABELS = {
    'name': '药名', 'dose': '剂量', 'unit': '单位', 'schedule': '服用频次',
    'date': '日期', 'route': '给药途径', 'form': '剂型', 'strength': '规格',
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


FINGERPRINT_FIELDS = ('name', 'dose', 'unit', 'schedule', 'date', 'route', 'form', 'strength')


def item_fingerprint(item) -> str:
    """材料条目的版本指纹。

    它必须覆盖**确定性差异的全部输入**（候选字段 + 该条目对比出的 kind/issues/current），
    否则"记录变了、差异重算了"不会表现为一次变化，旧的发现就会留在报告里和新发现
    并存——同一件事两个说法，而且都像是当前的。
    """
    fields = (item.get('fields') or {})
    return digest({'fields': {key: fields.get(key) for key in FINGERPRINT_FIELDS},
                   'kind': item.get('kind'),
                   'current': sorted(str(ref) for ref in (item.get('current') or [])),
                   'issues': sorted(str(issue) for issue in (item.get('issues') or []))})


class TaskSpec:
    """本次用户任务的完整声明。代码构造，模型只读（且只读其中的摘要）。"""

    __slots__ = ('task_id', 'contract_version', 'user_goal', 'scope_id',
                 'selected_material_refs', 'authoritative_snapshot_ref',
                 'requested_outputs', 'coverage_requirements', 'delivery_requirements',
                 'investigation_scope', 'input_versions', 'resource_limits')

    def __init__(self, *, task_id, user_goal, scope_id, selected_material_refs,
                 authoritative_snapshot_ref, requested_outputs=None,
                 coverage_requirements=None, delivery_requirements=None,
                 investigation_scope=None, input_versions=None, resource_limits=None,
                 contract_version=CONTRACT_VERSION):
        self.task_id = task_id
        self.contract_version = contract_version
        self.user_goal = user_goal
        self.scope_id = scope_id
        self.selected_material_refs = list(selected_material_refs or [])
        self.authoritative_snapshot_ref = dict(authoritative_snapshot_ref or {})
        self.requested_outputs = list(requested_outputs or REQUESTED_OUTPUTS)
        self.coverage_requirements = list(coverage_requirements or [])
        self.delivery_requirements = list(delivery_requirements or [])
        self.investigation_scope = dict(investigation_scope or {})
        self.input_versions = dict(input_versions or {})
        self.resource_limits = {**DEFAULT_RESOURCE_LIMITS, **(resource_limits or {})}

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__slots__}

    @classmethod
    def from_dict(cls, raw: dict) -> 'TaskSpec':
        if not isinstance(raw, dict) or raw.get('contract_version') != CONTRACT_VERSION:
            raise ValueError('task spec migration required: unsupported contract_version')
        payload = {key: raw.get(key) for key in cls.__slots__}
        payload['coverage_requirements'] = [dict(item) for item in raw.get('coverage_requirements') or []]
        payload['delivery_requirements'] = [dict(item) for item in raw.get('delivery_requirements') or []]
        return cls(**payload)

    def requirement(self, requirement_id: str):
        return next((item for item in self.coverage_requirements
                     if item['requirement_id'] == requirement_id), None)

    # ---- 交付要求 ------------------------------------------------------------

    def delivery_requirement(self, requirement_id: str):
        return next((item for item in self.delivery_requirements
                     if item['requirement_id'] == requirement_id), None)

    def required_requirements(self) -> list:
        return [item for item in self.delivery_requirements if item.get('required')]

    def optional_requirements(self) -> list:
        return [item for item in self.delivery_requirements if not item.get('required')]


def build_coverage_requirements(*, selected_case_ids, material_items, medications) -> list:
    """本次必须覆盖的材料与记录。

    规则：

    * 用户指定的**每一份材料**都要有一条要求——材料不能被静默扩大或遗漏；
    * 材料里的**每一条目**都要有一条要求——忽略一条差异不能让任务变便宜；
    * 权威药单里**没有被任何材料条目点名**的记录也要有一条要求——"材料没写这个药"
      本身就是必须报告的结论，不能靠不列它来省略。
    """
    requirements = []
    covered_refs = set()
    for case_id, items in material_items:
        requirements.append(_requirement(REQ_MATERIAL_CASE, case_id,
                                         f'材料 {case_id} 需要核对'))
        for item in items:
            ref = f"{case_id}/{item['item_id']}"
            requirements.append(_requirement(REQ_MATERIAL_ITEM, ref,
                                             f'材料条目 {ref} 需要核对'))
            covered_refs.update(str(value) for value in item.get('current') or [])
    for medication in medications or []:
        ref = str(medication.get('ref') or '')
        if not ref:
            continue
        requirements.append(_requirement(
            REQ_AUTHORITATIVE_RECORD, ref,
            f'当前记录 {medication.get("display_name") or ref} 需要确认是否被材料覆盖',
            name=medication.get('display_name'),
            covered_by_material=ref in covered_refs))
    return requirements


def delivery_requirement(*, requirement_id, kind, origin, text, required,
                         acceptance_rule, subject_refs=None, question_refs=None,
                         field=None, expect=None, source_refs=None, revision=1,
                         **details) -> dict:
    """一条交付要求。**满足条件写在它自己身上**，不是散在交付检查的 if 里。

    ``required`` 决定它是否阻塞"用户要求已完成"；``origin`` 决定谁能动它——
    ``user`` 与 ``contract`` 两类**不能**被模型删除或降级，``model_proposed``
    默认不阻塞。
    """
    if kind not in REQUIREMENT_KINDS:
        raise ValueError('unknown requirement kind: ' + str(kind))
    if acceptance_rule not in ACCEPTANCE_RULES:
        raise ValueError('unknown acceptance rule: ' + str(acceptance_rule))
    return {'requirement_id': str(requirement_id), 'kind': kind, 'origin': origin,
            'text': str(text), 'required': bool(required),
            'acceptance_rule': acceptance_rule, 'status': REQ_PENDING,
            'subject_refs': list(subject_refs or []), 'question_refs': list(question_refs or []),
            'field': field, 'expect': expect, 'source_refs': list(source_refs or []),
            'evidence_refs': [], 'finding_refs': [], 'comparison_refs': [],
            'input_request_refs': [], 'reason': None, 'revision': int(revision),
            'history': [], **details}


def build_user_requirements(requested, *, scope_id, medications, material_items) -> list:
    """把**结构化**的用户要求变成交付要求。

    本轮只支持三种明确要求，各自有不同的满足条件。**不做关键词解析**：任务理解
    来自明确的入口与结构化参数，不是从一句话里猜出一堆隐式任务。
    """
    requirements = []
    for index, raw in enumerate(requested or []):
        kind = str(raw.get('kind') or '')
        if kind not in (KIND_LIST_DIFFERENCES, KIND_CONFIRM_FIELD, KIND_ANSWER_FROM_SOURCE):
            raise ValueError('unsupported requested requirement: ' + kind)
        subjects = _resolve_subject_refs(raw.get('subjects') or raw.get('subject_refs') or [],
                                         medications=medications, material_items=material_items)
        requirement_id = f'user:{index}:{kind}'
        if kind == KIND_LIST_DIFFERENCES:
            requirements.append(delivery_requirement(
                requirement_id=requirement_id, kind=kind, origin=ORIGIN_USER, required=True,
                acceptance_rule=RULE_DIFFERENCES_LISTED,
                text=raw.get('text') or '列出所选材料与当前记录的差异及缺项',
                subject_refs=subjects))
        elif kind == KIND_CONFIRM_FIELD:
            field = str(raw.get('field') or '').strip()
            if field not in COMPARED_FIELD_LABELS:
                raise ValueError('unsupported field for confirm_field: ' + field)
            expect = raw.get('expect')
            if expect not in (None, 'equal', 'different'):
                raise ValueError('unsupported expect for confirm_field: ' + str(expect))
            label = COMPARED_FIELD_LABELS[field]
            requirements.append(delivery_requirement(
                requirement_id=requirement_id, kind=kind, origin=ORIGIN_USER, required=True,
                acceptance_rule=RULE_FIELD_DECIDED, field=field, expect=expect,
                text=raw.get('text') or (f'确认{label}是否一致' if expect is None
                                         else f'确认{label}与材料{"完全一致" if expect == "equal" else "不一致"}'),
                subject_refs=subjects))
        else:
            question = str(raw.get('question') or raw.get('text') or '').strip()
            if not question:
                raise ValueError('answer_from_source needs a question')
            requirements.append(delivery_requirement(
                requirement_id=requirement_id, kind=kind, origin=ORIGIN_USER, required=True,
                acceptance_rule=RULE_ANSWERED_FROM_SOURCE,
                text=question, subject_refs=subjects,
                source_refs=list(raw.get('source_refs') or []),
                # 用户允许的资料范围：为空表示本次任务允许的全部资料。
                question_key=str(raw.get('question_key') or f'user_question:{index}'),
                allows_external=bool(raw.get('allows_external', True))))
    return requirements


def build_contract_requirements(*, coverage_requirements, allow_external_evidence) -> list:
    """契约自身必需的两项。安全与权限**永远** required，且不受模型影响。"""
    return [
        delivery_requirement(
            requirement_id='contract:scope', kind=KIND_SCOPE_COVERED, origin=ORIGIN_CONTRACT,
            required=True, acceptance_rule=RULE_ALL_SCOPE_DISPOSITIONED,
            text='本次选中的材料与相关当前记录全部得到处置'),
        delivery_requirement(
            requirement_id='contract:safety', kind=KIND_SAFETY, origin=ORIGIN_CONTRACT,
            required=True, acceptance_rule=RULE_SAFETY_CLEAR,
            text='安全与权限检查通过',
            allow_external_evidence=bool(allow_external_evidence)),
    ]


def optional_requirement(*, requirement_id, text, subject_refs=None, question_refs=None,
                         reason=None) -> dict:
    """模型提出的**可选**补充调查：能展示、能建议继续，但不阻塞原任务。"""
    return delivery_requirement(
        requirement_id=requirement_id, kind=KIND_OPTIONAL_INVESTIGATION,
        origin=ORIGIN_MODEL_PROPOSED, required=False, acceptance_rule=RULE_NEVER_BLOCKS,
        text=text, subject_refs=subject_refs, question_refs=question_refs, reason=reason)


def _resolve_subject_refs(values, *, medications, material_items) -> list:
    """把用户写的对象（药名或材料条目）对到当前记录/材料上。

    对不上的**保留原样**：它仍然说明用户关心什么，只是暂时挂不到具体记录上。
    静默丢掉会让"用户要求确认某个药"变成一条没有对象的空要求。
    """
    known_names = {str(item.get('display_name')): str(item.get('ref'))
                   for item in medications or [] if item.get('display_name')}
    item_names = {}
    for case_id, items in material_items or []:
        for item in items:
            fields = item.get('fields') or {}
            if fields.get('name'):
                item_names.setdefault(str(fields['name']), f"{case_id}/{item['item_id']}")
    resolved = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        resolved.append(known_names.get(text) or item_names.get(text) or text)
    return list(dict.fromkeys(resolved))


def _requirement(kind, ref, description, **details):
    return {'requirement_id': f'{kind}:{ref}', 'kind': kind, 'ref': ref,
            'description': description, 'disposition': 'pending',
            'finding_refs': [], 'issue_refs': [], **details}


def build_task_spec(*, task_id, user_goal, scope_id, selected_case_ids,
                    material_items, medications, input_versions,
                    resource_limits=None, allow_external_evidence=True, requested=None) -> TaskSpec:
    """从**用户指定**的材料与当前记录确定性地构造契约。

    ``selected_case_ids`` 与 ``requested`` 都是用户的输入，代码不在此处扩大也不
    缩小它们；自动匹配（哪条材料对应哪条记录）发生在 ``ProductStore.recompute``
    里，且可追溯。

    交付要求由三部分构成：用户明确要求的（``requested``，结构化参数）、契约自身
    必需的（指定范围 + 安全权限）、以及后来由模型提出的可选调查（运行时追加）。
    用户与契约两类**不会**因为模型说了什么而消失。
    """
    selected = [case_id for case_id in selected_case_ids if case_id]
    items = [(case_id, list(items or [])) for case_id, items in material_items
             if case_id in set(selected)]
    coverage = build_coverage_requirements(selected_case_ids=selected,
                                           material_items=items,
                                           medications=medications)
    entities = [str(m.get('display_name')) for m in (medications or []) if m.get('display_name')]
    for _, case_items in items:
        for item in case_items:
            name = (item.get('fields') or {}).get('name')
            if name and name not in entities:
                entities.append(str(name))
    requirements = build_user_requirements(requested, scope_id=scope_id,
                                           medications=medications, material_items=items)
    requirements += build_contract_requirements(coverage_requirements=coverage,
                                                allow_external_evidence=allow_external_evidence)
    return TaskSpec(
        task_id=task_id,
        user_goal=user_goal,
        scope_id=scope_id,
        selected_material_refs=selected,
        authoritative_snapshot_ref={'medications_revision': input_versions.get('medications'),
                                    'semantic_revision': input_versions.get('semantic'),
                                    'materials_revision': input_versions.get('materials')},
        requested_outputs=list(REQUESTED_OUTPUTS),
        coverage_requirements=coverage,
        delivery_requirements=requirements,
        investigation_scope={'allow_external_evidence': bool(allow_external_evidence),
                             'entities': entities,
                             'selected_case_ids': selected},
        input_versions=dict(input_versions),
        resource_limits=dict(resource_limits or {}))
