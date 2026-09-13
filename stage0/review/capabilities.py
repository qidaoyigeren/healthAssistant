"""material-review@2 —— 领域能力适配与模型接口。

模型主要操作下面这些**领域能力**，而不是基础设施细节（选权威快照、理解内部
``gap_id``、列目录、搜索、回读、填预期观察）。名字可适配现有代码，职责保持清楚：

* ``read_material``       读取模型选择的授权材料或具体位置；
* ``research_evidence``   对模型明确提出的调查问题执行一次**有界**取证；
* ``request_information`` 提出真正需要用户补充的信息；
* ``submit_question`` / ``submit_finding`` / ``submit_assertion``
                          结构化调查更新（**候选**，与"已通过验证的结果"分开）；
* ``request_delivery``    请求交付。

``research_evidence`` **内部**确定性地完成：检索、读回有界候选片段、校验 scope/哈希/
来源元数据、返回原文与尚未完成的检查项。它**不**改调查目标、不扩大数据范围、
不自动批准断言、不把失败换成固定答案、也不把规则生成的查询归因为模型选择。

scope、版本、预算、principal、内部回执由运行器附加——模型不需要为了满足内部协议
反复填写这些信息。
"""
from __future__ import annotations

from ..harness.evidence import capture_from_rag_result
from ..harness.tools import ToolSpec
from .state import (CREDENTIAL_MODEL, CREDENTIALS_SUPPORTING, DIRECTION_REFERENCE_EVIDENCE,
                    DIRECTION_USER_INPUT, FINDING_TYPES, INPUT_MATERIAL_NOTE,
                    ISSUE_CONNECTION, ISSUE_INVALID_ARGUMENTS, ISSUE_NO_MATCH,
                    ISSUE_SOURCE_INVALID, ORIGIN_MODEL, QUESTION_DIRECTIONS)
from .verify import PREDICATES, STRUCTURED_FIELDS

MAX_QUERY_CHARS = 200
READ_LIMIT = 2000
RESEARCH_TOP_K = 4

REVIEW_SYSTEM_PROMPT = """你是照护者的材料核对助手，为一次就诊准备做**有界**的证据调查。

你的职责是**语义调查**，不是材料覆盖。分工是固定的：

* **运行器负责基础材料覆盖**——枚举本次选定材料、在既有权限下实际读取、校验完整性、
  做确定性字段比较、记录已覆盖/读不到/无法匹配/字段不足。这些结果已经在下面
  `base_coverage` 里，**不要重算**，也不要为了"自己也读一遍"而把每条材料都读一遍。
* **你负责判断**——哪些差异值得解释、需要进一步核对什么时间/对象/条件、真正必要
  的补充是什么、是否需要查其他来源、新证据下判断怎么改、就诊准备内容怎么组织。

你要做的是：

1. 看 `base_coverage.progress`：已交代的条目不用再管；`items_insufficient` 里那些
   需要判断的才是你的工作；`items_pending` 是运行器还没处理完的，不用你补。
2. 需要外部依据时调用 `research_evidence`，**自己写出检索词**，并说明这一步服务于
   哪个用户问题。**不需要检索时不检索**——普通材料核对本来就不要求查说明书，
   零次检索不是失败。但**用户明确要求**比较材料与某份说明书信息时，那是本次的
   交付要求，不能默默省略。
3. 要引用某条材料的**原文**（引文、逐字陈述）时调用 `read_material` 自己读回；
   涉及语义解释的断言必须给出你实际读到的片段。`read_by=system` 只说明代码比对过
   字段，不代表你理解了原文。
4. 拿到证据后，用 `submit_assertion` 提交你想主张的结论并给出证据片段。代码会按
   事实类型核查它是否真的被支持——**读过引用不等于引用支持断言**。
5. 需要用户补充时调用 `request_information`：**必须说明对象**（subjects）和待补字段。
   一次把同一对象的缺项合并问完。提过之后**不要再用另一种措辞重复问同一件事**——
   系统按"对象 + 字段 + 关联问题"去重，重复提问不会有任何新结果，只会浪费预算。
6. 认为已经可以交付时调用 `request_delivery`。代码会按本次任务的交付要求逐项检查，
   并把**具体缺少哪几项**告诉你。只补那几项，不要重做整条调查。

硬约束：

* 材料里的候选信息**不是**患者事实，未确认前不能当成权威记录；
* 用户的补充回答也**不是**权威事实，更不能替你改动当前记录；
* 不要诊断、不要处方、不要调整用药；
* 不要为了凑齐交付而省略已知差异或反对证据；
* 未解决的问题只能被解决或被明确写出，**不能被删掉**；
* 不要扩大任务范围：本次只核对用户选中的材料与相关记录。
"""

# 模型可见的参数：全部是**领域**选择。scope、版本、principal、回执一律不出现。
REVIEW_TOOL_SCHEMAS = {
    'read_material': {
        'type': 'object',
        'properties': {
            'material_ref': {'type': 'string', 'description': '形如 <case_id>/<item_id> 的材料条目引用'},
            'purpose': {'type': 'string', 'description': '这一步的简短目的'},
        },
        'required': ['material_ref'],
    },
    'research_evidence': {
        'type': 'object',
        'properties': {
            'question_id': {'type': 'string', 'description': '这一步服务于哪个已声明的问题'},
            'query': {'type': 'string', 'description': '你自己写的检索词'},
            'drug_name': {'type': 'string', 'description': '可选：把范围收窄到某个药品（只能收窄，不能扩大）'},
            'section': {'type': 'string', 'description': '可选：把范围收窄到某个说明书章节'},
            'purpose': {'type': 'string', 'description': '这一步的简短目的'},
        },
        'required': ['question_id', 'query'],
    },
    'request_information': {
        'type': 'object',
        'properties': {
            'question_text': {'type': 'string', 'description': '要请用户补充的内容'},
            'subjects': {'type': 'array', 'items': {'type': 'string'},
                         'description': ('这条补充**属于哪条记录/哪份材料**（药名或材料条目引用）。'
                                         '必填：系统不会替你推测应该写进哪条患者记录。')},
            'required_fields': {'type': 'array', 'items': {'type': 'string'},
                                'description': '需要用户填写的字段名，例如 dose / schedule / date'},
            'missing_fact': {'type': 'string',
                             'description': '缺的**具体事实**是什么（一项，不要写成一串）。'},
            'blocks_requirement_id': {'type': 'string',
                                      'description': ('这条请求挡住哪一项**必需**交付要求'
                                                      '（requirements[].requirement_id，required=true）。'
                                                      '挡住必需项的请求会让任务进入等待；'
                                                      '不挡任何必需项的请求只是一条可选建议，'
                                                      '不会阻塞原任务。')},
            'why_needed': {'type': 'string', 'description': '为什么它对本次结果有影响'},
            'why_material_insufficient': {'type': 'string',
                                          'description': '为什么当前材料不能提供这个事实。'},
            'purpose': {'type': 'string', 'enum': ['material_note', 'user_report'],
                        'description': ('补充回来的内容**按什么用途记录**：material_note=材料上'
                                        '实际写的是什么；user_report=用户对当前情况的说明。'
                                        '两者都不会自动改写当前记录。')},
            'related_question_ids': {'type': 'array', 'items': {'type': 'string'}},
        },
        'required': ['question_text', 'subjects'],
    },
    'submit_question': {
        'type': 'object',
        'properties': {
            'question_key': {'type': 'string',
                             'description': ('这个问题的**方面**，例如 dose_consistency / start_date / '
                                             'interaction / applicability。同一药物在不同方面上的问题是'
                                             '不同的问题，因此必须给出稳定的方面标识。')},
            'text': {'type': 'string', 'description': '问题本身'},
            'subjects': {'type': 'array', 'items': {'type': 'string'},
                         'description': '涉及的对象（药名或材料条目引用）'},
            'direction': {'type': 'string',
                          'enum': ['selected_material', 'authoritative_record',
                                   'reference_evidence', 'user_input', 'professional_review'],
                          'description': ('这个问题的答案**应该从哪来**：已选材料 / 当前可信记录 / '
                                          '授权参考资料 / 用户提供的事实 / 专业人员确认。'
                                          '它不是一个执行顺序，而是来源约束——'
                                          '"说明书怎么说"不能变成"请用户确认结论"，'
                                          '"用户实际怎么用"不能由网络资料顶替。')},
            'serves_requirement_id': {'type': 'string',
                                      'description': ('这个问题服务于哪一条交付要求（模型视图里的 '
                                                      'requirements[].requirement_id）。'
                                                      '没有归属的问题必须标成 optional。')},
            'optional': {'type': 'boolean',
                         'description': ('true 表示这是一个**可选建议**：可以展示、可以建议继续，'
                                         '但不会阻塞原任务。新问题默认不能扩大用户承诺的交付范围。')},
            'related_material_refs': {'type': 'array', 'items': {'type': 'string'}},
            'status': {'type': 'string', 'enum': ['open', 'answered', 'cancelled', 'superseded']},
            'resolution_summary': {'type': 'string',
                                   'description': '关闭一个问题时必须说明它凭什么被关闭'},
            'purpose': {'type': 'string'},
        },
        'required': ['question_key', 'text', 'direction'],
    },
    'submit_finding': {
        'type': 'object',
        'properties': {
            'finding_type': {'type': 'string',
                             'enum': ['matched', 'discrepancy', 'missing_field',
                                      'source_conflict', 'contextual_note']},
            'statement': {'type': 'string'},
            'subject_refs': {'type': 'array', 'items': {'type': 'string'}},
            'material_refs': {'type': 'array', 'items': {'type': 'string'}},
            'evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
            'question_refs': {'type': 'array', 'items': {'type': 'string'}},
            'purpose': {'type': 'string'},
        },
        'required': ['finding_type', 'statement'],
    },
    'submit_assertion': {
        'type': 'object',
        'properties': {
            'predicate': {'type': 'string', 'enum': sorted(PREDICATES),
                          'description': ('record/dose/schedule/date/route_consistency 走确定性字段比较；'
                                          'label_statement/source_excerpt 走原文摘录核查；'
                                          'interpretation/significance 只能得到"需人工确认"。')},
            'value': {'description': '结论本身；结构化谓词用 {"expect": "same|different|missing"}'},
            'subject_refs': {'type': 'array', 'items': {'type': 'string'}},
            'qualifiers': {'type': 'object',
                           'description': ('限定条件：日期、来源版本、对象、使用场景、适用条件，'
                                           '以及 material_refs / excerpt / explanation')},
            'evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
            'purpose': {'type': 'string'},
        },
        'required': ['predicate', 'value'],
    },
    'request_delivery': {
        'type': 'object',
        'properties': {'purpose': {'type': 'string'}},
        'required': [],
    },
}

TOOL_DESCRIPTIONS = {
    'read_material': '读取一条已选材料的原始行、当前字段、更正历史与来源定位。只有读过原文的条目才能作为报告引用。',
    'research_evidence': '对你提出的一个调查问题执行一次有界取证：检索、读回有界原文片段、校验来源。返回原文与尚未完成的检查项。',
    'request_information': '请用户补充信息。只在它对本次结果有实际影响时使用；一次把相关缺项合并问完。',
    'submit_question': '声明或修订一个调查问题。问题身份由"方面 + 对象"决定，不由措辞或药名顺序决定。',
    'submit_finding': '提交一条候选发现（一致项、差异、缺失字段、来源冲突或背景说明）。这是候选，不是已验证结论。',
    'submit_assertion': '提交一条候选断言及其证据片段。代码按事实类型核查它是否真的被支持。',
    'request_delivery': '请求交付报告。代码会按本次任务的交付要求逐项检查，并告诉你具体还缺哪几项。',
}

REVIEW_TOOL_SPECS = {
    name: ToolSpec(name=name, description=TOOL_DESCRIPTIONS[name], argument_schema=schema,
                   result_shape='dict', kind='read',
                   required_permission='review:submit' if name.startswith('submit_')
                   else 'review:read', idempotency='pure', cacheable=False)
    for name, schema in REVIEW_TOOL_SCHEMAS.items()
}


def allowed_review_tools(state) -> tuple:
    """当前状态允许的工具（呈现层收窄；校验器始终是唯一权威）。"""
    tools = ['read_material', 'submit_question', 'submit_finding', 'submit_assertion',
             'request_information', 'request_delivery']
    if _research_allowed(state):
        tools.insert(1, 'research_evidence')
    return tuple(tools)


def _research_allowed(state) -> bool:
    if not state.spec.investigation_scope.get('allow_external_evidence', True):
        return False
    return len(state.queries) < state.spec.resource_limits['max_searches']


# ---- 校验：模型提交的是候选，代码是唯一权威 ------------------------------------

def proposal_errors(state, proposal) -> list:
    tool = proposal.get('tool')
    args = proposal.get('arguments') or {}
    if tool not in REVIEW_TOOL_SCHEMAS:
        return ['unknown_tool']
    if tool == 'research_evidence' and not _research_allowed(state):
        return ['research_not_allowed_or_budget_exhausted']
    missing = [key for key in REVIEW_TOOL_SCHEMAS[tool]['required'] if not args.get(key)]
    if missing:
        return ['missing_argument:' + ','.join(missing)]
    checker = globals().get('_errors_for_' + tool)
    return checker(state, args) if checker else []


def _errors_for_read_material(state, args):
    ref = str(args.get('material_ref') or '')
    if ref not in state.material_items:
        return ['material_not_in_scope:' + ref]
    return []


def _errors_for_research_evidence(state, args):
    question = state.question(str(args.get('question_id') or ''))
    if question is None:
        return ['unknown_question']
    if question['status'] != 'open':
        return ['question_not_open']
    query = str(args.get('query') or '').strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        return ['invalid_query']
    if args.get('drug_name') and not str(args['drug_name']).strip():
        return ['invalid_source_scope']
    return []


def _errors_for_request_information(state, args):
    if not isinstance(args.get('related_question_ids') or [], list):
        return ['invalid_related_question_ids']
    if not str(args.get('question_text') or '').strip():
        return ['empty_question_text']
    # ``related_question_ids`` 是**关联提示**，不是这一步的内容。指错一个 id 不该
    # 让"请补充 X"这件事整个作废——它仍然是有价值的请求，只是暂时挂不上具体问题。
    # 挂不上的部分由 advance 记进观察，模型下一轮看得见。
    return []


def _errors_for_submit_question(state, args):
    key = str(args.get('question_key') or '').strip()
    if not key or len(key) > 48:
        return ['invalid_question_key']
    text = str(args.get('text') or '').strip()
    if not text or len(text) > 300:
        return ['invalid_question_text']
    subjects = args.get('subjects') or []
    if not isinstance(subjects, list):
        return ['invalid_subjects']
    if args.get('status') in ('answered', 'superseded', 'cancelled') \
            and not str(args.get('resolution_summary') or '').strip():
        return ['closing_requires_resolution_summary']
    errors = _question_routing_errors(state, args, subjects)
    return errors


def _errors_for_submit_finding(state, args):
    if args.get('finding_type') not in FINDING_TYPES:
        return ['invalid_finding_type']
    if not str(args.get('statement') or '').strip():
        return ['empty_statement']
    return _source_errors(state, args, need_read_material=True)


def _errors_for_submit_assertion(state, args):
    predicate = args.get('predicate')
    if predicate not in PREDICATES:
        return ['invalid_predicate']
    value = args.get('value')
    if value is None or (isinstance(value, str) and not value.strip()):
        return ['empty_assertion_value']
    if predicate in STRUCTURED_FIELDS:
        if not isinstance(value, dict) or value.get('expect') not in ('same', 'different', 'missing'):
            return ['structured_assertion_needs_expect']
    else:
        qualifiers = args.get('qualifiers') or {}
        if not str(qualifiers.get('excerpt') or '').strip():
            return ['assertion_needs_evidence_excerpt']
        if PREDICATES.get(predicate) == 'C' and not str(qualifiers.get('explanation') or '').strip():
            return ['semantic_assertion_needs_explanation']
    return _source_errors(state, args, need_read_material=predicate in STRUCTURED_FIELDS)


def _source_errors(state, args, *, need_read_material):
    """引用只接受**本轮真的观察到**的 id；材料引用还要求原文**已经被有效读过**。

    "有效读过"由**读取凭据**证明，不由"模型调用过 read_material"证明：系统通过
    同样的权限与完整性校验读过的条目同样算数。这不把代码的读取记成模型的读取——
    归因另有一处记录，报告里分得清。
    """
    materials = args.get('material_refs') or (args.get('qualifiers') or {}).get('material_refs') or []
    for ref in materials:
        if ref not in state.material_items:
            return ['material_not_in_scope:' + str(ref)]
        if need_read_material and state.read_credential(str(ref)) not in CREDENTIALS_SUPPORTING:
            return ['material_not_read_back:' + str(ref)]
    for ref in args.get('evidence_refs') or []:
        if ref not in state.evidence_refs:
            return ['evidence_not_observed_in_scope:' + str(ref)]
    # 问题引用与补充请求同理：解析得到就挂上，解析不到就丢弃——它不改变这条发现
    # 或断言本身讲的是不是事实，只影响"这条挂在哪条问题下面"。
    return []


def _question_routing_errors(state, args, subjects) -> list:
    """防止问题被分流到错误的来源。

    模型仍然决定**查什么**；运行器只拦下"这件事的答案根本不在那边"的分流。四条
    都是真实发生过的错配：

    1. 材料上已经写明的信息，回头再要用户填；
    2. "说明书对此怎么说"直接变成"请用户确认说明书结论"；
    3. "用户实际如何使用"被网络资料顶替；
    4. 专业判断被用户随口确认后升级成已验证事实。

    另外：新问题必须有归属——说清它服务于哪条现有要求，或者自己标成可选。
    没有归属的问题等于凭空给用户加了一项义务。
    """
    direction = args.get('direction')
    if direction not in QUESTION_DIRECTIONS:
        return ['invalid_direction']
    errors = []
    serves = str(args.get('serves_requirement_id') or '').strip()
    if serves and state.delivery_requirement(serves) is None:
        errors.append('unknown_requirement:' + serves)
    if not serves and not args.get('optional'):
        # 既没有归属、也没有标成可选 → 拒绝。这不是"多问一句"，是把一件它自己
        # 都没说清为什么要做的事塞进用户的待办里。
        errors.append('question_needs_requirement_or_optional')
    if direction == DIRECTION_REFERENCE_EVIDENCE \
            and not state.spec.investigation_scope.get('allow_external_evidence', True):
        errors.append('research_not_allowed_by_scope')
    if direction == DIRECTION_USER_INPUT and _material_already_answers(state, subjects):
        # 材料上已经写明了，再问用户就是让用户替系统读一遍自己的材料。
        errors.append('material_already_answers_this')
    return errors


def _material_already_answers(state, subjects) -> bool:
    """这个问题要的事实，已选材料里是不是已经有了**可比较的值**。

    只按对象收敛，不按字段猜：模型没给字段，系统就只在"这个对象在材料里已经比出
    结论"时提醒它——不替它决定该看哪一格。
    """
    from .fields import DECIDED_STATUSES
    if not subjects:
        return False
    wanted = {str(item) for item in subjects}
    for ref, comparisons in (state.field_comparisons or {}).items():
        known = {ref} | set(state.material_subject_refs(ref))
        if not (wanted & known):
            continue
        if any(row['comparison_status'] in DECIDED_STATUSES for row in comparisons):
            return True
    return False


def _resolve_subjects(state, subjects, linked_question_ids) -> list:
    """补充请求要**声明对象**。对象来自模型给的 subjects，或它挂上的问题的主体。

    两处都拿不到就返回空——调用方据此明确反馈，而不是留下一个无法恢复的阻塞请求，
    也不是去猜应该写进哪条患者记录。
    """
    resolved = [str(item).strip() for item in subjects if str(item).strip()]
    for question in (state.question(str(ref)) for ref in linked_question_ids or []):
        for subject in (question or {}).get('subjects') or []:
            if str(subject).strip() and str(subject).strip() not in resolved:
                resolved.append(str(subject).strip())
    return resolved


def _resolve_questions(state, refs) -> tuple:
    """把一个"问题引用"解析成真正的问题 id。

    模型的引用可以是 id，也可以是**问题的原文**——要求它逐字复制内部 id 是基础设施
    负担，不是调查能力。两条都对不上就丢弃并如实回报，而不是把整个提案拒掉。
    """
    linked, dropped = [], []
    for ref in refs:
        text = str(ref).strip()
        question = state.question(text) or next(
            (item for item in state.questions if item['text'].strip() == text), None)
        if question is None:
            dropped.append(text[:60])
        elif question['question_id'] not in linked:
            linked.append(question['question_id'])
    return linked, dropped


# ---- 处理器 ------------------------------------------------------------------

def build_review_handlers(*, rag_tool=None, evidence_store=None, run_id=None,
                          patient_revision_fn=None):
    """返回 ``{tool_name: handler(request)}``。handler 只执行领域动作并返回观察；
    **状态变更由 advance 从观察里采纳**（与旧路径同一条纪律：执行器不改状态）。"""

    def read_material(request):
        state = request.state.review
        args = request.arguments
        ref = str(args['material_ref'])
        case_id, item_id = ref.split('/', 1)
        detail = request.state.review_index.item(case_id, item_id)
        # 模型读的记在 ``material_read_refs``（归因），读取凭据记在 ``source_reads``
        # （可用性）。两者分开，"代码读的"就永远不会被算成"模型读的"。
        if ref not in state.material_read_refs:
            state.material_read_refs.append(ref)
        integrity = 'verified' if detail.get('document_id') else 'unknown'
        state.record_source_read(
            ref, credential=CREDENTIAL_MODEL,
            fingerprint=state.material_fingerprints.get(ref), integrity=integrity,
            document_id=detail.get('document_id'),
            parser_version=detail.get('parser_version'),
            locations=detail.get('locations') or {}, kind=detail.get('kind'))
        original = detail.get('original_fields') or {}
        fields = detail.get('fields') or {}
        return {
            'material_ref': ref, 'case_id': case_id, 'item_id': item_id,
            'original_fields': original, 'fields': fields,
            'corrections': detail.get('corrections') or [],
            'locations': detail.get('locations') or {},
            'kind': detail.get('kind'), 'issues': list(detail.get('issues') or []),
            'status': detail.get('status'),
            'document_id': detail.get('document_id'),
            'parser_version': detail.get('parser_version'),
            'version': state.material_fingerprints.get(ref),
            'integrity': integrity,
            'complete': True, 'omitted_fields': [],
            'note': ('这是材料条目的原文与当前字段。材料仍是**候选**，不是患者事实；'
                     '未确认前不能当成权威记录。'),
        }

    def research_evidence(request):
        state = request.state.review
        args = request.arguments
        question_id = str(args['question_id'])
        query = str(args['query']).strip()
        scope_args = {key: args[key] for key in ('drug_name', 'section') if args.get(key)}
        state.queries.append(query)
        from ..harness.retrieval import search
        # 每次研究动作都记下它**服务于哪个用户问题**。不要求长篇推理，只需要一个
        # 简短的行动目的——没有归属的检索无法回答"这次到底在查什么"。
        purpose = str(args.get('purpose') or '').strip() or None
        result = search(rag_tool, {'query': query, 'top_k': RESEARCH_TOP_K, **scope_args},
                        scope_id=state.spec.scope_id) if rag_tool is not None else {
            'status': 'retrieval_error', 'error': {'kind': 'no_retriever'},
            'search_executed': False, 'results': []}
        if result.get('status') != 'found':
            return _research_failure(state, question_id, query, result, purpose=purpose)
        view = capture_from_rag_result(evidence_store, result, run_id=run_id, query=query,
                                       patient_revision=patient_revision_fn() if patient_revision_fn else None,
                                       scope_id=state.spec.scope_id)
        bodies, unfinished = [], []
        for item in view:
            ref = item['evidence_id']
            if ref not in state.evidence_refs:
                state.evidence_refs.append(ref)
            page = evidence_store.read(ref, scope_id=state.spec.scope_id, offset=0, limit=READ_LIMIT)
            if ref not in state.read_evidence_refs:
                state.read_evidence_refs.append(ref)
            bodies.append({'evidence_id': ref, 'content': page['content'],
                           'total_chars': page['total_chars'], 'truncated': page['truncated'],
                           'source_uri': page['source_uri'],
                           'next_offset': page['offset'] + page['returned_chars'] if page['truncated'] else None})
            if page['truncated']:
                unfinished.append(f'{ref} 还有 {page["total_chars"] - page["returned_chars"]} 字未读')
        state.link_question_evidence(question_id, [r['evidence_id'] for r in bodies])
        state.research_decisions.append(
            {'question_id': question_id, 'query': query, 'purpose': purpose,
             'outcome': 'found', 'evidence_refs': [r['evidence_id'] for r in bodies],
             'at': None})
        return {'status': 'found', 'query': query, 'question_id': question_id,
                'search_executed': True, 'applied_filters': result.get('applied_filters'),
                'corpus_version': result.get('corpus_version'),
                'evidence': bodies, 'unfinished_checks': unfinished,
                'note': ('以上是检索到的原文片段。它们只是材料，不是结论：'
                         '要主张什么请用 submit_assertion，由代码核查它是否真的被支持。')}

    def _research_failure(state, question_id, query, result, *, purpose=None):
        status = result.get('status')
        if status == 'no_match':
            # 正常无匹配：**不是**执行故障，也不能被当成"没有医学证据"。
            category, summary = ISSUE_NO_MATCH, '已检索，本节范围内没有匹配内容'
        elif status in ('invalid_filter', 'invalid_query'):
            category, summary = ISSUE_INVALID_ARGUMENTS, '检索条件不正确，这一步没有执行'
        elif status == 'empty_filter_scope':
            category, summary = ISSUE_NO_MATCH, '所选范围没有可供核查的内容'
        elif status == 'retrieval_error':
            category, summary = ISSUE_CONNECTION, '检索执行失败，尚不能判断是否存在依据'
        else:
            category, summary = ISSUE_SOURCE_INVALID, '检索结果未能通过来源校验'
        issue = state.add_issue(operation_ref=f'research:{question_id}:{query[:40]}',
                                category=category, affected_question_ids=[question_id],
                                remote_outcome='executed' if result.get('search_executed') else 'not_executed',
                                user_visible_summary=summary, detail={'status': status})
        state.research_decisions.append(
            {'question_id': question_id, 'query': query, 'purpose': purpose,
             'outcome': status, 'evidence_refs': [], 'at': None})
        return {'status': status, 'query': query, 'question_id': question_id,
                'search_executed': bool(result.get('search_executed')),
                'applied_filters': result.get('applied_filters'),
                'directory': result.get('directory'), 'invalid_fields': result.get('invalid_fields'),
                'issue': {'issue_id': issue['issue_id'], 'category': category,
                          'retryability': issue['retryability'], 'summary': summary},
                'note': '这一步没有找到可用依据；它会被记录为执行情况，而不是被当成"没有风险"。'}

    def request_information(request):
        state = request.state.review
        args = request.arguments
        linked, dropped = _resolve_questions(state, args.get('related_question_ids') or [])
        subjects = _resolve_subjects(state, args.get('subjects') or [], linked)
        if not subjects:
            # 说不清"补充的是哪一条记录上的什么字段"的请求，用户答了也无处安放。
            # 返回明确反馈让模型补上对象，而不是留下一个无法恢复的阻塞请求，也不是
            # 由系统去猜应该写进哪条患者记录。
            return {'request_id': None, 'status': 'rejected_missing_target',
                    'note': ('这条补充请求没有指明对象：请用 subjects 给出涉及的对象'
                             '（药名或材料条目引用），或把 related_question_ids 指向一条'
                             '已声明的问题。系统不会替你推测这条补充属于哪条记录。')}
        before = {entry['request_id'] for entry in state.input_requests}
        blocks = str(args.get('blocks_requirement_id') or '').strip()
        item = state.add_input_request(
            question_text=args['question_text'],
            related_question_ids=linked,
            required_fields=args.get('required_fields') or [],
            why_needed=args.get('why_needed') or '',
            subjects=subjects,
            purpose=args.get('purpose') or INPUT_MATERIAL_NOTE,
            target={'subjects': subjects,
                    'fields': list(args.get('required_fields') or []),
                    'material_ref': args.get('material_ref')},
            missing_fact=args.get('missing_fact'),
            why_material_insufficient=args.get('why_material_insufficient'),
            blocks_requirement_ids=[blocks] if blocks else [],
            origin=ORIGIN_MODEL)
        reused = item['request_id'] in before
        blocking = bool(item['blocks_requirement_ids'])
        value = {'request_id': item['request_id'], 'question_text': item['question_text'],
                 'status': item['status'], 'linked_questions': linked,
                 'subjects': item['subjects'], 'required_fields': item['required_fields'],
                 'purpose': item['purpose'], 'reused_existing_request': reused,
                 'blocks_requirement_ids': item['blocks_requirement_ids'],
                 'blocking': blocking,
                 'note': (('这条补充请求之前已经提过，返回的是同一条；不需要重复提问。'
                           if reused else
                           ('这条请求挡住一项必需要求，系统会在本轮结束时暂停等待用户补充。'
                            if blocking else
                            '这条请求没有挡住任何必需要求——它是一条**可选建议**，'
                            '不会让任务变成等待状态，也不会阻塞原任务。'))
                          + '如果同样的对象和字段再次缺少，仍然指向这一条。')}
        if blocks and not item['blocks_requirement_ids']:
            value['blocked_requirement_ignored'] = blocks
            value['blocked_requirement_note'] = (
                '这个 requirement_id 不是一条必需要求，已按可选处理：'
                '可选建议不能通过补问阻塞原任务。')
        if dropped:
            value['unlinked_question_refs'] = dropped
            value['unlinked_note'] = ('这几个引用对不上任何已声明的问题，已忽略；'
                                      '要挂到某条问题上，请照模型视图里 questions[].question_id 原样填写，'
                                      '或者直接用问题的原文。')
        return value

    def submit_question(request):
        state = request.state.review
        args = request.arguments
        optional = bool(args.get('optional'))
        serves = str(args.get('serves_requirement_id') or '').strip() or None
        if optional and not serves:
            # 可选问题也要有一条**自己的**要求记录，否则它会把"当前任务完成了没有"
            # 这个问题搅浑：一个没有归属的问题挂在待办里，却没人说得清它属于谁。
            optional_requirement = state.spec.delivery_requirement(
                'model:optional:' + str(args['question_key'])[:32])
            if optional_requirement is None:
                from .contract import optional_requirement as build_optional
                state.spec.delivery_requirements.append(build_optional(
                    requirement_id='model:optional:' + str(args['question_key'])[:32],
                    text=str(args['text']),
                    subject_refs=args.get('subjects') or [],
                    reason='模型提出的可选补充调查'))
                serves = 'model:optional:' + str(args['question_key'])[:32]
        item = state.submit_question(question_key=args['question_key'], text=args['text'],
                                     subjects=args.get('subjects') or [],
                                     origin=ORIGIN_MODEL,
                                     related_material_refs=args.get('related_material_refs') or [],
                                     status=args.get('status'),
                                     resolution_summary=args.get('resolution_summary'),
                                     direction=args['direction'],
                                     serves_requirement_id=serves, optional=optional)
        return {'question_id': item['question_id'], 'revision': item['revision'],
                'status': item['status'], 'direction': item['direction'],
                'serves_requirement_id': item.get('serves_requirement_id'),
                'optional': item.get('optional'),
                'open_questions': [q['question_id'] for q in state.open_questions()]}

    def submit_finding(request):
        state = request.state.review
        args = request.arguments
        linked, dropped = _resolve_questions(state, args.get('question_refs') or [])
        item = state.add_finding(finding_type=args['finding_type'], statement=args['statement'],
                                 origin=ORIGIN_MODEL,
                                 subject_refs=args.get('subject_refs') or [],
                                 material_refs=args.get('material_refs') or [],
                                 evidence_refs=args.get('evidence_refs') or [],
                                 question_refs=linked)
        value = {'finding_id': item['finding_id'], 'finding_type': item['finding_type'],
                 'assessment_status': item['assessment_status'],
                 'note': '这是候选发现。要让它成为结论，需要有已读回的来源，并在交付检查里通过。'}
        if dropped:
            value['unlinked_question_refs'] = dropped
        return value

    def submit_assertion(request):
        state = request.state.review
        args = request.arguments
        qualifiers = dict(args.get('qualifiers') or {})
        item = state.submit_assertion(subject_refs=args.get('subject_refs') or [],
                                      predicate=args['predicate'], value=args['value'],
                                      qualifiers=qualifiers,
                                      evidence_refs=args.get('evidence_refs') or [])
        from .verify import verify_assertion
        result = verify_assertion(state, item, evidence_store=evidence_store)
        item['verification_status'] = result['status']
        item['verification_reasons'] = result['reasons']
        item['auto_assessment'] = result.get('auto_assessment')
        item['verification_basis'] = result['basis']
        state.refresh_axes()
        return {'assertion_id': item['assertion_id'], 'revision': item['revision'],
                'verification_status': item['verification_status'],
                'reasons': item['verification_reasons'],
                'note': ('supported 才可写进"一致项"一节；requires_human_confirmation 只表示'
                         '片段存在，语义解释仍需人工确认。')}

    def request_delivery(request):
        return {'requested': True}

    return {'read_material': read_material, 'research_evidence': research_evidence,
            'request_information': request_information, 'submit_question': submit_question,
            'submit_finding': submit_finding, 'submit_assertion': submit_assertion,
            'request_delivery': request_delivery}


class ReviewPlanner:
    """模型决策的适配器：复用 ``LLMPlanner`` 的**传输层**（重试、429 退避、
    ``tool_choice`` 降级、多调用裁剪、用量记账），只换 system prompt、payload 与
    provider 真正收到的函数表。

    刻意不复用旧 planner 的 payload：它假设 ``state.investigation`` 存在，而本契约
    的模型视图**不**建立在"先读权威快照"的前提上。传输层与决策层在这里分开，
    所以两者可以各自演进而不互相拖累。
    """

    def __init__(self, transport):
        self.planner = transport

    def propose(self, state):
        planner = self.planner
        planner.system_prompt = lambda _state: REVIEW_SYSTEM_PROMPT
        planner.prompt_payload = review_payload
        planner.tool_definitions = review_tool_definitions
        planner.tool_schemas = dict(REVIEW_TOOL_SCHEMAS)
        return planner.propose(state)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.planner, 'proposal_provider', None) or
                    getattr(self.planner, 'client', None) or
                    getattr(self.planner, 'model', None))


def review_payload(state) -> dict:
    from ..agent import PLANNER_PROTOCOL_VERSION
    return {
        'task': 'material-review@2',
        'protocol': {
            'version': PLANNER_PROTOCOL_VERSION,
            'note': ('每次只提交一个工具调用。参数由函数表本身约束；'
                     '不需要填写 scope、版本、预算或内部回执——运行器会附加。'),
        },
        'review': state.review.model_view(state.review.review_context),
        'tool_functions': review_tool_definitions(state),
        'correction_task': state.pending_correction,
    }


def review_tool_definitions(state) -> list:
    import copy
    definitions = []
    for name in allowed_review_tools(state.review):
        schema = REVIEW_TOOL_SCHEMAS[name]
        definitions.append({'type': 'function', 'function': {
            'name': name, 'description': TOOL_DESCRIPTIONS[name],
            'parameters': {'type': 'object',
                           'properties': copy.deepcopy(schema['properties']),
                           'required': list(schema.get('required') or []),
                           'additionalProperties': False}}})
    return definitions
