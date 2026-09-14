"""Code-owned, versioned bounded medication evidence review (A1).

This is a run artifact, not an A2 persistent open task. Raw documents are data;
only authorised tool observations and fresh memory can advance coverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any
import hashlib
import itertools
import json
import os
import re

from . import answer_grounding as grounding
from .claim_support import assess_support
from .evidence_quality import assess_claim
from .response_safety import composed_text_prescribes

VERSION = 'investigation@1'
CONTRACT = 'medication-evidence-review@1'
REQUIRED = ('authority', 'interaction_evidence', 'applicability')
MAX_CLAIMS = 12
MAX_SEARCHES = 3
MAX_EVIDENCE = 12
# 连续被拒的子问题声明次数上限。A planner is allowed to revise; it is not
# allowed to spin. 界是**连续**的：一次成功即归零，所以反复摸索不会累积成
# 一次停摆，而原地打转很快就会撞上。
MAX_PLAN_ATTEMPTS = 3
# 一次调查内**成功采用**的修订轮数上限。与"连续被拒次数"是两个不同的界：
# 反复被拒不消耗修订额度，成功修订重置被拒计数。
MAX_PLAN_REVISIONS = 2
# Protocol v2: state-conditional tool exposure + structured rejection
# feedback.  The state schema itself is unchanged (restore() still validates
# VERSION/CONTRACT), so persisted investigations stay loadable.
PROTOCOL_VERSION = 'investigation-protocol@2'

# ---- 问题模型 ---------------------------------------------------------------
# 旧契约只有一件事：把每条子问题变成"去搜支持和反对证据"。那对**证据类**问题
# 是对的，对"患者自己的用药开始时间"这种问题是错的——它把模型逼进"反复检索
# 一个资料库里根本不存在的答案"，最后以 no_progress 收场。
#
# 现在把"需要查清的问题"与"等待验证的结论"分开：问题有类型，只有**确实需要
# 证据支持或反驳**的那一类才建 claim，其余各走各的来源。
QUESTION_USER_FACT = 'user_fact'               # 需要用户补充的事实
QUESTION_MATERIAL_READ = 'material_read'       # 需要读取材料核实
QUESTION_REFERENCE_LOOKUP = 'reference_lookup'  # 需要检索权威资料
QUESTION_SOURCE_CONFLICT = 'source_conflict'   # 两个来源之间的冲突
QUESTION_PROFESSIONAL = 'professional_judgment'  # 需要专业人员判断
QUESTION_KINDS = (QUESTION_USER_FACT, QUESTION_MATERIAL_READ, QUESTION_REFERENCE_LOOKUP,
                  QUESTION_SOURCE_CONFLICT, QUESTION_PROFESSIONAL)

#: 只有这些问题需要"证据支持/反驳"，因此才建 claim。
EVIDENCE_QUESTION_KINDS = (QUESTION_REFERENCE_LOOKUP,)
#: 阻塞完成的问题类型。拿到了答案、或明确不可得，才不再阻塞。
BLOCKING_QUESTION_KINDS = QUESTION_KINDS
#: 这些类型等的是**人**，不是资料。
WAITING_ON_PERSON = (QUESTION_USER_FACT, QUESTION_PROFESSIONAL)

QUESTION_STATUS_OPEN = 'open'
QUESTION_STATUS_ANSWERED = 'answered'
QUESTION_STATUS_UNAVAILABLE = 'unavailable'   # 资料确实拿不到，如实记下
QUESTION_STATUS_CLOSED = 'closed'

# ---- 信息目标 × 取证策略 ----------------------------------------------------
# 上一版把"要弄清什么"和"从哪里取"压成一个 ``kind``，于是**换来源就等于换问题**：
# 模型发现"问用户"不合适、改去查资料，身份一变就被判成"删掉了一个未决问题"。
#
# 现在拆开：
#   * **信息目标**表示这条问题要弄清的是哪一类信息 —— 它决定身份；
#   * **取证策略**表示这一次从哪里取 —— 它**不参与身份**，可以中途更换并留痕。
#
# 同一个问题因此可以在不同阶段换来源，而问题、历史、回答、证据都不丢。
TARGET_PATIENT_STATE = 'patient_actual_state'      # 这位患者实际是什么情况
TARGET_MATERIAL_RECORD = 'material_record'         # 材料里记的是什么
TARGET_GENERAL_REFERENCE = 'general_reference'     # 一般参考知识
TARGET_PROFESSIONAL = 'professional_judgment'      # 需要专业判断
INFORMATION_TARGETS = (TARGET_PATIENT_STATE, TARGET_MATERIAL_RECORD,
                       TARGET_GENERAL_REFERENCE, TARGET_PROFESSIONAL)

STRATEGY_PATIENT_RECORD = 'patient_record'         # 读已有的患者记录
STRATEGY_ASK_USER = 'ask_user'                     # 向用户询问
STRATEGY_PATIENT_MATERIAL = 'patient_material'     # 读患者上传的材料
STRATEGY_GENERAL_REFERENCE = 'general_reference'   # 检索一般药品资料
STRATEGY_PROFESSIONAL_REVIEW = 'professional_review'  # 请求专业复核
STRATEGIES = (STRATEGY_PATIENT_RECORD, STRATEGY_ASK_USER, STRATEGY_PATIENT_MATERIAL,
              STRATEGY_GENERAL_REFERENCE, STRATEGY_PROFESSIONAL_REVIEW)

#: 来源**能力**：某个策略能回答哪一类信息目标。
#:
#: 这里最重要的一条是 ``general_reference`` **不能**回答 ``patient_actual_state``
#: —— 一般药品资料可以说"这类药一般怎么用"，说不了"这位用户实际怎么吃"。
#: 选错时给出具体反馈让模型改，而不是替它把来源换掉。
STRATEGY_CAPABILITY = {
    TARGET_PATIENT_STATE: (STRATEGY_PATIENT_RECORD, STRATEGY_ASK_USER,
                           STRATEGY_PATIENT_MATERIAL, STRATEGY_PROFESSIONAL_REVIEW),
    TARGET_MATERIAL_RECORD: (STRATEGY_PATIENT_MATERIAL,),
    TARGET_GENERAL_REFERENCE: (STRATEGY_GENERAL_REFERENCE,),
    TARGET_PROFESSIONAL: (STRATEGY_PROFESSIONAL_REVIEW,),
}

#: 每个策略**通常**由哪个工具执行。能力陈述，不是顺序规定。
STRATEGY_TOOLS = {
    STRATEGY_PATIENT_RECORD: ('memory_read',),
    STRATEGY_ASK_USER: ('ask_clarification',),
    STRATEGY_PATIENT_MATERIAL: ('read_material_item', 'list_materials'),
    STRATEGY_GENERAL_REFERENCE: ('rag_search', 'rag_catalog', 'read_evidence',
                                 'acquire_evidence', 'ddi_check'),
    STRATEGY_PROFESSIONAL_REVIEW: (),
}

#: 默认策略：声明时不给就按目标推断。
DEFAULT_STRATEGY = {
    TARGET_PATIENT_STATE: STRATEGY_ASK_USER,
    TARGET_MATERIAL_RECORD: STRATEGY_PATIENT_MATERIAL,
    TARGET_GENERAL_REFERENCE: STRATEGY_GENERAL_REFERENCE,
    TARGET_PROFESSIONAL: STRATEGY_PROFESSIONAL_REVIEW,
}

#: 上一版的 ``kind`` → （信息目标，取证策略）。旧记录按这张表**读懂**，
#: 但**不重算它们的 question_id**——那会让前端已有的请求和用户回答失去关联。
LEGACY_KIND = {
    'user_fact': (TARGET_PATIENT_STATE, STRATEGY_ASK_USER),
    'material_read': (TARGET_MATERIAL_RECORD, STRATEGY_PATIENT_MATERIAL),
    'reference_lookup': (TARGET_GENERAL_REFERENCE, STRATEGY_GENERAL_REFERENCE),
    'source_conflict': (TARGET_PATIENT_STATE, STRATEGY_PATIENT_MATERIAL),
    'professional_judgment': (TARGET_PROFESSIONAL, STRATEGY_PROFESSIONAL_REVIEW),
}

#: 信息的**状态**（与执行结果、事项状态都不是一回事）。
INFO_NOT_ATTEMPTED = 'not_attempted'        # 尚未尝试获取
INFO_ATTEMPTED_NO_RESULT = 'attempted_no_result'  # 已尝试但未取得
INFO_SOURCE_LIMITED = 'source_limited'      # 当前可用来源受限
INFO_RECEIVED_UNCONFIRMED = 'received_unconfirmed'  # 收到信息但未确认
INFO_AVAILABLE = 'available'                # 已有适用依据
INFORMATION_STATES = (INFO_NOT_ATTEMPTED, INFO_ATTEMPTED_NO_RESULT,
                      INFO_SOURCE_LIMITED, INFO_RECEIVED_UNCONFIRMED, INFO_AVAILABLE)

#: 这些信息状态**不阻塞**完成。`source_limited` 与 `attempted_no_result` **阻塞**
#: ——"没查到"不等于"已经有答案"。
SETTLED_INFORMATION_STATES = (INFO_AVAILABLE,)


#: 答案的来源种类 → 服务端用哪条规则判断"来源真的支持这个答案"。
ANSWER_SOURCE_PATIENT_RECORD = 'patient_record'
ANSWER_SOURCE_EVIDENCE = 'evidence'
ANSWER_SOURCE_MATERIAL = 'material'
ANSWER_SOURCE_USER = 'user_answer'
ANSWER_SOURCE_PROFESSIONAL = 'professional'
ANSWER_SOURCES = (ANSWER_SOURCE_PATIENT_RECORD, ANSWER_SOURCE_EVIDENCE,
                  ANSWER_SOURCE_MATERIAL, ANSWER_SOURCE_USER,
                  ANSWER_SOURCE_PROFESSIONAL)

#: 来源种类 → 它的**认识论属性**。用户报告永远不等于已核实事实。
SOURCE_PROVENANCE = {
    ANSWER_SOURCE_PATIENT_RECORD: 'authoritative_record',
    ANSWER_SOURCE_EVIDENCE: 'reference_evidence',
    ANSWER_SOURCE_MATERIAL: 'material_record',
    ANSWER_SOURCE_USER: 'user_reported',
    ANSWER_SOURCE_PROFESSIONAL: 'professional_opinion',
}


def utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _normalise_answer(value) -> str:
    """答案比对前的归一化：去空白、去全角空格、统一大小写。"""
    return re.sub(r'\s+', '', str(value or '')).lower()


def _row_field(row, key):
    """从一行记录里取字段。这行**没有**这一列时返回 None，而不是抛错。

    sqlite3.Row 与测试用的最小替身（普通 dict）都能走这一条；"没有这一列"与
    "这一列是空值"因此能分开——前者表示这条记录没有声明该字段，判定会在
    ``reason`` 里写明未做该部分核对，而不是把"没声明"当成"声明为 None"。
    """
    if row is None:
        return None
    try:
        keys = row.keys()
    except AttributeError:
        keys = row
    if key not in keys:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


#: 值里带的单位。剂量类字段是"数值+单位"写在一个字符串里的，所以单位不是
#: 单独一列，而是从值里读出来的——核对时它随值一起比，不在别处再比一遍。
_UNIT = re.compile(r'^\s*[\d.]+\s*([A-Za-z%]{1,6}|毫克|微克|克|毫升|国际单位|单位|片|粒|滴|喷|次)\s*$')


def _unit_of(value):
    match = _UNIT.match(str(value if value is not None else ''))
    return match.group(1) if match else None


def strategy_can_serve(target, strategy) -> bool:
    return strategy in STRATEGY_CAPABILITY.get(target, ())


#: 补问文本里的**祈使式用药改动**。这是一条很窄的检查，只服务于"发给用户的
#: 问句"这一条路：`composed_text_prescribes` 是给报告正文用的，它**故意**不把
#: 疑问句算成处方（上一轮修掉过一类误报）。但"请把剂量调整为 10mg"虽然以句号
#: 结尾，本质是一条改动用药的指令，不能借补问这条路发出去。纯提问
#: （"要不要调整？"）不受影响。
_CHANGE_VERBS = ('加', '减', '停', '换', '调整', '改为', '加倍', '减半', '增加', '减少')
_MED_NOUNS = ('剂量', '用量', '药量', '用药', '服药', '频次')
_QUESTION_FORM = ('要不要', '是否需要', '是否可以', '能不能', '该不该', '需不需要',
                  '是不是', '吗？', '吗?', '呢？', '呢?')


def is_medication_instruction(text: str) -> bool:
    if not any(verb in text for verb in _CHANGE_VERBS):
        return False
    if not any(noun in text for noun in _MED_NOUNS):
        return False
    return not any(marker in text for marker in _QUESTION_FORM)


def question_id_for(target, subject_refs, target_field=None) -> str:
    """问题的稳定身份：**（信息目标、相关对象、要确认的事实）**。

    取证策略**不在这里**——同一件事先查材料、再问用户，是同一个问题的策略变化，
    不是两个问题，更不是"删掉了一个未决问题"。

    也刻意**不**用"药名集合"当身份：那正是更早一版把不同子问题压成一条的地方。
    对象与目标字段都在，所以"同一药物的剂量问题"和"开始时间问题"天然是两条；
    "这位用户实际怎样服用"与"资料记载的一般用法"目标不同，也是两条。
    """
    refs = sorted({str(ref) for ref in (subject_refs or ()) if str(ref)})
    payload = '|'.join([target or '', ','.join(refs), target_field or ''])
    return 'q:' + digest(payload)[:12]


def _question_text(question) -> str:
    """一条问题给报告用的一句话——两种形状都认（typed 的 statement / 旧投影的 question）。"""
    text = question.get('statement') or question.get('question') or question.get('description')
    if not text:
        text = question.get('target_field') or question.get('gap_id') or '（未命名的问题）'
    kind = question.get('kind') or question.get('question_kind')
    return f'{text}（{kind}）' if kind else text


def is_question_answered(question) -> bool:
    """这条问题**已经有适用依据**了吗。

    `unavailable`（来源暂时取不到）**不在此列**：没查到不等于已经有答案。
    把它当成已回答，会让完成条件、修订判断和关闭校验一起误判——本轮修的
    正是这里。
    """
    return (question.get('status') == QUESTION_STATUS_ANSWERED
            and question.get('information_state') in SETTLED_INFORMATION_STATES)


def question_blocks(question) -> bool:
    """这条问题是否**仍然阻塞**本轮完成。

    "拿到了适用信息"才不阻塞。"这一轮取不到"（`source_limited`）与"试过没结果"
    （`attempted_no_result`）都仍然阻塞——预算不足不代表现实中没有资料，
    没找到也不代表风险已经处理。
    """
    if question.get('status') == QUESTION_STATUS_CLOSED:
        return False
    return not is_question_answered(question)


def open_questions(questions) -> list:
    return [q for q in questions or () if q.get('status') == QUESTION_STATUS_OPEN]


# ---- 契约策略 ---------------------------------------------------------------
# safety_case 与 evidence_review 需要**不同**的范围与完成条件。默认仍是旧行为，
# 所以既有契约逐字不变。
POLICY_EVIDENCE_REVIEW = 'evidence_review'
POLICY_SAFETY_CASE = 'safety_case'

POLICIES = {
    POLICY_EVIDENCE_REVIEW: {
        # 旧契约：子问题必须覆盖整个权威药单，完成要求三查全绿。
        'require_full_coverage': True,
        'required_checks': REQUIRED,
        'typed_questions': False,
    },
    POLICY_SAFETY_CASE: {
        # 新契约：范围是**当前事项**，不强制覆盖整个药单；不再要求固定的
        # 三查全绿——那三查是证据核查契约的完成条件，不是"这件事查清了没有"。
        'require_full_coverage': False,
        'required_checks': None,
        'typed_questions': True,
    },
}


def policy_of(name) -> dict:
    return POLICIES.get(name) or POLICIES[POLICY_EVIDENCE_REVIEW]

# Protocol v2: the ONE gap through which this investigation's sub-questions are
# declared.  It is the only legal ``gap_id`` for ``plan_questions``, so that
# tool can never be used to sidestep another open gap.
GAP_PLAN = 'subquestions'

# 只作为**结论**存在的缺口：它们必须出现在报告里，但不是待办，因此不阻塞
# 完成。产生 material_conflict 的地方写得很清楚——"Recorded as a finding,
# not a stop: a discrepancy is something to report"。可它**没有任何工具能
# 关闭**，而完成条件要求"无任何开放缺口"，于是任何读到过材料差异的回合都
# 只能以 no_progress 收尾：一个被声明为"结论"的东西实际上是一道永久闸门。
# 清单是**白名单**：未知缺口种类一律照旧阻塞（fail-closed）。
FINDING_GAPS = frozenset({'material_conflict', 'plan_revision_capped'})


def gap_closing_tools(inv, gap):
    """Which permitted tools could advance THIS open gap (presentation only).

    The catalog listed the tools and ``open_gaps`` listed the problems, but
    nothing connected them: the live traces show a planner linking
    ``memory_read`` to an ``evidence_missing`` claim gap and re-reading the
    snapshot while the gap that named "核查X的支持和反对证据" stayed open.  The
    mapping is derived from the same predicates ``allowed_tools`` uses and
    intersected with it, so this can never advertise a tool the state forbids,
    and it states capability rather than order — it does not say which to pick
    or in what sequence.

    An empty list is a real answer: it says no tool closes this gap, so the
    review ends with it unresolved (a conflict goes to human review) rather
    than pretending another read would help.
    """
    permitted = set(allowed_tools(inv))
    if gap.get('gap_id') == 'authority':
        candidates = {'memory_read'}
    elif gap.get('gap_id') == GAP_PLAN:
        candidates = {'plan_questions'}
    elif gap.get('kind') == 'question_open':
        # 每条问题按**它当前的取证策略**指出能推进它的工具。这是能力陈述，
        # 不是顺序规定——换来源就是换一组工具，问题本身不变。
        # 专业复核类空列表是**真答案**：没有工具能关它，它就该以未决状态进入
        # 人工复核，而不是假装再读一次会有用。
        candidates = set(STRATEGY_TOOLS.get(gap.get('question_strategy'), ()))
    elif gap.get('kind') == 'patient_fact_missing':
        candidates = {'ask_clarification'}
    elif gap.get('kind') == 'evidence_missing':
        candidates = {'rag_catalog', 'rag_search', 'ddi_check', 'read_evidence', 'acquire_evidence'}
    else:
        candidates = set()
    return sorted(candidates & permitted)


def allowed_tools(inv):
    """Tools the current state may legally use (presentation only).

    Mirrors ``proposal_errors``: narrowing the advertised catalog shrinks the
    parameter space the model searches, while the validator remains the sole
    authority.  ``memory_write`` stays available for consolidation before
    respond; ``respond`` appears only once the code has set a termination
    reason."""
    if inv.termination_reason:
        return ('respond',)
    if not inv.authority_read:
        return ('memory_write', 'memory_read')
    # Read-only, always legal inside a review.  When no MaterialIndex is
    # attached these are not registered, so tool_definitions skips them (it
    # drops names with no schema) and a proposal naming one is unknown_tool.
    allowed = ['memory_write', 'memory_read', 'list_materials', 'read_material_item']
    # 首次规划，或**有新证据**触发的修订。旧闸门问的是"``subquestions`` 缺口
    # 是否打开"，而接受声明正好把它解析掉——接受之后便再也不能修订，与工具
    # 自身的描述矛盾。
    if inv.revision_trigger() is not None:
        allowed.append('plan_questions')
    if policy_of(inv.policy).get('typed_questions'):
        # 有等用户回答的问题，就等于有补问的权力——不需要先有一个代码写好的
        # "缺 field 的事实缺口"才被允许开口。
        if any(g['kind'] == 'question_open' and g['status'] == 'open'
               and g.get('question_strategy') == STRATEGY_ASK_USER for g in inv.gaps):
            allowed.append('ask_clarification')
        # 有未决问题就可以**采纳**——不论来源是刚读的材料、刚查的资料，还是
        # 本来就在权威记录里。采纳的门槛在 `answer_question` 里，不在工具列表。
        if any(g['kind'] == 'question_open' and g['status'] == 'open' for g in inv.gaps):
            allowed.append('answer_question')
            # 变更候选与采纳同一个前提：得有一条**在问的问题**，才会有"用户的回答
            # 意味着记录该改了"这件事。没有问题时提候选，等于凭空发起一次改药。
            allowed.append('propose_medication_change')
    elif any(g['kind'] == 'patient_fact_missing' and g['status'] == 'open' and g.get('field') for g in inv.gaps):
        allowed.append('ask_clarification')
    if any(g['kind'] == 'evidence_missing' and g['status'] == 'open' for g in inv.gaps):
        from .harness.evidence_acquire import enabled
        allowed.extend(('rag_catalog', 'ddi_check'))
        if len(inv.queries) < inv.search_limit():
            allowed.append('acquire_evidence' if enabled() else 'rag_search')
    # 回读**本身就是完成条件的一部分**：``forced_stop`` 要求
    # ``not unread_evidence()``。所以只要还有已检索未回读的原文，这个工具就
    # 必须可用，与"当前有没有开放的证据缺口"无关——缺口已经关闭而原文尚未
    # 回读是最常见的情形，此时若把工具收走，"还差一次回读"与"没有工具能回读"
    # 就同时成立，回合只能靠预算耗尽或熔断收尾。
    if any(ref not in inv.read_refs for ref in inv.evidence_refs):
        allowed.append('read_evidence')
    return tuple(dict.fromkeys(allowed))


# Vocabulary the delivered-text safety check treats as a hazard assertion.
# Mirrors response_safety's concrete-hazard set; a line naming one of these
# needs a grounded citation, so the report never REPEATS such a sentence from
# model-authored text — it reports the subjects instead.
CONCRETE_HAZARD = re.compile(r'出血|低血压|致命|肾损伤|肝损伤|bleeding|fatal', re.IGNORECASE)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class InvestigationState:
    goal: str
    scope_id: str
    version: str = VERSION
    contract_version: str = CONTRACT
    patient_version: dict = field(default_factory=dict)
    claims: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    checks: dict = field(default_factory=lambda: {k: 'uncovered' for k in REQUIRED})
    facts: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    evidence_refs: list = field(default_factory=list)
    read_refs: list = field(default_factory=list)
    #: **证据回读回执的正文**：`{'ref','offset','content'}`，每次 ``read_evidence``
    #: 真的返回给模型的那一段（材料走 `material_items[ref]['fields']`，见观察阶段）。
    #:
    #: 为什么连正文一起存：答案依据的核对要判"引文是不是**这个来源**已读片段里
    #: 逐字存在的一段"。只存 ref 名单的话，核对时只能把整篇原文再读一遍——那正好
    #: 是"校验时悄悄读取模型没看过的内容"。存窗口则核对的是**当时的回执本身**，
    #: 跨会话恢复后仍然有效（`read_refs` 一并保留，两者同步失效）。
    #: 旧状态没有这个键，缺省为空表：那时按 `read_refs` 的旧口径恢复成整篇窗口。
    read_windows: list = field(default_factory=list)
    content_hashes: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    search_keys: list = field(default_factory=list)
    retrieval_attempts: int = 0
    retrieval_feedback: list = field(default_factory=list)
    # 调查中识别出的**待确认**用药变更候选。它们**不是记录**：任务收尾时由执行器
    # 把它们登记进这次回访的候选队列，由**用户**决定改不改。放在这里而不是直接写
    # 库，是为了让本模块继续"不认识数据库"——它只声明意图。
    pending_change_candidates: list = field(default_factory=list)
    assessments: dict = field(default_factory=dict)
    no_progress_count: int = 0
    termination_reason: str | None = None
    authority_read: bool = False
    invalidations: list = field(default_factory=list)
    context_integrity: str = 'full_authority_checked_evidence_body_on_demand'
    mode: str = 'deterministic'
    # Protocol v2.  Additive only — an older persisted state restores fine and
    # simply reports 'unset'.  'model' means the model declared the
    # sub-questions; 'code_default' means the degraded policy had to.
    subquestion_source: str = 'unset'
    # 连续被拒的子问题声明次数。改数这个计数器而不是数 ``plan:*`` 缺口：缺口
    # 按 id 去重，而 id 是错误签名的拼接——同样的错误重复多少次都只有一个缺口
    # （上限永远够不到），三种不同的单次错误却会凑够三个（误触发）。
    plan_attempts: int = 0
    # 本轮的检索次数上限。任务可以声明一个更紧的预算（"首次检索无结果"族用它
    # 把可用检索压到 1，逼出"改写查询"而不是"换个说法再搜一次"）。0 = 用全局
    # 常量。声明了却只读全局常量，等于这个键从来没被执行过。
    search_budget: int = 0
    # 追加式修订历史：每条记下触发原因、实体集的变更与**显式保留**的证据。
    # 追加而非覆盖，因为"计划变过"本身是结论的一部分——哪一轮、因为什么、
    # 哪些证据继续有效，都要能回看。
    plan_revisions: list = field(default_factory=list)
    # Materials the model has SEEN (index) versus READ BACK (item).  Only the
    # latter can support a citation, mirroring the label-evidence rule that
    # "found" is not "read and verified".
    material_refs: list = field(default_factory=list)
    material_read_refs: list = field(default_factory=list)
    # 已"看到"的材料条目，唯一规范形状（ref -> {'name','kind','current'}）。
    # 写入端与读取端共用这一种形状，所以"材料里的药名能不能被子问题引用"与
    # "差异该点名哪条当前记录"读的是同一份数据。
    material_items: dict = field(default_factory=dict)
    pending_statements: list = field(default_factory=list)
    # 事项调查上下文（仅 safety_case 契约使用）：为什么有这件事、已知什么、还缺
    # 什么、上一步做了什么、这次相对上次新增了什么。**派生视图**——由调用方从
    # 既有真相源现取，随状态一起持久化，所以跨会话恢复后 Agent 仍有同一份上下文，
    # 不必重新读一遍患者档案。旧契约留空字典，行为不变。
    case_context: dict = field(default_factory=dict)
    #: 契约策略名。旧契约缺省 = evidence_review，行为逐字不变。
    policy: str = POLICY_EVIDENCE_REVIEW
    #: 权威记录是**程序**读的（真实快照校验），不是模型的一次工具调用。
    authority_source: str | None = None
    #: 问题集是否已经定下来（模型声明过、或降级策略给出过）。
    #: 在它之前**不能**判"没有未决问题"——那等于在模型还没来得及开口时
    #: 就说"没什么要问的"，把提问的机会整个吃掉。
    questions_settled: bool = False
    #: 最近一次采纳的**真实结果**（新增/复用/换策略 + 每个 question_id）。
    #: 由唯一生效点写入，观察阶段回填给模型。
    last_adoption: Any = None
    #: 本轮开始时有**模型还没看过**的新信息（用户的补充、记录变化…）。
    #: 在它被消费之前不能判定"只剩等人"：那样回答就永远用不上，
    #: 用户补了一句、系统却立刻回到等待。
    new_information_pending: bool = False
    #: 模型真正做过多少次决策。用来区分"模型选择了交付"与"代码在模型开口前
    #: 就收尾了"——后者交付里没有模型的选择，不能记成"Agent 决定了下一步"。
    model_decisions: int = 0
    # `memory` / `evidence_store` 是**实例属性，不是 dataclass 字段**：它们由
    # sync_authority / validate_sources 在运行期绑定，只用于按作用域核对对象引用
    # 是否真实存在。写成字段会被 ``asdict`` 带进持久化状态（序列化一个数据库
    # 连接），所以刻意不声明。
    # 只渲染了空态句的节标题。**派生字段**：每次 ``report_text()`` 重新计算，
    # 只为让序列化视图能把它带给评分器。默认空列表 = "没有一节是空态"，
    # 于是渲染失败时评分器按"该写没写"判失败——失败方向是保守的。
    empty_sections: list = field(default_factory=list)

    @classmethod
    def restore(cls, raw, scope):
        if raw.get('version') != VERSION or raw.get('contract_version') != CONTRACT:
            raise ValueError('investigation migration required: unsupported version')
        if raw.get('scope_id') != scope:
            raise ValueError('investigation scope mismatch')
        state = cls(**raw)
        if set(state.checks) != set(REQUIRED):
            raise ValueError('investigation contract coverage mismatch')
        return state

    def to_dict(self):
        return asdict(self)

    def planner_view(self):
        from .harness.context import bounded_patient_snapshot, omissions
        view = self.to_dict()
        # Bound with explicit omission markers; do not silently prefilter
        # chronic conditions or other reported patient facts.
        view['facts'] = bounded_patient_snapshot(self.facts)
        view['context_omissions'] = omissions(view['facts'])
        view['authority_validation'] = 'full_snapshot_outside_model_context'
        # Protocol v2: make "searched" vs "read and verified" explicit, expose
        # the open-gap queue and the tools the current state may legally use.
        # Presentation only — the validator stays the authority.
        view['protocol_version'] = PROTOCOL_VERSION
        view['evidence_unread'] = [ref for ref in self.evidence_refs if ref not in self.read_refs]
        view['evidence_searched_count'] = len(self.evidence_refs)
        view['evidence_read_count'] = len(self.read_refs)
        view['open_gaps'] = [{'gap_id': g['gap_id'], 'kind': g['kind'],
                              'description': g['description'],
                              # 哪些工具**可以**推进这个问题。空缺 = 没有工具能关它，
                              # 该缺口只能以未决状态进入报告或人工复核。
                              'closable_by': gap_closing_tools(self, g)}
                             for g in self.gaps if g['status'] == 'open']
        view['allowed_tools'] = list(allowed_tools(self))
        # 事项调查上下文：只覆盖**这一件事**（为什么有它、已知/未知、上次做了什么、
        # 这次新增了什么）。它是派生视图，不复制真相源；没有事项时整个键不出现，
        # 旧契约的载荷逐字不变。
        if self.case_context:
            view['case_context'] = self.case_context
        # 回读回执的**正文**是核对用的，不是给模型看的一份原文副本——模型已经
        # 在它自己的对话里看过那一段了。这里换成定位摘要，载荷与 content_hashes
        # 同一量级，不会因为"多存了回执"就把模型上下文顶爆。
        view['read_windows'] = [{'ref': w['ref'], 'offset': w['offset'],
                                 'chars': len(w.get('content') or '')}
                                for w in (view.get('read_windows') or [])]
        # 列过索引 ≠ 读过原文。两份清单分开给出，与 evidence_unread 同一口径：
        # 只有读回原文的条目才能作为引用。
        read_materials = set(self.material_read_refs)
        view['material_unread'] = [ref for ref in self.material_refs if ref not in read_materials]
        for ref, detail in (view.get('material_items') or {}).items():
            if isinstance(detail, dict):
                detail['read'] = ref in read_materials
        return view

    def validate_sources(self, evidence_store):
        """Revalidate on restore/publication; immutable old content can be superseded."""
        self.evidence_store = evidence_store
        invalid = []
        for ref in self.read_refs:
            try:
                evidence_store.read(ref, scope_id=self.scope_id, limit=1)
                with evidence_store._lock:
                    has_product = evidence_store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_objects'").fetchone()
                    replacements = evidence_store.connection.execute(
                        "SELECT body_json FROM product_objects WHERE scope_id=? AND kind='source_replacement'", (self.scope_id,)).fetchall() if has_product else []
                if any(json.loads(r[0]).get('old_evidence_id') == ref for r in replacements):
                    invalid.append(ref)
            except Exception:
                invalid.append(ref)
        for ref in invalid:
            self.gap('source:' + ref, 'source_invalid', '来源已失效或被明确替代；历史原文不作为当前支持。', evidence_ref=ref)
            for assessments in self.assessments.values():
                if ref in assessments:
                    assessments[ref].update(status='insufficient', source_status='invalid', condition_status='unknown')
            self.termination_reason = 'unrecoverable_failure'
        self._assess()

    # ---- Protocol v2: sub-questions -------------------------------------

    @property
    def subquestions_delegated(self) -> bool:
        """Whether the PLANNER owns the sub-question set.

        Only a real model does by default.  A ``scripted`` planner is the
        offline double: it proves the state machine and the execution
        constraints, and it must not be credited with a planning capability it
        was never given.  An evaluation may opt a script into the contract
        explicitly with ``AGENT_SUBQUESTION_PLANNER=model`` — that is a
        declared choice about who owns the decomposition, not a test branch.
        """
        if self.mode == 'llm':
            return True
        if self.mode == 'scripted':
            return os.getenv('AGENT_SUBQUESTION_PLANNER', '').strip().lower() == 'model'
        return False

    def allowed_entities(self) -> set[str]:
        """Names a sub-question may reference: the authoritative medication
        names, plus the names carried by materials staged for this scope.  A
        planner may not invent a drug.

        材料那一半读的是 ``material_items``——与写入端**同一个形状**。旧写法
        在这里按字典取 ``entry['candidate']['fields']['name']``，而写入端放的
        是字符串 ref，两处都不符，这段于是成了永不生效的死代码：材料独有的
        药名（"维生素D"）从来没能进入规划范围。
        """
        names = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        for detail in self.material_items.values():
            name = (detail or {}).get('name')
            if name:
                names.add(str(name))
        return names

    def _new_claim(self, statement, entities, source, *, question_id=None):
        # 旧契约：id 只由实体集决定——持久化的 claim id 因此跨升级稳定。这条
        # 公式**保留给 evidence_review**（它的身份本来就是"这组药要证据"）。
        # safety_case 走 question_id：同一药物的剂量问题与开始时间问题是两条
        # 不同的证据问题，不能挤进同一个 id。
        identifier = ('claim:' + digest([question_id])[:12]) if question_id else (
            'claim:' + digest(list(entities))[:12])
        if any(claim['claim_id'] == identifier for claim in self.claims):
            return identifier
        self.claims.append({'claim_id': identifier, 'statement': statement, 'entities': list(entities),
            'status': 'insufficient', 'supporting_evidence': [], 'opposing_evidence': [],
            'source_status': 'unknown', 'condition_status': 'unknown', 'source': source,
            'support_status': 'unknown'})
        self.gap(identifier, 'evidence_missing',
                 '核查' + '、'.join(entities) + '的支持和反对证据', claim_id=identifier)
        return identifier

    def apply_default_subquestions(self):
        """The DEGRADED sub-question policy: every drug pair, bounded.

        Used only where no planner can declare sub-questions (deterministic
        mode) or where the planner failed and the loop fell back.  The source
        is recorded so a report can never present this as model reasoning.
        """
        meds = self.facts.get('medications', [])
        if self.claims or not meds:
            return
        names = [m['display_name'] for m in meds]
        pairs = (list(itertools.islice(itertools.combinations(names, 2), MAX_CLAIMS + 1))
                 if len(names) > 1 else [(names[0],)])
        if len(pairs) > MAX_CLAIMS:
            self.gap('coverage_limit', 'evidence_missing',
                     '药物组合超过本轮有界核查范围，剩余组合未检查。')
        for pair in pairs[:MAX_CLAIMS]:
            self._new_claim('、'.join(pair) + '的标签证据', list(pair), 'code_default')
        self.subquestion_source = 'code_default'
        for g in self.gaps:
            if g['gap_id'] == GAP_PLAN:
                g['status'] = 'resolved'

    def search_limit(self) -> int:
        """本轮的检索次数上限：实例 > 环境 > 全局常量。"""
        if self.search_budget:
            return int(self.search_budget)
        raw = os.getenv('AGENT_INVESTIGATION_SEARCH_BUDGET', '').strip()
        try:
            declared = int(raw)
        except ValueError:
            declared = 0
        return declared if declared > 0 else MAX_SEARCHES

    def unsolved_entities(self) -> set[str]:
        """本次调查**尚未解决**的问题所涉及的实体。

        一次修订不得把它们丢掉：那会让 ``checks_completed`` 因为**忘记**一个
        问题而变便宜——把未决项删掉，比把它查清楚省事得多。这是"不得伪造
        完成"的唯一强制点。
        """
        conflict_claims = {g['gap_id'].split(':', 1)[1] for g in self.gaps
                           if g['kind'] == 'evidence_conflict' and g['status'] == 'open'
                           and g['gap_id'].startswith('conflict:')}
        unsolved = set()
        for claim in self.claims:
            if claim.get('source') != 'model':
                continue
            if claim['status'] == 'insufficient' or claim['claim_id'] in conflict_claims:
                unsolved.update(claim.get('entities') or [])
        return unsolved

    def revision_trigger(self) -> str | None:
        """为什么当前子问题集**可以**被声明或修订——绝不是"重置计划"。

        首次规划由 ``GAP_PLAN`` 打开；此后只有**新证据**才重新打开规划：
        新读到的材料差异，或未被任何 claim 覆盖的开放证据缺口。这样"证据变了
        就重新规划"是可达的，而原地重述一遍计划不是。修订次数有界。
        """
        if policy_of(self.policy).get('typed_questions'):
            if not self.questions_settled:
                return 'first_plan'
        elif any(g['gap_id'] == GAP_PLAN and g['status'] == 'open' for g in self.gaps):
            # 旧契约的首次规划阶段由它自己的缺口表达，行为逐字不变。
            return 'first_plan'
        if len(self.plan_revisions) >= MAX_PLAN_REVISIONS:
            return None
        if policy_of(self.policy).get('typed_questions'):
            # 换来源的理由就摆在问题上：来源取不到、试过没结果、用户说不知道。
            # **不要求先找到新证据才能纠正来源选择**。
            for question in self.questions:
                if not question_blocks(question):
                    continue
                # 只有**真的取不到**才是换来源的理由。
                # `received_unconfirmed`（读到了内容、还没分析）不是——那叫
                # "内容待处理"，先处理它（answer_question），别急着换地方查。
                if self.classify_reading(question) == 'insufficient':
                    return 'strategy_needs_change'
        revised_refs = {rev.get('material_ref') for rev in self.plan_revisions}
        if any(g['kind'] == 'material_conflict' and g['status'] == 'open'
               and g.get('material_ref') not in revised_refs for g in self.gaps):
            return 'new_material_evidence'
        claim_ids = {claim['claim_id'] for claim in self.claims}
        if any(g['kind'] in {'evidence_missing', 'evidence_conflict'} and g['status'] == 'open'
               and g.get('claim_id') not in claim_ids for g in self.gaps):
            return 'uncovered_gap'
        return None

    def accept_questions(self, questions) -> list[str]:
        """按本契约的策略采用规划器声明的问题。"""
        if policy_of(self.policy).get('typed_questions'):
            return self._accept_typed_questions(questions)
        return self._accept_review_questions(questions)

    # ---- safety_case：带类型的问题声明 --------------------------------------
    def _accept_typed_questions(self, questions) -> list[str]:
        """模型声明"还需要知道什么、去哪里取"。

        校验的是**结构与边界**：信息目标已知、对象在本作用域真实存在、目标字段
        格式、问句不含处方指令、**取证策略与目标能力匹配**、修订不得丢掉未决问题。

        **不**要求覆盖整个药单，**不**要求逐字复述，**不**把每条问题都变成证据检索。
        """
        if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_CLAIMS:
            return ['invalid_subquestion_count']
        allowed = self.allowed_entities()
        normalised = []
        for item in questions:
            if not isinstance(item, dict):
                return ['invalid_subquestion_statement']
            statement = item.get('statement')
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 200:
                return ['invalid_subquestion_statement']
            if composed_text_prescribes(statement):
                return ['subquestion_prescribes']
            target = item.get('information_target') or item.get('kind')
            strategy = item.get('strategy')
            if target in LEGACY_KIND:            # 旧名字 → 目标 + 默认策略
                legacy_target, legacy_strategy = LEGACY_KIND[target]
                target, strategy = legacy_target, strategy or legacy_strategy
            if target not in INFORMATION_TARGETS:
                return ['unknown_information_target']
            strategy = strategy or DEFAULT_STRATEGY[target]
            if strategy not in STRATEGIES:
                return ['unknown_question_strategy']
            refs = item.get('subject_refs')
            if not isinstance(refs, list) or not refs:
                return ['invalid_subquestion_entities']
            if any(not isinstance(ref, str) or not ref for ref in refs):
                return ['invalid_subquestion_entities']
            # 对象必须**真实存在且在当前作用域**——不再按字符串前缀放行。
            for ref in refs:
                if not self._reference_is_visible(ref, allowed):
                    return ['subquestion_entity_not_in_scope']
            field = item.get('target_field')
            if field is not None and (not isinstance(field, str) or not field.strip()
                                      or len(field) > 60):
                return ['invalid_question_target']
            # 来源能力：一般药品资料说得了"这类药一般怎么用"，说不了"这位用户
            # 实际怎么吃"。不匹配就**具体说明**，由模型改，而不是替它换来源。
            if not strategy_can_serve(target, strategy):
                return ['strategy_cannot_serve_target']
            normalised.append({
                'statement': statement.strip(),
                'information_target': target, 'strategy': strategy,
                'subject_refs': list(refs),
                'target_field': (field or '').strip() or None,
                'why': str(item.get('why') or '').strip()[:200] or None,
                'basis_refs': [str(ref) for ref in (item.get('basis_refs') or [])
                               if isinstance(ref, str)][:8],
                'origin': 'model',
            })
        # 修订不得把仍未解决的问题抹掉。匹配按**身份**（目标×对象×字段），
        # 不看策略——换来源是同一个问题。
        if self.questions:
            incoming = {self._identity_of(item) for item in normalised}
            dropped = {q['question_id'] for q in self.questions
                       if question_blocks(q) and self._identity_of(q) not in incoming}
            if dropped:
                return ['revision_drops_open_problem']
        # 校验通过 → **就地采纳**（生效点只有这一个），并把真实结果留在这里，
        # 由观察阶段回填给模型。校验收下了却不生效，就会出现"工具说成功、
        # 状态其实没变"。
        self.plan_attempts = 0
        self.last_adoption = self.adopt_questions(normalised)
        return []

    def _identity_of(self, item) -> tuple:
        return (item.get('information_target') or item.get('kind') or '',
                tuple(sorted({str(ref) for ref in item.get('subject_refs') or ()})),
                (item.get('target_field') or ''))

    def _reference_is_visible(self, ref, allowed) -> bool:
        """这个对象**真的存在、而且属于当前患者**吗。

        按前缀放行（``memory:`` / ``ev-``）等于承认任何自造的字符串——模型只要
        写一个 ``memory:conclusion:999@v1`` 就能引用并不存在的东西。这里用既有的
        **受作用域约束的解析器**核对。
        """
        if not isinstance(ref, str) or not ref:
            return False
        if ref in allowed:
            return True
        memory = getattr(self, 'memory', None) or getattr(self, '_memory', None)
        evidence = getattr(self, 'evidence_store', None) or getattr(self, '_evidence_store', None)
        if ref.startswith('memory:'):
            if memory is None:
                return False
            try:
                memory.resolve_ref(ref)
            except Exception:
                return False
            return True
        if ref.startswith('ev-'):
            if evidence is None:
                return False
            return evidence.get_meta(ref) is not None
        case = (self.case_context or {}).get('case') or {}
        if ref.startswith('safety-case:'):
            return ref == case.get('case_id')
        if ref.startswith('material:'):
            return ref.split(':', 1)[1] in self.material_refs
        # 材料条目的规范形状是 ``<case_id>/<item_id>``（``list_materials`` 写入、
        # ``read_material_item`` 读取，两端共用）。这个形状没有前缀，所以它既不是
        # ``memory:`` 也不是 ``ev-``——不在这里放行，材料答案会被当成"引用不存在"。
        if ref in self.material_refs:
            return True
        return False

    # ---- 采纳：一次完整的生效点 ---------------------------------------------

    def adopt_questions(self, items) -> dict:
        """把声明的问题并入问题集。**唯一**的采纳生效点。

        返回真实结果：新增了哪些、复用了哪些（含换策略的）、每个 question_id。
        身份由（信息目标×对象×字段）决定：措辞改写、**换取证策略**都不产生新问题，
        旧尝试、回答、证据与未决状态全部保留。
        """
        added, reused, restated = [], [], []
        for item in items:
            identifier = question_id_for(item['information_target'], item['subject_refs'],
                                         item['target_field'])
            existing = self.question(identifier)
            if existing is None:
                # 旧记录的身份公式不同（含 kind）。按身份**匹配已有记录**，
                # 复用它的 question_id——绝不重算，否则前端已有的请求与用户回答
                # 会失去关联。
                existing = self._match_legacy_question(item)
                if existing is not None:
                    identifier = existing['question_id']
            if existing is not None:
                changed = self._restate_question(existing, item)
                (restated if changed else reused).append(identifier)
                continue
            record = {
                'question_id': identifier,
                'information_target': item['information_target'],
                'strategy': item['strategy'],
                'subject_refs': list(item['subject_refs']),
                'target_field': item.get('target_field'),
                'statement': item['statement'], 'why': item.get('why'),
                'basis_refs': list(item.get('basis_refs') or []),
                'status': QUESTION_STATUS_OPEN,
                'information_state': INFO_NOT_ATTEMPTED,
                'origin': item.get('origin') or 'model',
                'dependency_version': dict(self.patient_version or {}),
                'attempts': [], 'strategy_history': [],
            }
            self.questions.append(record)
            self._open_question_gap(record)
            added.append(identifier)
        self.questions_settled = True
        # **首次规划阶段到此结束**：把那个缺口真正关掉，否则 `revision_trigger`
        # 会永远说"还能首次规划"，工具列表也一直放着 `plan_questions`——
        # 模型于是被鼓励把同一份计划再交一遍。
        for gap_item in self.gaps:
            if gap_item['gap_id'] == GAP_PLAN and gap_item.get('kind') == 'plan_missing':
                gap_item['status'] = 'resolved'
            if gap_item.get('kind') == 'plan_missing' and gap_item['gap_id'].startswith('plan:'):
                # 已经被这一次成功修正的规划错误，不再是开放缺口。
                gap_item['status'] = 'resolved'
        return {'added': added, 'reused': reused, 'restated': restated,
                'questions': [self.question_view(q) for q in self.questions]}

    def _match_legacy_question(self, item):
        """按身份匹配一条**旧公式**下创建的问题，用于保留它的 question_id。"""
        wanted = self._identity_of(item)
        for question in self.questions:
            if self._identity_of(question) == wanted:
                return question
        return None

    def _restate_question(self, question, item) -> bool:
        """更新一条已有问题。策略变了就**留痕**，问题不换身份。"""
        changed = False
        question['statement'] = item['statement']
        if item.get('why'):
            question['why'] = item['why']
        if item.get('basis_refs'):
            question['basis_refs'] = list(item['basis_refs'])
        question['information_target'] = item['information_target']
        new_strategy = item['strategy']
        if question.get('strategy') != new_strategy:
            question.setdefault('strategy_history', []).append({
                'from': question.get('strategy'), 'to': new_strategy,
                'reason': item.get('why') or '模型调整取证来源',
                'at_revision': len(self.plan_revisions) + 1})
            question['strategy'] = new_strategy
            # 换了来源，之前那条"这条来源取不到"的结论就不适用了。
            if question.get('information_state') in (INFO_SOURCE_LIMITED,
                                                     INFO_ATTEMPTED_NO_RESULT):
                question['information_state'] = INFO_NOT_ATTEMPTED
            changed = True
        if question.get('status') != QUESTION_STATUS_OPEN and not is_question_answered(question):
            question['status'] = QUESTION_STATUS_OPEN
        self._open_question_gap(question)
        return changed

    def _open_question_gap(self, question) -> None:
        """每条未决问题是一个 open gap，id 就是 question_id。

        既有执行循环要求提案链接到真实存在的开放缺口；这样"模型声明的问题"与
        "能推进它的工具"直接对上，不需要第二套链接规则。
        """
        item = self.gap(question['question_id'], 'question_open', question['statement'],
                        question_id=question['question_id'],
                        question_strategy=question.get('strategy'))
        item['question_strategy'] = question.get('strategy')
        # 只有需要证据支持/反驳的**信息目标**才建 claim：一般参考知识。
        # "这位患者实际怎么服用"不该被变成一次资料检索。
        if question.get('information_target') == TARGET_GENERAL_REFERENCE:
            self._new_claim(question['statement'], list(question['subject_refs']),
                            'model', question_id=question['question_id'])

    def visit_ready_to_deliver(self) -> bool:
        """这次回访是不是已经无事可做——可以复用已有结论直接交付。

        五个条件缺一不可，任何一项不成立都还要继续做：

        * 这**是**一次回访（不是别的调查）；
        * 有仍有效的答案可以复用；
        * 上次之后没有真正的新事件；
        * 没有未决问题；
        * 没有需要重核的依据。

        这是"允许有效答案直接结束当前事实问题"的判据：满足时 `respond` 对模型
        开放，它不必先规划、再搜索、再提问，就可以选择复用并交付。
        """
        visit = (self.case_context or {}).get('visit') or {}
        if not visit:
            return False
        if not visit.get('reusable_answers'):
            return False
        if (visit.get('new_since_last_visit') or {}).get('events'):
            return False
        if visit.get('open_questions'):
            return False
        return not any(item.get('reason') for item in (visit.get('retired_answers') or []))

    def invalidate_answer(self, question_id: str, reason: str) -> bool:
        """一条已有答案不再适用于当前记录：标成 ``stale`` 并重开问题。

        与 `safety_cases.retire_stale_answers` 是**同一件事的两面**——那边重开
        请求，这边让答案自己承认不再适用。只做前一半的话，界面会同时看到
        "这条要重新补充"和"这条的答案仍然可靠"。

        只降 `verified`：``candidate`` 本来就没被当成依据，改它的性质等于
        凭空给它加一次判定。返回是否真的改动了什么，调用方据此决定要不要存回。
        """
        question = self.question(question_id)
        if question is None or not question.get('answers'):
            return False
        changed = False
        for answer in question['answers']:
            assessment = answer.get('assessment') or {}
            if assessment.get('status') == grounding.STATUS_VERIFIED:
                assessment['status'] = grounding.STATUS_STALE
                assessment['reason'] = reason
                answer['assessment'] = assessment
                changed = True
        if question.get('status') == QUESTION_STATUS_ANSWERED:
            self.settle_question(question_id, QUESTION_STATUS_OPEN,
                                 information_state=INFO_RECEIVED_UNCONFIRMED)
            changed = True
        return changed

    def claim_target_field(self, claim_id: str) -> str | None:
        """这条 claim 对应的问题问的是哪个字段。

        支持判定要用它查属性词表：claim 与 question 是同一条问题的两面
        （``claim_id`` 由 ``question_id`` 派生），但字段只记在 question 上。
        """
        for question in self.questions:
            if not question.get('question_id'):
                continue
            if 'claim:' + digest([question['question_id']])[:12] == claim_id:
                return question.get('target_field')
        return None

    def question_view(self, question) -> dict:
        """一条问题给模型/界面看的样子——含**信息状态**与策略历史。"""
        return {
            'question_id': question['question_id'],
            'information_target': question.get('information_target'),
            'strategy': question.get('strategy'),
            'strategy_history': list(question.get('strategy_history') or []),
            'subject_refs': list(question.get('subject_refs') or []),
            'target_field': question.get('target_field'),
            'statement': question.get('statement'),
            'why': question.get('why'),
            'status': question.get('status'),
            'information_state': question.get('information_state'),
            'blocking': question_blocks(question),
            'attempts': len(question.get('attempts') or []),
        }

    def settle_questions_by_default(self) -> None:
        """降级路径：没有规划器时，问题集就此定下（可能是空的）。"""
        self.questions_settled = True
        for gap_item in self.gaps:
            if gap_item['gap_id'] == GAP_PLAN and gap_item.get('kind') == 'plan_missing':
                gap_item['status'] = 'resolved'

    def settle_question(self, question_id, status, *, information_state=None, **details) -> None:
        """问题有了结果：更新它，并关闭它对应的缺口。单一权威在这里。

        ``information_state`` 与 ``status`` 是两件事：状态说流程走到哪，
        信息状态说**到底有没有拿到可用的东西**。
        """
        question = self.question(question_id)
        if question is None:
            return
        question['status'] = status
        if information_state is not None:
            question['information_state'] = information_state
        question.update({k: v for k, v in details.items() if v is not None})
        if not question_blocks(question):
            for gap_item in self.gaps:
                if gap_item['gap_id'] == question_id:
                    gap_item['status'] = 'resolved'

    def question(self, question_id):
        # 旧契约的 questions 是 {'gap_id','field','question'} 投影，没有 question_id。
        # 用 .get 而不是直接取键：读取不该在旧形状上炸掉。
        return next((q for q in self.questions
                     if q.get('question_id') == question_id and question_id), None)

    def open_questions(self, targets=None):
        return [q for q in open_questions(self.questions)
                if targets is None or (q.get('information_target') or q.get('kind')) in targets]

    def blocking_questions(self) -> list:
        return [q for q in self.questions if question_blocks(q)]

    def questions_by_strategy(self, strategy) -> list:
        return [q for q in self.questions if q.get('strategy') == strategy]

    def record_question_attempt(self, question_id, result) -> None:
        item = self.question(question_id)
        if item is None:
            return
        item.setdefault('attempts', []).append(result)
        if result.get('information_state'):
            item['information_state'] = result['information_state']

    def _accept_review_questions(self, questions) -> list[str]:
        """旧契约（evidence_review）的子问题声明——行为逐字不变。

        Returns error codes; empty means accepted.  A rejection NEVER falls
        back to a code-authored substitute — the model either satisfies the
        contract or the degraded policy is used, labelled.  Validation here is
        grounding, not scriptedness: any set of sub-questions is legal as long
        as its entities exist in this scope and the authoritative list is
        covered, so different-but-valid decompositions all pass.
        """
        if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_CLAIMS:
            return ['invalid_subquestion_count']
        allowed = self.allowed_entities()
        normalised = []
        for item in questions:
            if not isinstance(item, dict) or not isinstance(item.get('statement'), str) \
                    or not item['statement'].strip() or len(item['statement']) > 200:
                return ['invalid_subquestion_statement']
            if composed_text_prescribes(item['statement']):
                # A sub-question is a LABEL for evidence to gather, and the
                # report renders it.  Text that diagnoses, prescribes or
                # changes a dose must never enter the review in that role —
                # rejecting it here is the only place that keeps it out of
                # every downstream rendering.
                return ['subquestion_prescribes']
            entities = item.get('entities')
            if not isinstance(entities, list) or not entities \
                    or any(not isinstance(entity, str) or not entity for entity in entities):
                return ['invalid_subquestion_entities']
            if any(entity not in allowed for entity in entities):
                return ['subquestion_entity_not_in_scope']
            normalised.append({'statement': item['statement'].strip(), 'entities': list(entities)})
        covered = {entity for item in normalised for entity in item['entities']}
        required = {str(m['display_name']) for m in self.facts.get('medications', []) if m.get('display_name')}
        if not required.issubset(covered):
            return ['subquestion_coverage_incomplete']
        if self.claims:
            # 允许的修订是**增加**或**改写措辞**，不是抹掉未决项。被拒绝时
            # 什么都不改——否则"拒绝"只是一个返回码。
            dropped = self.unsolved_entities() - covered
            if dropped:
                return ['revision_drops_open_problem']
        trigger = self.revision_trigger()
        before = [claim['claim_id'] for claim in self.claims]
        is_revision = bool(self.claims)
        self.claims = []
        for item in normalised:
            self._new_claim(item['statement'], item['entities'], 'model')
        after = [claim['claim_id'] for claim in self.claims]
        if is_revision:
            # 仍有效的证据：claim_id 由实体集决定，实体集未变的 claim 会拿到
            # 同一个 id，其 assessments 因此继续有效。**显式记录**保留了哪些，
            # 而不是让复用悄悄发生在"id 恰好撞上"里。
            retained = sorted(set(before) & set(after))
            self.plan_revisions.append({
                'revision': len(self.plan_revisions) + 1,
                'trigger': trigger,
                'before': before,
                'after': after,
                'retained': retained,
                'material_ref': next((g.get('material_ref') for g in self.gaps
                                      if g['kind'] == 'material_conflict' and g['status'] == 'open'
                                      and g.get('material_ref') not in
                                      {rev.get('material_ref') for rev in self.plan_revisions}), None),
            })
        if len(self.plan_revisions) >= MAX_PLAN_REVISIONS:
            # 到界时留一条**可读、非失败**的记录：这不是错误，是边界本身。
            self.gap('plan_revision_capped', 'plan_revision_capped',
                     f'本次调查已完成 {MAX_PLAN_REVISIONS} 轮计划修订，此后不再接受新的修订。')
        self.subquestion_source = 'model'
        self.plan_attempts = 0
        for g in self.gaps:
            # 一次成功的声明同时取代 GAP_PLAN 与此前**所有**的 plan:* 错误
            # 缺口。不关掉它们，"已经被修正的错误"会永久挡住 checks_completed
            # ——完成条件要求无任何开放缺口，而这些缺口没有任何工具能关。
            if g['gap_id'] == GAP_PLAN or g['gap_id'].startswith('plan:'):
                g['status'] = 'resolved'
        return []

    def gap(self, identifier, kind, description, **details):
        item = next((g for g in self.gaps if g['gap_id'] == identifier), None)
        if item is None:
            item = {'gap_id': identifier, 'kind': kind, 'description': description, 'status': 'open', **details}
            self.gaps.append(item)
        return item

    def sync_authority(self, memory):
        # 记住解析器：对象引用要按**作用域约束的解析器**核对存在性，而不是看
        # 字符串前缀。这两个属性不是 dataclass 字段，因此不会被序列化。
        self.memory = memory
        snapshot = memory.snapshot()  # Full authoritative set: never relevance-truncated.
        versions = {k: memory.scope_revision(k) for k in ('medications', 'semantic')}
        if self.patient_version and versions != self.patient_version:
            medications_changed = versions.get('medications') != self.patient_version.get('medications')
            if medications_changed:
                # The claim set is derived from the medication list; a changed
                # list invalidates every claim and all collected evidence use.
                self.invalidations.append({'reason': 'medications_changed', 'before': self.patient_version, 'after': versions})
                self.assessments.clear()
                self.claims.clear()
                self.gaps.clear()
                self.read_refs.clear()
                self.read_windows.clear()
                self.evidence_refs.clear()
                self.queries.clear()
                self.search_keys.clear()
            else:
                # Semantic-only change: applicability must be re-verified against
                # the corrected fact, but unchanged material evidence and reads
                # are reused (A2 selective invalidation; conservative on purpose —
                # applicability depends on semantic facts, so they re-check).
                self.invalidations.append({'reason': 'semantic_changed_applicability_recheck', 'before': self.patient_version, 'after': versions})
                self.assessments.clear()
                # Collected evidence and past searches are reused; the bodies
                # must be re-read so applicability is checked against the
                # corrected facts (read_refs cleared, evidence_refs kept).
                self.read_refs.clear()
                self.read_windows.clear()
            self.authority_read = False
            self.no_progress_count = 0
            self.termination_reason = None
        self.patient_version = versions
        self.facts = snapshot
        self.conflicts = snapshot.get('open_conflicts', [])
        self.checks['authority'] = 'checked' if self.authority_read else 'uncovered'
        if not self.authority_read:
            self.gap('authority', 'patient_fact_missing', '读取完整权威药单与关键事实')
            return
        for g in self.gaps:
            if g['gap_id'] == 'authority':
                g['status'] = 'resolved'
        meds = snapshot.get('medications', [])
        typed = policy_of(self.policy).get('typed_questions')
        if not meds:
            self.gap('fact:medication_name', 'patient_fact_missing', '当前权威记忆没有药名，请补充需要核查的药名。', field='medication_name')
        if not typed:
            # 旧契约的一棵**规则树**：目标里出现"剂量/单位/日期"且对应字段缺失，
            # 就固定生成一条追问。它对证据核查契约是有效的兜底，但它替模型做了
            # "该问什么"的决定——于是模型只能复述这些句子。typed 契约下，
            # "还缺什么"由模型声明（问题带类型与目标字段），这里不再代劳。
            for med in meds:
                name = med['display_name']
                if re.search(r'剂量|单位|用量', self.goal) and not re.search(r'mg|μg|ug|g|ml|毫克|克|片|粒|单位|毫升', str(med.get('dose') or ''), re.I):
                    self.gap('fact:dose_unit:' + name, 'patient_fact_missing', f'请补充{name}记录剂量的单位。', field='dose_unit:' + name)
                if re.search(r'日期|何时|什么时候|开始时间', self.goal) and (not med.get('start_at') or med.get('start_at_basis') != 'reported'):
                    self.gap('fact:start_date:' + name, 'patient_fact_missing', f'请补充{name}的开始日期。', field='start_date:' + name)
        # A supplemented fact closes its recorded gap; a stale open gap would
        # otherwise re-ask an already-answered question.
        namespaces = {f['namespace'] for f in self.facts.get('semantic', [])}
        doses = {med['display_name']: str(med.get('dose') or '') for med in meds}
        for g in self.gaps:
            if g['kind'] != 'patient_fact_missing' or g['status'] != 'open' or not g.get('field'):
                continue
            field = g['field']
            if field == 'medication_name':
                resolved = bool(meds)
            elif field.startswith('dose_unit:'):
                resolved = bool(re.search(r'mg|μg|ug|g|ml|毫克|克|片|粒|单位|毫升', doses.get(field.split(':', 1)[1], ''), re.I))
            elif field.startswith('start_date:'):
                resolved = any(m['display_name'] == field.split(':', 1)[1] and m.get('start_at') and m.get('start_at_basis') == 'reported' for m in meds)
            else:
                resolved = field in namespaces
            if resolved:
                g['status'] = 'resolved'
        if typed:
            # typed 契约下这个缺口就是"你还没说要查清什么"。模型声明之后它自动
            # 关闭；声明之前它让 `plan_questions` 有一个合法的落点。
            if not self.questions_settled:
                self.gap(GAP_PLAN, 'plan_missing',
                         '声明这件事还需要查清什么（提出问题的唯一入口）。')
        elif not self.claims and meds:
            if self.subquestions_delegated:
                # Protocol v2: splitting the question into sub-questions is
                # investigation strategy, so it belongs to the planner.  The
                # code only opens the gap and states the contract.
                self.gap(GAP_PLAN, 'plan_missing',
                         '声明本轮要核查的子问题（拆分问题的唯一入口）。')
            else:
                # No planner here can propose sub-questions, so the degraded
                # policy owns them — and says so.
                self.apply_default_subquestions()
        if self.conflicts:
            self.gap('authority_conflict', 'evidence_conflict', '权威记录存在未决冲突；保留两侧，需通过现有审核流程核实。')

    def observe(self, observation, evidence_store):
        self._record_question_attempt(observation)
        if not observation.ok:
            self.gap('failure:' + observation.tool, 'tool_failure', '工具执行失败，已有结果保留。', error_kind=observation.error_kind)
            self.termination_reason = 'unrecoverable_failure'
            return
        value = observation.result if isinstance(observation.result, dict) else {}
        if observation.tool in {'rag_search', 'acquire_evidence'}:
            self.retrieval_attempts += 1
            from .harness.retrieval import FEEDBACK_KEYS
            feedback = {k: value[k] for k in FEEDBACK_KEYS if k in value}
            if feedback:
                self.retrieval_feedback.append(feedback)
                self.retrieval_feedback = self.retrieval_feedback[-12:]
            if value.get('status') == 'retrieval_error' and self.mode == 'deterministic':
                self.termination_reason = 'unrecoverable_failure'
            # A validation response is information, not an executed search.
            # Its identical repetition is still caught by NoProgressTracker.
            if value.get('search_executed') is False:
                return
        if observation.tool == 'memory_read' and observation.arguments.get('query') == 'snapshot':
            self.authority_read = True
        if observation.tool == 'ask_clarification':
            self.termination_reason = 'waiting_input'
        if observation.tool == 'list_materials':
            # Enumerate what this run may subsequently read.  Listing is not
            # reading: these refs only become citations once read back.
            for material in (observation.result or {}).get('materials', []) or []:
                for item in material.get('items', []) or []:
                    ref = f"{material.get('case_id')}/{item.get('item_id')}"
                    if ref not in self.material_refs:
                        self.material_refs.append(ref)
                    # 唯一规范形状，写入端与读取端共用：药名供
                    # ``allowed_entities``，``current`` 供差异的双方具名。
                    self.material_items[ref] = {
                        'name': (item.get('fields') or {}).get('name'),
                        'kind': item.get('kind'),
                        'current': list(item.get('current') or []),
                    }
            return
        if observation.tool == 'read_material_item':
            # A material item read back in this run; the ONLY way a material
            # entry can support a report citation (the index alone cannot).
            ref = f"{observation.arguments.get('case_id')}/{observation.arguments.get('item_id')}"
            if ref not in self.material_read_refs:
                self.material_read_refs.append(ref)
            detail = observation.result if isinstance(observation.result, dict) else {}
            if isinstance(detail.get('fields'), dict):
                # 回读到的字段值留下来：材料答案的支持关系要按**这条记录真的记了
                # 什么**核对，而不是按"条目存在"或索引里的差异标题。列表阶段不写
                # 这个键——列过索引 != 读过原文，两个清单一直是分开的。
                self.material_items.setdefault(ref, {})['fields'] = dict(detail['fields'])
            kind = detail.get('kind')
            if kind and kind != 'same':
                # A difference between a material and the authoritative record
                # that the planner actually READ.  Recorded as a finding, not a
                # stop: a discrepancy is something to report, not something
                # that ends the review (that is what evidence_conflict is for).
                issues = [str(item) for item in (detail.get('issues') or [])]
                # 差异必须**具名双方**：只写"材料 X 有差异"，读者无法去核对
                # 另一边，质检也就只能退回标题匹配。对方 ref 取自
                # list_materials 时记下的同一条目（material_items）。
                counterparts = list((self.material_items.get(ref) or {}).get('current') or [])
                self.gap('material:' + ref, 'material_conflict',
                         f"材料 {ref} 与当前记录"
                         + ('（' + '、'.join(counterparts) + '）' if counterparts else '')
                         + f"的差异：{kind}"
                         + ('；未决问题：' + '、'.join(issues) if issues else ''),
                         material_ref=ref, kind_detail=kind, counterparts=counterparts)
            return
        if observation.tool == 'plan_questions':
            # The sub-question set is adopted by the STATE, from the observed
            # arguments — the executor only echoes what it saw.  A rejected set
            # is recorded as a gap the planner can see and revise against; it
            # is never silently replaced by a code-authored substitute.  Only a
            # planner that keeps failing is stopped, so one badly worded
            # statement does not end an otherwise valid review.
            errors = self.accept_questions(observation.arguments.get('questions'))
            if errors:
                self.plan_attempts += 1
                self.gap('plan:' + ','.join(errors), 'plan_missing',
                         '问题声明未通过校验（' + ','.join(errors) + '），请修订后重新提交。')
                if self.plan_attempts >= MAX_PLAN_ATTEMPTS:
                    self.termination_reason = 'no_progress'
                # 让模型**看见**被拒了什么、为什么。工具的返回值不能是"已接受"。
                observation.result = {
                    'adopted': False, 'errors': errors,
                    'submitted': observation.arguments.get('questions'),
                    'questions': [self.question_view(q) for q in self.questions],
                    'allowed_tools': list(allowed_tools(self)),
                }
                return
            # 采纳已在 `_accept_typed_questions` 里就地完成（唯一生效点）。
            # 这里只把**真实结果**写回这条观察：采纳了哪些、question_id 是什么、
            # 复用/换策略了哪些。模型下一步读到的就是它——不必再调一次
            # plan_questions 去问"我刚生成的问题 ID 是什么"。
            observation.result = {
                'adopted': True,
                **(self.last_adoption or {'added': [], 'reused': [], 'restated': [],
                                          'questions': [self.question_view(q)
                                                        for q in self.questions]}),
                'allowed_tools': list(allowed_tools(self)),
                'revision_trigger': self.revision_trigger(),
            }
            return
        if observation.tool == 'answer_question':
            # 采纳在**这里**发生（唯一生效点），结果回填给模型：采纳了没有、
            # 依据的性质、这条问题还剩什么没确定。工具本身只回显。
            args = observation.arguments or {}
            outcome = self.answer_question(
                str(args.get('question_id') or ''), source=str(args.get('source') or ''),
                value=args.get('value'), field=args.get('field'), quote=args.get('quote'),
                source_ref=args.get('source_ref'), object_ref=args.get('object_ref'),
                basis_refs=args.get('basis_refs') or ())
            observation.result = {**outcome, 'allowed_tools': list(allowed_tools(self))}
            return
        if observation.tool == 'propose_medication_change':
            # 记成**候选**，不是记录。写进调查状态（本模块不认识数据库），
            # 任务收尾时由执行器登记进这次回访，由用户确认。
            args = observation.arguments or {}
            candidate = {'question_id': str(args.get('question_id') or ''),
                         'name': str(args.get('name') or '').strip(),
                         'field': str(args.get('field') or ''),
                         'value': str(args.get('value') or '').strip(),
                         'quote': str(args.get('quote') or '').strip()[:300] or None}
            existing = next((item for item in self.pending_change_candidates
                             if item['name'] == candidate['name']
                             and item['field'] == candidate['field']), None)
            if existing is not None:
                # 同一味药同一字段只留一条：两条并存，用户确认一条之后另一条
                # 就和记录对不上了。
                existing.update(candidate)
                candidate = existing
            else:
                self.pending_change_candidates.append(candidate)
            observation.result = {'recorded': True, 'candidate': dict(candidate),
                                  'detail': '已记为待确认的变更候选；改不改由用户决定，'
                                            '这一步没有改动任何记录'}
            return
        if observation.tool == 'acquire_evidence':
            # Reuse exactly the existing acquisition and assessment rules.
            # These are internal deterministic operations, never extra model actions.
            value = observation.result or {}
            before = (len(self.content_hashes), len(self.read_refs))
            if value.get('search_executed', value.get('search_ok')):
                attempts = self.retrieval_attempts
                self.observe(replace(observation, tool='rag_search'), evidence_store)
                self.retrieval_attempts = attempts
                if feedback:
                    self.retrieval_feedback.pop()  # inner search is not a model attempt
            for page in value.get('pages') or []:
                self.observe(replace(observation, tool='read_evidence',
                    arguments={'evidence_id': page['evidence_id'], 'offset': page['offset'], 'limit': 2000},
                    result=page), evidence_store)
            observation.added_information = before != (len(self.content_hashes), len(self.read_refs))
            return
        if observation.tool in {'rag_search', 'ddi_check'}:
            query = str(observation.arguments.get('query', '')).strip().casefold()
            key = json.dumps([observation.arguments, value.get('corpus_version')], sort_keys=True, ensure_ascii=False)
            repeated = key in self.search_keys
            if observation.tool == 'rag_search':
                self.queries.append(query)
                self.search_keys.append(key)
            added = 0
            for ref in observation.evidence_refs:
                # First read enforces scope and hash before metadata influences progress.
                try:
                    page = evidence_store.read(ref, scope_id=self.scope_id, limit=1)
                    content_hash = page['content_hash']
                except Exception:
                    self.gap('source:' + ref, 'source_invalid', '证据来源不可回读或完整性失效。', evidence_ref=ref)
                    continue
                if ref not in self.evidence_refs and len(self.evidence_refs) < MAX_EVIDENCE:
                    self.evidence_refs.append(ref)
                if content_hash not in self.content_hashes:
                    self.content_hashes.append(content_hash)
                    added += 1
            # 计数保留，但**不再在这里终止**。旧规则是"两次检索、第二次没带来
            # 新内容就停"，它有两个毛病：只认 rag_search（连续 5 次 list_materials
            # 一次都不算），而且停得比反馈早——真实批次里模型刚进入取证这一步，
            # 第二次改写过的检索换来的是当场终止，它永远看不到"这一步没有新增"
            # 的反馈，也就没有机会改正。重复检测现在统一由循环的 NoProgressTracker
            # 负责：先反馈，达到上限才停，且对所有工具一视同仁。检索次数仍由
            # ``search_limit()`` 约束（``forced_stop``），所以这里的上界没有丢。
            self.no_progress_count = self.no_progress_count + 1 if repeated or not added else 0
            # 但"这次检索有没有带回新内容"必须原样交给循环：换一种说法再搜一次
            # （query 不同 → 签名不同）却拿回同一批证据，正是"改变无关参数但没有
            # 新增信息"。只按签名判，这一步会被记成进展。
            if observation.tool == 'rag_search':
                observation.added_information = bool(added)
        if observation.tool == 'read_evidence':
            ref = observation.arguments.get('evidence_id')
            if ref not in self.evidence_refs:
                return  # Arbitrary tool-visible references cannot join this investigation.
            # Re-read using the same authorised store; observations never author authority.
            # **按模型声明的窗口重读**，与执行器交给它的那一页逐字相同：回执要记的
            # 就是它真的看到的那一段。旧写法固定读 offset=0/limit=2000，模型读的是
            # 后 1000 字时，回执里却多出 2000 字它没看过的内容——那不是回执，是补读。
            page = evidence_store.read(ref, scope_id=self.scope_id,
                                       offset=observation.arguments.get('offset', 0),
                                       limit=observation.arguments.get('limit', self.READ_BACK_CHARS))
            self._record_read(ref, page.get('offset', 0), page.get('content', ''))
            meta = evidence_store.get_meta(ref) or {}
            namespaces = {f['namespace'] for f in self.facts.get('semantic', [])}
            for claim in self.claims:
                text = page['content']
                # Applicability v1 (lexical, conservative): when the chunk
                # states a population/condition, the claim stays insufficient
                # until a matching patient fact is RECORDED in authoritative
                # memory; the check then runs against that recorded context.
                # A model can never assert this — only the versioned fact store.
                required_subject = None
                conditional_namespaces = set()
                for word, namespace in [('肾功能', 'renal_function'), ('肝功能', 'hepatic_function'),
                                        ('儿童', 'age'), ('孕妇', 'pregnancy'), ('妊娠', 'pregnancy')]:
                    if word in text:
                        conditional_namespaces.add(namespace)
                # A recorded number/status alone does not prove the label's
                # population condition applies. Keep lexical screening conservative;
                # no clinical thresholds or inferred patient eligibility here.
                assessment = assess_claim(quote=text, text=text, entities=claim['entities'], evidence_id=ref,
                    subject=required_subject, required_subject=required_subject,
                    source_status='current' if meta.get('corpus_version') else 'unknown',
                    conditions_known=True, evidence_date=meta.get('retrieved_at'),
                    content_complete=not page.get('truncated'))
                # claim-support@1 叠加在既有判定之上，写在**独立键**里，不覆盖
                # ``status``：历史 assessments 缺这个键时按 not_applicable 恢复，
                # 不会因为口径升级而集体失效或自相矛盾。
                support = assess_support(statement=claim['statement'], quote=text,
                                         entities=claim['entities'],
                                         material_item=self.material_items.get(ref),
                                         target_field=self.claim_target_field(claim['claim_id']))
                assessment['support_status'] = support['status']
                assessment['support_scope'] = support['scope']
                assessment['support_reasons'] = support['reasons']
                if support['status'] == 'supported_by_span':
                    # 记下**命中支持的那段文字本身**。只记状态不记片段，正是
                    # "问题读成已有依据、却说不出是什么让它变成这样"的成因——
                    # settle 时要拿它写答案元素。
                    claim.setdefault('support_spans', {})[ref] = text[:600]
                self.assessments.setdefault(claim['claim_id'], {})[ref] = assessment
                if conditional_namespaces and conditional_namespaces.issubset(namespaces):
                    assessment['unresolved'].append('recorded_context_does_not_verify_applicability')
                if 'unstated_population_or_condition' in assessment['unresolved']:
                    for word, namespace in [('肾功能', 'renal_function'), ('肝功能', 'hepatic_function'),
                                            ('儿童', 'age'), ('孕妇', 'pregnancy'), ('妊娠', 'pregnancy')]:
                        if word in text and namespace not in namespaces:
                            self.gap('fact:' + namespace, 'patient_fact_missing', '请补充与材料适用条件相关的' + word + '记录；这不是临床审批。', field=namespace)
                    self.gap('condition:' + claim['claim_id'], 'evidence_missing', '材料的适用条件尚未核实，保留 insufficient。')
            self._assess()

    def _assess(self):
        for claim in self.claims:
            assessments = self.assessments.get(claim['claim_id'], {})
            support = [ref for ref, a in assessments.items() if a['status'] == 'supported']
            opposing = [ref for ref, a in assessments.items() if a['status'] == 'contradicted']
            claim.update(supporting_evidence=support, opposing_evidence=opposing,
                source_status='current' if assessments and all(a['source_status'] == 'current' for a in assessments.values()) else 'unknown',
                condition_status='verified' if (support or opposing) else 'unknown')
            claim['status'] = 'insufficient' if support and opposing else 'supported' if support else 'contradicted' if opposing else 'insufficient'
            # claim-support@1 的 claim 级汇总。"有支持证据"与"支持证据真的
            # 覆盖了这句断言"是两件事：前者是既有语义，后者决定这句话能不能
            # 以**结论**的身份出现在报告第 2 节。
            spans = [assessments[ref].get('support_status') for ref in support]
            if not support:
                claim['support_status'] = 'unknown'
            elif all(span is None for span in spans):
                # 旧记录：本 scope 之前采集的 assessment。按"不可判定"恢复，
                # 不当作"不支持"——口径升级不该追溯否定历史结论。
                claim['support_status'] = 'not_applicable'
            elif any(span == 'supported_by_span' for span in spans):
                claim['support_status'] = 'supported_by_span'
            else:
                claim['support_status'] = 'no_supporting_span'
            if support and opposing:
                self.gap('conflict:' + claim['claim_id'], 'evidence_conflict', '支持与反对证据并存，不能以投票或用户选边消除。', evidence_refs=support + opposing)
            for g in self.gaps:
                if g['gap_id'] == claim['claim_id']:
                    g['status'] = 'resolved' if claim['status'] != 'insufficient' else 'open'
            # claim 与 question 是同一条问题的两面（claim_id 由 question_id 派生）。
            # 证据支持到位时，**问题同步**得到它的依据——否则"这一条查到了"
            # 只活在 claims 里，问题永远停在未决，模型只好再查一遍。
            self._sync_question_from_claim(claim)
            cond_gap = next((g for g in self.gaps if g['gap_id'] == 'condition:' + claim['claim_id']), None)
            if cond_gap and claim['condition_status'] == 'verified':
                cond_gap['status'] = 'resolved'
        self.checks['interaction_evidence'] = 'checked' if self.claims and all(c['status'] != 'insufficient' for c in self.claims) else 'uncovered'
        self.checks['applicability'] = 'checked' if self.claims and all(c['condition_status'] == 'verified' for c in self.claims) else 'uncovered'

    # ---- 职责四分（协议 v2）------------------------------------------------
    # forced_stop()              纯状态检查 + 强制停止条件   —— 执行约束，不可协商
    # model_policy()             由 LLMPlanner 执行          —— 本类不实现
    # degraded_next_action()     确定性降级策略（含固定搜索词）
    # observe()/sync_authority() 事实读取与状态同步          —— 基础设施

    def forced_stop(self) -> str | None:
        """Non-negotiable stop conditions only.

        Returns and sets ``termination_reason``, and produces NO action — so it
        can never pre-plan.  These are the conditions a model must not be able
        to argue past: an already-set termination, an unresolved evidence
        conflict (code must not vote, and must not let the user pick a side), a
        conflict-free completion, and an exhausted search budget.  A repeated
        read with no new information is terminated inside ``observe``.
        """
        if self.termination_reason:
            return self.termination_reason
        policy = policy_of(self.policy)
        if policy.get('typed_questions'):
            return self._forced_stop_typed()
        if any(g['kind'] == 'evidence_conflict' and g['status'] == 'open' for g in self.gaps):
            self.termination_reason = 'waiting_review'
        elif (self.claims and all(v == 'checked' for v in self.checks.values())
              and not any(g['status'] == 'open' and g['kind'] not in FINDING_GAPS
                          for g in self.gaps)
              and not self.unread_evidence()):
            # A captured body that was never read back can carry the OPPOSING
            # source.  Declaring completion with one outstanding would make
            # coverage cheaper by skipping the read-back the evidence contract
            # rests on, and would hide a disagreement behind "completed".
            # Retrieval is not verification; this is the same rule the label
            # path already states as "'搜到' 不等于 '已读取并验证'".
            self.termination_reason = 'checks_completed'
        elif len(self.queries) >= self.search_limit() and not self.unread_evidence():
            self.termination_reason = 'budget_insufficient'
        return self.termination_reason

    def _degraded_typed_action(self, action):
        """no-planner 时的降级动作：**按问题类型**挑选能推进它的工具。

        降级路径只做"这一类问题通常怎么推进"，且如实标注来源。它不替模型
        决定问什么——问题集本来就是模型声明的（或为空）。没有任何问题可推进
        时返回 None，让调用方按实际原因收尾。
        """
        if not self.authority_read:
            return action('memory_read', 'authority', {'query': 'snapshot'},
                          '获得完整当前事实及版本')
        pending = [q for q in self.blocking_questions()
                   if q.get('information_state') == INFO_NOT_ATTEMPTED]
        for question in pending:
            qid = question['question_id']
            if question.get('strategy') == STRATEGY_ASK_USER:
                return action('ask_clarification', qid,
                              {'question': question['statement'], 'question_id': qid},
                              '等待用户补充；未写入临床审批')
            if question.get('strategy') == STRATEGY_PATIENT_RECORD:
                return action('memory_read', qid, {'query': 'snapshot'},
                              '读取已有患者记录以回答这条问题')
            if question.get('strategy') == STRATEGY_PATIENT_MATERIAL:
                unread = [ref for ref in self.material_refs if ref not in self.material_read_refs]
                if unread:
                    case_id, item_id = unread[0].split('/', 1)
                    return action('read_material_item', qid,
                                  {'case_id': case_id, 'item_id': item_id},
                                  '读取材料条目以核实这条问题')
                continue
            if question.get('strategy') == STRATEGY_GENERAL_REFERENCE:
                if len(self.queries) >= self.search_limit():
                    continue
                terms = ' '.join(str(ref) for ref in question['subject_refs']) or self.goal[:150]
                return action('rag_search', qid, {'query': terms, 'top_k': 5},
                              '检索权威资料以回答这条问题')
        for ref in self.evidence_refs:
            if ref not in self.read_refs:
                return action('read_evidence', 'authority',
                              {'evidence_id': ref, 'offset': 0, 'limit': 2000},
                              '回读已检索证据以核实支持关系')
        return None

    def _record_question_attempt(self, observation) -> None:
        """把一次动作对应到它要推进的**那条问题**上。

        ``gap_id`` 就是 question_id（见 `_open_question_gap`）。记录的是**实际
        发生了什么**：有没有执行、有没有找到东西。终止原因据此区分"没试过"、
        "试过没结果"与"已经拿到"——而不是一律写成"资料不可得"。
        """
        question = self.question(getattr(observation, 'gap_id', None) or '')
        if question is None:
            return
        value = observation.result if isinstance(observation.result, dict) else {}
        found = None
        if observation.tool in ('rag_search', 'acquire_evidence'):
            found = bool(value.get('results'))
        elif observation.tool == 'read_evidence':
            found = bool(value.get('content'))
        elif observation.tool == 'read_material_item':
            found = bool(value)
        elif observation.tool == 'memory_read':
            found = bool(value)
        attempt = {'tool': observation.tool, 'ok': bool(observation.ok),
                   'found_information': found}
        if not observation.ok:
            attempt['information_state'] = INFO_SOURCE_LIMITED
        elif found:
            # 找到了材料**不等于**这条问题已经有答案——那要由采纳/回答流程确认。
            attempt['information_state'] = INFO_RECEIVED_UNCONFIRMED
        elif found is False:
            attempt['information_state'] = INFO_ATTEMPTED_NO_RESULT
        self.record_question_attempt(question['question_id'], attempt)

    @staticmethod
    def source_exhausted(question) -> bool:
        """这条问题的来源是不是**确实取不到**。

        一个规则只写一处：来源被标成受限，或者**尝试过**但没拿到任何信息。
        "还没试过"与"读到了内容但没答上"都不算——它们不是"资料不存在"。
        """
        if question.get('information_state') == INFO_SOURCE_LIMITED:
            return True
        attempts = question.get('attempts') or []
        return bool(attempts) and not any(a.get('found_information') for a in attempts)

    # ---- 答案采纳：把"读到的内容"变成"某条问题的答案" ------------------------
    #
    # 这一轮之前，读取成功只会把问题标成 `received_unconfirmed`，然后**没有
    # 下一步**：既没有代码把内容转成答案，`revision_trigger` 还会立刻说
    # "换个来源吧"。于是"读到了，但无法采纳"——模型只能重复规划。
    #
    # 这里补上那条生产路径。它**不是**一个"把问题设成已回答"的通用开关：
    # 采纳要按来源、对象、版本与支持关系逐条校验，校验不过就如实说明差什么。

    # ---- 答案可信性：真实记录、回读回执、判定内核 ---------------------------
    #
    # 判定本身在 `answer_grounding` 里（纯函数、可独立测试）。这里只负责把
    # **本模块掌握的真实记录**接进去：权威快照里的版本化记录、本 run 的回读
    # 回执、作用域可见性。模块不替它下结论，也不替它补默认值。

    #: 与 harness.evidence.MAX_READ_LIMIT 同一口径：证据回读一次的上限。
    #: 本模块不 import harness（避免环），所以这里自己声明一个同样的值。
    READ_BACK_CHARS = 2000

    #: 问题字段 → ``medications`` 表上的列。答案里的字段名与列名不是一回事。
    RECORD_COLUMNS = {
        'dose': 'dose', 'schedule': 'schedule', 'route': 'route',
        'start_date': 'start_at', 'start_at': 'start_at',
        'end_date': 'end_at', 'end_at': 'end_at',
        'name': 'display_name', 'display_name': 'display_name',
        'status': 'status', 'version': 'version',
    }

    def _record_facts(self, object_ref, field):
        """``object_ref`` 这条**真实记录**里 ``field`` 的事实。

        先看权威快照（``facts['medications']``——只含**当前有效**的用药，每条带
        自己的版本化 ``ref``），再看 ``medications`` 表里那条 id 行。

        两者是**互补**的，不是重复：快照保证"这是现在的用药"，表行保证"这条记录
        本身怎么说"。状态与版本**记录声明了才核对**（真实表的这两列都是 NOT NULL，
        所以生产路径上一定核得到；只回值的最小替身没有列，判定会在 reason 里
        写明"未做该部分核对"，而不是假装核过）。
        """
        memory = getattr(self, 'memory', None)
        if not object_ref or not field:
            return None
        column = self.RECORD_COLUMNS.get(str(field))
        if column is None:
            return None
        row = self._snapshot_record(object_ref)
        current = True
        if row is None:
            if memory is None:
                return None
            row = self._table_record(object_ref)
            # 快照里**带版本化引用**（生产路径一定如此）而这条不在其中：它已经不是
            # 当前的用药了。历史版本按 id 照样取得出来，所以"取得到"不等于
            # "还是现在的值"——这一步正是把这两件事分开的地方。
            current = not self._snapshot_refs() or str(object_ref) in self._snapshot_refs()
        if row is None:
            return None
        value = _row_field(row, column)
        if value in (None, ''):
            return None
        return grounding.RecordFacts(
            value=value,
            unit=_unit_of(value),
            status=_row_field(row, 'status'),
            version=_row_field(row, 'version'),
            locator=f'{object_ref}#{field}',
            current=current)

    def _snapshot_refs(self) -> set:
        """权威快照里**带版本化引用**的那些用药。空集 = 这份快照不声明引用。"""
        return {str(item['ref']) for item in (self.facts.get('medications') or ())
                if isinstance(item, dict) and item.get('ref')}

    def _snapshot_record(self, object_ref):
        """权威快照里 ``object_ref`` 那条记录；没有就 None。"""
        for item in self.facts.get('medications') or ():
            if str((item or {}).get('ref') or '') == str(object_ref):
                return item
        return None

    def _table_record(self, object_ref):
        """按 id 读 ``medications`` 那一行。引用里的版本与行上的版本要一致。

        版本一致性由既有的严格解析器判（``memory.resolve_ref`` 对不上就抛），
        所以这里只负责把行取出来。旧版本 / 别的对象因此在这里就过不去。
        """
        memory = getattr(self, 'memory', None)
        match = re.fullmatch(r'memory:medication:(\d+)@v(\d+)', str(object_ref or ''))
        if memory is None or not match:
            return None
        row = memory.connection.execute(
            'SELECT * FROM medications WHERE id=?', (int(match.group(1)),)).fetchone()
        if row is None:
            return None
        stated = _row_field(row, 'version')
        if stated is not None and int(stated) != int(match.group(2)):
            return None
        return row

    def _material_facts(self, material_ref, field):
        """本 run **回读过的**材料条目里 ``field`` 的事实。

        只列过索引不算读到（``material_refs`` 与 ``material_read_refs`` 是两个
        清单），所以这里只认后者。
        """
        if material_ref not in self.material_read_refs:
            return None
        detail = self.material_items.get(material_ref) or {}
        fields = detail.get('fields') or {}
        if field and field in fields:
            value = fields.get(field)
        else:
            value = fields.get('name')
        if value in (None, ''):
            return None
        return grounding.RecordFacts(
            value=value, unit=_unit_of(value),
            locator=f'{material_ref}#{field or "name"}')

    def _read_ledger(self):
        """本 run（含跨会话复用）的回读回执。

        ``read_windows`` 是**正文**回执，核对的是模型当时真的看到的那一段，不是
        校验时重新翻开原文。旧状态只有 ``read_refs`` 名单、没有窗口：那种记录
        按旧口径恢复成"整篇读过"，否则滚动升级前存下的调查会集体失去引用能力。
        """
        ledger = grounding.ReadLedger.from_list(self.read_windows)
        legacy = [ref for ref in self.read_refs if not ledger.has_ref(ref)]
        if legacy:
            store = getattr(self, 'evidence_store', None)
            if store is not None:
                def content_of(ref):
                    try:
                        return store.read(ref, scope_id=self.scope_id, offset=0,
                                          limit=self.READ_BACK_CHARS)['content']
                    except Exception:
                        return None
                ledger.merge(grounding.ReadLedger.legacy(legacy, content_of))
        return ledger

    def _record_read(self, ref, offset, content) -> None:
        if ref not in self.read_refs:
            self.read_refs.append(ref)
        entry = {'ref': str(ref), 'offset': int(offset or 0), 'content': str(content or '')}
        if entry not in self.read_windows:
            self.read_windows.append(entry)

    def _grounding_context(self) -> grounding.GroundingContext:
        return grounding.GroundingContext(
            ledger=self._read_ledger(),
            lookup_record=self._record_facts,
            visible_ref=lambda ref: self._reference_is_visible(ref, self.allowed_entities()),
            lookup_material=self._material_facts,
            # 本项目**没有**连接真实医护服务，所以这个口永远解析不出真实来源。
            # 保留它而不是删掉：将来接上真实服务时，改的是这一处注入，判定逻辑
            # 不用动——"没有真实来源就不算专业确认"这条规则本身是常驻的。
            lookup_professional=lambda ref: None)

    def question_objects(self, question) -> list:
        """这条问题涉及的**对象**（去重、保序）。

        直接用 ``subject_refs``：它已经是"这条问题针对谁"的唯一声明处，另立一套
        只会让两者漂移。规范化后比较，避免同一对象因大小写/空白被算成两个。
        """
        seen, objects = set(), []
        for ref in question.get('subject_refs') or ():
            key = _normalise_answer(ref)
            if key and key not in seen:
                seen.add(key)
                objects.append(str(ref))
        return objects

    def _grounding(self, question, *, source, value, quote, source_ref, field,
                   object_ref=None) -> grounding.Grounding:
        return grounding.assess(
            declared_source=source, value=value, quote=quote, source_ref=source_ref,
            target_field=field or question.get('target_field'),
            object_ref=object_ref, question_objects=self.question_objects(question),
            ctx=self._grounding_context())

    def answer_question(self, question_id, *, source, value, field=None, quote=None,
                        source_ref=None, origin='model', answer_ref=None,
                        basis_refs=(), object_ref=None) -> dict:
        """把一条候选答案提交给服务端校验；**核对通过才算已有依据**。

        返回真实结果：记下了没有、可信到什么程度、这条问题还剩什么没确定。

        ``source`` 是**模型的说法**，不是事实：来源种类由真实记录解析（见
        ``answer_grounding.resolve_source``），解析不出真实来源的提交不会被记录。
        校验不过时问题保持未决并写明原因——不提供任何"直接置为已回答"的开关。
        """
        question = self.question(question_id)
        if question is None:
            return {'accepted': False, 'errors': ['unknown_question_id'],
                    'detail': f'没有这条问题：{question_id}'}
        if is_question_answered(question):
            # 重复提交幂等：已有适用答案就不再改写，也不重复计入进展。
            return {'accepted': False, 'errors': ['question_already_answered'],
                    'question': self.question_view(question),
                    'detail': '这条问题已经有适用答案，重复提交不产生新的采纳'}
        if source not in ANSWER_SOURCES:
            return {'accepted': False, 'errors': ['unknown_answer_source'],
                    'question': self.question_view(question),
                    'detail': f'来源种类只能是 {list(ANSWER_SOURCES)}'}
        if value is None or not str(value).strip():
            return {'accepted': False, 'errors': ['empty_answer'],
                    'question': self.question_view(question),
                    'detail': '空值不是答案'}
        answered_field = field or question.get('target_field')
        verdict = self._grounding(question, source=source, value=value, quote=quote,
                                  source_ref=source_ref, field=answered_field,
                                  object_ref=object_ref)
        if not verdict.accepted:
            # **来源存在但不支持答案**（或根本没有真实来源）：保持未决，如实记下
            # 差什么。这里**不写**答案元素——写下去就等于承认了一个不成立的来源。
            self.record_question_attempt(question_id, {
                'tool': 'answer_question', 'ok': True, 'found_information': False,
                'rejected': verdict.errors[0], 'information_state': INFO_ATTEMPTED_NO_RESULT})
            return {'accepted': False, 'errors': list(verdict.errors),
                    'detail': verdict.detail, 'stage': verdict.stage,
                    # 解析到哪一类来源、卡在哪一步，一起给出来：模型据此才能改对。
                    'source_kind': verdict.kind,
                    'question': self.question_view(question)}
        # 这条答案针对的对象：模型指明了就用它，没指且问题只有一个对象就是它。
        objects = self.question_objects(question)
        answer_object = object_ref or (source_ref if source_ref in objects else None) \
            or (objects[0] if len(objects) == 1 else None)
        assessment = verdict.assessment
        answer = {
            'value': str(value).strip(), 'field': answered_field,
            # 来源种类取**解析出来的**那个（``verdict.kind``），不是模型自报的
            # 字符串。两者不等时说明模型报错了来源身份——那种提交根本到不了这里。
            'source': verdict.kind, 'provenance': SOURCE_PROVENANCE[verdict.kind],
            'source_ref': source_ref, 'quote': quote, 'origin': origin,
            'answer_ref': answer_ref, 'at': utcnow_iso(),
            'version': dict(self.patient_version or {}),
            'object_ref': answer_object,
            'assessment': assessment,
        }
        remaining = self._unanswered_parts(question, answered_field, answer_object,
                                           provisional=answer)
        answer['still_uncertain'] = list(remaining)
        if self._same_answer_recorded(question, answer):
            # 同一条答案重复提交：不改写历史，也不重复计入进展。
            return {'accepted': False, 'errors': ['answer_already_recorded'],
                    'detail': '这条答案已经记过，重复提交不产生新的记录',
                    'assessment': assessment, 'question': self.question_view(question)}
        question['answers'] = [*(question.get('answers') or []), answer]
        question['answer_ref'] = answer_ref or source_ref
        # **只有核对通过的答案才算"已有依据"。** 依据未核对到通过的（候选 / 无依据）
        # 如实保留在答案历史里，问题继续未决——这正是"被标为已有依据的答案，
        # 必须关联到真实、适用且支持该答案的来源"这一条硬约束的落点。
        if not remaining and assessment['status'] == grounding.STATUS_VERIFIED:
            self.settle_question(question_id, QUESTION_STATUS_ANSWERED,
                                 information_state=INFO_AVAILABLE,
                                 answered_at=utcnow_iso())
            if question.get('information_target') == TARGET_GENERAL_REFERENCE:
                self._mark_claim_supported(question_id, source_ref)
        else:
            # 保留了已知部分，把剩余缺口写清楚，问题保持未决。
            self.settle_question(question_id, QUESTION_STATUS_OPEN,
                                 information_state=INFO_RECEIVED_UNCONFIRMED)
            for part in [p for p in remaining if p not in ('', None)]:
                self.gap(f"answer_missing:{question_id}:{part}", 'question_open',
                         f'{question["statement"]}：还缺 {part}',
                         question_id=question_id, missing_field=part)
        return {'accepted': True, 'partial': bool(remaining),
                'answered': [] if remaining else [question.get('target_field')],
                'still_open': list(remaining),
                'assessment': assessment, 'assessment_status': assessment['status'],
                'provenance': answer['provenance'], 'detail': verdict.detail,
                'question': self.question_view(question)}

    @staticmethod
    def _same_answer_recorded(question, answer) -> bool:
        """同一条答案是不是已经记过（同一对象、同一字段、同一值、同一来源）。"""
        for existing in question.get('answers') or ():
            if (_normalise_answer(existing.get('value')) == _normalise_answer(answer.get('value'))
                    and existing.get('field') == answer.get('field')
                    and existing.get('object_ref') == answer.get('object_ref')
                    and existing.get('source') == answer.get('source')
                    and existing.get('source_ref') == answer.get('source_ref')):
                return True
        return False

    def _unanswered_parts(self, question, answered_field, answered_object=None,
                          provisional=None):
        """这条问题**还没答上**的部分。

        结构化字段的问题：目标字段答上了就算答上了。开放问题没有任何字段约束，
        就按"至少给了一个有来源的值"处理——但那最多是 `received_unconfirmed`，
        绝不自动升级成"已有依据"。

        **多对象问题保留对象与答案的对应关系**：问题涉及 N 个对象时，每个对象都
        要有自己那条答案才算答上；只答上一个对象，剩下的对象仍然缺。单对象问题
        的写法保持原样（字段名就是缺口名），所以既有口径逐字不变。

        判"答上了"看的是**核对通过**：`candidate` / `unsupported` / `stale` 的答案
        在历史里保留，但不填缺口。
        """
        target = question.get('target_field')
        if not target:
            return []
        objects = self.question_objects(question)
        if len(objects) < 2:
            return [] if answered_field == target else [target]

        def verified(entry):
            assessment = grounding.assessment_of(entry) or {}
            return assessment.get('status') == grounding.STATUS_VERIFIED

        covered = {entry.get('object_ref') for entry in (question.get('answers') or [])
                   if verified(entry) and entry.get('field') == target}
        if provisional and verified(provisional):
            covered.add(answered_object)
        # 只答了一个对象，剩下的对象按"字段@对象"逐条写清楚缺哪一条。
        return [f'{target}@{obj}' for obj in objects if obj not in covered]

    def claim_support_span(self, claim_id: str) -> tuple[str | None, str | None]:
        """这条 claim 被哪一条证据的哪一段文字支持。指不出来就返回 (None, None)。"""
        claim = next((c for c in self.claims if c['claim_id'] == claim_id), None)
        if claim is None:
            return None, None
        spans = claim.get('support_spans') or {}
        assessments = self.assessments.get(claim_id, {})
        for ref in (claim.get('supporting_evidence') or []):
            if (assessments.get(ref, {}).get('support_status') == 'supported_by_span'
                    and spans.get(ref)):
                return ref, spans[ref]
        return None, None

    def _sync_question_from_claim(self, claim) -> None:
        """把一条 claim 的证据状态同步到它对应的问题上。"""
        # 旧契约的 questions 是 {'gap_id','field','question'} 投影，没有
        # question_id——读取不该在旧形状上炸掉。
        question = next((q for q in self.questions
                         if q.get('question_id')
                         and 'claim:' + digest([q['question_id']])[:12] == claim['claim_id']),
                        None)
        if question is None or is_question_answered(question):
            return
        if claim['status'] == 'supported' and claim.get('support_status') == 'supported_by_span':
            ref, span = self.claim_support_span(claim['claim_id'])
            if ref is None:
                # 判定说"被支持"，却指不出是哪一段让它成立——那就**不 settle**。
                # 把问题读成"已有依据"却没有任何东西支撑它，正是要修的那类缺陷：
                # 界面只能显示"已回答"，显示不出回答是什么。
                return
            # 答案元素与状态一起写。"已有依据"必须能指认是什么回答了它。
            question.setdefault('answers', []).append({
                'value': span, 'field': question.get('target_field'),
                'source': 'evidence', 'provenance': 'external_evidence',
                'source_ref': ref, 'quote': span, 'origin': 'code',
                'answer_ref': ref, 'at': utcnow_iso(),
                'version': dict(self.patient_version or {}),
                'object_ref': None, 'still_uncertain': [],
                # assessment 复用既有判定，不另立一套口径。
                'assessment': {
                    'status': grounding.STATUS_VERIFIED,
                    'reason': f'{ref} 的片段支持该断言（逐字引用见 quote）',
                    'source_ref': ref, 'locator': ref,
                    'dependency_refs': list(claim.get('dependency_refs') or [])},
            })
            question['answer_ref'] = ref
            self.settle_question(question['question_id'], QUESTION_STATUS_ANSWERED,
                                 information_state=INFO_AVAILABLE,
                                 answered_by='evidence',
                                 evidence_refs=[ref])
        elif claim['status'] == 'contradicted' or claim['status'] == 'insufficient':
            # 有内容但没形成支持关系：**保持未决**，如实记为"读到但未建立支持"。
            if (claim.get('opposing_evidence') or claim.get('supporting_evidence')):
                question['information_state'] = INFO_RECEIVED_UNCONFIRMED
            else:
                question['information_state'] = INFO_ATTEMPTED_NO_RESULT

    def _mark_claim_supported(self, question_id, source_ref) -> None:
        """一般参考知识类问题答上后，把对应的 claim 一并标记为已有依据。"""
        claim_id = 'claim:' + digest([question_id])[:12]
        claim = next((c for c in self.claims if c['claim_id'] == claim_id), None)
        if claim is None or source_ref is None:
            return
        self.assessments.setdefault(claim_id, {})[source_ref] = {
            'status': 'supported', 'source_status': 'current',
            'support_status': 'supported_by_span', 'quote_verified': True}
        self._assess()

    # ---- 读取结果分流：A 已有答案 / B 部分 / C 不足 / D 冲突 / E 需专业判断 ----
    def classify_reading(self, question) -> str:
        """读到内容之后，这条问题属于哪一种。

        关键是 **B 与 C 都不等于"换个来源"**：
        * B（部分回答）：保留已知部分，只处理缺失部分；
        * C（不足以回答）：记下具体缺什么，再由模型选补问、补读还是换来源。

        上一版把 `received_unconfirmed` 一律当成 `strategy_needs_change`，于是
        "还没分析"被读成"必须换地方查"——这正是模型重复规划的原因之一。
        """
        state = question.get('information_state')
        if state == INFO_AVAILABLE:
            return 'answered'
        attempts = question.get('attempts') or []
        found = [a for a in attempts if a.get('found_information')]
        if state == INFO_SOURCE_LIMITED or (attempts and not found):
            return 'insufficient'
        if not found:
            return 'not_attempted'
        if any(a.get('conflict') for a in found):
            return 'conflict'
        if question.get('information_target') == TARGET_PROFESSIONAL:
            return 'professional'
        # 读到了内容但还没有采纳动作：这是"内容待处理"，不是"来源不对"。
        return 'content_pending'


    def _forced_stop_typed(self) -> str | None:
        """safety_case 的完成与停止条件。

        完成不是"三查全绿"——那三查是**证据核查**契约的完成条件，不是"这件事
        查清了没有"。这里问的是：

        * 还有没有**仍然阻塞**的问题？有 → 按它的**策略**决定是等人、等材料，
          还是这一轮确实拿不到；
        * 读回来的证据还有没有没读完的？有 → 不算完成（"搜到"不等于"读过"）。

        终止原因按**实际发生了什么**给：没有检索动作，就不会得到
        "检索后资料不可得"。预算不足不代表现实中不存在资料。
        """
        if self.visit_ready_to_deliver() and self.model_decisions >= 1:
            # 这次回访无事可做：模型已经看过摘要并选择了交付，可以收尾。
            #
            # `model_decisions >= 1` 不能省。循环在**规划之前**还会检查一次停止
            # 条件（`agent.run_open_review`），那里直接 break 的话整个 run 一次
            # 模型调用都不会发生——交付沦为代码渲染，也就没有"Agent 选择了
            # 下一步"这回事。
            self.termination_reason = 'checks_completed'
            return self.termination_reason
        if not self.questions_settled or self.new_information_pending:
            # 还没轮到模型开口（问题集未定），或有**它还没看过的新信息**。
            # 此刻判定"没有未决问题"或"只剩等人"都是把机会吃掉。
            return None
        pending = self.blocking_questions()
        if not pending:
            if self.unread_evidence():
                return None
            self.termination_reason = 'checks_completed'
            return self.termination_reason
        strategies = {q.get('strategy') for q in pending}
        if strategies & {STRATEGY_ASK_USER, STRATEGY_PROFESSIONAL_REVIEW}:
            # 有等人回答的问题，就以等待收尾：本轮该做的已经做了（问题已持久化），
            # 不需要为了别的问题继续把检索预算烧完。
            self.termination_reason = ('waiting_review'
                                       if STRATEGY_PROFESSIONAL_REVIEW in strategies
                                       else 'waiting_input')
            return self.termination_reason
        # 还有**可换的来源**就先别收：读了记录没答上来、材料里没有、用户说不知道，
        # 都是"换个地方取"的理由，不是"资料不可得"。
        if self.revision_trigger() == 'strategy_needs_change':
            return None
        # 只有**确实取不到**（尝试过、且来源受限）才叫资料不可得。
        # 成功读到了记录却没答上来，是"这条记录没有答案"，不是"资料不存在"。
        if pending and all(self.source_exhausted(q) for q in pending):
            self.termination_reason = 'evidence_unavailable'
            return self.termination_reason
        return None

    def unread_evidence(self) -> list:
        """Captured evidence whose body was never read back in this run."""
        return [ref for ref in self.evidence_refs if ref not in self.read_refs]

    def degraded_next_action(self):
        """The scripted, deterministic policy — the DEGRADED path only.

        Its fixed ordering and fixed search wording live here deliberately:
        they are a fallback for when the model path is unavailable, not the
        normal planning policy.  The model path must never see this action, so
        ``candidates`` is populated here and nowhere else.
        """
        from .agent import ToolAction
        self.candidates = []
        def action(tool, gap_id, arguments, expected):
            item = ToolAction(tool, 'investigation:' + gap_id, arguments, '解决已记录缺口', gap_id, expected)
            self.candidates = [asdict(item)]
            return item
        if self.termination_reason:
            return None
        if policy_of(self.policy).get('typed_questions'):
            return self._degraded_typed_action(action)
        if not self.authority_read:
            return action('memory_read', 'authority', {'query': 'snapshot'}, '获得完整当前事实及版本')
        missing = [g for g in self.gaps if g['kind'] == 'patient_fact_missing' and g['status'] == 'open' and g.get('field')]
        if missing:
            self.questions = [{'gap_id': g['gap_id'], 'field': g['field'], 'question': g['description']} for g in missing]
            return action('ask_clarification', missing[0]['gap_id'], {'question': '\n'.join(g['description'] for g in missing)}, '等待补充指定字段；未写入临床审批')
        for ref in self.evidence_refs:
            if ref not in self.read_refs:
                # 链接到一个**真实存在**的缺口即可：完成条件要求回读所有已
                # 检索的原文，而那时证据缺口往往已经关闭。
                claim_id = next((g['gap_id'] for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'),
                                next((g['gap_id'] for g in self.gaps if g['status'] == 'open'),
                                     self.claims[0]['claim_id'] if self.claims else 'authority'))
                return action('read_evidence', claim_id, {'evidence_id': ref, 'limit': 2000}, '回读并校验原文、实体、否定和适用条件')
        if self.forced_stop():
            return None
        gap = next((g for g in self.gaps if g['kind'] == 'evidence_missing' and g['status'] == 'open'), None)
        if gap is None:
            self.termination_reason = 'no_progress'
            return None
        claim = next((c for c in self.claims if c['claim_id'] == gap.get('claim_id')), self.claims[0] if self.claims else None)
        terms = ' '.join(claim['entities']) if claim else self.goal[:150]
        suffix = ('药物相互作用 风险', '适用条件 禁忌 否定 相互作用', '证据不足 相互作用 日期')[len(self.queries) % 3]
        return action('rag_search', gap['gap_id'], {'query': terms + ' ' + suffix, 'top_k': 5}, '获得新增可核验证据或明确冲突')

    def next_action(self):
        """Compatibility shim.  The normal (model) path calls ``forced_stop()``
        instead; only the degraded path plans an action."""
        return self.degraded_next_action()

    def supply_default_subquestions(self):
        """If the planner never declared sub-questions, the degraded policy
        supplies them at wrap-up — labelled ``code_default``, so a report can
        never present them as the model's own decomposition.  Never raises:
        a bounded report with fewer questions is a valid outcome."""
        if any(g['gap_id'] == GAP_PLAN and g['status'] == 'open' for g in self.gaps):
            self.apply_default_subquestions()

    def finish(self, degraded_reason=None):
        if degraded_reason and degraded_reason.startswith('planner_circuit_break:') and self.termination_reason:
            # A code-verified result may finish after the planner was disabled.
            # The run still exposes its degraded planner status separately.
            self.supply_default_subquestions()
            return
        if degraded_reason:
            self.termination_reason = ('cancelled' if degraded_reason == 'cancelled' else
                'budget_insufficient' if 'budget' in degraded_reason or 'max_cycles' in degraded_reason else
                'no_progress' if 'no_progress' in degraded_reason else 'unrecoverable_failure')
        if not self.termination_reason:
            if policy_of(self.policy).get('typed_questions'):
                # 收尾**不再规划默认动作**：那是旧路径的行为，它会把"预算耗尽"
                # 之类的真实原因盖成一个假装的动作，再退回 no_progress。这里只
                # 决定"以什么状态收尾"，不补造模型没提出过的问题。
                self.termination_reason = self._forced_stop_typed() or 'no_progress'
            else:
                self.next_action()
        self.termination_reason = self.termination_reason or 'no_progress'
        if not policy_of(self.policy).get('typed_questions'):
            self.supply_default_subquestions()
        else:
            # 降级到收尾时问题集还没有定下来：说明这一轮根本没有可问的规划器
            # （或它没能给出问题）。就此定下，再按真实原因收尾。
            self.settle_questions_by_default()
            # 先把原因说准，再决定哪些资料类问题该记为"不可得"。顺序反了的话，
            # 问题一旦被标成 unavailable 就不再是"未决问题"，原因也就无从分辨。
            self.termination_reason = self._refine_typed_reason(self.termination_reason)
            self._mark_unavailable_questions()

    def _mark_unavailable_questions(self) -> None:
        """**尝试过**检索、额度用尽仍无结果的资料类问题 → 记为"当前来源受限"。

        两条边界：
        * 一次检索都没做过 → 什么都不改（那叫"还没试"，不叫"取不到"）；
        * 记的是 ``information_state``，**不是** `answered`。它仍然未决、仍然阻塞、
          仍然出现在关闭校验里。预算不足不代表现实中不存在资料。
        """
        attempted = {q['question_id'] for q in self.questions if (q.get('attempts') or [])}
        if not attempted:
            return
        for question in self.blocking_questions():
            if question.get('strategy') != STRATEGY_GENERAL_REFERENCE:
                continue
            if question['question_id'] not in attempted:
                continue
            if any(a.get('found_information') for a in question.get('attempts') or ()):
                continue
            question['information_state'] = INFO_SOURCE_LIMITED
            question['source_limited_reason'] = (
                '本轮检索未取得可核验资料；这不表示资料不存在，也不表示风险已处理')

    def _refine_typed_reason(self, reason):
        """把收尾原因说准：等待 / 来源受限 / 重复行动 / 预算耗尽 各是各的。

        旧路径把它们统一写成 no_progress，还会把"模型原地重复规划"改写成
        "检索后资料不可得"——**一次检索都没发生过**就报检索结论，是凭空造事实。
        现在：只有真的尝试过检索、且来源确实取不到，才说 `evidence_unavailable`。
        """
        if not policy_of(self.policy).get('typed_questions'):
            return reason
        strategies = {q.get('strategy') for q in self.blocking_questions()}
        attempted = {q['question_id'] for q in self.questions if (q.get('attempts') or [])}
        if reason in ('budget_insufficient', 'no_progress'):
            if strategies & {STRATEGY_ASK_USER, STRATEGY_PROFESSIONAL_REVIEW}:
                return ('waiting_review' if STRATEGY_PROFESSIONAL_REVIEW in strategies
                        else 'waiting_input')
            # `evidence_unavailable` 的意思是"**资料确实取不到**"。只有尝试过、
            # 且来源被标成受限时才是这样。成功读到了记录却没答上来、或者模型原地
            # 重复同一个提案，都不是——那些如实保留原因为 no_progress / 预算耗尽。
            pending = self.blocking_questions()
            if pending and all(self.source_exhausted(q) for q in pending):
                return 'evidence_unavailable'
        return reason

    def verify_statements(self) -> list[dict]:
        """Evidence-support check for MODEL-authored explanations.

        A statement written by the model is only a conclusion when the evidence
        it cites was actually READ BACK in this run — the same rule the label
        path already enforces ('搜到' 不等于 '已读取并验证').  Anything else is
        demoted to a question to raise at the visit: never silently deleted, and
        never rendered as a finding.
        """
        read = set(self.read_refs) | set(self.material_read_refs)
        pending = []
        for claim in self.claims:
            if claim.get('source') != 'model':
                continue
            refs = set(claim.get('supporting_evidence') or []) | set(claim.get('opposing_evidence') or [])
            if not refs:
                # Nothing read back supports it at all.
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'no_read_evidence'})
            elif not refs.issubset(read):
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'citation_not_read_back'})
            elif claim.get('support_status') == 'no_supporting_span':
                # 引用真实存在、也确实回读过，但引用体里没有这句断言所说的
                # 内容。降级为**待确认项**，不用免责声明替代证据校验。
                pending.append({'claim_id': claim['claim_id'], 'statement': claim['statement'],
                                'reason': 'citation_does_not_support_statement'})
        self.pending_statements = pending
        return pending

    def _render_statement(self, statement, entities):
        """Render one model-authored statement into the DELIVERED report.

        The delivered text passes a code-owned safety check that flags any line
        naming a hazard concept without a grounded citation or an explicit
        disclaimer.  Code-authored labels always satisfied that; model wording
        is arbitrary, so a sentence asserting a CONCRETE harm is reported by
        its subjects rather than repeated.  Nothing is hidden: the original
        statement stays in the structured artifact and in the claim record.
        """
        subjects = '、'.join(entities) or '相关药物'
        if CONCRETE_HAZARD.search(statement or ''):
            return f'- 涉及 {subjects} 的一项说法包含未经逐字核实的危害描述，本报告不复述；请与医生或药师核对。'
        if composed_text_prescribes(statement or ''):
            # Defence in depth: accept_questions already refuses these, so this
            # only fires for a statement that reached the claim set another way.
            return f'- 涉及 {subjects} 的一项说法带有诊断或用药调整措辞，本报告不复述；请与医生或药师核对。'
        return f'- {statement}（仅基于已回读原文并列呈现，不构成诊断）'

    # 空态句：某一节没有实质内容时写的句子。**渲染与评分共用**同一份定义
    # （评分器读的是同名副本），所以"这一节写了东西没有"只有一个答案。
    EMPTY_SECTION_SENTENCE = {
        '2. 有来源支持的事实': '- 本次没有得到可作为结论的事实；证据不足不等于证明绝对安全。',
        '3. 不同材料之间的差异': '- 本次未在已读取的材料与记录之间发现可记录的差异；未读取的材料不在此列。',
        '4. 仍缺少依据的问题（待核实）': '- 本契约内没有剩余缺口。',
        '5. 就诊时可以向医生或药师确认什么': '- 可将本报告的差异与未决项逐条向医生或药师确认。',
    }

    def section_content(self):
        """每一节的**实质**条目（不含空态句）。

        渲染与空态判定读同一个集合，于是不会出现"渲染说有内容、评分说没有"
        的漂移——那种漂移会让评分器判的其实是另一份报告。
        """
        self.verify_statements()
        pending_ids = {item['claim_id'] for item in self.pending_statements}
        concluded = []
        for claim in self.claims:
            # 第 2 节的标题是"有来源支持的事实"，谓词就必须只说 supported：
            # 旧谓词是"非 insufficient"，于是 ``contradicted``（只有反对证据）
            # 的断言也挂在"支持的事实"底下——标题与内容互相矛盾。
            if claim['status'] != 'supported' or claim['claim_id'] in pending_ids:
                continue
            # 缺这个键 = 本 scope 之前采集的记录，按 **not_applicable** 恢复：
            # 口径升级不得追溯否定历史结论（与 ``_assess`` 的汇总同一条规则）。
            if claim.get('support_status') not in {None, 'supported_by_span', 'not_applicable'}:
                # 引用被回读过，但引用体里没有这句断言所说的内容。
                continue
            concluded.append(self._render_statement(claim['statement'], claim.get('entities') or []))
            concluded.append(f"  状态：{claim['status']}。"
                             f"支持引用：{', '.join(claim['supporting_evidence']) or '无'}；"
                             f"反对引用：{', '.join(claim['opposing_evidence']) or '无'}。")
        return {
            '2. 有来源支持的事实': concluded,
            '3. 不同材料之间的差异': [
                f"- {g['description']}" for g in self.gaps
                if g.get('kind') in {'evidence_conflict', 'material_conflict'}],
            '4. 仍缺少依据的问题（待核实）': (
                [f"- {g['description']}" for g in self.gaps if g['status'] == 'open']
                # 被反驳的结论不能从报告里消失：它不再属于"有来源支持的事实"
                # （那才是标题的意思），但它恰恰是**最**需要当面确认的一条。
                + [self._render_statement(claim['statement'], claim.get('entities') or [])
                   + '（现有证据与这一说法相反，列为待确认问题）'
                   for claim in self.claims if claim['status'] == 'contradicted']
                + [self._render_statement(item['statement'], [])
                   + f"（未核实的解释，原因：{item['reason']}，列为待确认问题）"
                   for item in self.pending_statements]),
            # 模型的**澄清问题**与**未获支持的断言**都是该当面问出口的话。
            # 旧口径下第 5 节只由降级路径写入，模型自己提的问题从不出现——
            # 于是报告缺的恰恰是它最该问的部分。
            '5. 就诊时可以向医生或药师确认什么': list(dict.fromkeys(
                # 未决问题用它们自己的说法进来（typed 契约下是 statement，
                # 旧投影是 question）。**不问来源**：模型提的问题和程序记的
                # 问题在这里一样重要，报告缺的恰恰常常是前者。
                [_question_text(question) for question in self.questions]
                + [claim['statement'] for claim in self.claims
                   if claim.get('source') == 'model' and claim['status'] != 'supported'])),
        }

    def citable_memory_refs(self) -> list:
        """本报告有权引用的记忆 ref。

        报告的差异一节会**点名双方**——材料条目与它所对比的当前记录，后者
        是 ``memory:<kind>:<n>`` 形状。响应的最后一道安全校验把"报告里出现
        而它没被告知"的 ref 判为 ``fabricated_memory_ref``，所以这些 ref 必须
        一并交出去；否则双方具名会被自己的安全边界拦下，报告根本发不出去。
        """
        refs = []
        for medication in self.facts.get('medications') or []:
            if medication.get('ref'):
                refs.append(str(medication['ref']))
        for gap in self.gaps:
            refs.extend(str(ref) for ref in (gap.get('counterparts') or []) if ref)
        for conflict in self.conflicts or []:
            for key in ('left_ref', 'right_ref', 'ref'):
                if conflict.get(key):
                    refs.append(str(conflict[key]))
        return list(dict.fromkeys(refs))

    def empty_report_sections(self) -> list:
        """只渲染了空态句的节标题。

        评分器据此区分两种"这一节没写东西"：状态**确实为空**时占位句是正确
        内容（诚实空态），状态非空时才是缺陷。没有这份声明，要求"必须有实质
        条目"会把"材料本就一致"的任务判成**恒假**——与恒真一样不可证伪。
        """
        return [title for title, items in self.section_content().items() if not items]

    def report_text(self):
        """The visit-preparation report: five answers, each grounded.

        The order is fixed because a caregiver reads it that way; the CONTENT
        is entirely derived from what was actually read back.
        """
        self.verify_statements()
        checked = [key for key, value in self.checks.items() if value == 'checked']
        labels = {'authority': '权威用药及关键事实', 'interaction_evidence': '标签证据',
                  'applicability': '材料适用条件'}
        sections = self.section_content()
        # 刷新派生字段，使序列化视图与**这份**报告一致。
        self.empty_sections = [title for title, items in sections.items() if not items]
        # The goal is deliberately NOT echoed here: it is the caregiver's own
        # free text, and the delivered response passes a keyword safety check
        # that a quoted "…风险…" would trip even though nothing is asserted.
        # `goal` stays on the investigation and in the saved artifact.
        lines = ['# 有界证据核查报告 · 就诊准备', '']
        lines += ['## 1. 本次调查解决了什么', '',
                  '已核查范围：' + ('、'.join(labels[key] for key in checked) or '尚无完成项') + '。',
                  '终止原因：' + str(self.termination_reason) + '。', '']
        for title in ('2. 有来源支持的事实', '3. 不同材料之间的差异',
                      '4. 仍缺少依据的问题（待核实）',
                      '5. 就诊时可以向医生或药师确认什么'):
            lines += ['## ' + title, '']
            lines += sections[title] or [self.EMPTY_SECTION_SENTENCE[title]]
            lines += ['']
        lines += ['这份报告仅说明有界核查结果；insufficient/unknown 不是无风险，'
                  '未列药物不代表停药，也不代表已停用。本系统不做诊断、处方或用药调整建议。'
                  '请携带本报告与医生或药师当面确认；建议咨询医生/药师后再做任何用药决定。']
        return '\n'.join(lines)


def proposal_errors(inv, proposal):
    """Additive schema applies only to versioned investigation runs."""
    if proposal.get('decision') == 'respond':
        return [] if inv.termination_reason else ['investigation_not_terminal']
    tool = proposal.get('tool')
    args = proposal.get('arguments') or {}
    if tool == 'memory_write':
        return []  # Existing write policy and receipt guard still apply.
    if tool == 'read_evidence':
        # 回读的必要性**先于**它所服务的缺口：完成条件要求把所有已检索的原文
        # 回读一遍，而"证据缺口已关闭"恰恰是那时最常见的状态。因此这一条只
        # 要求指向一个真实存在的缺口（开放或已关闭），不像其他工具那样要求
        # 它仍然开放——否则完成条件与可用工具互相矛盾，形成死锁。
        if args.get('evidence_id') not in inv.evidence_refs:
            return ['evidence_not_observed_in_scope']
        if not any(g['gap_id'] == proposal.get('gap_id') for g in inv.gaps):
            return ['invalid_gap_link']
        return []
    gap = next((g for g in inv.gaps if g['gap_id'] == proposal.get('gap_id') and g['status'] == 'open'), None)
    if gap is None or not isinstance(proposal.get('expected_observation'), str) or not proposal['expected_observation'].strip():
        return ['invalid_gap_link']
    if tool == 'plan_questions':
        # 只在**真正可以规划**时可用：首次规划，或有新证据触发的修订。这条
        # 闸门同时守住"不得用它绕开别的开放缺口"——绕不绕得开由触发条件决定，
        # 不由它链接到哪个 gap_id 决定。
        return [] if inv.revision_trigger() is not None else ['plan_questions_only_when_revisable']
    if tool == 'read_material_item':
        # Same rule as read_evidence: only a material this run actually
        # enumerated may be read, so an id cannot be probed into existence.
        ref = f"{args.get('case_id')}/{args.get('item_id')}"
        if ref not in inv.material_refs:
            return ['material_not_observed_in_scope']
        return []
    if tool == 'acquire_evidence':
        from .harness.evidence_acquire import enabled
        if not enabled() or not inv.authority_read or gap['kind'] != 'evidence_missing':
            return ['investigation_tool_not_allowed']
    if tool in {'rag_search', 'acquire_evidence'} and len(inv.queries) >= inv.search_limit():
        return ['search_budget_exhausted']
    if tool not in {'memory_read', 'rag_catalog', 'rag_search', 'read_evidence', 'acquire_evidence', 'ask_clarification',
                    'ddi_check', 'list_materials', 'answer_question',
                    'propose_medication_change'}:
        return ['investigation_tool_not_allowed']
    if tool == 'answer_question':
        return _answer_question_errors(inv, args)
    if tool == 'propose_medication_change':
        return _propose_change_errors(inv, args)
    if tool == 'ask_clarification':
        if policy_of(inv.policy).get('typed_questions'):
            return _typed_clarification_errors(inv, args)
        if not inv.authority_read or gap['kind'] != 'patient_fact_missing' or not gap.get('field'):
            return ['clarification_without_missing_fact']
    # The authority gap accepts only the full snapshot read.  A memory_read
    # with the query OMITTED is a correctable omission (the guard hydrates
    # query='snapshot' and records the correction) — only a WRONG query value
    # violates the contract here; a missing required argument is still caught
    # by the shared schema check when hydration is disabled.
    if gap['gap_id'] == 'authority' and (tool != 'memory_read'
                                         or (tool == 'memory_read' and 'query' in args and args.get('query') != 'snapshot')):
        return ['authority_requires_full_memory_read']
    if tool == 'ask_clarification' and not policy_of(inv.policy).get('typed_questions') \
            and args.get('question') not in {g['description'] for g in inv.gaps if g['kind'] == 'patient_fact_missing' and g.get('field')}:
        return ['question_does_not_match_missing_fact']
    return []


#: 变更候选能针对的字段 = 能**机械核对**的用药字段。与 `review_visits` 的那份
#: 是同一个集合，有一条测试钉住两边一致——工具 schema、这里、以及确认时的写入，
#: 三处漂移任何一处，模型产出的候选都会被另一处拒掉。
CHANGE_CANDIDATE_FIELDS = ('dose', 'schedule', 'route', 'start_at')


def _propose_change_errors(inv, args) -> list[str]:
    """候选变更提案的**结构**校验。

    这里挡的是"这条候选根本没法核对"：问题不存在、药名空、字段不可核对、新值空。
    真正的"这句话是不是用户说的"不在这一步——那条线索由模型给出、由**用户**在
    确认那一步定夺，所以界面上必须把来源摆出来。
    """
    if inv.question(str(args.get('question_id') or '')) is None:
        return ['unknown_question_id']
    if not str(args.get('name') or '').strip():
        return ['change_without_medication']
    if str(args.get('field') or '') not in CHANGE_CANDIDATE_FIELDS:
        return ['change_field_not_checkable']
    value = args.get('value')
    if value is None or not str(value).strip():
        return ['change_without_value']
    return []


def _answer_question_errors(inv, args) -> list[str]:
    """采纳提案的**结构**校验；支持关系由 answer_question 自己核。

    这里只挡明显不合格的：问题不存在、来源种类未知、值为空、指到别的问题的缺口。
    真正的"来源是否支持答案"是业务判断，放在采纳入口里，结果如实回给模型。

    **来源是模型自报的，所以这里不替它背书**：用户回答与专业意见不是模型能声明
    的种类，这里就挡下来，不必等到业务判定那一步。
    """
    question = inv.question(str(args.get('question_id') or ''))
    if question is None:
        return ['unknown_question_id']
    if is_question_answered(question):
        return ['question_already_answered']
    if str(args.get('source') or '') not in ANSWER_SOURCES:
        return ['unknown_answer_source']
    if str(args.get('source') or '') not in grounding.MODEL_SUBMITTABLE_SOURCES:
        return ['answer_source_not_model_declarable']
    value = args.get('value')
    if value is None or not str(value).strip():
        return ['empty_answer']
    if args.get('source_ref') and not inv._reference_is_visible(
            str(args['source_ref']), inv.allowed_entities()):
        return ['source_not_in_scope']
    if args.get('object_ref') and str(args['object_ref']) not in inv.question_objects(question):
        return ['object_not_in_question']
    return []


def _typed_clarification_errors(inv, args) -> list[str]:
    """safety_case 下的补问校验：校验**结构与边界**，不校验句子相等。

    模型用自己的话问是可以的——只要它指向一条**真实存在**的、等用户回答的问题。
    校验的是：问题存在、类型是"等用户"、问题里带了指向那一条的 ``question_id``、
    文本本身不含诊断/处方指令、对象是本事项的相关对象。

    不做的是：要求它与程序写好的句子逐字相同。那正是上一版把模型逼成"复读机"
    的地方，也是它无法表达"我还需要知道开始时间"的原因。
    """
    if not inv.authority_read:
        return ['clarification_without_missing_fact']
    question_id = str(args.get('question_id') or '')
    if not question_id:
        return ['clarification_without_question_id']
    question = inv.question(question_id)
    if question is None:
        return ['unknown_question_id']
    if question.get('strategy') != STRATEGY_ASK_USER:
        return ['strategy_is_not_ask_user']
    if is_question_answered(question):
        return ['question_already_settled']
    text = str(args.get('question') or '')
    if not text.strip() or len(text) > 300:
        return ['invalid_question_text']
    if composed_text_prescribes(text) or is_medication_instruction(text):
        return ['question_prescribes']
    return []
