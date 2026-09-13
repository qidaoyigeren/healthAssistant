"""安全事项（SafetyCase）——围绕同一安全问题持续跟进的业务事项。

主线里的位置：一次用药或背景变化先经过**确定性必要检查**产生 `conclusions`，
再由本模块把这些结论收敛成一件"事项"；Agent 的调查（``care_task`` 的
``safety_case`` 契约）、用户的补充（``input_request``）和复核处置都挂在事项上，
所以下次会话回来时继续的是**同一件事**，不是重新生成一张卡片。

本模块是一条**引用层**，不是第二个真相源：

* 患者事实、用药值与风险等级一律仍在 ``medications`` / ``semantic_memory`` /
  ``conclusions`` 里；事项只保存 ref。
* 「检查做完了没有」从被引用的 conclusion 的 **当前状态**读出来，不另存一份布尔量。
* 事项生命周期状态与任何一次运行的状态分开：运行失败只把事项推进到
  ``execution_failed``，绝不会把它推到 ``resolved``。

关闭条件由代码强制（见 ``SafetyCaseStore.disposition``）：只有
``deterministic_check_completed`` 与 ``professional_review_applied`` 两种依据能把事项
关成 ``resolved``；用户点"已读"、模型说"应该没问题"、provider 失败、拿旧版本的复核
批准新版本，都会被拒绝。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Iterable, Sequence

from . import followup_runtime as _follow_up
from .memory import utc_now
from .product import ProductError, SCOPE, packed

KIND = 'safety_case'

# ---- 事项类型 ---------------------------------------------------------------
CASE_INTERACTION_RISK = 'interaction_risk'
CASE_CONDITION_RISK = 'condition_risk'
CASE_EVIDENCE_GAP = 'evidence_gap'
CASE_DISCREPANCY = 'discrepancy'
CASE_SOURCE_INVALIDATED = 'source_invalidated'
CASE_TYPES = (CASE_INTERACTION_RISK, CASE_CONDITION_RISK, CASE_EVIDENCE_GAP,
              CASE_DISCREPANCY, CASE_SOURCE_INVALIDATED)

# ---- 生命周期状态（与运行状态无关） ------------------------------------------
STATUS_OPEN = 'open'                              # 待调查
STATUS_INVESTIGATING = 'investigating'            # 调查中
STATUS_AWAITING_USER = 'awaiting_user'            # 等待用户补充
STATUS_AWAITING_PROFESSIONAL = 'awaiting_professional'  # 等待专业复核
STATUS_RESOLVED = 'resolved'                      # 已完成有依据的处置
STATUS_MONITORING = 'monitoring'                  # 风险仍在，已有安排，持续跟进
STATUS_NEEDS_RECHECK = 'needs_recheck'            # 因新信息需要重新复核
STATUS_EXECUTION_FAILED = 'execution_failed'      # 本次执行失败，事项仍未解决
STATUSES = (STATUS_OPEN, STATUS_INVESTIGATING, STATUS_AWAITING_USER,
            STATUS_AWAITING_PROFESSIONAL, STATUS_RESOLVED, STATUS_MONITORING,
            STATUS_NEEDS_RECHECK, STATUS_EXECUTION_FAILED)
TERMINAL_STATUSES = (STATUS_RESOLVED,)
#: 事项仍然"活着"（需要有人做点什么）的状态。
UNSETTLED_STATUSES = tuple(s for s in STATUSES if s not in TERMINAL_STATUSES)

STATUS_LABELS = {
    STATUS_OPEN: '待调查',
    STATUS_INVESTIGATING: '调查中',
    STATUS_AWAITING_USER: '等待您补充',
    STATUS_AWAITING_PROFESSIONAL: '等待专业复核',
    STATUS_RESOLVED: '已有依据的处置',
    STATUS_MONITORING: '持续跟进中（风险仍在）',
    STATUS_NEEDS_RECHECK: '需要重新核对',
    STATUS_EXECUTION_FAILED: '本次执行未完成（事项仍未解决）',
}

# ---- 处置依据种类 -----------------------------------------------------------
BASIS_CHECK = 'deterministic_check_completed'
BASIS_PROFESSIONAL = 'professional_review_applied'
BASIS_USER_REPORTED = 'user_reported'
#: 只有这两种依据可以关闭事项。用户转述医生意见（``user_reported``）不是其中之一。
CLOSING_BASES = (BASIS_CHECK, BASIS_PROFESSIONAL)

DISPOSITION_RESOLVED = 'resolved_with_basis'
DISPOSITION_ESCALATED = 'escalated_to_professional'
DISPOSITION_MONITORING = 'accepted_monitoring'

#: 处置动作（按依据细分）→ 需要哪一个角色。**由认证上下文判定**，不读请求体
#: 自报的身份。按依据细分是必要的：以"确定性检查证明触发条件已消除"关闭，是照护
#: 者权限内的操作；以**专业复核决定**关闭，则必须是复核方自己的权限。
DISPOSITION_ROLES = {
    (DISPOSITION_RESOLVED, BASIS_CHECK): ('caregiver', 'ops'),
    (DISPOSITION_RESOLVED, BASIS_PROFESSIONAL): ('reviewer', 'ops'),
    (DISPOSITION_ESCALATED, None): ('caregiver', 'ops'),
    (DISPOSITION_MONITORING, None): ('caregiver', 'ops'),
}


def required_roles(disposition: str, basis_kind: str) -> tuple[str, ...]:
    return (DISPOSITION_ROLES.get((disposition, basis_kind))
            or DISPOSITION_ROLES.get((disposition, None)) or ())

#: 复核决定里**允许完成事项**的动作。其余动作（要求补充、确认报告事实、
#: 解决冲突、驳回候选）都不能当成"这件事可以结束了"。
CLOSING_REVIEW_ACTIONS = ('close_with_safe_guidance',)

#: 复核来源的系统标识。当前项目**没有连接真实医护服务**：来自本地模拟工作台的
#: 决定不能被包装成"专业医疗确认"。
SIMULATED_REVIEW_SOURCES = ('local-demo-simulated-reviewer', 'simulated')


def _drug_key(name: str) -> str:
    """归一化药名，与 memory._drug_dep_key 同一口径（去空白、小写）。"""
    return re.sub(r"\s+", "", str(name or "")).lower()


#: 持续跟进可以挂在什么上。``arrangement`` 是"还没有可信依据的安排"——它必须
#: 如实显示成待确认，而不是被当成一个真实的复查周期。
FOLLOW_UP_KINDS = ('review_at', 'on_event', 'arrangement')

# ---- 补充回答的分类 ---------------------------------------------------------
#: 收到了回答之前，"收到了东西"和"问题被回答了"是两件事。
ANSWER_PROVIDED = 'provided'
ANSWER_UNKNOWN = 'unknown'    # 用户明说不知道 —— 结束追问，但不消除不确定性
ANSWER_EMPTY = 'empty'        # 空值/缺失 —— 什么都不关
ANSWER_IRRELEVANT = 'irrelevant'  # 答非所问（可由模型提示，但不由模型定论）

#: 明说"不知道"的常见说法。这是一份**输入归一化**表，不是安全判定：
#: 它只决定"这条回答算不算有内容"，不决定风险是否存在。
_UNKNOWN_PHRASES = ('不知道', '不清楚', '不确定', '不记得', '记不清', '说不好',
                    '没有记录', '无法确认', '不了解', '没注意', '忘了')


#: 结构化字段的**格式**要求。这里只有格式与取值范围，没有任何临床阈值：
#: "开始时间要是个日期"是格式，"这个剂量是否安全"不是这里能回答的问题。
_UNIT_TOKENS = ('mg', 'g', 'ml', 'μg', 'ug', 'mcg', 'iu', '毫克', '克', '毫升', '微克',
                '片', '粒', '袋', '支', '单位', '国际单位', '%', 'ml/次')
FIELD_FORMATS = {
    'start_date': {'kind': 'date', 'hint': '请给出日期（例如 2026-09-01）'},
    'dose_unit': {'kind': 'unit', 'hint': '请给出剂量单位（例如 mg、片、ml）'},
    'dose': {'kind': 'amount', 'hint': '请给出剂量数字（可带单位，例如 5mg）'},
    'schedule': {'kind': 'short_text', 'max': 40, 'hint': '请给出服用频次的简短说明（例如 每日一次）'},
}


def field_mismatch(fields: Sequence[str], value: Any) -> list[str]:
    """回答是否满足这些目标字段的**格式**要求。

    返回不满足的字段名。开放文本字段（没有格式约定）一律满足——只有确实有
    格式约定的字段才会拒绝。"不知道"已经在 ``classify_answer`` 里分流，不在这里。
    """
    text = str(value or '').strip()
    if not text:
        return list(fields)
    bad: list[str] = []
    for field in fields:
        rule = FIELD_FORMATS.get(str(field))
        if rule is None:
            continue
        kind = rule['kind']
        if kind == 'date':
            if not _looks_like_date(text):
                bad.append(field)
        elif kind == 'unit':
            if not _looks_like_unit(text):
                bad.append(field)
        elif kind == 'amount':
            if not re.match(r'^\s*\d+(?:[.,]\d+)?\s*\S{0,8}\s*$', text):
                bad.append(field)
        elif kind == 'short_text':
            if len(text) > int(rule.get('max') or 40):
                bad.append(field)
    return bad


def _looks_like_date(text: str) -> bool:
    import datetime
    cleaned = text.strip().replace('年', '-').replace('月', '-').replace('日', '')
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y-%m', '%Y'):
        try:
            datetime.datetime.strptime(cleaned[:len(fmt) + 2].strip(), fmt)
            return True
        except ValueError:
            continue
    return bool(re.search(r'\d{4}\s*[-/.]\s*\d{1,2}', text))


def _looks_like_unit(text: str) -> bool:
    cleaned = re.sub(r'\s+', '', text).lower()
    if len(cleaned) > 12:
        return False
    return any(token in cleaned for token in _UNIT_TOKENS)


def classify_answer(value: Any) -> str:
    """一条回答算"有内容"还是"明说不知道"还是"空的"。

    它**不**回答"这条回答是否真的解决了那个问题"——那需要判断关联性，
    可以由模型提示，但不由这里定论。
    """
    text = str(value or '').strip()
    if not text:
        return ANSWER_EMPTY
    collapsed = re.sub(r"[\s，。、,.!！?？~～]+", "", text)
    if collapsed in _UNKNOWN_PHRASES or any(
            collapsed == phrase for phrase in _UNKNOWN_PHRASES):
        return ANSWER_UNKNOWN
    return ANSWER_PROVIDED


def _normalise_follow_up(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把"持续跟进"落成一条**可持久化、未确认**的安排。

    模型不能凭自己的判断生成临床复查周期：调用方没有给出时间或触发条件时，
    这里记的是"尚无可信依据的待确认安排"，而不是编一个周期出来。

    **有时间或条件不等于已确认。** 这里产出的 `confirmed` 恒为 `False`——确认只能
    由 `confirm_follow_up` 写入确认记录后产生。历史实现把 `bool(at or condition)`
    当成 `confirmed`，于是"给了一个时间"就被读成"有人确认过"；那些确认从来没有人
    做过。语义澄清见 [CONTRACT §4.5]。
    """
    if not isinstance(raw, dict):
        return _follow_up.build_arrangement(kind='arrangement')
    kind = str(raw.get('kind') or 'arrangement')
    if kind not in FOLLOW_UP_KINDS:
        # 未知的**种类词**是调用方错误，不是"没给时间"。降级成 arrangement 会让
        # 一次拼错的请求看起来像一条正常排上的安排。
        raise ProductError(f'不支持的安排类型：{kind!r}', 422)
    return _follow_up.build_arrangement(
        kind=kind,
        at=raw.get('at'),
        condition=raw.get('condition'),
        owner=raw.get('owner'),
        note=raw.get('note'))


# ---- 身份：dedup_key ---------------------------------------------------------
def incarnation_id(connection, medication_id: int) -> int:
    """一条用药的**本次生效链**标识。

    "不同用药阶段不得因药名相同被错误合并"——剂量变更会把旧版本置为
    ``superseded`` 而药名不变，那仍是同一次用药；停药（``stopped``）则切断了链。
    这里沿 ``predecessor_id`` 回溯到最近一次 ``stopped`` 边界之后的第一个版本，
    用它当分期锚点。
    """
    seen: set[int] = set()
    cursor = int(medication_id)
    while cursor not in seen:
        seen.add(cursor)
        row = connection.execute(
            "SELECT id, predecessor_id, status FROM medications WHERE id=?", (cursor,)
        ).fetchone()
        if row is None:
            return cursor
        predecessor = row["predecessor_id"]
        if predecessor is None:
            return cursor
        prior = connection.execute(
            "SELECT status FROM medications WHERE id=?", (predecessor,)
        ).fetchone()
        if prior is None or prior["status"] != "superseded":
            # 前一个版本不是"被剂量变更取代"——它是一次停用/争议，分期从此断开。
            return cursor
        cursor = int(predecessor)
    return cursor


def episode_anchor(store, medication_refs: Sequence[str]) -> str:
    """由相关用药的**分期**推出锚点；没有用药引用的类型锚定在事项对象上。"""
    connection = store.memory.connection
    anchors: list[str] = []
    for ref in medication_refs or ():
        match = re.fullmatch(r"memory:medication:(\d+)(?:@v\d+)?", str(ref))
        if not match:
            continue
        anchors.append(str(incarnation_id(connection, int(match.group(1)))))
    return "+".join(sorted(set(anchors)))


def dedup_key(*, patient_scope: str, case_type: str, subject_keys: Iterable[str],
              anchor: str) -> str:
    """事项身份 = 类型 + 对象 + 相关事件范围。

    刻意**不**包含模型生成的标题、当前时间或运行 id：同一件事在每天的复检里
    必须是同一个 key，否则就会出现"每天一张新卡片"。
    """
    payload = packed({"scope": patient_scope, "type": case_type,
                      "subjects": sorted({str(k) for k in subject_keys if str(k)}),
                      "anchor": anchor or ""})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def interaction_subjects(display_names: Sequence[str]) -> list[str]:
    return sorted({_drug_key(name) for name in display_names if _drug_key(name)})


# ---- 结论 → 对象 -------------------------------------------------------------
def subject_keys_for_conclusion(memory, conclusion: dict[str, Any]) -> tuple[str, list[str]]:
    """从一条结论推出 (case_type, subject_keys)。

    相互作用的 pair 从结论文本里按既有口径解析（`drug_a × drug_b：...`）；
    解析不出来时降级为按用药集合定位的事项，绝不降级成"没有对象"。
    """
    text = str(conclusion.get("text") or "")
    kind = str(conclusion.get("kind") or "warning")
    if kind == "condition_warning" or "患者个体风险" in text:
        names = []
        for ref in conclusion.get("memory_refs") or ():
            match = re.fullmatch(r"memory:medication:(\d+)(?:@v\d+)?", str(ref))
            if not match:
                continue
            row = memory.connection.execute(
                "SELECT display_name FROM medications WHERE id=?", (int(match.group(1)),)
            ).fetchone()
            if row:
                names.append(row["display_name"])
        subjects = [f"condition:{_drug_key(n)}" for n in names] or ["condition:unknown"]
        return CASE_CONDITION_RISK, subjects
    head = text.split("：", 1)[0]
    if "×" in head:
        left, _, right = head.partition("×")
        keys = interaction_subjects([left, right])
        if len(keys) == 2:
            return CASE_INTERACTION_RISK, [f"pair:{keys[0]}|{keys[1]}"]
    return CASE_INTERACTION_RISK, [f"medication_set:{memory.medication_set_hash()}"]


def resolve_chain_head(memory, conclusion_id: int) -> int | None:
    """沿 ``superseded_by`` 走到这条结论链的**最新版本**。

    事项引用的是"当时那条结论"，但检查重跑会写后继版本。判断当前状态时必须看链头，
    否则事项会永远盯着一条已经过期的记录，把"已经重查过"读成"还没有依据"。
    """
    seen: set[int] = set()
    cursor: int | None = int(conclusion_id)
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        row = memory.connection.execute(
            "SELECT superseded_by FROM conclusions WHERE id=?", (cursor,)).fetchone()
        if row is None or row['superseded_by'] is None:
            return cursor
        cursor = int(row['superseded_by'])
    return cursor


# ---- 事项存储 ---------------------------------------------------------------
class SafetyCaseStore:
    """``product_objects(kind='safety_case')`` 上的事项仓储。

    复用既有的 ``ProductStore`` 事务与回执机制（``BEGIN IMMEDIATE`` + ``command``），
    因此事项的建立/更新与它引用的域写入可以在同一个事务里提交，重放不会加倍。
    """

    def __init__(self, product):
        self.p = product
        self.memory = product.memory

    # ---- 读 ---------------------------------------------------------------
    def objects(self) -> list[dict[str, Any]]:
        return self.p.objects(KIND)

    def get(self, case_id: str) -> dict[str, Any]:
        return self.p.get(case_id, KIND)

    def by_dedup_key(self, key: str) -> dict[str, Any] | None:
        for case in self.objects():
            if case.get("dedup_key") == key:
                return case
        return None

    def open_cases(self) -> list[dict[str, Any]]:
        return [c for c in self.objects() if c["current_status"] in UNSETTLED_STATUSES]

    # ---- 写 ---------------------------------------------------------------
    def open_or_update(self, key: str, *, case_type: str, subject_keys: Sequence[str],
                       anchor: str, trigger: dict[str, Any],
                       medication_refs: Sequence[str] = (),
                       fact_refs: Sequence[str] = (),
                       conclusion_refs: Sequence[str] = (),
                       evidence_refs: Sequence[str] = (),
                       questions: Sequence[dict[str, Any]] = (),
                       required_inputs: Sequence[dict[str, Any]] = (),
                       next_action_summary: str | None = None,
                       responsible_party: str | None = None) -> tuple[dict[str, Any], bool]:
        """建立或更新一事项。返回 ``(case, created)``。

        去重身份只由 ``dedup_key`` 决定，所以同一风险每天复检一次不会产生新卡片；
        已经 `resolved` 的事项被新事件命中时会**重新打开**为 ``needs_recheck``。
        """
        if case_type not in CASE_TYPES:
            raise ProductError('不支持的安全事项类型')
        identity = dedup_key(patient_scope=SCOPE, case_type=case_type,
                             subject_keys=subject_keys, anchor=anchor)

        def execute():
            existing = self.by_dedup_key(identity)
            created = existing is None
            reopened = False
            if created:
                case = {
                    'id': f'safety-case:{uuid.uuid4().hex}', 'case_id': None,
                    'scope_id': SCOPE, 'patient_scope': SCOPE, 'case_type': case_type,
                    'dedup_key': identity, 'subject_keys': list(subject_keys),
                    'episode_anchor': anchor,
                    'related_medication_refs': [], 'trigger_event_refs': [],
                    'linked_conclusion_refs': [], 'relevant_fact_refs': [],
                    'evidence_refs': [], 'open_questions': [], 'required_inputs': [],
                    'linked_run_ids': [], 'linked_review_case_ids': [],
                    'follow_up': None, 'resolution_basis': None, 'disposition': None,
                    'user_seen_at': None, 'history': [], 'created_at': utc_now(),
                    'updated_at': utc_now(), 'revision': 1,
                    'current_status': STATUS_OPEN,
                    'next_action_summary': next_action_summary or '等待程序执行必要安全检查',
                    'responsible_party': responsible_party or 'system',
                    'input_versions': self.p.revisions(),
                }
                case['case_id'] = case['id']
                case['history'].append({'at': case['created_at'], 'event': 'opened',
                                        'case_type': case_type, 'trigger': dict(trigger)})
            else:
                case = existing
                reopened = case['current_status'] in TERMINAL_STATUSES
                case['history'].append({
                    'at': utc_now(), 'event': 'reopened' if reopened else 'updated',
                    'trigger': dict(trigger)})
                if reopened:
                    case['current_status'] = STATUS_NEEDS_RECHECK
                    case['resolution_basis'] = None
                    case['disposition'] = None
                case['updated_at'] = utc_now()
                case['revision'] += 1

            def add_unique(field: str, values: Sequence[str]) -> None:
                bucket = case[field]
                for value in values or ():
                    if value and value not in bucket:
                        bucket.append(value)

            add_unique('related_medication_refs', medication_refs)
            add_unique('relevant_fact_refs', fact_refs)
            add_unique('linked_conclusion_refs', conclusion_refs)
            add_unique('evidence_refs', evidence_refs)
            trigger_ref = trigger.get('ref')
            if trigger_ref:
                add_unique('trigger_event_refs', [trigger_ref])
            for question in questions or ():
                qid = question.get('question_id')
                if qid and not any(q.get('question_id') == qid for q in case['open_questions']):
                    case['open_questions'].append(dict(question))
            for request in required_inputs or ():
                rid = request.get('request_id')
                if rid and not any(r.get('request_id') == rid for r in case['required_inputs']):
                    case['required_inputs'].append({**dict(request), 'status': 'open'})
            if next_action_summary:
                case['next_action_summary'] = next_action_summary
            if responsible_party:
                case['responsible_party'] = responsible_party
            case['input_versions'] = self.p.revisions()
            # 刚刚重新打开的事项必须**显示**为"需要重新复核"：它在被下一次
            # 检查刷新的 sync 之前，确实还没有新依据。这里不能让通用的
            # "依据有效就回到待调查" 规则把这次重开抹掉。
            self.derive_status(case, allow_reopen_demotion=not reopened)
            self.p.save(KIND, case)
            return case

        case = self.p.command(key, {'type': 'safety_case_open', 'case_type': case_type,
                                    'dedup_key': identity, 'trigger': dict(trigger)}, execute)
        # Replay-safe: a replayed command returns the stored case, and "was this the
        # opening write" is read off the case itself rather than re-queried.
        created = len(case['history']) == 1 and case['history'][0]['event'] == 'opened'
        return case, created

    # ---- 状态推导 -----------------------------------------------------------
    def derive_status(self, case: dict[str, Any], *, allow_reopen_demotion: bool = True) -> None:
        """从**被引用的真相**推出事项状态——不新存一份判断结果。

        优先级：任何一条被引用的结论失效 → 需要重新核对；有等待专业复核的输入 →
        awaiting_professional；有未答的用户输入 → awaiting_user；否则保持现状。

        ``allow_reopen_demotion=False`` 用于"刚刚被新事件重新打开"的这一次：那时
        还没有任何新依据，把 `needs_recheck` 立刻降回 `open` 会让界面上的
        "因新信息需要重新复核" 一闪而过。
        """
        before = case['current_status']
        self.retire_stale_answers(case)
        statuses = self._conclusion_statuses(case)
        if any(status != 'current' for status in statuses.values()):
            case['current_status'] = STATUS_NEEDS_RECHECK
            case['next_action_summary'] = '依据发生变化，需要按当前记录重新核对'
            case['responsible_party'] = 'system'
            # 旧处置依据**不再生效**。它没有被改写——原样搬进历史，因为那件事确实
            # 发生过；但当前字段必须清空，否则界面会把一条已经不适用的处置当成
            # 现存结论展示给用户。
            if case.get('resolution_basis'):
                case['history'].append({
                    'at': utc_now(), 'event': 'resolution_basis_retired',
                    'reason': case['next_action_summary'],
                    'previous_basis': case['resolution_basis'],
                    'previous_disposition': case.get('disposition')})
                case['resolution_basis'] = None
                case['disposition'] = None
        else:
            inputs = case.get('required_inputs') or []
            still_waiting = [item for item in inputs
                             if item.get('status') == 'open'
                             and not item.get('needs_alternative_evidence')]
            if any(item.get('for_professional') for item in still_waiting):
                case['current_status'] = STATUS_AWAITING_PROFESSIONAL
                case['responsible_party'] = 'professional'
            elif still_waiting:
                case['current_status'] = STATUS_AWAITING_USER
                case['responsible_party'] = 'caregiver'
            elif any(item.get('needs_alternative_evidence') for item in inputs):
                # 用户明说不知道：不再等他，轮到系统去找**替代证据**。
                case['current_status'] = STATUS_OPEN
                case['responsible_party'] = 'agent'
                case['next_action_summary'] = '用户无法提供这条信息，需要改从其他来源核实'
            elif allow_reopen_demotion and before in (
                    STATUS_AWAITING_USER, STATUS_AWAITING_PROFESSIONAL, STATUS_NEEDS_RECHECK):
                # 未决项已清空且依据仍有效：回到"可以继续调查"。
                # `monitoring` **不在此列**：它是一次处置（风险仍在、已有安排），
                # 不是"在等某个人"。让它被一次例行同步降级成"待调查"，用户刚登记
                # 的跟进安排就消失了。
                case['current_status'] = STATUS_OPEN
                case['next_action_summary'] = '已具备依据，可以进行调查或复核'
                case['responsible_party'] = 'agent'
        # 每一次**状态迁移**都记进历史，并在迁移时推进 revision。否则界面只能看到
        # "现在需要重新核对"，答不出"它是什么时候、因为什么变成这样的"——而那正是
        # §"为何重新复核"要回答的问题。
        if case['current_status'] != before:
            case['history'].append({'at': utc_now(), 'event': 'status_changed',
                                    'from': before, 'to': case['current_status'],
                                    'why': case.get('next_action_summary')})
            case['updated_at'] = utc_now()
            case['revision'] += 1
        return case

    def _conclusion_statuses(self, case: dict[str, Any]) -> dict[int, str]:
        ids = []
        for ref in case.get('linked_conclusion_refs') or ():
            match = re.fullmatch(r"memory:conclusion:(\d+)(?:@v\d+)?", str(ref))
            if match:
                ids.append(int(match.group(1)))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        return {
            row["id"]: row["status"]
            for row in self.memory.connection.execute(
                f"SELECT id, status FROM conclusions WHERE id IN ({placeholders})", ids)
        }

    # ---- 事项调查上下文（按需组装，不复制任何真相源） ------------------------
    def investigation_context(self, case: dict[str, Any], *, history_limit: int = 8) -> dict[str, Any]:
        """Agent 开始调查一件**具体事项**时看到的上下文。

        每一样都从**已有真相源**现取：事项对象、结论表、依赖索引、必要检查队列。
        事项里不另存一份患者档案或药物表——所以这份上下文永远不会和权威记录分叉。

        刻意只覆盖"这件事"，不铺开整个患者。Agent 有具体理由时可以在执行循环里
        主动扩大查询范围（那是它自己的动作），但**默认起点**必须窄。
        """
        refs = list(case.get('linked_conclusion_refs') or ())
        conclusions: list[dict[str, Any]] = []
        current_ids: list[int] = []
        for ref in refs[:24]:
            match = re.fullmatch(r"memory:conclusion:(\d+)(?:@v\d+)?", str(ref))
            if not match:
                continue
            head = resolve_chain_head(self.memory, int(match.group(1)))
            if head is None or head in current_ids:
                continue
            current_ids.append(head)
            row = self.memory.connection.execute(
                "SELECT id, kind, text, status, stale_reason, input_revision, conclusion_outcome "
                "FROM conclusions WHERE id=?", (head,)).fetchone()
            if row is None:
                continue
            verdict = self.memory.evaluate_trigger(head)
            conclusions.append({
                'ref': f"memory:conclusion:{row['id']}@v1", 'kind': row['kind'],
                'text': row['text'], 'state': row['status'],
                'trigger_state': verdict['state'], 'trigger_reasons': verdict['reasons'],
                'recorded_outcome': row['conclusion_outcome'],
                'version_applies': (_load_json(row['input_revision']) or {}) == self.p.revisions(),
                'stale_reason': row['stale_reason'],
            })
        inputs = case.get('required_inputs') or []
        return {
            'case': {'case_id': case.get('id') or case.get('case_id'),
                     'case_type': case['case_type'],
                     'subject_keys': case.get('subject_keys') or [],
                     'status': case['current_status'],
                     'status_label': STATUS_LABELS.get(case['current_status'])},
            'why': {'trigger': (case.get('history') or [{}])[0].get('trigger'),
                    'recent_events': [{'event': e.get('event'), 'at': e.get('at'),
                                       'why': e.get('why')}
                                      for e in (case.get('history') or [])[-history_limit:]
                                      if e.get('event') in ('opened', 'reopened', 'status_changed',
                                                            'disposition', 'input_recorded',
                                                            'resolution_basis_retired')]},
            'subjects': {
                'medications': [_resolve(self, ref)
                                for ref in (case.get('related_medication_refs') or ())[:16]],
                'facts': [_resolve(self, ref)
                          for ref in (case.get('relevant_fact_refs') or ())[:16]],
            },
            'conclusions': conclusions,
            'questions': {
                'open': [{'request_id': i['request_id'], 'question': i.get('question'),
                          'why_needed': i.get('why_needed'),
                          'for_professional': bool(i.get('for_professional'))}
                         for i in inputs if i.get('status') == 'open'],
                'unanswered_by_user': [{'request_id': i['request_id'], 'question': i.get('question'),
                                        'note': '用户表示不知道；需要替代证据，不能当成已解决'}
                                       for i in inputs if i.get('status') == ANSWER_UNKNOWN],
                'answered': [{'request_id': i['request_id'], 'question': i.get('question'),
                              'answer_kind': i.get('answer_kind')}
                             for i in inputs if i.get('status') == 'answered'],
            },
            'existing_disposition': {'disposition': case.get('disposition'),
                                     'basis_kind': (case.get('resolution_basis') or {}).get('kind'),
                                     'follow_up': case.get('follow_up')},
            'next_action_summary': case.get('next_action_summary'),
            'responsible_party': case.get('responsible_party'),
            'allowed_actions': ['read_evidence', 'search', 'ask_user', 'request_professional',
                                'propose_disposition'],
            'note': ('上下文只覆盖本事项；需要时可以在执行循环里扩大查询范围，'
                     '但要在动作说明里给出理由。'),
        }

    def sync(self, key: str, *, command_key: str | None = None) -> dict[str, Any]:
        """按被引用的真相重算状态并把变化记进历史。

        事件驱动：只有调用方（worker / 路由）触发时才跑，不做轮询。
        幂等键带上是**调用时**的事项版本，所以"同一次同步"重放返回同一结果，
        而事项真的变了以后的同步会另立一条回执。
        """
        revision = self.get(key)['revision']
        operation = command_key or f'{key}:sync:{revision}'

        def execute():
            case = self.get(key)
            before = (case['current_status'], case['revision'])
            self.derive_status(case)
            if (case['current_status'], case['revision']) != before:
                self.p.save(KIND, case)
            return case
        return self.p.command(operation, {'type': 'safety_case_sync', 'case_id': key,
                                          'at_revision': revision}, execute)

    # ---- 用户已看到（不是状态） ----------------------------------------------
    def mark_seen(self, key: str, *, command_key: str | None = None) -> dict[str, Any]:
        """用户已看到提示。**只**记录时间戳，不关闭、不降级、不改变任何未决项。

        ``revision`` **不**递增：它是生命周期操作的 CAS 令牌，而"已读"不是生命周期
        变化。递增会让一次"看到了"作废用户手上正在填的那张表单——点过已读之后再
        想处置，就会撞上"事项已被其他操作更新"。
        """
        def execute():
            case = self.get(key)
            if case.get('user_seen_at') is None:
                case['user_seen_at'] = utc_now()
                case['history'].append({'at': case['user_seen_at'], 'event': 'seen_by_user'})
                case['updated_at'] = utc_now()
                self.p.save(KIND, case)
            return case
        return self.p.command(command_key or f'{key}:seen',
                             {'type': 'safety_case_seen', 'case_id': key}, execute)

    # ---- 补充到达 -----------------------------------------------------------
    def apply_input(self, case: dict[str, Any], *, request_id: str, answer_ref: str | None,
                    value: Any = None, for_professional: bool = False,
                    answer_kind: str | None = None) -> dict[str, Any]:
        """把一条补充记进**已经取出的**事项对象，并只关闭它**实际回答到**的那一条请求。

        三种"收到东西"必须分开处理，混成一个 `answered` 会让事项把不确定性丢掉：

        * ``provided``——有内容地回答 → 请求关闭。
        * ``unknown``——用户**明说不知道**。它结束这一轮追问（不再等这位用户），
          但**不消除不确定性**：请求转入 ``unknown``，继续阻止关闭，Agent 应当转去
          找替代证据或明确阻塞。
        * ``empty``——空值/缺失 → 什么都不关，只如实记一笔。

        没有指名道姓回答哪一条时，一条都不关——收到任意补充就清空全部未决项，会让
        事项再也想不起那件事。

        这是纯变更函数（不自己开事务、不写回执），好让调用方能把它并进**自己的**
        事务里：补充输入先是照护待办的一次提交，事项的更新必须和那次提交同生共死。
        """
        # 先退休过期回答，再判断"这条回答命中了哪条请求"。顺序反了的话，一条因为
        # 事实变化而被重新打开的问题会认不出新回答——用户答了，系统却什么都没记。
        self.retire_stale_answers(case)
        open_ids = {item['request_id'] for item in case['required_inputs']
                    if item.get('status') == 'open'}
        matched = request_id if request_id in open_ids else None
        kind = answer_kind or classify_answer(value)
        unsatisfied: list[str] = []
        target = next((item for item in case['required_inputs']
                       if item['request_id'] == matched), None)
        if target is not None and kind == ANSWER_PROVIDED:
            # 有内容 ≠ 答到了。目标字段有格式要求时，不符合的回答**不消除缺口**，
            # 只如实记下"收到了什么、还差什么"——否则一句"我看看"就能把
            # "开始时间是什么"标成已解决。判断放在写历史**之前**：历史里的
            # `answered` 必须是这件事的实际结论，不能是判断完成前的默认值。
            unsatisfied = field_mismatch(target.get('fields') or (), value)
        case['history'].append({'at': utc_now(), 'event': 'input_recorded',
                                'request_id': request_id, 'answer_ref': answer_ref,
                                'value': value, 'answer_kind': kind,
                                'answered': bool(matched and kind == ANSWER_PROVIDED
                                                 and not unsatisfied),
                                'unsatisfied_fields': list(unsatisfied),
                                'for_professional': for_professional})
        if matched and kind in (ANSWER_PROVIDED, ANSWER_UNKNOWN):
            for item in case['required_inputs']:
                if item['request_id'] != matched:
                    continue
                item['answer_ref'] = answer_ref
                item['answered_at'] = utc_now()
                item['answer_kind'] = kind
                item['answered_against'] = self.p.revisions()
                if unsatisfied:
                    item['status'] = 'open'
                    item['unsatisfied_fields'] = list(unsatisfied)
                    item['received_value'] = str(value or '')[:120]
                else:
                    item.pop('unsatisfied_fields', None)
                    item['status'] = ('answered' if kind == ANSWER_PROVIDED else ANSWER_UNKNOWN)
                    if kind == ANSWER_UNKNOWN:
                        # 明说不知道：不再等这位用户，但问题本身**没有解决**。
                        item['needs_alternative_evidence'] = True
        case['updated_at'] = utc_now()
        case['revision'] += 1
        self.derive_status(case)
        return case

    def retire_stale_answers(self, case: dict[str, Any]) -> dict[str, Any]:
        """回答之后事实变了 → 那条回答不再适用于当前状态，重新打开该问题。

        只影响**依赖当前记录版本**的回答。重新打开是保守方向：宁可再问一次，
        也不要拿一条针对旧状态的回答去支撑新状态下的判断。

        这与 `require_input` 的按 id 去重是两件事：去重防止"每次恢复都生成新问题"，
        这里防止"一个历史 answered 永久阻止重新核对"。两条一起才成立。
        """
        current = self.p.revisions()
        reopened: list[str] = []
        for item in case.get('required_inputs') or ():
            if item.get('status') != 'answered':
                continue
            recorded = item.get('answered_against') or {}
            if recorded == current:
                continue
            item['status'] = 'open'
            item['reopened_reason'] = '记录在回答之后发生变化，这条回答不再适用于当前状态'
            item['answer_ref'] = None
            # 注意区分：这里**要**再问这位用户一次（回答过期了），所以不设
            # ``needs_alternative_evidence``——那是"用户说不知道、别再等他"的标记。
            item.pop('needs_alternative_evidence', None)
            reopened.append(item['request_id'])
            case['history'].append({'at': utc_now(), 'event': 'answer_retired',
                                    'request_id': item['request_id'],
                                    'reason': item['reopened_reason'],
                                    'answered_against': recorded, 'now': current})
        if reopened:
            case['updated_at'] = utc_now()
            case['revision'] += 1
        return case

    def record_input(self, key: str, *, request_id: str, answer_ref: str | None,
                     value: Any = None, for_professional: bool = False,
                     answer_kind: str | None = None,
                     command_key: str | None = None) -> dict[str, Any]:
        """独立提交的一条补充（自带事务与回执）。"""
        def execute():
            case = self.apply_input(self.get(key), request_id=request_id,
                                    answer_ref=answer_ref, value=value,
                                    for_professional=for_professional,
                                    answer_kind=answer_kind)
            self.p.save(KIND, case)
            return case
        return self.p.command(command_key or f'{key}:input:{request_id}:{answer_ref or ""}',
                              {'type': 'safety_case_input', 'case_id': key,
                               'request_id': request_id, 'answer_ref': answer_ref,
                               'answer_kind': answer_kind}, execute)

    def require_input(self, key: str, *, request_id: str, question: str,
                      fields: Sequence[str] = (), for_professional: bool = False,
                      why_needed: str | None = None,
                      question_kind: str | None = None,
                      question_strategy: str | None = None,
                      strategy_history: Sequence[dict[str, Any]] = (),
                      subject_refs: Sequence[str] = (),
                      command_key: str | None = None) -> dict[str, Any]:
        """登记一条等待补充的信息（去重按 request_id）。

        ``question_kind`` / ``subject_refs`` 是调查契约给出的**问题类型与对象**：
        界面据此说明"这条信息要从哪里拿"，回答回来时也据此判断它是否真的答到了。
        """
        def execute():
            case = self.get(key)
            if not any(item['request_id'] == request_id for item in case['required_inputs']):
                case['required_inputs'].append({
                    'request_id': request_id, 'question': question,
                    'fields': list(fields), 'for_professional': bool(for_professional),
                    'why_needed': why_needed, 'status': 'open',
                    'question_kind': question_kind,
                    'question_strategy': question_strategy,
                    'strategy_history': [dict(entry) for entry in strategy_history],
                    'subject_refs': list(subject_refs),
                    'asked_at': utc_now()})
                case['history'].append({'at': utc_now(), 'event': 'input_requested',
                                        'request_id': request_id,
                                        'for_professional': bool(for_professional)})
                case['updated_at'] = utc_now()
                case['revision'] += 1
                self.derive_status(case)
                self.p.save(KIND, case)
            return case
        return self.p.command(command_key or f'{key}:require:{request_id}',
                              {'type': 'safety_case_require_input', 'case_id': key,
                               'request_id': request_id}, execute)

    # ---- 处置：关闭 / 转人工 / 持续跟进 --------------------------------------
    def closure_evidence(self, case: dict[str, Any]) -> dict[str, Any]:
        """**关闭所需证据**的现场核对结果。只回答"能不能关"，不做任何写入。

        关闭必须同时满足（缺一不可）：

        1. 存在一条仍 ``current`` 的关联结论，其记录的输入版本等于**当前**版本
           （否则是拿旧版本上的检查批准新状态）；
        2. 这条结论的**触发条件**在当前权威记录下已经被消除——由
           ``memory.evaluate_trigger`` 从依赖行判定，**不是**对正文做关键词匹配；
        3. 关联结论里没有一条"触发条件仍然成立"的风险结论（风险还在就不能关）；
        4. 没有阻塞性的未决问题（用户或专业人员的问题都算）。

        返回结构固定，界面直接展示，不做二次解释。
        """
        refs = [ref for ref in (case.get('linked_conclusion_refs') or ())]
        evidence: dict[str, Any] = {
            'refs': refs, 'checked': [], 'eliminated': [], 'still_present': [],
            # 未决问题包含两种："还在等回答"与"用户明说不知道"。后者不再等用户，
            # 但**不确定性没有消失**，所以同样阻止关闭。
            'blocking_inputs': [item['request_id'] for item in case.get('required_inputs') or ()
                                if item.get('status') in ('open', ANSWER_UNKNOWN)],
        }
        if not refs:
            return {**evidence, 'ok': False, 'reason': '该事项还没有可依据的检查结论'}
        current = self.p.revisions()
        for ref in refs:
            match = re.fullmatch(r"memory:conclusion:(\d+)(?:@v\d+)?", str(ref))
            if not match:
                return {**evidence, 'ok': False, 'reason': '依据引用不合法'}
            # 看链头：重查写过后继版本时，当前状态由后继决定。
            conclusion_id = resolve_chain_head(self.memory, int(match.group(1)))
            row = self.memory.connection.execute(
                "SELECT status, input_revision, text FROM conclusions WHERE id=?",
                (conclusion_id,)).fetchone()
            if row is None:
                return {**evidence, 'ok': False, 'reason': '依据结论不存在'}
            if row['status'] != 'current':
                return {**evidence, 'ok': False,
                        'reason': '依据结论已失效，请重新核对后再处置'}
            recorded = _load_json(row['input_revision']) or {}
            for scope_key, revision in current.items():
                if recorded.get(scope_key) != revision:
                    return {**evidence, 'ok': False,
                            'reason': f'{scope_key} 记录在检查后发生变化，'
                                      f'旧结论不能批准当前状态'}
            verdict = self.memory.evaluate_trigger(conclusion_id)
            evidence['checked'].append({'ref': ref, 'state': verdict['state'],
                                        'reasons': verdict['reasons']})
            if verdict['state'] == 'trigger_eliminated':
                evidence['eliminated'].append(ref)
            elif verdict['state'] == 'risk_present':
                evidence['still_present'].append(ref)
        if evidence['still_present']:
            return {**evidence, 'ok': False,
                    'reason': '关联结论仍显示风险存在，不能关闭；'
                              '如已有管理安排，请登记为持续跟进'}
        if not evidence['eliminated']:
            return {**evidence, 'ok': False,
                    'reason': '没有结论表明本事项的触发条件已经消除，不能关闭'}
        if evidence['blocking_inputs']:
            return {**evidence, 'ok': False, 'reason': '仍有未决问题需要先处理'}
        return {**evidence, 'ok': True, 'reason': None}

    def disposition(self, key: str, *, expected_revision: int, disposition: str,
                    basis_kind: str, actor: str, roles: Sequence[str] = (),
                    note: str | None = None,
                    decision_id: str | None = None,
                    conclusion_refs: Sequence[str] | None = None,
                    follow_up: dict[str, Any] | None = None,
                    command_key: str | None = None) -> dict[str, Any]:
        """按依据更新处置状态。**关闭条件由这里强制**，调用方无法绕过。

        ``actor`` 与 ``roles`` 来自**认证上下文**（服务层解析的 principal），
        绝不从请求体读取——自报的身份不是身份。

        三个动作的语义是分开的：

        * ``resolved_with_basis``——**关闭**。必须由 ``closure_evidence`` 现场证明
          触发条件已消除（确定性路径），或由一条**适用且允许完成**的专业处置决定
          授权。检查"做过了"本身不构成关闭理由。
        * ``escalated_to_professional``——交给专业人员。用户转述医生意见走这条，
          它**不**关闭事项。
        * ``accepted_monitoring``——风险仍在但已有管理安排。这是**持续跟进**，
          不是"等待专业人员"，也不是风险消失。
        """
        if disposition not in (DISPOSITION_RESOLVED, DISPOSITION_ESCALATED,
                               DISPOSITION_MONITORING):
            raise ProductError('不支持的处置动作')
        if disposition == DISPOSITION_MONITORING:
            # 持续跟进**不使用关闭依据**——要求调用方随便填一个，只会让界面
            # 以为这里有一种"依据"。它记的是"风险还在 + 后续怎么跟进"。
            basis_kind = 'monitoring_arrangement'
        elif basis_kind not in CLOSING_BASES + (BASIS_USER_REPORTED,):
            raise ProductError('处置依据不合法')
        required = required_roles(disposition, basis_kind)
        if required and not (set(required) & set(roles or ())):
            raise ProductError(
                f'当前身份没有执行该处置的权限（需要 {" 或 ".join(required)}）', 403)

        def execute():
            case = self.get(key)
            if case['revision'] != expected_revision:
                raise ProductError('事项已被其他操作更新，请刷新', 409)
            if disposition == DISPOSITION_MONITORING:
                # 持续跟进**不需要关闭依据**——它表达的是反面：风险仍然成立。
                # 这里记录的是"凭什么说风险还在"，以及跟进安排。
                evidence = self.closure_evidence(case)
                case['current_status'] = STATUS_MONITORING
                case['responsible_party'] = 'caregiver'
                case['follow_up'] = _normalise_follow_up(follow_up)
                case['next_action_summary'] = (
                    '风险仍然存在，按下面的安排继续跟进；这不表示风险已经消除')
                case['resolution_basis'] = {
                    'kind': 'monitoring_arrangement', 'actor': actor, 'at': utc_now(),
                    'still_present': evidence['still_present'],
                    'why_not_closed': evidence['reason'],
                }
                case['disposition'] = DISPOSITION_MONITORING
                case['history'].append({'at': utc_now(), 'event': 'disposition',
                                        'disposition': case['disposition'],
                                        'basis_kind': 'monitoring_arrangement',
                                        'actor': actor, 'roles': sorted(roles or ()),
                                        'note': note, 'follow_up': case['follow_up']})
                case['updated_at'] = utc_now()
                case['revision'] += 1
                self.p.save(KIND, case)
                return case
            basis = self._build_basis(case, basis_kind=basis_kind, actor=actor,
                                      decision_id=decision_id,
                                      conclusion_refs=conclusion_refs)
            if disposition == DISPOSITION_RESOLVED and basis_kind not in CLOSING_BASES:
                # 用户转述医生意见 ≠ 已验证的专业记录；模型判断不是依据。
                # 这里**拒绝**而不是悄悄降级：调用方要求的是"关闭"，静默改成
                # "转人工"会让界面显示一个没人请求过的结果。
                raise ProductError(
                    '用户转述与模型判断不能作为关闭事项的依据；请提交给专业人员复核', 409)

            if disposition == DISPOSITION_ESCALATED or basis_kind == BASIS_USER_REPORTED:
                case['current_status'] = STATUS_AWAITING_PROFESSIONAL
                case['responsible_party'] = 'professional'
                case['next_action_summary'] = (
                    '已记录您转述的意见，等待专业复核确认'
                    if basis_kind == BASIS_USER_REPORTED else '已提交专业复核，等待结论')
                case['resolution_basis'] = basis
                case['disposition'] = DISPOSITION_ESCALATED
            else:
                case['current_status'] = STATUS_RESOLVED
                case['responsible_party'] = 'none'
                case['next_action_summary'] = '已有依据的处置，后续变化会重新打开'
                case['resolution_basis'] = basis
                case['disposition'] = DISPOSITION_RESOLVED
            case['history'].append({'at': utc_now(), 'event': 'disposition',
                                    'disposition': case['disposition'],
                                    'basis_kind': basis_kind, 'actor': actor,
                                    'roles': sorted(roles or ()),
                                    'note': note})
            case['updated_at'] = utc_now()
            case['revision'] += 1
            self.p.save(KIND, case)
            return case
        return self.p.command(
            command_key or f'{key}:disposition:{expected_revision}:{disposition}:{basis_kind}',
            {'type': 'safety_case_disposition', 'case_id': key,
             'disposition': disposition, 'basis_kind': basis_kind,
             'decision_id': decision_id}, execute)

    # ---- 长期跟进：安排 / 改期 / 取消 / 确认 ---------------------------------
    #
    # 在此之前 `follow_up` 是**只写一次、不可改、不可取消**的：唯一的写入点是
    # `accepted_monitoring` 处置分支，而且 `confirmed` 是派生的。要支持"长期跟进"
    # 就必须补上这三个动作，并让确认成为一件**发生过的事**而不是一个派生布尔量。
    #
    # 三个动作都推进**事项** revision（CAS 令牌），但只有"安排/取消"推进**安排**
    # revision——确认改变的是"谁承诺了"，不是"安排是什么"，推进它会让一条刚确认的
    # 安排作废自己已经排好的触发。

    def schedule_follow_up(self, key: str, *, expected_revision: int, kind: str,
                           at: str | None = None, condition: dict | None = None,
                           owner: str | None = None, note: str | None = None,
                           actor: str | None = None,
                           command_key: str | None = None) -> dict[str, Any]:
        """登记或改期一条跟进安排。

        **不接受 `confirmed`。** 请求体送了也不读，更不会因此把 `confirmed` 置真：
        确认是一次单独的、有记录的承诺（``confirm_follow_up``）。
        """
        effective_key = command_key or f'{key}:follow-up:schedule:{expected_revision}:{kind}'

        def execute():
            case = self.get(key)
            if case['revision'] != expected_revision:
                raise ProductError('事项已被其他操作更新，请刷新', 409)
            if case['current_status'] in TERMINAL_STATUSES:
                raise ProductError(
                    '事项已有依据的处置，不能再安排跟进；如情况变化请重新打开', 409)
            previous = case.get('follow_up') or {}
            arrangement = _follow_up.build_arrangement(
                kind=kind, at=at, condition=condition, owner=owner, note=note,
                revision=int(previous.get('revision') or 0) + 1)
            _follow_up.cancel_pending_runs(self.memory, case['id'],
                                           reason='安排已改期，旧触发作废')
            case['follow_up'] = arrangement
            case['history'].append({
                'at': utc_now(), 'event': 'follow_up_scheduled', 'actor': actor,
                'kind': arrangement['kind'], 'at_time': arrangement['at'],
                'condition': arrangement['condition'],
                'schedule_state': arrangement['schedule_state'],
                'note': arrangement['note']})
            case['updated_at'] = utc_now()
            case['revision'] += 1
            self.p.save(KIND, case)
            return case
        return self.p.command(effective_key, {
            'type': 'safety_case_follow_up_schedule', 'case_id': key,
            'kind': kind, 'at': at, 'condition': condition}, execute)

    def cancel_follow_up(self, key: str, *, expected_revision: int,
                         reason: str | None = None, actor: str | None = None,
                         command_key: str | None = None) -> dict[str, Any]:
        """取消一条安排。**不清空** `at`/`condition`/`owner`/`note`——历史要留着，
        靠状态表达"已取消"。

        取消推进安排 revision，因此**已经排上的触发身份随之作废**：一条取消了的安排
        不会在后台继续执行副作用。
        """
        effective_key = command_key or f'{key}:follow-up:cancel:{expected_revision}'

        def execute():
            case = self.get(key)
            if case['revision'] != expected_revision:
                raise ProductError('事项已被其他操作更新，请刷新', 409)
            current = case.get('follow_up')
            if not isinstance(current, dict):
                raise ProductError('这件事项还没有跟进安排，没有可取消的东西', 409)
            state = _follow_up.schedule_state(current)
            if state == _follow_up.SCHEDULE_CANCELLED:
                raise ProductError('该跟进安排已经取消', 409)
            if state == _follow_up.SCHEDULE_TRIGGERED:
                raise ProductError('该跟进安排已经执行，不能再取消', 409)
            cancelled = dict(current)
            cancelled['schedule_state'] = _follow_up.SCHEDULE_CANCELLED
            cancelled['blocked_reason'] = None
            cancelled['revision'] = int(current.get('revision') or 1) + 1
            _follow_up.cancel_pending_runs(self.memory, case['id'], reason=reason
                                           or '安排已取消')
            case['follow_up'] = cancelled
            case['history'].append({
                'at': utc_now(), 'event': 'follow_up_cancelled', 'actor': actor,
                'reason': reason, 'previous_state': state})
            case['updated_at'] = utc_now()
            case['revision'] += 1
            self.p.save(KIND, case)
            return case
        return self.p.command(effective_key, {
            'type': 'safety_case_follow_up_cancel', 'case_id': key,
            'reason': reason}, execute)

    def confirm_follow_up(self, key: str, *, expected_revision: int,
                          actor: str | None = None, note: str | None = None,
                          command_key: str | None = None) -> dict[str, Any]:
        """确认一条**已安排**的安排，产生一条可追溯的确认记录。

        `confirmed_by` 取认证主体（调用方传入的 ``actor``），不接受请求体自称。
        前置条件：存在一条 `scheduled`/`due` 的安排——**没有安排就没有可确认的东西**。
        """
        effective_key = command_key or f'{key}:follow-up:confirm:{expected_revision}'

        def execute():
            case = self.get(key)
            if case['revision'] != expected_revision:
                raise ProductError('事项已被其他操作更新，请刷新', 409)
            current = case.get('follow_up')
            state = _follow_up.schedule_state(current) if isinstance(current, dict) else None
            if state not in _follow_up.CONFIRMABLE_STATES:
                raise ProductError(
                    '没有可确认的跟进安排：请先登记一条有时间的安排，再确认由谁跟进', 409)
            confirmed = _follow_up.apply_confirmation(
                current, by=actor, at=utc_now(), note=note,
                # 指向**真实存在的**确认记录：产品命令的幂等回执。自报的确认不是确认，
                # 而没有落点的"确认"和没有确认是一回事。
                confirmation_ref=f'product:{effective_key}')
            case['follow_up'] = confirmed
            case['history'].append({
                'at': confirmed['confirmed_at'], 'event': 'follow_up_confirmed',
                'actor': actor, 'confirmation_ref': confirmed['confirmation_ref'],
                'note': note})
            case['updated_at'] = utc_now()
            case['revision'] += 1
            self.p.save(KIND, case)
            return case
        return self.p.command(effective_key, {
            'type': 'safety_case_follow_up_confirm', 'case_id': key}, execute)

    def _build_basis(self, case: dict[str, Any], *, basis_kind: str, actor: str,
                     decision_id: str | None,
                     conclusion_refs: Sequence[str] | None) -> dict[str, Any]:
        """构造并**校验**处置依据。校验不通过就抛错，不做降级放行。"""
        basis: dict[str, Any] = {'kind': basis_kind, 'actor': actor, 'at': utc_now()}
        if basis_kind == BASIS_PROFESSIONAL:
            return self._professional_basis(case, basis, decision_id)
        if basis_kind == BASIS_USER_REPORTED:
            basis['note'] = '用户转述，未经核实；不作为已验证的专业记录'
            return basis
        # BASIS_CHECK：不能只看"检查做过了、版本对得上"——必须现场证明
        # **本事项的触发条件已经消除**。
        evidence = self.closure_evidence(case)
        if not evidence['ok']:
            raise ProductError(evidence['reason'], 409)
        basis.update({'conclusion_refs': evidence['eliminated'],
                      'input_revision': self.p.revisions(),
                      'checked_at': utc_now(),
                      'evidence': evidence})
        return basis

    def _professional_basis(self, case: dict[str, Any], basis: dict[str, Any],
                            decision_id: str | None) -> dict[str, Any]:
        """专业依据必须：存在、已生效、**属于本事项**、版本适用、动作允许完成、
        且来源是真实专业人员。

        现在项目里没有连接真实医护服务，所以来自本地模拟工作台的决定会被明确
        拒绝——把它包装成"专业医疗确认"是这一整条路径上最危险的谎。
        """
        if not decision_id:
            raise ProductError('专业复核依据必须指明复核决定', 409)
        row = self.memory.connection.execute(
            "SELECT decision_id, case_id, case_revision, action, outcome, actor_id, payload_json "
            "FROM review_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if row is None:
            raise ProductError('复核决定不存在', 404)
        if row['outcome'] == 'review_stale':
            raise ProductError('该复核决定作出时事实已移动，不能用来批准当前状态', 409)
        if row['outcome'] != 'applied':
            raise ProductError('复核决定尚未生效', 409)
        if row['action'] not in CLOSING_REVIEW_ACTIONS:
            raise ProductError(
                f'该复核决定的动作是「{row["action"]}」，它不允许完成事项；'
                f'只有 {"/".join(CLOSING_REVIEW_ACTIONS)} 可以', 409)
        review_case = self.memory.connection.execute(
            "SELECT summary_json, fact_scope_revision FROM review_cases WHERE id=?",
            (row['case_id'],)).fetchone()
        summary = _load_json(review_case['summary_json']) if review_case else {}
        if summary.get('simulated') or str(row['actor_id'] or '') in SIMULATED_REVIEW_SOURCES:
            raise ProductError(
                '该复核来自本地模拟工作台，不是真实医护服务；'
                '不能作为专业医疗确认，请勿据此关闭事项', 409)
        # 必须**属于本事项**：复核决定要能指回这件事。
        linked = {str(item) for item in (case.get('linked_review_case_ids') or ())}
        if str(row['case_id']) not in linked:
            raise ProductError(
                '该复核决定不是针对本事项作出的，不能用来关闭这一件事', 409)
        # 必须**适用于当前版本**：决定作出时看到的事实与现在必须一致。
        recorded = review_case['fact_scope_revision'] if review_case else None
        current = self.p.revisions().get('medications', 0) + self.p.revisions().get('semantic', 0)
        if recorded is not None and int(recorded) != int(current):
            raise ProductError(
                '复核决定作出后用药或事实发生了变化，该决定不再适用于当前状态', 409)
        basis.update({'decision_id': decision_id, 'review_case_id': row['case_id'],
                      'review_action': row['action'],
                      'review_actor': row['actor_id'],
                      'fact_scope_revision': recorded})
        return basis


# ---- 读模型：主页面的分组与单事项详情 ----------------------------------------
def _resolve(store, ref: str) -> dict[str, Any]:
    """把一条引用还原成可以展示的一行。解析不了的引用**照实说明**，不隐藏。"""
    try:
        resolved = store.memory.resolve_ref(ref)
    except Exception:
        return {'ref': ref, 'label': ref, 'available': False}
    row = dict(resolved.get('row') or {})
    label = next((row[key] for key in ('display_name', 'text', 'description', 'value_json', 'value')
                  if row.get(key)), ref)
    return {'ref': ref, 'label': str(label), 'available': True,
            'layer': resolved.get('layer'), 'version': row.get('version')}


def _conclusion_view(store, ref: str) -> dict[str, Any]:
    """一条结论给界面看的样子。

    ``state`` 是结论自身是否还有效；``trigger_state`` 是**本事项的触发条件**在
    当前权威记录下是否仍然成立。两者不是一回事：一条 `current` 的结论完全可以
    是"风险仍然成立"。界面必须分得开，用户才不会把"检查跑了"读成"没事了"。
    """
    match = re.fullmatch(r"memory:conclusion:(\d+)(?:@v\d+)?", str(ref))
    if not match:
        return {'ref': ref, 'available': False}
    head = resolve_chain_head(store.memory, int(match.group(1)))
    row = store.memory.connection.execute(
        "SELECT id, kind, text, status, stale_reason, source_refs_json, input_revision "
        "FROM conclusions WHERE id=?", (head,)).fetchone()
    if row is None:
        return {'ref': ref, 'available': False}
    verdict = store.memory.evaluate_trigger(head)
    return {'ref': f'memory:conclusion:{row["id"]}@v1', 'original_ref': ref,
            'conclusion_id': row['id'], 'kind': row['kind'],
            'text': row['text'], 'status': row['status'],
            'state': row['status'],
            'trigger_state': verdict['state'], 'trigger_reasons': verdict['reasons'],
            'version_applies': (_load_json(row['input_revision']) or {}) == store.p.revisions(),
            'stale_reason': row['stale_reason'],
            'sources': [source.get('uri') for source in (_load_json(row['source_refs_json']) or [])
                        if isinstance(source, dict) and source.get('uri')]}


def case_view(store: SafetyCaseStore, case: dict[str, Any]) -> dict[str, Any]:
    """一个事项的完整视图：触发原因、涉及记录、已知信息、依据、进展、未决、下一步、历史。"""
    inputs = case.get('required_inputs') or []
    return {
        'case_id': case['id'],
        'case_type': case['case_type'],
        'status': case['current_status'],
        'status_label': STATUS_LABELS.get(case['current_status'], case['current_status']),
        'subject_keys': case.get('subject_keys') or [],
        'trigger': (case.get('history') or [{}])[0].get('trigger'),
        'medications': [_resolve(store, ref) for ref in case.get('related_medication_refs') or ()],
        'facts': [_resolve(store, ref) for ref in case.get('relevant_fact_refs') or ()],
        'conclusions': [_conclusion_view(store, ref)
                        for ref in case.get('linked_conclusion_refs') or ()],
        'evidence_refs': case.get('evidence_refs') or [],
        'open_questions': [q for q in case.get('open_questions') or []],
        # 三种"补充状态"分开给：还在等回答 / 用户明说不知道（不再等他，但**没有**
        # 解决）/ 已回答。混成一个列表，界面就只能把"不知道"画成"已回答"。
        'required_inputs': [dict(item) for item in inputs
                            if item.get('status') == 'open'
                            and not item.get('needs_alternative_evidence')],
        'unanswered_by_user': [dict(item) for item in inputs
                               if item.get('status') == ANSWER_UNKNOWN
                               or (item.get('status') == 'open'
                                   and item.get('needs_alternative_evidence'))],
        # 已答的请求不作为"待补充"出现，但**它补上了什么**必须看得见：
        # 只显示一句"已补充 N 条"，用户仍然不知道查清了哪一部分。
        'answered_inputs': [dict(item) for item in inputs
                            if item.get('status') == 'answered'],
        'answered_inputs_count': sum(1 for item in inputs if item.get('status') == 'answered'),
        # 投影是只读的：拿不出确认记录的 `confirmed=True` 一律按 `false` 读。
        # 在此之前"给了一个时间"会被读成"已确认"，而那个确认从来没有人做过。
        'follow_up': _follow_up.project_follow_up(case.get('follow_up')),
        'linked_review_case_ids': case.get('linked_review_case_ids') or [],
        'linked_run_ids': case.get('linked_run_ids') or [],
        'next_action_summary': case.get('next_action_summary'),
        'responsible_party': case.get('responsible_party'),
        'resolution_basis': case.get('resolution_basis'),
        'disposition': case.get('disposition'),
        'user_seen_at': case.get('user_seen_at'),
        'input_versions': case.get('input_versions') or {},
        'revision': case['revision'],
        'created_at': case['created_at'],
        'updated_at': case['updated_at'],
        'history': list(case.get('history') or []),
    }


def safety_mainline(product, memory) -> dict[str, Any]:
    """主页面要的六块内容，顺序就是用户读它的顺序。

    "已读"只用来排序和提示，**不**决定任何一块是否出现：未解决的事项不会因为
    用户点过"知道了"就从"需要关注"里消失。
    """
    store = SafetyCaseStore(product)
    cases = [case_view(store, case) for case in store.objects()]
    medications = memory.current_medications()
    recent = memory.retrieve_episodic(
        event_types=['medication_add', 'medication_remove', 'medication_dose_change'], limit=20)
    settled = [c for c in cases if c['status'] == STATUS_RESOLVED]
    settled.sort(key=lambda c: c['updated_at'], reverse=True)
    return {
        'generated_at': utc_now(),
        'current_medications': medications,
        'recent_medication_changes': recent,
        'attention': [c for c in cases if c['status'] in
                      (STATUS_OPEN, STATUS_INVESTIGATING, STATUS_EXECUTION_FAILED)],
        'awaiting_user': [c for c in cases if c['status'] == STATUS_AWAITING_USER],
        'awaiting_professional': [c for c in cases if c['status'] == STATUS_AWAITING_PROFESSIONAL],
        'needs_recheck': [c for c in cases if c['status'] == STATUS_NEEDS_RECHECK],
        'recently_settled': settled[:10],
        'counts': {
            'attention': sum(1 for c in cases if c['status'] in
                             (STATUS_OPEN, STATUS_INVESTIGATING, STATUS_EXECUTION_FAILED)),
            'awaiting_user': sum(1 for c in cases if c['status'] == STATUS_AWAITING_USER),
            'awaiting_professional': sum(1 for c in cases
                                         if c['status'] == STATUS_AWAITING_PROFESSIONAL),
            'needs_recheck': sum(1 for c in cases if c['status'] == STATUS_NEEDS_RECHECK),
            'settled': len(settled),
        },
        # 必要检查队列的真实状态——"检查跑没跑"必须可见，而不是靠"没看到提示"推断。
        'necessary_checks': _check_queue_state(memory),
    }


def _check_queue_state(memory) -> dict[str, Any]:
    try:
        row = memory.connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open, "
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
            "MAX(updated_at) AS last_at FROM necessary_checks").fetchone()
    except Exception:
        return {'available': False}
    return {'available': True, 'total': int(row['total'] or 0),
            'open': int(row['open'] or 0), 'running': int(row['running'] or 0),
            'failed': int(row['failed'] or 0), 'last_at': row['last_at'],
            'note': ('未运行时检查队列不会自动推进：需要后台 worker。'
                     '失败或未完成的检查不表示风险已排除。')}


def _sync_answer_to_investigation(product, case_id: str, request_id: str,
                                  value: Any, key: str | None) -> None:
    """把用户刚提交的回答写回 investigation 里对应的那条问题。

    性质是 ``user_reported``——它是**记录**，不是临床确认，所以信息状态停在
    "收到但未确认"，问题继续保持未决直到有适用依据。事实写入仍走既有的受控
    确认路径，不经过这里。
    """
    from .care_tasks import CareTasks
    try:
        tasks = CareTasks(product)
        active = [t for t in product.objects('care_task')
                  if t.get('goal_type') == 'safety_case'
                  and t.get('safety_case_id') == case_id]
        for task in reversed(active):
            if not task.get('investigation'):
                continue
            with product.transaction():
                fresh = product.get(task['id'], 'care_task')
                tasks._sync_answers_to_investigation(
                    fresh, [request_id], {request_id: {'value': value}}, None, key)
                fresh['revision'] += 1
                product.save('care_task', fresh)
            # 立刻把问题的进度投回事项：用户提交完就该看到"这一答补上了哪一部分、
            # 还剩什么不确定"，而不是等下一次调查跑完。（它自己开事务，所以必须
            # 在上面那个事务**之外**调用——嵌套会被拒绝并把整笔回滚掉。）
            tasks._sync_questions_to_case(
                SafetyCaseStore(product), case_id,
                product.get(task['id'], 'care_task').get('investigation') or {})
            return
    except Exception:
        # 回答已经落盘；同步失败不回滚它，也不假装同步成功。
        import logging
        logging.getLogger(__name__).warning('answer->investigation sync failed',
                                            exc_info=True)


def _wake_investigation(product, case_id: str, key: str | None) -> dict[str, Any] | None:
    """补充到达后，把等待中的调查**经既有队列**唤醒。

    用的是现有 `care_task` + outbox + 租约 + 预算那一条路——没有新的调度框架。
    只唤醒**确实在等待补充**的那一件；其它状态不动（跑着的调查由它自己的租约管，
    终态的不会因为用户多说一句话就复活）。

    唤醒失败**不影响**刚刚保存的回答：回答已经在自己的事务里提交了。这里返回
    失败原因，让调用方（和界面）如实知道"补充已存下、但这轮还没能继续"。
    """
    from .care_tasks import CareTasks, REVIEW_GOAL_TYPES
    try:
        tasks = CareTasks(product)
        waiting = [t for t in product.objects('care_task')
                   if t.get('goal_type') == 'safety_case'
                   and t.get('safety_case_id') == case_id
                   and t['status'] == 'waiting_input']
        if not waiting:
            return None
        task = waiting[-1]
        tasks.resume(task['id'], f"{key or task['id']}:wake:{task['revision']}",
                     task['revision'], 'continue', enqueue=True)
        return {'woke': task['id']}
    except Exception as exc:  # 唤醒是尽力而为；回答本身已经落盘
        return {'woke': None, 'reason': f'{type(exc).__name__}: {exc}'}


def register_safety_routes(app, product, access, invoke, principal=None,
                           require_role=None):
    """安全事项的主线接口：列表、详情、已读、补充、处置。

    处置的操作者与角色一律来自**认证上下文**解析出的 principal——请求体里的
    ``actor`` 字段不被读取。自报的身份不是身份。
    """
    from fastapi import Request
    globals()['Request'] = Request

    def _actor(request):
        who = principal(request) if principal is not None else None
        return (str(getattr(who, 'user_id', 'local-demo-caregiver')),
                sorted(getattr(who, 'roles', ()) or ()))

    @app.get('/v1/safety-mainline')
    def mainline(request: Request):
        access(request)
        return invoke(lambda: safety_mainline(product, product.memory))

    @app.get('/v1/safety-cases')
    def list_cases(request: Request):
        access(request)
        store = SafetyCaseStore(product)
        return invoke(lambda: {'items': [case_view(store, c) for c in store.objects()],
                               'statuses': STATUS_LABELS})

    @app.get('/v1/safety-cases/{case_id}')
    def get_case(case_id: str, request: Request):
        access(request)
        return invoke(lambda: case_view(SafetyCaseStore(product),
                                        SafetyCaseStore(product).get(case_id)))

    @app.post('/v1/safety-cases/{case_id}/seen')
    def mark_seen(case_id: str, request: Request, body: dict):
        """用户看到提示。**只**记录时间戳——不关闭、不降级、不清空未决项。"""
        access(request, True)
        store = SafetyCaseStore(product)
        return invoke(lambda: case_view(store, store.mark_seen(
            case_id, command_key=body.get('key'))))

    @app.get('/v1/safety-cases/{case_id}/closure-evidence')
    def closure_evidence(case_id: str, request: Request):
        """关闭前先给界面看的现场核对结果（只读，不写入）。"""
        access(request)
        store = SafetyCaseStore(product)
        return invoke(lambda: store.closure_evidence(store.get(case_id)))

    @app.post('/v1/safety-cases/{case_id}/disposition')
    def disposition(case_id: str, request: Request, body: dict):
        access(request, True)
        store = SafetyCaseStore(product)
        actor, roles = _actor(request)
        return invoke(lambda: case_view(store, store.disposition(
            case_id, expected_revision=body.get('expected_revision'),
            disposition=body.get('disposition') or DISPOSITION_RESOLVED,
            # 持续跟进没有"依据"可填，缺省即合法；其余动作缺省按确定性检查依据处理。
            basis_kind=body.get('basis_kind') or (
                'monitoring_arrangement'
                if body.get('disposition') == DISPOSITION_MONITORING else BASIS_CHECK),
            actor=actor, roles=roles, note=body.get('note'),
            decision_id=body.get('decision_id'), follow_up=body.get('follow_up'),
            command_key=body.get('key'))))

    @app.post('/v1/safety-cases/{case_id}/follow-up')
    def follow_up(case_id: str, request: Request, body: dict):
        """安排 / 改期 / 取消一条长期跟进。**同一路径，靠 `action` 分流。**

        请求体里的 `confirmed` 一律不读：确认是一次单独的、有记录的承诺，走
        `.../follow-up/confirmation`。给了时间就自动算"已确认"是这里要修掉的缺陷。
        """
        access(request, True)
        store = SafetyCaseStore(product)
        actor, _roles = _actor(request)
        action = body.get('action') or 'schedule'
        if action == 'schedule':
            return invoke(lambda: case_view(store, store.schedule_follow_up(
                case_id, expected_revision=body.get('expected_revision'),
                kind=body.get('kind') or 'arrangement', at=body.get('at'),
                condition=body.get('condition'), owner=body.get('owner'),
                note=body.get('note'), actor=actor,
                command_key=body.get('key'))))
        if action == 'cancel':
            return invoke(lambda: case_view(store, store.cancel_follow_up(
                case_id, expected_revision=body.get('expected_revision'),
                reason=body.get('reason'), actor=actor,
                command_key=body.get('key'))))
        # 未知动作是调用方错误，不是"什么都不做"。
        raise ProductError(f'不支持的跟进动作：{action!r}', 422)

    @app.post('/v1/safety-cases/{case_id}/follow-up/confirmation')
    def confirm_follow_up(case_id: str, request: Request, body: dict):
        """确认一条已安排的跟进安排。

        `confirmed_by` 取**认证主体**——请求体里的自称不被读取。自报的身份不是身份。
        """
        access(request, True)
        store = SafetyCaseStore(product)
        actor, _roles = _actor(request)
        return invoke(lambda: case_view(store, store.confirm_follow_up(
            case_id, expected_revision=body.get('expected_revision'),
            actor=actor, note=body.get('note'), command_key=body.get('key'))))

    @app.post('/v1/safety-cases/{case_id}/answer')
    def answer(case_id: str, request: Request, body: dict):
        """回答事项上的一条补问。

        空值与"不知道"走的是不同路径：空值什么都不关；"不知道"结束追问但保留
        不确定性，并把下一步交回系统去找替代证据。
        """
        access(request, True)
        store = SafetyCaseStore(product)
        actor, _roles = _actor(request)
        def run():
            case = store.get(case_id)
            if case['revision'] != body.get('expected_revision'):
                raise ProductError('事项已被其他操作更新，请刷新', 409)
            request_id = str(body.get('request_id') or '')
            updated = store.record_input(
                case_id, request_id=request_id,
                answer_ref=f'answer:{body.get("key")}',
                value=body.get('value'), answer_kind=body.get('answer_kind'),
                command_key=body.get('key'))
            # 同一次提交也要落到**调查里的那条问题**上：只写事项的话，
            # investigation 的问题永远停在未决，下一轮又把同一件事问一遍。
            _sync_answer_to_investigation(product, case_id, request_id,
                                          body.get('value'), body.get('key'))
            _wake_investigation(product, case_id, body.get('key'))
            return case_view(store, updated)
        return invoke(run)


def _load_json(raw: Any) -> Any:
    import json
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# ---- 从结论收敛出事项 --------------------------------------------------------
def observe_conclusion(product, conclusion: dict[str, Any], *,
                       evidence_refs: Sequence[str] = (),
                       trigger: dict[str, Any] | None = None,
                       key: str | None = None) -> dict[str, Any]:
    """把一条（必要检查产生的）结论记到它所属的事项上。

    这是"安全事项"与既有结论之间的**唯一**连接点：事项引用结论，不复制结论。
    """
    store = SafetyCaseStore(product)
    case_type, subjects = subject_keys_for_conclusion(product.memory, conclusion)
    medication_refs = [ref for ref in (conclusion.get('memory_refs') or ())
                       if str(ref).startswith('memory:medication:')]
    anchor = episode_anchor(product, medication_refs)
    ref = conclusion.get('ref') or f"memory:conclusion:{conclusion['id']}@v1"
    case, _ = store.open_or_update(
        key or f"necessary-check:{conclusion['id']}",
        case_type=case_type, subject_keys=subjects, anchor=anchor,
        trigger={'kind': 'conclusion_recorded', 'ref': ref,
                 'conclusion_kind': conclusion.get('kind'),
                 **(trigger or {})},
        medication_refs=medication_refs,
        evidence_refs=list(evidence_refs) + [
            source.get('uri') for source in (conclusion.get('source_refs') or [])
            if isinstance(source, dict) and source.get('uri')],
        conclusion_refs=[ref],
        next_action_summary='已记录检查结果，可继续调查或补充信息',
        responsible_party='agent')
    return case
