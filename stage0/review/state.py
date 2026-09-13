"""material-review@2 —— 调查工作状态。

**不要再用一个 gaps 列表承载所有业务含义。** 这里把此前挤在 ``gaps`` 里的东西拆成
职责清楚的对象：

* ``Question``      需要调查什么；
* ``Finding``       已经取得的发现（差异是发现，不是永久阻塞项）；
* ``Assertion``     准备主张什么（身份含谓词与限定条件，不由药名决定）；
* ``InputRequest``  请求用户补充（只在对结果有实际影响时）；
* ``ExecutionIssue``执行出了问题（不得伪装成"没有医学证据"）。

并且把运行、交付、证据拆成**三条独立轴**，任何一个 passed 字段都不再表达全部结果。
"""
from __future__ import annotations

import hashlib
import json

from .contract import (CONTRACT_VERSION, STATE_VERSION, DISPOSITIONS,
                       REQ_AUTHORITATIVE_RECORD, REQ_MATERIAL_CASE,
                       REQ_MATERIAL_ITEM, REQ_PENDING, REQ_SATISFIED,
                       REQUIREMENT_STATUSES, TaskSpec)

# ---- 问题的解决方向 ----------------------------------------------------------
# 它约束的是**这个问题的答案应该从哪来**，不规定用哪个工具、按什么顺序做。
# 方向错配是真实发生过的错误：材料上已经写明的信息回头再问用户；"说明书怎么
# 说"直接变成"请用户确认说明书结论"；"用户实际怎么用"被网络资料顶替；
# 专业判断被用户随口确认后升级成已验证事实。
DIRECTION_SELECTED_MATERIAL = 'selected_material'
DIRECTION_AUTHORITATIVE_RECORD = 'authoritative_record'
DIRECTION_REFERENCE_EVIDENCE = 'reference_evidence'
DIRECTION_USER_INPUT = 'user_input'
DIRECTION_PROFESSIONAL_REVIEW = 'professional_review'
QUESTION_DIRECTIONS = (DIRECTION_SELECTED_MATERIAL, DIRECTION_AUTHORITATIVE_RECORD,
                       DIRECTION_REFERENCE_EVIDENCE, DIRECTION_USER_INPUT,
                       DIRECTION_PROFESSIONAL_REVIEW)
DIRECTION_LABELS = {
    DIRECTION_SELECTED_MATERIAL: '从已选材料中取得',
    DIRECTION_AUTHORITATIVE_RECORD: '从当前可信记录中取得',
    DIRECTION_REFERENCE_EVIDENCE: '从授权参考资料中调查',
    DIRECTION_USER_INPUT: '需要用户提供具体事实',
    DIRECTION_PROFESSIONAL_REVIEW: '保留给专业人员确认',
}
# 只有这两类方向的答案**可以**由用户的补充提供。``reference_evidence`` 不行：
# 用户不是资料的替代品。``professional_review`` 也不行：用户的确认真是"记录"
# 而不是"临床审核"。
ANSWERABLE_BY_USER = (DIRECTION_USER_INPUT, DIRECTION_AUTHORITATIVE_RECORD,
                      DIRECTION_SELECTED_MATERIAL)

# ---- 运行状态 ----------------------------------------------------------------
RUN_RUNNING = 'running'
RUN_WAITING_INPUT = 'waiting_input'
RUN_ENDED = 'ended'
RUN_CANCELLED = 'cancelled'
RUN_FAILED = 'failed'
RUN_STATUSES = (RUN_RUNNING, RUN_WAITING_INPUT, RUN_ENDED, RUN_CANCELLED, RUN_FAILED)

# ---- 交付状态 ----------------------------------------------------------------
DELIVERY_NONE = 'none'
DELIVERY_PARTIAL = 'partial'
DELIVERY_COMPLETE = 'complete'
DELIVERY_STATUSES = (DELIVERY_NONE, DELIVERY_PARTIAL, DELIVERY_COMPLETE)

# ---- 证据状态 ----------------------------------------------------------------
EVIDENCE_VERIFIED = 'verified'
EVIDENCE_CONFLICTING = 'conflicting'
EVIDENCE_INSUFFICIENT = 'insufficient'
EVIDENCE_STATUSES = (EVIDENCE_VERIFIED, EVIDENCE_CONFLICTING, EVIDENCE_INSUFFICIENT)

# ---- 问题 --------------------------------------------------------------------
ORIGIN_USER = 'user'
ORIGIN_MODEL = 'model'
ORIGIN_SYSTEM = 'system'
QUESTION_OPEN = 'open'
QUESTION_ANSWERED = 'answered'
QUESTION_SUPERSEDED = 'superseded'
QUESTION_CANCELLED = 'cancelled'
QUESTION_CLOSED = (QUESTION_ANSWERED, QUESTION_SUPERSEDED, QUESTION_CANCELLED)

# ---- 发现 --------------------------------------------------------------------
FINDING_MATCHED = 'matched'
FINDING_DISCREPANCY = 'discrepancy'
FINDING_MISSING_FIELD = 'missing_field'
FINDING_SOURCE_CONFLICT = 'source_conflict'
FINDING_CONTEXTUAL_NOTE = 'contextual_note'
FINDING_TYPES = (FINDING_MATCHED, FINDING_DISCREPANCY, FINDING_MISSING_FIELD,
                 FINDING_SOURCE_CONFLICT, FINDING_CONTEXTUAL_NOTE)
# 确定性差异（ProductStore.recompute 的 kind）→ 发现类型。这一层映射是**代码**
# 完成的：模型不需要把材料差异重新算一遍，它需要决定的是差异之外还要查什么。
KIND_TO_FINDING = {'same': FINDING_MATCHED, 'changed': FINDING_DISCREPANCY,
                   'new': FINDING_DISCREPANCY, 'not_listed': FINDING_MISSING_FIELD,
                   'possible_duplicate': FINDING_CONTEXTUAL_NOTE,
                   'unresolved': FINDING_CONTEXTUAL_NOTE}
KIND_LABELS = {'same': '与当前记录一致', 'changed': '与当前记录不一致',
               'new': '当前记录里没有这一条', 'not_listed': '材料未列出这条当前记录',
               'possible_duplicate': '可能与另一条材料重复', 'unresolved': '字段不完整或有疑点'}

# ---- 执行问题 ----------------------------------------------------------------
ISSUE_INVALID_ARGUMENTS = 'invalid_arguments'
ISSUE_NO_MATCH = 'no_match'
ISSUE_SOURCE_INVALID = 'source_invalid'
ISSUE_CONNECTION = 'connection_or_timeout'
ISSUE_PERMISSION_DENIED = 'permission_denied'
ISSUE_BUDGET = 'budget_insufficient'
ISSUE_DELIVERY_FAILED = 'delivery_failed'
ISSUE_CATEGORIES = (ISSUE_INVALID_ARGUMENTS, ISSUE_NO_MATCH, ISSUE_SOURCE_INVALID,
                    ISSUE_CONNECTION, ISSUE_PERMISSION_DENIED, ISSUE_BUDGET,
                    ISSUE_DELIVERY_FAILED)
ISSUE_LABELS = {
    ISSUE_INVALID_ARGUMENTS: '这一步的参数不正确，没有执行',
    ISSUE_NO_MATCH: '已检索，没有找到匹配内容',
    ISSUE_SOURCE_INVALID: '来源已失效或完整性校验未通过',
    ISSUE_CONNECTION: '连接或超时，远端结果未知',
    ISSUE_PERMISSION_DENIED: '当前权限不允许这一步',
    ISSUE_BUDGET: '本次可用预算已用尽',
    ISSUE_DELIVERY_FAILED: '报告交付未通过检查',
}
# 分类 → 覆盖要求处置。``no_match`` 是**正常无匹配**，不是故障：它说明"查过了，
# 没有"，因此对应 ``unmatched``；只有真正的执行故障才落到 ``insufficient``。
ISSUE_DISPOSITION = {
    ISSUE_NO_MATCH: 'unmatched',
    ISSUE_SOURCE_INVALID: 'unreadable',
    ISSUE_PERMISSION_DENIED: 'unreadable',
    ISSUE_INVALID_ARGUMENTS: 'insufficient',
    ISSUE_CONNECTION: 'insufficient',
    ISSUE_BUDGET: 'insufficient',
    ISSUE_DELIVERY_FAILED: 'insufficient',
}
RETRYABILITY = {ISSUE_INVALID_ARGUMENTS: 'retryable', ISSUE_NO_MATCH: 'not_retryable',
                ISSUE_SOURCE_INVALID: 'not_retryable', ISSUE_CONNECTION: 'unknown',
                ISSUE_PERMISSION_DENIED: 'not_retryable', ISSUE_BUDGET: 'retryable',
                ISSUE_DELIVERY_FAILED: 'retryable'}

# ---- 来源读取凭据 ------------------------------------------------------------
# "来源已有效读取"**不能**由"模型调用过 read_material"证明。系统通过同样的权限、
# 哈希与版本检查完成的读取，与模型请求的读取在证据效力上等价，但在**归因上不等价**：
# 报告里必须分得清"这条是代码读的"和"这条是模型读的"。所以凭据有两个轴——
# 谁读的（credential）与读得成不成立（integrity + 指纹是否仍然对得上）。
CREDENTIAL_SYSTEM = 'system_read'
CREDENTIAL_MODEL = 'model_requested_read'
CREDENTIAL_NOT_READ = 'source_not_read'
CREDENTIAL_INVALID = 'source_invalid'
CREDENTIALS = (CREDENTIAL_SYSTEM, CREDENTIAL_MODEL, CREDENTIAL_NOT_READ, CREDENTIAL_INVALID)
CREDENTIAL_LABELS = {
    CREDENTIAL_SYSTEM: '系统读取（确定性字段比较）',
    CREDENTIAL_MODEL: '模型请求读取',
    CREDENTIAL_NOT_READ: '尚未读取',
    CREDENTIAL_INVALID: '读取未通过完整性校验',
}
# 有这些凭据的来源才能支撑结论。
CREDENTIALS_SUPPORTING = (CREDENTIAL_SYSTEM, CREDENTIAL_MODEL)

INTEGRITY_VERIFIED = 'verified'
INTEGRITY_UNKNOWN = 'unknown'
INTEGRITY_FAILED = 'failed'

# ---- 补充输入的用途 ----------------------------------------------------------
# 用户说"材料上写的是 10mg"**不等于**授权把当前药单改成 10mg。三类输入各自
# 落到不同的地方，前端不靠解析模型问句来猜要写入什么。
INPUT_MATERIAL_NOTE = 'material_note'      # A. 材料说明：补某份材料的缺失字段/日期/含义
INPUT_USER_REPORT = 'user_report'          # B. 用户陈述：记录用户对当前情况的说明
INPUT_AUTHORITATIVE_UPDATE = 'authoritative_update'  # C. 权威记录更新：走既有确认流程
INPUT_PURPOSES = (INPUT_MATERIAL_NOTE, INPUT_USER_REPORT, INPUT_AUTHORITATIVE_UPDATE)
INPUT_PURPOSE_LABELS = {
    INPUT_MATERIAL_NOTE: '材料说明（按您所说记录，不改变当前记录）',
    INPUT_USER_REPORT: '情况说明（按报告记录，需核实）',
    INPUT_AUTHORITATIVE_UPDATE: '修改当前记录（需要单独确认）',
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


def question_id_for(question_key: str, subjects) -> str:
    """问题身份 = **方面** + 主体集合，与措辞、药名顺序解耦。

    同一药物在不同方面上的问题（剂量、日期、相互作用）拿到不同的 id，因此不会
    因为药名相同就共享问题身份，也不会连带复用旧的支持判断。措辞变化不改变 id，
    只增加 ``revision``。
    """
    key = str(question_key or '').strip().lower()
    subject_list = sorted({str(item) for item in (subjects or [])})
    return 'q:' + digest({'key': key, 'subjects': subject_list})[:16]


def input_request_id_for(task_id, subjects, required_fields) -> str:
    """补充请求的身份 = **任务 + 对象 + 待补字段**。

    它**不**包含自然语言原句：同一件事换个措辞再问一次，是同一个请求——那正是
    "模型连续重复提问"的止点。它**也**不包含关联问题：系统按材料缺项建的请求和
    模型针对同一缺项建的请求，关联到的问题本来就不一样，把问题算进身份会让同
    一件事变成两条并列的请求挂在界面上。关联问题改为在复用时**合并**（见
    :meth:`MaterialReviewState.add_input_request`），信息不丢，请求不重复。

    同一对象的**不同字段**不是同一条：合并了，用户就只答得到一个字段，另一个
    永远等不到答案。输入版本同样不参与身份——它只被记下来做审计，否则用户每补
    一条信息，同一个未决请求就会换一个 id，旧的那条还挂着，看起来像两件事。
    """
    return 'ir:' + digest({
        'task': str(task_id or ''),
        'subjects': sorted({str(item) for item in (subjects or []) if str(item).strip()}),
        'fields': sorted({str(item) for item in (required_fields or []) if str(item).strip()})})[:16]


def assertion_id_for(subject_refs, predicate, qualifiers) -> str:
    """断言身份 = 主体 + 谓词 + 限定条件。

    值**不在**身份里：同一个断言换了取值是同一条断言的新修订，修订使旧的支持判断
    失效。这也正是"相同药名/相同数字/引用读过"都不能推导整句断言获支持的原因——
    身份里根本没有"药名"这一个维度可用。
    """
    payload = {'subjects': sorted({str(item) for item in (subject_refs or [])}),
               'predicate': str(predicate or '').strip(),
               'qualifiers': {str(k): qualifiers[k] for k in sorted(qualifiers or {})}}
    return 'a:' + digest(payload)[:16]


class MaterialReviewState:
    """一次材料核对/就诊准备任务的**全部**工作状态（可序列化、可恢复）。"""

    def __init__(self, spec: TaskSpec):
        self.spec = spec
        self.version = STATE_VERSION
        self.contract_version = CONTRACT_VERSION
        self.revision = 1
        self.questions: list = []
        self.findings: list = []
        self.assertions: list = []
        self.input_requests: list = []
        self.issues: list = []
        self.evidence_refs: list = []
        self.read_evidence_refs: list = []
        # ``material_read_refs`` **只**记模型请求读取过的条目——它是归因信息，
        # 不是"这个来源可不可用"的信息。系统读取记在 ``source_reads`` 里。
        self.material_read_refs: list = []
        self.source_reads: dict = {}
        self.material_items: dict = {}
        self.material_fingerprints: dict = {}
        # 字段级比较：材料条目 → [FieldComparison]。行级 kind 由它派生。
        self.field_comparisons: dict = {}
        self.queries: list = []
        self.concluded_questions: list = []
        self.run_status = RUN_RUNNING
        self.delivery_status = DELIVERY_NONE
        self.evidence_status = EVIDENCE_INSUFFICIENT
        self.termination_reason = None
        self.reports: list = []
        self.invalidation_log: list = []
        self.change_summary: dict | None = None
        self.safety_checks: list = []
        self.no_progress = 0
        self.last_delivery_gaps: list = []
        self.notes: list = []
        # 基础覆盖（运行器的数据准备职责）的进度。它**不**消耗模型规划调用，
        # 因此必须与"模型调查进展"分开记录：代码读完了不等于模型查完了。
        self.coverage_runs: list = []
        self.coverage_cursor: dict = {}
        # 用户补充的回答。它们**只是回答或候选事实**：不因为被回答过就变成
        # 权威记录，也不因为来自用户就自动获得核实状态。
        self.answers: list = []
        # 模型每次研究动作服务于哪个用户问题。不要求长篇推理，只记行动目的。
        self.research_decisions: list = []
        # 归因计数：模型真正参与了几轮、代码独立处理了什么。
        self.model_cycles = 0
        self.model_purpose_missing = 0
        # 权威事实的**当前**快照。由执行器在每轮开始时同步；它不进入模型视图
        # （模型看到的是 context.py 里的有界摘要），只用于确定性比较和安全校验。
        self.facts: dict = {}
        # 派生字段（不持久化）：本轮交给模型的上下文，以及上一次被拒提案的
        # 结构化反馈。两者都由运行器每轮重算，恢复时不从旧值继续。
        self.review_context: dict = {}
        self.pending_correction: dict | None = None

    def medications_by_ref(self) -> dict:
        return {str(medication.get('ref')): medication
                for medication in (self.facts or {}).get('medications') or []
                if medication.get('ref')}

    # ---- 来源读取凭据 --------------------------------------------------------

    def record_source_read(self, ref: str, *, credential, fingerprint=None, **detail) -> dict:
        """记下一次**真的读过**的来源。只有通过完整性校验的读取才写成 ``verified``。

        它不改变任何结论，只回答"这个来源有没有被有效读过、被谁读的"。
        """
        record = {'credential': credential,
                  'fingerprint': fingerprint if fingerprint is not None
                  else self.material_fingerprints.get(ref),
                  'at': detail.pop('at', None), **detail}
        previous = self.source_reads.get(ref)
        if previous and previous.get('credential') == credential:
            # 同一凭据重复读取：更新证据性字段，但不假装这是一次新的读取。
            record['reads'] = (previous.get('reads') or 1) + 1
        else:
            record['reads'] = 1
            if previous:
                record['previous_credential'] = previous.get('credential')
        self.source_reads[ref] = record
        return record

    def read_credential(self, ref: str) -> str:
        """来源当下的读取凭据。**材料版本变了，旧凭据就作废**——读过的原文已经
        不是现在这一版，它不能再支撑结论。"""
        record = self.source_reads.get(ref)
        if record is None:
            return CREDENTIAL_NOT_READ
        if record.get('integrity') not in (None, INTEGRITY_VERIFIED):
            return CREDENTIAL_INVALID
        if record.get('fingerprint') != self.material_fingerprints.get(ref):
            return CREDENTIAL_NOT_READ
        return record.get('credential') or CREDENTIAL_NOT_READ

    def read_refs(self) -> list:
        """**可用**的来源（系统或模型读过、且版本仍然对得上）。"""
        return sorted(ref for ref in self.material_items
                      if self.read_credential(ref) in CREDENTIALS_SUPPORTING)

    def system_read_refs(self) -> list:
        return sorted(ref for ref in self.material_items
                      if self.read_credential(ref) == CREDENTIAL_SYSTEM)

    def model_read_refs(self) -> list:
        return sorted(ref for ref in self.material_items
                      if self.read_credential(ref) == CREDENTIAL_MODEL)

    def read_attribution(self) -> dict:
        """报告里"谁读了什么"的**唯一**口径。"""
        return {'system': self.system_read_refs(), 'model': self.model_read_refs(),
                'unread': sorted(ref for ref in self.material_items
                                 if self.read_credential(ref) == CREDENTIAL_NOT_READ),
                'invalid': sorted(ref for ref in self.material_items
                                  if self.read_credential(ref) == CREDENTIAL_INVALID)}

    # ---- 序列化 --------------------------------------------------------------

    def to_dict(self) -> dict:
        return {'version': self.version, 'contract_version': self.contract_version,
                'spec': self.spec.to_dict(), 'revision': self.revision,
                'questions': self.questions, 'findings': self.findings,
                'assertions': self.assertions, 'input_requests': self.input_requests,
                'issues': self.issues, 'evidence_refs': self.evidence_refs,
                'read_evidence_refs': self.read_evidence_refs,
                'material_read_refs': self.material_read_refs,
                'source_reads': self.source_reads,
                'material_items': self.material_items,
                'material_fingerprints': self.material_fingerprints,
                'field_comparisons': self.field_comparisons,
                'coverage_runs': self.coverage_runs, 'coverage_cursor': self.coverage_cursor,
                'answers': self.answers, 'research_decisions': self.research_decisions,
                'model_cycles': self.model_cycles,
                'model_purpose_missing': self.model_purpose_missing,
                'queries': self.queries, 'concluded_questions': self.concluded_questions,
                'run_status': self.run_status, 'delivery_status': self.delivery_status,
                'evidence_status': self.evidence_status,
                'termination_reason': self.termination_reason, 'reports': self.reports,
                'invalidation_log': self.invalidation_log,
                'change_summary': self.change_summary, 'safety_checks': self.safety_checks,
                'no_progress': self.no_progress,
                'last_delivery_gaps': self.last_delivery_gaps, 'notes': self.notes,
                # 派生视图：它们完全由上面的持久字段算出来，存一份只是为了让
                # 任务记录与页面不必各自再实现一遍同一套口径。``restore`` 忽略
                # 它们——恢复的是状态，不是上一次的投影。
                'coverage_progress': self.coverage_progress(),
                'requirements': [dict(item) for item in self.spec.delivery_requirements],
                'read_attribution': self.read_attribution(),
                'read_refs': self.read_refs()}

    @classmethod
    def restore(cls, raw, scope_id: str) -> 'MaterialReviewState':
        if not isinstance(raw, dict):
            raise ValueError('material review migration required: not an object')
        if raw.get('version') != STATE_VERSION or raw.get('contract_version') != CONTRACT_VERSION:
            raise ValueError('material review migration required: unsupported version')
        spec = TaskSpec.from_dict(raw.get('spec') or {})
        if spec.scope_id != scope_id:
            raise ValueError('material review scope mismatch')
        state = cls(spec)
        for key in ('revision', 'questions', 'findings', 'assertions', 'input_requests',
                    'issues', 'evidence_refs', 'read_evidence_refs', 'material_read_refs',
                    'source_reads', 'material_items', 'material_fingerprints',
                    'field_comparisons',
                    'coverage_runs', 'coverage_cursor', 'answers', 'research_decisions',
                    'model_cycles', 'model_purpose_missing', 'queries',
                    'concluded_questions', 'reports', 'invalidation_log', 'safety_checks',
                    'no_progress', 'last_delivery_gaps', 'notes'):
            if key in raw:
                setattr(state, key, raw[key])
        for key, allowed in (('run_status', RUN_STATUSES),
                             ('delivery_status', DELIVERY_STATUSES),
                             ('evidence_status', EVIDENCE_STATUSES)):
            value = raw.get(key)
            setattr(state, key, value if value in allowed else
                    {'run_status': RUN_RUNNING, 'delivery_status': DELIVERY_NONE,
                     'evidence_status': EVIDENCE_INSUFFICIENT}[key])
        state.termination_reason = raw.get('termination_reason')
        state.change_summary = raw.get('change_summary')
        return state

    # ---- 问题 ----------------------------------------------------------------

    def open_questions(self) -> list:
        return [item for item in self.questions if item['status'] == QUESTION_OPEN]

    def question(self, question_id: str):
        return next((item for item in self.questions if item['question_id'] == question_id), None)

    def submit_question(self, *, question_key, text, subjects=None, origin=ORIGIN_MODEL,
                        related_material_refs=None, related_finding_ids=None,
                        resolution_summary=None, status=None, superseded_by=None,
                        direction=DIRECTION_SELECTED_MATERIAL, serves_requirement_id=None,
                        optional=False) -> dict:
        """新建或修订一个问题。返回它现在的样子。

        修订只增加 ``revision``；关闭必须给出 ``resolution_summary``——**未解决的
        问题不能被删掉以伪造完成**。

        ``direction`` 说明这个问题的答案该从哪来（材料 / 当前记录 / 参考资料 /
        用户 / 专业人员）。``serves_requirement_id`` 说明它服务于哪条交付要求；
        没有归属的问题必须是 ``optional``——**问题不能凭空变成一项义务**。
        """
        text = str(text or '').strip()
        if not text:
            raise ValueError('question text is required')
        if direction not in QUESTION_DIRECTIONS:
            raise ValueError('unknown question direction: ' + str(direction))
        identifier = question_id_for(question_key, subjects)
        existing = self.question(identifier)
        if existing is None:
            item = {'question_id': identifier, 'revision': 1, 'text': text,
                    'subjects': sorted({str(s) for s in (subjects or [])}),
                    'question_key': str(question_key or '').strip().lower(),
                    'origin': origin, 'status': QUESTION_OPEN, 'dependencies': [],
                    'direction': direction, 'optional': bool(optional),
                    'serves_requirement_id': serves_requirement_id,
                    'related_material_refs': list(related_material_refs or []),
                    'related_finding_ids': list(related_finding_ids or []),
                    'related_evidence_refs': [], 'resolution_summary': None,
                    'superseded_by': None, 'history': []}
            self.questions.append(item)
            if serves_requirement_id:
                self.link_question_to_requirement(identifier, serves_requirement_id)
            return item
        changed = False
        if existing['text'] != text:
            existing['history'].append({'revision': existing['revision'], 'text': existing['text']})
            existing['text'] = text
            changed = True
        if serves_requirement_id and not existing.get('serves_requirement_id'):
            existing['serves_requirement_id'] = serves_requirement_id
            existing['optional'] = bool(optional)
            changed = True
        if serves_requirement_id:
            self.link_question_to_requirement(identifier, serves_requirement_id)
        for key, value in (('related_material_refs', related_material_refs),
                           ('related_finding_ids', related_finding_ids)):
            for ref in value or []:
                if ref not in existing[key]:
                    existing[key].append(ref)
                    changed = True
        if status in ('answered', 'superseded', 'cancelled'):
            if not str(resolution_summary or '').strip():
                raise ValueError('closing a question requires a resolution summary')
            existing['status'] = status
            existing['resolution_summary'] = str(resolution_summary).strip()
            existing['superseded_by'] = superseded_by
            changed = True
        elif status == QUESTION_OPEN and existing['status'] != QUESTION_OPEN:
            # 重新打开一个已关闭的问题：同样是留痕的状态变化，不是删除。
            existing['status'] = QUESTION_OPEN
            existing['resolution_summary'] = None
            changed = True
        if changed:
            existing['revision'] += 1
        return existing

    # ---- 交付要求 ------------------------------------------------------------

    def delivery_requirement(self, requirement_id: str):
        return self.spec.delivery_requirement(requirement_id)

    def link_question_to_requirement(self, question_id: str, requirement_id: str) -> None:
        requirement = self.delivery_requirement(requirement_id)
        if requirement is None:
            return
        if question_id not in requirement['question_refs']:
            requirement['question_refs'].append(question_id)

    def set_requirement_status(self, requirement_id: str, status: str, *, reason=None,
                               evidence_refs=None, comparison_refs=None,
                               input_request_refs=None) -> dict:
        """给一条交付要求一个状态。**用户与契约两类不会被降级成可选**。

        状态是算出来的，但必须写回状态里：报告、页面与交付检查读的是同一份，
        不能各自再算一遍（那正是"同一件事几个答案"的来源）。
        """
        if status not in REQUIREMENT_STATUSES:
            raise ValueError('unknown requirement status: ' + str(status))
        requirement = self.delivery_requirement(requirement_id)
        if requirement is None:
            raise ValueError('unknown delivery requirement: ' + str(requirement_id))
        if requirement['origin'] in (ORIGIN_USER, ORIGIN_CONTRACT) and not requirement['required']:
            # 用户与契约的要求不能被"降级"到不阻塞：那等于用一次改写撤销了用户
            # 已经提出的要求。要放弃它只能把它明说成没满足。
            raise ValueError('user and contract requirements cannot be made optional')
        requirement['status'] = status
        requirement['reason'] = reason
        for key, values in (('evidence_refs', evidence_refs),
                            ('comparison_refs', comparison_refs),
                            ('input_request_refs', input_request_refs)):
            for ref in values or []:
                if ref not in requirement[key]:
                    requirement[key].append(ref)
        return requirement

    def requirements_by_status(self, status: str) -> list:
        return [item for item in self.spec.delivery_requirements if item['status'] == status]

    def required_requirements(self) -> list:
        return self.spec.required_requirements()

    def optional_requirements(self) -> list:
        return self.spec.optional_requirements()

    def unsatisfied_required(self) -> list:
        return [item for item in self.spec.required_requirements()
                if item['status'] != REQ_SATISFIED]

    # ---- 字段级比较 ----------------------------------------------------------

    def record_comparisons(self, ref: str, comparisons) -> list:
        """存下一条材料条目的字段级比较。行级 `kind` 由它们**派生**。"""
        self.field_comparisons[str(ref)] = [dict(row) for row in comparisons or []]
        return self.field_comparisons[str(ref)]

    def comparisons_for(self, ref: str) -> list:
        return list(self.field_comparisons.get(str(ref)) or [])

    def comparisons_for_field(self, field: str, subject_refs=None) -> list:
        """某个字段的全部比较。``subject_refs`` 为空表示不限定对象。"""
        wanted = {str(item) for item in (subject_refs or [])}
        rows = []
        for ref, comparisons in self.field_comparisons.items():
            if wanted and ref not in wanted and not (
                    wanted & {str(s) for s in self.material_subject_refs(ref)}):
                continue
            rows.extend(row for row in comparisons if row['field'] == field)
        return rows

    def material_subject_refs(self, ref: str) -> list:
        """这条材料条目**对应**的当前记录（自动匹配的结果，可追溯）。"""
        return [str(value) for value in
                ((self.material_items.get(str(ref)) or {}).get('current') or [])]

    def link_question_evidence(self, question_id: str, evidence_refs) -> None:
        item = self.question(question_id)
        if item is None:
            return
        for ref in evidence_refs or []:
            if ref not in item['related_evidence_refs']:
                item['related_evidence_refs'].append(ref)

    # ---- 发现 ----------------------------------------------------------------

    def add_finding(self, *, finding_type, statement, origin=ORIGIN_MODEL,
                    subject_refs=None, material_refs=None, evidence_refs=None,
                    question_refs=None, assessment_status=None) -> dict:
        if finding_type not in FINDING_TYPES:
            raise ValueError('unknown finding type: ' + str(finding_type))
        statement = str(statement or '').strip()
        if not statement:
            raise ValueError('finding statement is required')
        identifier = 'f:' + digest({'t': finding_type, 's': statement,
                                    'm': sorted(set(material_refs or [])),
                                    'r': sorted(set(subject_refs or []))})[:16]
        existing = next((item for item in self.findings if item['finding_id'] == identifier), None)
        if existing is not None:
            return existing
        item = {'finding_id': identifier, 'finding_type': finding_type, 'statement': statement,
                'origin': origin, 'subject_refs': list(subject_refs or []),
                'material_refs': list(material_refs or []),
                'evidence_refs': list(evidence_refs or []),
                'assessment_status': assessment_status or 'unverified',
                'question_refs': list(question_refs or []),
                'stale': False, 'created_in_revision': self.revision}
        self.findings.append(item)
        self.sync_coverage()
        return item

    def findings_for_material(self, material_ref: str) -> list:
        return [item for item in self.findings if material_ref in item['material_refs']]

    # ---- 断言 ----------------------------------------------------------------

    def submit_assertion(self, *, subject_refs, predicate, value, qualifiers=None,
                         evidence_refs=None, origin=ORIGIN_MODEL) -> dict:
        identifier = assertion_id_for(subject_refs, predicate, qualifiers)
        existing = next((item for item in self.assertions if item['assertion_id'] == identifier), None)
        if existing is None:
            item = {'assertion_id': identifier, 'revision': 1,
                    'subject_refs': sorted({str(s) for s in (subject_refs or [])}),
                    'predicate': str(predicate).strip(), 'value': value,
                    'qualifiers': dict(qualifiers or {}),
                    'evidence_refs': list(evidence_refs or []), 'origin': origin,
                    'verification_status': 'unverified', 'verification_reasons': [],
                    'history': []}
            self.assertions.append(item)
            return item
        if existing['value'] != value or dict(existing['qualifiers']) != dict(qualifiers or {}):
            existing['history'].append({'revision': existing['revision'], 'value': existing['value'],
                                        'qualifiers': dict(existing['qualifiers'])})
            existing['revision'] += 1
            existing['value'] = value
            existing['qualifiers'] = dict(qualifiers or {})
            # 修订使**旧的支持判断失效**：引用可以复用，判断不可以。
            existing['verification_status'] = 'unverified'
            existing['verification_reasons'] = ['assertion_revised']
        for ref in evidence_refs or []:
            if ref not in existing['evidence_refs']:
                existing['evidence_refs'].append(ref)
        return existing

    # ---- 请求补充 ------------------------------------------------------------

    def blocking_requirement_ids(self) -> list:
        """被**未回答**的补充请求挡住的必需要求。"""
        blocked = []
        for item in self.open_input_requests():
            for ref in item.get('blocks_requirement_ids') or []:
                if ref not in blocked:
                    blocked.append(ref)
        return blocked

    def add_input_request(self, *, question_text, related_question_ids=None,
                          required_fields=None, why_needed='', blocking_scope=None,
                          subjects=None, purpose=INPUT_MATERIAL_NOTE, target=None,
                          origin=ORIGIN_MODEL, reopen=False,
                          blocks_requirement_ids=None, missing_fact=None,
                          why_material_insufficient=None) -> dict:
        """新建或复用一条补充请求。

        身份由**任务 + 对象 + 待补字段 + 关联问题**决定，不由自然语言原句决定：
        同一个请求换个措辞再问一次，返回的是同一条记录——这正是"模型连续重复提问"
        的止点。只有在这条请求已经被回答、而底层材料/记录版本又变了（``reopen``）
        时，才会带着新的修订号重新打开它。
        """
        question_text = str(question_text or '').strip()
        if not question_text:
            raise ValueError('input request text is required')
        related = list(related_question_ids or [])
        # 主体从相关问题里推导：用户看到的是"请补充二甲双胍的剂量单位和服用频次"，
        # 补充回来的值必须知道**挂到哪一条记录上**，否则界面上只能靠解析问句猜药名。
        known = [self.question(str(ref)) for ref in related]
        derived = list(subjects or [])
        for question in known:
            if question:
                derived.extend(question.get('subjects') or [])
        if not derived and (target or {}).get('subjects'):
            derived.extend(target['subjects'])
        derived = self._display_subjects(derived)
        target = dict(target or {})
        if target.get('subjects'):
            target['subjects'] = self._display_subjects(target['subjects'])
        fields = list(dict.fromkeys(str(item) for item in (required_fields or []) if str(item).strip()))
        identifier = input_request_id_for(self.spec.task_id, derived, fields)
        version = digest(self.spec.input_versions)
        existing = self._mergeable_request(identifier, derived, fields)
        if existing is not None:
            # 复用时把**字段、关联问题与阻塞范围合并**：同一条请求可能同时服务于
            # 系统发现和模型发现，两边的信息都不该丢——但界面上它只有一条。
            for field in fields:
                if field not in existing['required_fields']:
                    existing['required_fields'].append(field)
            for ref in related:
                if ref not in existing['related_question_ids']:
                    existing['related_question_ids'].append(ref)
                if ref not in existing['blocking_scope']:
                    existing['blocking_scope'].append(ref)
            existing['question_text'] = existing['question_text'] or question_text
            if existing['status'] != 'open' and reopen and existing.get('input_version') != version:
                existing.setdefault('history', []).append(
                    {'revision': existing['revision'], 'status': existing['status'],
                     'answer_refs': list(existing.get('answer_refs') or [])})
                existing.update(status='open', revision=existing['revision'] + 1,
                                answer_refs=[], input_version=version,
                                question_text=question_text)
            return existing
        item = {'request_id': identifier, 'related_question_ids': related,
                'question_text': question_text, 'required_fields': fields,
                'why_needed': str(why_needed or '').strip(),
                'blocking_scope': list(blocking_scope or related or []),
                'subjects': derived,
                'purpose': purpose if purpose in INPUT_PURPOSES else INPUT_MATERIAL_NOTE,
                'target': target,
                'origin': origin,
                # 这条请求**挡住哪一项必需要求**。空表示它不挡任何必需项——
                # 那它就是一条可选建议，不能把整个任务变成等待状态。
                'blocks_requirement_ids': [ref for ref in dict.fromkeys(
                    blocks_requirement_ids or [])
                    if self.delivery_requirement(ref) is not None
                    and self.delivery_requirement(ref)['required']],
                'missing_fact': str(missing_fact or '').strip() or None,
                'why_material_insufficient': str(why_material_insufficient or '').strip() or None,
                'input_version': version,
                'status': 'open', 'answer_refs': [], 'revision': 1, 'history': []}
        self.input_requests.append(item)
        for ref in item['blocks_requirement_ids']:
            requirement = self.delivery_requirement(ref)
            if item['request_id'] not in requirement['input_request_refs']:
                requirement['input_request_refs'].append(item['request_id'])
        return item

    def requirements_blocked_by_missing(self, ref: str, fields) -> list:
        """这些字段缺值，会挡住哪些**必需**交付要求。

        两条规则，都是可推导的，不是白名单：

        * 一条 ``confirm_field`` 要求，如果它问的正是这些字段、对象也正是这条材料
          对应的记录，那它非等这个值不可；
        * ``list_differences`` 要求：缺的是**核心字段**时，差异根本比不出来，也就
          列不出来；缺的是记录里本来就没有的字段（剂型、规格…）时不挡它——那一项
          照常作为"缺项"被列出来。
        """
        from .contract import RULE_ALL_SCOPE_DISPOSITIONED, RULE_DIFFERENCES_LISTED, \
            RULE_FIELD_DECIDED
        from .fields import CORE_FIELDS
        wanted = {str(field) for field in fields or []}
        known = {str(ref)} | set(self.material_subject_refs(ref))
        core_missing = bool(wanted & set(CORE_FIELDS))
        blocked = []
        for requirement in self.spec.required_requirements():
            rule = requirement['acceptance_rule']
            if rule == RULE_FIELD_DECIDED:
                if requirement.get('field') in wanted \
                        and (not requirement['subject_refs']
                             or set(requirement['subject_refs']) & known):
                    blocked.append(requirement['requirement_id'])
            elif rule == RULE_DIFFERENCES_LISTED and core_missing:
                blocked.append(requirement['requirement_id'])
            elif rule == RULE_ALL_SCOPE_DISPOSITIONED and core_missing:
                # 核心字段缺值 → 这一条比不出结果 → 指定范围没核对完。用户把值
                # 补上，它就能完成，所以这是"在等用户"，不是"没满足"。
                # 记录里本来就没有的字段（剂型、规格）不在此列：那几项缺着，
                # 材料这一条照样算核对过（缺项会被列出来）。
                blocked.append(requirement['requirement_id'])
        return blocked

    def is_blocking_request(self, request_id: str) -> bool:
        """这条请求挡不挡必需项。可选建议不挡。"""
        item = next((entry for entry in self.input_requests
                     if entry['request_id'] == request_id), None)
        return bool(item and item.get('blocks_requirement_ids'))

    def _display_subjects(self, subjects) -> list:
        """把主体里出现的**内部引用**换成它的人话名字。

        模型可以用 ``memory:medication:1@v1`` 这种引用来指明对象（那是它上下文里
        真实存在的东西），但用户看到的必须是"氨氯地平"。引用仍然是同一个对象，
        换的只是展示——**不**因为这层替换而改变任何判断。
        """
        known = self.medications_by_ref()
        out = []
        for item in subjects:
            text = str(item).strip()
            if not text:
                continue
            medication = known.get(text)
            out.append(str(medication['display_name'])
                       if medication and medication.get('display_name') else text)
        return list(dict.fromkeys(out))

    def _mergeable_request(self, identifier, subjects, fields):
        """找一条**该被合并进来**的未决请求。

        完全相同（同一个 id）当然合并。此外：同一任务、同一对象、**待补字段有交集**
        的未决请求也合并——系统按材料缺项问"单位和频次"，模型针对同一个缺项问
        "频次"，那是同一件事，界面上不该出现两条。字段**完全不相交**时保持独立：
        把"剂量"和"频次"并成一条，用户只会答一个，另一个永远等不到答案。
        """
        exact = next((item for item in self.input_requests
                      if item['request_id'] == identifier), None)
        if exact is not None:
            return exact
        wanted = set(fields)
        for item in self.input_requests:
            if item['status'] != 'open':
                continue
            if sorted(item['subjects']) != sorted(subjects):
                continue
            known = set(item['required_fields'])
            if known & wanted or (not known and not wanted):
                return item
        return None

    def record_answer(self, *, request_id, value, field=None, kind=INPUT_MATERIAL_NOTE,
                      subjects=None, target=None, source='caregiver_input') -> dict:
        """记下用户补充的一条内容。

        它**只是回答或候选事实**：不写权威记录、不因为被回答过就变成已核实。权威
        记录的修改走另一条明确的确认与写入流程，两者在这里绝不互相冒充。
        """
        text = str(value or '').strip()
        if not text:
            raise ValueError('answer value is required')
        request = next((item for item in self.input_requests
                        if item['request_id'] == request_id), None)
        item = {'answer_id': 'ans:' + digest({'r': request_id, 'f': field,
                                              'v': text, 'v#': digest(self.spec.input_versions)})[:16],
                'request_id': request_id, 'field': field, 'kind': kind, 'value': text,
                'subjects': list(subjects or (request or {}).get('subjects') or []),
                'target': dict(target or (request or {}).get('target') or {}),
                'source': source, 'verification_status': 'recorded_as_reported',
                'input_version': digest(self.spec.input_versions),
                'at': None, 'applied_authoritative': False}
        existing = next((entry for entry in self.answers if entry['answer_id'] == item['answer_id']), None)
        if existing is not None:
            return existing
        self.answers.append(item)
        return item

    def answers_for(self, request_id: str) -> list:
        return [item for item in self.answers if item['request_id'] == request_id]

    def answer_input_requests(self, answer_refs=None, *, request_ids=None,
                              question_ids=None) -> tuple:
        """用户的补充到达后，关闭**受影响**的请求，而不是全部。

        选择器是请求 id：只有**确实被回答到**的那几条被关闭。只答了一部分，另一条
        仍然开放——收到任意一条补充就关掉全部未决项，会让报告再也想不起那件事。

        另有一条不能绕过的边界：**用户不是资料的替代品**。挂在"应从参考资料调查"
        或"保留给专业人员确认"的问题上的请求，用户回答只是被记录下来，请求**不**
        因此关闭——否则"说明书对此怎么说"会被"用户说了一句"顶替掉。

        返回 ``(answered, recorded_only)``。
        """
        answered, recorded_only = [], []
        wanted_requests = set(request_ids or [])
        wanted_questions = set(question_ids or [])
        for item in self.input_requests:
            if item['status'] != 'open':
                continue
            if wanted_requests:
                match = item['request_id'] in wanted_requests
            elif wanted_questions:
                match = bool((set(item['blocking_scope']) | set(item['related_question_ids']))
                             & wanted_questions)
            else:
                match = False  # 没有指名道姓地说答了哪一条，就不关闭任何一条
            if not match:
                continue
            if not self.request_answerable_by_user(item['request_id']):
                recorded_only.append(item['request_id'])
                continue
            item['status'] = 'answered'
            item['answer_refs'] = list(answer_refs or [])
            answered.append(item['request_id'])
        return answered, recorded_only

    def request_answerable_by_user(self, request_id: str) -> bool:
        """这条请求的答案，用户能不能提供。

        它挂着的每个问题都必须是"用户能回答"的方向。一个问题都没挂时按**能**处理：
        那是一条由系统按缺项建出来的请求，缺的就是事实本身。
        """
        item = next((entry for entry in self.input_requests
                     if entry['request_id'] == request_id), None)
        if item is None:
            return False
        linked = [self.question(ref) for ref in item.get('related_question_ids') or []]
        linked = [question for question in linked if question is not None]
        if linked:
            return all(question.get('direction') in ANSWERABLE_BY_USER for question in linked)
        # 请求没有挂到具体问题上时，看**同一个对象**上有没有一条"答案不该由用户
        # 提供"的未决问题。有的话，让用户来答等于用一句转述顶替掉那次资料调查。
        # 这不是猜"该写进哪条记录"，而是拒绝让用户替系统回答资料里的问题。
        subjects = {str(value) for value in item.get('subjects') or []}
        for question in self.open_questions():
            if question.get('direction') in ANSWERABLE_BY_USER:
                continue
            if subjects & {str(value) for value in question.get('subjects') or []}:
                return False
        return True

    def open_input_requests(self) -> list:
        return [item for item in self.input_requests if item['status'] == 'open']

    def blocked_questions(self) -> list:
        """被未回答的补充请求挡住的调查问题。"""
        blocked = set()
        for item in self.open_input_requests():
            blocked.update(item.get('blocking_scope') or [])
            blocked.update(item.get('related_question_ids') or [])
        return [self.question(ref) for ref in blocked if self.question(ref)]

    # ---- 执行问题 ------------------------------------------------------------

    def add_issue(self, *, operation_ref, category, affected_question_ids=None,
                  remote_outcome='unknown', user_visible_summary=None, detail=None) -> dict:
        if category not in ISSUE_CATEGORIES:
            raise ValueError('unknown issue category: ' + str(category))
        identifier = 'x:' + digest({'op': operation_ref, 'c': category})[:16]
        existing = next((item for item in self.issues if item['issue_id'] == identifier), None)
        if existing is not None:
            return existing
        item = {'issue_id': identifier, 'operation_ref': str(operation_ref),
                'category': category, 'retryability': RETRYABILITY.get(category, 'unknown'),
                'affected_question_ids': list(affected_question_ids or []),
                'remote_outcome': remote_outcome, 'status': 'open',
                'user_visible_summary': user_visible_summary or ISSUE_LABELS[category],
                'detail': dict(detail or {})}
        self.issues.append(item)
        self.sync_coverage()
        return item

    def resolve_issue(self, issue_id: str, *, status='resolved') -> None:
        for item in self.issues:
            if item['issue_id'] == issue_id:
                item['status'] = status

    def open_issues(self) -> list:
        return [item for item in self.issues if item['status'] == 'open']

    # ---- 覆盖 ----------------------------------------------------------------

    def sync_coverage(self) -> None:
        """把**有效读取**、发现与执行问题折算成覆盖处置。

        只有 ``covered`` / ``unreadable`` / ``unmatched`` / ``insufficient`` 四种
        **明确**处置算"已交代"；``pending`` 说明这条还没人碰过，任务不得判为完整。

        一条材料条目只有在**确实被有效读过**（系统或模型，且版本仍然对得上）并且
        得到了结论时才算 ``covered``。材料索引里出现过一个条目**不算**已经核实了
        原文——那正是"只读了 4 条里的 1 条，报告却看起来没问题"的来源。
        """
        finding_materials = set()
        for finding in self.findings:
            if finding.get('stale'):
                continue
            finding_materials.update(finding['material_refs'])
        subject_refs = set()
        for finding in self.findings:
            if finding.get('stale'):
                continue
            subject_refs.update(str(ref) for ref in finding['subject_refs'])
        issue_disposition = {}
        for issue in self.issues:
            ref = issue['operation_ref']
            rank = ISSUE_DISPOSITION.get(issue['category'], 'insufficient')
            # 已有更强处置（covered）时不降级。
            if issue_disposition.get(ref) != 'covered':
                issue_disposition[ref] = rank
        case_covered = {}
        for requirement in self.spec.coverage_requirements:
            if requirement['disposition'] in ('unreadable', 'unmatched', 'insufficient'):
                continue
            ref = requirement['ref']
            if requirement['kind'] == REQ_MATERIAL_ITEM:
                credential = self.read_credential(ref)
                if ref in finding_materials and credential in CREDENTIALS_SUPPORTING:
                    requirement['disposition'] = 'covered'
                elif credential == CREDENTIAL_INVALID:
                    requirement['disposition'] = 'unreadable'
                    requirement.setdefault('disposition_reason', '来源未通过完整性校验')
                elif ref in issue_disposition:
                    requirement['disposition'] = issue_disposition[ref]
                elif ref in finding_materials:
                    # 有结论，但依据来自一个**当下不可用**的来源：结论不能继续成立，
                    # 而这条也不能算交代过了。
                    requirement['disposition'] = 'insufficient'
                    requirement['disposition_reason'] = '结论的来源已不可用，需要重新读取'
            elif requirement['kind'] == REQ_AUTHORITATIVE_RECORD:
                # 这条药用**名字**出现在发现里，或者材料里有一条条目正好指向它。
                # 后者由 build_coverage_requirements 预先算好（可追溯的自动匹配），
                # 它说明"材料确实提到了这条当前记录"，因此已经被核对过。
                name = str(requirement.get('name') or '')
                if requirement.get('covered_by_material') or (name and name in subject_refs):
                    requirement['disposition'] = 'covered'
            elif requirement['kind'] == REQ_MATERIAL_CASE:
                dispositions = case_covered.get(ref.split(':', 1)[1], [])
                if dispositions and all(value != 'pending' for value in dispositions):
                    requirement['disposition'] = 'covered'
        # 材料条目全部交代后，材料这一条才算交代。
        #
        # 这一趟**独立于上面的循环**：上面那些条目可能已经由基础覆盖直接给了处置
        # （``set_coverage``），因此在主循环里被 ``continue`` 跳过——按主循环顺带
        # 收集子项处置，材料这一条就会永远停在 ``pending``，而它下面每一条都已经
        # 交代过了。
        for requirement in self.spec.coverage_requirements:
            if requirement['kind'] != REQ_MATERIAL_CASE:
                continue
            # ``ref`` 就是带 ``case:`` 前缀的完整 case_id；去掉前缀会让下面按
            # ``case_id + '/'`` 找子条目**一条都找不到**，材料这一条于是永远
            # 停在 pending，而它下面每一条其实都已经交代过了。
            case_id = requirement['ref']
            children = [item for item in self.spec.coverage_requirements
                        if item['kind'] == REQ_MATERIAL_ITEM
                        and item['ref'].startswith(case_id + '/')]
            dispositions = [item['disposition'] for item in children]
            if children and all(value != 'pending' for value in dispositions):
                requirement['disposition'] = 'covered'
                requirement['disposition_reason'] = f'本次该材料的 {len(children)} 条条目都已交代'
            # 只要还有子项没交代，材料这一条就仍然是 pending——由主循环与覆盖
            # pass 继续推进，而不是在这里被提前判成"已交代"。

    def set_coverage(self, requirement_id: str, disposition: str, *,
                     finding_id=None, issue_id=None, reason=None) -> dict:
        if disposition not in DISPOSITIONS:
            raise ValueError('unknown disposition: ' + str(disposition))
        requirement = self.spec.requirement(requirement_id)
        if requirement is None:
            raise ValueError('unknown coverage requirement: ' + str(requirement_id))
        requirement['disposition'] = disposition
        if finding_id and finding_id not in requirement['finding_refs']:
            requirement['finding_refs'].append(finding_id)
        if issue_id and issue_id not in requirement['issue_refs']:
            requirement['issue_refs'].append(issue_id)
        if reason:
            requirement['disposition_reason'] = str(reason)
        return requirement

    def pending_coverage(self) -> list:
        return [item for item in self.spec.coverage_requirements
                if item['disposition'] == 'pending']

    def coverage_progress(self) -> dict:
        """基础覆盖的**实际处理记录**——页面上显示的数字只能来自这里。

        它数的是覆盖要求当下的处置与来源凭据，不是"跑过几次"。因此取消、崩溃恢复、
        材料改版之后它仍然说得清"哪些条目已核对、哪些还没处理、哪些读不到"。
        """
        requirements = self.spec.coverage_requirements
        by_disposition = {value: 0 for value in DISPOSITIONS}
        for requirement in requirements:
            by_disposition[requirement['disposition']] = \
                by_disposition.get(requirement['disposition'], 0) + 1
        attribution = self.read_attribution()
        cursor = self.coverage_cursor or {}
        return {
            'total': len(requirements),
            'processed': len(requirements) - by_disposition.get('pending', 0),
            'by_disposition': by_disposition,
            'items_covered': by_disposition.get('covered', 0),
            'items_unreadable': by_disposition.get('unreadable', 0),
            'items_unmatched': by_disposition.get('unmatched', 0),
            'items_insufficient': by_disposition.get('insufficient', 0),
            'items_pending': by_disposition.get('pending', 0),
            'sources_read_by_system': len(attribution['system']),
            'sources_read_by_model': len(attribution['model']),
            'sources_unread': len(attribution['unread']),
            'sources_invalid': len(attribution['invalid']),
            'unprocessed': list(cursor.get('unprocessed') or []),
            'truncated': list(cursor.get('truncated') or []),
            'failed': list(cursor.get('failed') or []),
            'passes': len(self.coverage_runs),
            'last_pass': dict(self.coverage_runs[-1]) if self.coverage_runs else None,
            'note': ('这些数字来自实际的读取与比较记录。系统读取**不等于**模型核查：'
                     '模型没有读过的来源仍会记为模型未覆盖。'),
        }

    def semantic_pending(self) -> list:
        """需要**判断**、而不是确定性比较就能解决的条目。

        它们的存在意味着"基础覆盖完成"不能被读成"语义调查完成"。
        """
        pending = [item['requirement_id'] for item in self.spec.coverage_requirements
                   if item['disposition'] == 'insufficient']
        pending += [item['question_id'] for item in self.open_questions()]
        pending += [item['request_id'] for item in self.open_input_requests()]
        return pending

    # ---- 轴 ------------------------------------------------------------------

    def refresh_axes(self) -> None:
        """三条轴各自独立取值，互不替代。"""
        if any(item['status'] == 'conflicting' for item in self._evidence_verdicts()):
            self.evidence_status = EVIDENCE_CONFLICTING
        elif self._evidence_insufficient():
            self.evidence_status = EVIDENCE_INSUFFICIENT
        else:
            self.evidence_status = EVIDENCE_VERIFIED
        if self.delivery_status == DELIVERY_NONE and self.reports:
            self.delivery_status = DELIVERY_PARTIAL

    def _evidence_verdicts(self):
        for assertion in self.assertions:
            yield {'status': assertion.get('verification_status')}
        for finding in self.findings:
            yield {'status': finding.get('assessment_status')}

    def _evidence_insufficient(self) -> bool:
        conclusive = [item for item in self.assertions
                      if item.get('verification_status') not in (None, 'unverified')]
        if any(item.get('verification_status') in ('insufficient', 'requires_human_confirmation',
                                                   'unverified') for item in self.assertions):
            return True
        if any(item.get('assessment_status') in ('unverified', 'insufficient')
               for item in self.findings):
            return True
        if any(item['disposition'] == 'insufficient'
               for item in self.spec.coverage_requirements):
            return True
        if not conclusive and not self.findings:
            return True
        return False

    # ---- 报告 ----------------------------------------------------------------

    def record_report(self, report: dict) -> dict:
        # 渲染输入一并留档：下一版报告要能**据实**算出"哪些结论变了"，
        # 而不是凭现在的状态反推上一版长什么样。
        self.reports.append({'report_id': report['report_id'], 'revision': len(self.reports) + 1,
                             'created_at': report.get('created_at'),
                             'delivery_status': report.get('delivery_status'),
                             'evidence_status': report.get('evidence_status'),
                             'sections': report.get('sections') or {},
                             'versions': report.get('versions') or {},
                             # 下一版报告要能**据实**说出"这一版收到了哪些补充"，
                             # 而不是凭现在的状态反推上一版收到了什么。
                             'answer_ids': list(report.get('answer_ids') or []),
                             # 下一版要能**据实**说出"哪一项要求这一版满足了"。
                             'requirements': [dict(item) for item in
                                              (report.get('requirements') or [])],
                             # 这一版发布时的模型参与计数：下一版据此算出**自己**
                             # 有没有得到模型的判断。
                             'model_cycles': self.model_cycles,
                             'coverage_progress': dict(report.get('coverage_progress') or {})})
        self.delivery_status = report.get('delivery_status') or DELIVERY_PARTIAL
        return report

    def latest_report(self):
        return self.reports[-1] if self.reports else None

    def model_cycles_this_delivery(self) -> int:
        """**这一版报告**里模型参与了几轮。

        累计值会让"上一版有模型、这一版模型挂了"看起来仍然有模型参与——那正好是
        "把模型缺席隐藏成全部核查完成"。所以交付检查、报告与页面都看这个差值。
        """
        recorded = (self.reports[-1].get('model_cycles') or 0) if self.reports else 0
        return max(0, self.model_cycles - recorded)

    # ---- 模型视图 ------------------------------------------------------------

    def model_view(self, context: dict) -> dict:
        """交给模型的**摘要**，带省略标记。

        完整事实仍由代码保留并用于安全校验；这里给出的是本次目标相关的部分，
        并且明确说出哪里被省略了——截断后的子集绝不冒充全部事实。

        **按优先级组织，不把所有历史问题平铺成同一个 pending 列表**：先看用户
        目标，再看还没满足的必需要求，然后才是对它们有帮助的材料与证据、可以推进
        的问题、正在等待的输入、可选建议，最后是预算与限制。模型据此决定下一步；
        平铺的列表会让它分不清"用户要求的"和"顺带想起来的"。
        """
        requirements = [dict(item) for item in self.spec.delivery_requirements]
        required_open = [item for item in requirements
                         if item['required'] and item['status'] != REQ_SATISFIED]
        optional = [item for item in requirements if not item['required']]
        blocked = {item['question_id'] for item in self.blocked_questions()}
        return {
            'contract_version': self.contract_version,
            # 1) 用户目标
            'user_goal': self.spec.user_goal,
            # 2) 尚未满足的必需要求（模型**不能**删除或降级它们）
            'requirements': requirements,
            'unsatisfied_required': [
                {'requirement_id': item['requirement_id'], 'kind': item['kind'],
                 'origin': item['origin'], 'text': item['text'], 'status': item['status'],
                 'reason': item['reason'], 'field': item.get('field'),
                 'subject_refs': item['subject_refs'],
                 'question_refs': item['question_refs']}
                for item in required_open],
            # 3) 对这些要求有帮助的事实、材料与证据
            'trusted_context': context,
            'field_comparisons': {ref: rows for ref, rows in (self.field_comparisons or {}).items()},
            'questions': [{'question_id': q['question_id'], 'text': q['text'],
                           'subjects': q['subjects'], 'origin': q['origin'],
                           'status': q['status'], 'revision': q['revision'],
                           'direction': q.get('direction'),
                           'optional': bool(q.get('optional')),
                           'serves_requirement_id': q.get('serves_requirement_id'),
                           'waiting_for_input': q['question_id'] in blocked,
                           'related_material_refs': q['related_material_refs'],
                           # 模型必须能看出**这条问题已经取到过哪些依据**，否则它
                           # 只能靠"再查一次"来确认——那既浪费预算，也让循环看起来
                           # 像在原地打转。
                           'related_evidence_refs': q['related_evidence_refs']}
                          for q in self.questions],
            'findings': [{'finding_id': f['finding_id'], 'finding_type': f['finding_type'],
                          'statement': f['statement'], 'origin': f['origin'],
                          'material_refs': f['material_refs'],
                          'assessment_status': f['assessment_status'],
                          'evidence_refs': f['evidence_refs']}
                         for f in self.findings],
            'assertions': [{'assertion_id': a['assertion_id'], 'predicate': a['predicate'],
                            'value': a['value'], 'subject_refs': a['subject_refs'],
                            'qualifiers': a['qualifiers'],
                            'verification_status': a['verification_status'],
                            'evidence_refs': a['evidence_refs']}
                           for a in self.assertions],
            'open_input_requests': [{'request_id': i['request_id'],
                                     'question_text': i['question_text'],
                                     'purpose': i.get('purpose'),
                                     'subjects': i.get('subjects') or [],
                                     'required_fields': i.get('required_fields') or [],
                                     'related_question_ids': i['related_question_ids']}
                                    for i in self.input_requests if i['status'] == 'open'],
            'answered_input_requests': [{'request_id': i['request_id'],
                                         'question_text': i['question_text']}
                                        for i in self.input_requests if i['status'] == 'answered'],
            'user_answers': [{'request_id': a['request_id'], 'field': a['field'],
                              'value': a['value'], 'kind': a['kind'],
                              'subjects': a['subjects'],
                              'verification_status': a['verification_status']}
                             for a in self.answers],
            'open_issues': [{'issue_id': i['issue_id'], 'category': i['category'],
                             'summary': i['user_visible_summary'],
                             'retryability': i['retryability']}
                            for i in self.issues if i['status'] == 'open'],
            'coverage_pending': [{'requirement_id': r['requirement_id'],
                                  'description': r['description']}
                                 for r in self.pending_coverage()],
            # 4) 可以推进的问题 5) 正在等待的输入 6) 可选建议 7) 预算与限制
            'actionable_questions': [q['question_id'] for q in self.open_questions()
                                     if q['question_id'] not in blocked
                                     and not q.get('optional')],
            'waiting_input': [item['request_id'] for item in self.open_input_requests()
                              if self.is_blocking_request(item['request_id'])],
            'optional_suggestions': [{'requirement_id': r['requirement_id'],
                                      'text': r['text'], 'status': r['status'],
                                      'reason': r['reason']}
                                     for r in optional],
            'constraints_note': ('可选建议**不阻塞**本次任务：把它们列在 '
                                 'optional_suggestions 里，不要为了让它们有结论而'
                                 '停下整个任务。'),
            'read_attribution': self.read_attribution(),
            'research_decisions': [{'question_id': d['question_id'], 'query': d['query'],
                                    'purpose': d.get('purpose'), 'outcome': d.get('outcome')}
                                   for d in self.research_decisions],
            'material_unread': [ref for ref in self.material_items
                                if self.read_credential(ref) == CREDENTIAL_NOT_READ],
            'change_summary': self.change_summary,
            'axes': {'run': self.run_status, 'delivery': self.delivery_status,
                     'evidence': self.evidence_status},
        }
