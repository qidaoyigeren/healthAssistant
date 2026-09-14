"""回访（visit）：把"回来继续跟进"做成一次有原因、有重点、有结果、可恢复的业务单元。

**这不是第二套事项系统。** 一件安全事项仍然只有一条记录；回访是**关于它**的一段
过程记录，只存三类东西：

1. **引用**——case_id、care_task_id、question_id/request_id、候选变更 id；
2. **游标**——这次回访开始时事项历史走到哪、结束时走到哪；
3. **渲染后的叙述**——本次为什么跟进、相对上次新增了什么、做完了什么、还剩什么、
   下一步是什么。每条陈述带 `basis`，说明它是程序核对的结果、用户的报告，
   还是模型的解释。

**它不存患者事实、药单、结论或证据的副本。** 那些一律现取（`investigation_context`
与 `case_view` 已经在做），所以这份记录永远不会和权威记录分叉。

两条写死在实现里、并有断言钉住的规矩：

* **"没有新记录"只能写成"系统尚未收到新记录"**，绝不定性成"情况稳定"或"风险已解除"。
  这两句话差别很大：前者说的是系统的信息状态，后者是一句没有人做过的判断。
* **候选变更在没有被确认之前，权威记录一个字节都不动。** 用户自己声明的和模型从
  自由文本里抽出来的，走的是同一个待确认队列、同一个确认入口、同一条权威写入通道。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Sequence

from .product import ProductError, SCOPE, packed
from .memory import utc_now

KIND = 'review_visit'

#: 一次回访的四种由来（需求 III.1）。服务端**按事实判定**，不由调用方自报——
#: 否则每次都可以说成"用户主动发起"，真正到期该跟进的那件事就永远不会被算进来。
REASON_DUE = 'due'                      # 已确认的安排到期
REASON_RECORD_CHANGE = 'record_change'  # 相关记录发生变化
REASON_INPUT_ARRIVED = 'input_arrived'  # 新补充到达
REASON_USER_STARTED = 'user_started'    # 用户主动发起
REASON_KINDS = (REASON_DUE, REASON_RECORD_CHANGE, REASON_INPUT_ARRIVED,
                REASON_USER_STARTED)

STATUS_OPEN = 'open'
STATUS_AWAITING = 'awaiting_user'
STATUS_COMPLETED = 'completed'
STATUS_BLOCKED = 'blocked'
VISIT_STATUSES = (STATUS_OPEN, STATUS_AWAITING, STATUS_COMPLETED, STATUS_BLOCKED)

CANDIDATE_PENDING = 'pending'
CANDIDATE_CONFIRMED = 'confirmed'
CANDIDATE_DISMISSED = 'dismissed'
#: 被**另一条候选**取代（例如一次纠正撤回了先前那条）。与"用户放弃"分开：
#: 前者是"这条不再代表用户的说法了"，后者是"用户看过并决定不要它"。
CANDIDATE_SUPERSEDED = 'superseded'
CANDIDATE_STATUSES = (CANDIDATE_PENDING, CANDIDATE_CONFIRMED, CANDIDATE_DISMISSED,
                      CANDIDATE_SUPERSEDED)

#: 候选的来源。必须在界面上可见——用户要能分出"这是你说的"和"这是模型猜的"。
SOURCE_USER_DECLARED = 'user_declared'
SOURCE_MODEL_PROPOSED = 'model_proposed'
CANDIDATE_SOURCES = (SOURCE_USER_DECLARED, SOURCE_MODEL_PROPOSED)

#: 候选只能改的字段。与 `investigation.RECORD_COLUMNS` 同一组：能核对才谈得上确认。
CANDIDATE_FIELDS = ('dose', 'schedule', 'route', 'start_at')

#: 候选表达的**操作**。``changes`` 说改哪个字段，``operation`` 说这是哪一件事——
#: "停用一个药"和"把剂量改成 0"在字段层面看着像，在业务上是两回事。
CANDIDATE_ADD = 'add'
CANDIDATE_STOP = 'remove'
CANDIDATE_DOSE_CHANGE = 'dose_change'
CANDIDATE_RESUME = 'resume'
CANDIDATE_CORRECTION = 'correction'
CANDIDATE_OPERATIONS = (CANDIDATE_ADD, CANDIDATE_STOP, CANDIDATE_DOSE_CHANGE,
                        CANDIDATE_RESUME, CANDIDATE_CORRECTION)

#: 必须**指向一条已存在的记录**的操作。``add`` 是唯一不需要的。
OPERATIONS_NEEDING_TARGET = (CANDIDATE_STOP, CANDIDATE_DOSE_CHANGE,
                             CANDIDATE_RESUME, CANDIDATE_CORRECTION)

#: 该操作要求目标记录当前处于什么状态。确认时在写入事务内核对。
OPERATION_REQUIRES_STATUS = {CANDIDATE_STOP: 'active', CANDIDATE_DOSE_CHANGE: 'active',
                             CANDIDATE_RESUME: 'stopped', CANDIDATE_CORRECTION: 'stopped'}

CANDIDATE_OPERATION_LABELS = {
    CANDIDATE_ADD: '新增用药', CANDIDATE_STOP: '停用', CANDIDATE_DOSE_CHANGE: '调整用法',
    CANDIDATE_RESUME: '恢复服用', CANDIDATE_CORRECTION: '纠正记录',
}

CANDIDATE_FIELD_LABELS = {'dose': '剂量', 'schedule': '服用频次',
                          'route': '给药途径', 'start_at': '开始时间'}

#: 换药：一组有关联的变更。两条各自说明自己发生了什么，不互相代言。
GROUP_REPLACE_FROM = 'replace_from'
GROUP_REPLACE_TO = 'replace_to'
GROUP_ROLES = (GROUP_REPLACE_FROM, GROUP_REPLACE_TO)

#: 时间表达的精度。``reported_vague`` 表示"用户说了个大概"，**不解析成具体日期**。
TIME_PRECISIONS = ('exact', 'day', 'week', 'month', 'vague', 'unknown')

#: "没有新记录"的**唯一**允许写法。渲染函数从这里取，测试也断言这一句。
NO_NEW_RECORDS = ('系统尚未收到新记录；这不等于情况没有变化，也不表示风险已经解除。')

#: "上次之后发生了什么"里**算数**的事件：信息到达，或记录本身改变。
#:
#: 事项自身的生命周期事件（``status_changed`` / ``input_requested`` / ``opened`` /
#: ``reopened``）**不算**。它们是上面这些事件的**后果**，混进来会把"系统刚刚做了
#: 什么"报成"患者那里发生了什么"——一次刚建立的回访会立刻显示"上次之后有新情况"，
#: 而实际上一个字的新信息都没有。取消回访、事项被复核推进这类系统动作也在此列。
NEWS_EVENTS = ('input_recorded', 'answer_retired', 'resolution_basis_retired', 'disposition')


def news_entries(case: dict[str, Any], start: int) -> list[dict[str, Any]]:
    """游标之后**算数**的那些事件。定义只有这一处，读它的人共用同一口径。"""
    return [entry for entry in (case.get('history') or ())[int(start or 0):]
            if entry.get('event') in NEWS_EVENTS]


class ReviewVisitStore:
    """回访记录的读写。只碰 `product_objects` 里的 `review_visit`，不碰任何真相源。"""

    def __init__(self, product):
        self.p = product

    # ---- 读 ---------------------------------------------------------------
    def objects(self) -> list[dict[str, Any]]:
        return self.p.objects(KIND)

    def get(self, visit_id: str) -> dict[str, Any]:
        for visit in self.objects():
            if visit['id'] == visit_id:
                return visit
        raise ProductError('回访记录不存在或不属于当前患者', 404)

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        """这一事项上的全部回访，**按开始时间升序**。

        顺序必须由这里定死：`objects()` 的返回顺序取决于存储，而"第几次回访"、
        "上一次是哪一次"都按位置数。不定序的话，同一个库读两次可能给出不同的
        "第二次"。同一微秒开的两访用 id 兜底，至少是**稳定**的。
        """
        return sorted((visit for visit in self.objects() if visit['case_id'] == case_id),
                      key=lambda visit: (str(visit.get('opened_at') or ''), visit['id']))

    def open_for_case(self, case_id: str) -> dict[str, Any] | None:
        """这一事项上**还没结束**的那次回访。

        "继续本次跟进"靠的就是它：已有未结束的回访就接着它走，不新开一次——
        新开会让用户已经答过的问题变成上一访的遗留，他回来时看到的第一题又是原来那题。
        """
        for visit in self.for_case(case_id):
            if visit['status'] in (STATUS_OPEN, STATUS_AWAITING):
                return visit
        return None

    def last_closed_for_case(self, case_id: str) -> dict[str, Any] | None:
        closed = [visit for visit in self.for_case(case_id)
                  if visit['status'] in (STATUS_COMPLETED, STATUS_BLOCKED)]
        return closed[-1] if closed else None

    # ---- 写 ---------------------------------------------------------------
    def open_or_continue(self, key: str, *, case_id: str, reason: dict[str, Any],
                         actor: str, history_length: int = 0,
                         ) -> tuple[dict[str, Any], bool]:
        """开始或继续一次回访。`key` 是调用方的幂等键。返回 ``(visit, created)``。

        继续是**幂等**的：不新开记录、不改 `opened_at`、不动 `cursor.before`。
        本次为什么跟进则**允许更新**——上一次判定为"用户主动发起"，这中间记录真的
        变了，后来居上的事实更强的理由应当覆盖它。
        """
        created_holder: dict[str, bool] = {}

        def execute():
            existing = self.open_for_case(case_id)
            created_holder['created'] = existing is None
            if existing is not None:
                if _reason_rank(reason['kind']) > _reason_rank(existing['reason']['kind']):
                    existing['reason'] = dict(reason)
                existing['updated_at'] = utc_now()
                existing['revision'] += 1
                self.p.save(KIND, existing)
                return existing
            previous = self.last_closed_for_case(case_id)
            visit = {
                'id': f'review-visit:{uuid.uuid4().hex}', 'visit_id': None,
                # **写死的序号**。不能用"按 opened_at 排序后的位置"当第几次：两次
                # 回访落在同一秒里时（测试与真实操作都会），平局由随机 uuid 决定，
                # 于是"第二次"可能被数成"第一次"，而这个数字会直接写进给模型的
                # 目标文本。序号在创建这一刻定死，此后永不重算。
                'sequence': len(self.for_case(case_id)) + 1,
                'scope_id': SCOPE, 'case_id': case_id, 'care_task_id': None,
                'opened_at': utc_now(), 'opened_by': actor, 'closed_at': None,
                'reason': dict(reason), 'status': STATUS_OPEN,
                # "上次之后"的起点：上一次回访**结束**的位置。
                #
                # 第一次回访没有"上次"，起点就是**这次开始的位置**——不是事项历史的
                # 开头。事项从建立起发生了什么属于"为什么现在跟进"（`reason`），
                # 不属于"上次之后新增了什么"；混在一起，第一次回访会把整件事项的
                # 来龙去脉当成"新变化"报一遍。
                'cursor': {
                    # 起点：上一次回访**结束**的位置。上一次没走到落结果那一步
                    # （例如它被阻塞）时，`after` 是空的——那就退回**它开始的位置**，
                    # 而不是 0：退回 0 会把整件事项的来龙去脉重报一遍，而它的起因
                    # 已经写在 `reason` 里了。
                    'before': int((previous or {}).get('cursor', {}).get('after')
                                  or ((previous or {}).get('cursor', {}).get('opened_at_history')
                                      if previous is not None else history_length)),
                    'opened_at_history': int(history_length),
                    'after': None,
                    # 开始那一刻的记录版本。**必须在这里存**：事项自己的
                    # `input_versions` 会被"造成变化的那次改动"顺手刷新，事后再看
                    # 什么都比不出来；回访自己留一份，下一次才说得清"这中间变了什么"。
                    'versions': dict(self.p.revisions())},
                'previous_visit_id': (previous or {}).get('id'),
                'focus': [], 'change_candidates': [],
                'result': None, 'revision': 1, 'created_at': utc_now(),
                'updated_at': utc_now(),
            }
            visit['visit_id'] = visit['id']
            self.p.save(KIND, visit)
            return visit
        visit = self.p.command(
            f'{case_id}:visit:{key}',
            {'type': 'review_visit_open', 'case_id': case_id,
             'reason_kind': reason['kind']}, execute)
        return visit, bool(created_holder.get('created'))

    def bind_task(self, visit_id: str, task_id: str) -> dict[str, Any]:
        """把这次回访挂到执行它的 care_task 上（引用，不复制任务状态）。"""
        def execute():
            visit = self.get(visit_id)
            if visit.get('care_task_id') == task_id:
                return visit
            visit['care_task_id'] = task_id
            visit['updated_at'] = utc_now()
            visit['revision'] += 1
            self.p.save(KIND, visit)
            return visit
        return self.p.command(f'{visit_id}:task:{task_id}',
                              {'type': 'review_visit_bind', 'visit_id': visit_id,
                               'task_id': task_id}, execute)

    def set_status(self, visit_id: str, status: str, *, reason: str | None = None,
                   command_key: str | None = None) -> dict[str, Any]:
        """推进回访状态。幂等键带**当时的 revision**——同一个 key 配不同内容会 409，
        而"再次进入 awaiting_user"是合法的状态转移，不能用固定键把它挡掉。"""
        if status not in VISIT_STATUSES:
            raise ProductError('不支持的回访状态')

        def execute():
            visit = self.get(visit_id)
            if visit['status'] == status:
                return visit
            visit['status'] = status
            visit['status_reason'] = reason
            if status in (STATUS_COMPLETED, STATUS_BLOCKED):
                visit['closed_at'] = utc_now()
            visit['updated_at'] = utc_now()
            visit['revision'] += 1
            self.p.save(KIND, visit)
            return visit
        return self.p.command(
            command_key or f'{visit_id}:status:{status}:{self.get(visit_id)["revision"]}',
            {'type': 'review_visit_status', 'visit_id': visit_id,
             'status': status}, execute)

    def save_result(self, visit_id: str, result: dict[str, Any], *,
                    focus: Sequence[dict[str, Any]] = (),
                    cursor_after: int | None = None,
                    command_key: str | None = None) -> dict[str, Any]:
        """落一次回访的**结果**。结果由调用方从既有真相源渲染，这里只存下来。"""
        def execute():
            visit = self.get(visit_id)
            visit['result'] = dict(result)
            visit['focus'] = [dict(item) for item in focus]
            if cursor_after is not None:
                visit['cursor'] = {**visit.get('cursor', {}), 'after': int(cursor_after)}
            visit['updated_at'] = utc_now()
            visit['revision'] += 1
            self.p.save(KIND, visit)
            return visit
        # 幂等键带**当时的 revision**。`save_result` 会被调用多次（调查跑完一次、
        # 用户确认一条候选之后再算一次），固定的键会让第二次直接命中第一次的回执，
        # 新结果被**静默丢弃**——用户看到"已确认"，却读不到这次改变了什么。
        return self.p.command(
            command_key or f'{visit_id}:result:{self.get(visit_id)["revision"]}',
            {'type': 'review_visit_result', 'visit_id': visit_id}, execute)

    # ---- 候选变更：两个来源，一个待确认队列 ---------------------------------
    def add_candidate(self, visit_id: str, *, name: str, field: str, before: Any,
                      after: Any, source: str, basis: dict[str, Any],
                      command_key: str | None = None) -> dict[str, Any]:
        """登记一条**待确认**的用药变更候选（字段形态，等价于一次"调整用法"）。

        保留这个入口是为了既有的结构化声明路径不变；操作语义版见
        ``add_change_candidate``。两者写的是**同一个队列**，不是两套候选。
        """
        if field not in CANDIDATE_FIELDS:
            raise ProductError('候选只能针对可核对的用药字段')
        target = self.resolve_target(name)
        return self.add_change_candidate(
            visit_id,
            spec={'operation': CANDIDATE_DOSE_CHANGE, 'target': target,
                  'changes': {field: after}, 'before': {field: before},
                  'before_ref': target.get('record_ref'),
                  'origin': {'kind': 'structured'}},
            source=source,
            basis=basis,
            # 幂等键带**提议的值与提议时依据的那一版**：同一味药同一字段再提一个
            # 新值是另一件请求；而"记录变了、用户按新基准重新声明同一个新值"也
            # 必须是另一件请求——否则第二次声明会命中旧回执被静默丢弃（这个项目
            # 已经在这类键上栽过三次）。
            command_key=command_key or (
                f'{visit_id}:candidate:{str(name).strip()}:{field}:{after}:{before}'),
        )

    def resolve_target(self, name: str, *, status: str = 'active') -> dict[str, Any]:
        """把药名解析成**具体的记录引用**。

        名称、别名只用于**解析与展示**——真正写进记录的是记录 ID + 版本。
        解析不到就如实标 ``unmatched``，让上层去补问，而不是猜一个。
        """
        wanted = str(name or '').strip()
        rows = self.p.memory.connection.execute(
            "SELECT * FROM medications WHERE status=? ORDER BY version DESC",
            (status,)).fetchall()
        matches = [row for row in rows if row['display_name'] == wanted]
        if not matches:
            return {'name': wanted, 'matched_by': 'unmatched'}
        item = matches[0]
        return {'name': item['display_name'], 'matched_by': 'current',
                'record_id': int(item['id']), 'record_version': int(item['version']),
                'record_ref': f"memory:medication:{item['id']}@v{item['version']}",
                'scope_id': SCOPE, 'episode_id': item['episode_id']}

    def add_change_candidate(self, visit_id: str, *, spec: dict[str, Any], source: str,
                             basis: dict[str, Any],
                             command_key: str | None = None) -> dict[str, Any]:
        """登记一条**待确认**的用药变更候选（操作语义）。**不写权威记录。**

        `source` 是 `user_declared` 还是 `model_proposed` 必须如实标出：两者的
        可信程度不同，界面必须让用户看得出来，确认的人才知道自己在确认什么。
        """
        if source not in CANDIDATE_SOURCES:
            raise ProductError('候选来源只能是用户声明或模型提议')
        spec = normalise_candidate_spec(spec)
        identity = candidate_identity(spec)

        def execute():
            visit = self.get(visit_id)
            candidates = list(visit.get('change_candidates') or [])
            pending = [item for item in candidates
                       if item['status'] == CANDIDATE_PENDING
                       and candidate_identity(item) == identity]
            if pending:
                # 同一味药上的同一件事已经有一条待确认的候选：更新它，不排队第二条。
                # 两条并存会让用户确认一条之后记录与另一条对不上。
                candidate = pending[0]
                candidate.update({key: value for key, value in spec.items()
                                  if key not in ('before',)})
                candidate['before'] = spec.get('before') or candidate.get('before')
                candidate['source'] = source
                candidate['basis'] = dict(basis)
                candidate['revision'] = int(candidate.get('revision') or 1) + 1
            else:
                candidate = {
                    'id': f"change-candidate:{uuid.uuid4().hex}",
                    'visit_id': visit_id, 'case_id': visit['case_id'],
                    **{key: value for key, value in spec.items() if key != 'before'},
                    'before': spec.get('before'),
                    'source': source, 'basis': dict(basis),
                    'status': CANDIDATE_PENDING, 'recorded_at': utc_now(),
                    'decided_at': None, 'decided_by': None, 'applied': None,
                    'conflict': None, 'superseded_by': None, 'revision': 1}
                candidates.append(candidate)
            visit['change_candidates'] = candidates
            visit['updated_at'] = utc_now()
            visit['revision'] += 1
            self.p.save(KIND, visit)
            return self.get(visit_id)

        # 幂等身份 = **来源**（哪条补充的哪次解释，或调用方的 key）+ 身份摘要。
        # 只追加 `before` 不够：撤销后重新声明、同值不同版本、重新解释是三种不同的
        # 请求，必须分得开。
        return self.p.command(
            command_key or f'{visit_id}:candidate:{candidate_command_key(spec)}',
            {'type': 'review_visit_candidate', 'visit_id': visit_id,
             'operation': spec['operation'], 'identity': identity}, execute)

    def candidate(self, visit_id: str, candidate_id: str) -> dict[str, Any]:
        visit = self.get(visit_id)
        for candidate in visit.get('change_candidates') or []:
            if candidate['id'] == candidate_id:
                return candidate
        raise ProductError('这条变更候选不存在', 404)

    def decide_candidate(self, visit_id: str, candidate_id: str, *, status: str,
                         actor: str, applied: dict[str, Any] | None = None,
                         command_key: str | None = None) -> dict[str, Any]:
        if status not in (CANDIDATE_CONFIRMED, CANDIDATE_DISMISSED):
            raise ProductError('候选只能被确认或放弃')
        def execute():
            visit = self.get(visit_id)
            for candidate in visit.get('change_candidates') or []:
                if candidate['id'] != candidate_id:
                    continue
                if candidate['status'] != CANDIDATE_PENDING:
                    raise ProductError('这条候选已经处理过了', 409)
                candidate['status'] = status
                candidate['decided_at'] = utc_now()
                candidate['decided_by'] = actor
                candidate['revision'] = int(candidate.get('revision') or 1) + 1
                if applied is not None:
                    candidate['applied'] = dict(applied)
                visit['updated_at'] = utc_now()
                visit['revision'] += 1
                self.p.save(KIND, visit)
                return visit
            raise ProductError('这条变更候选不存在', 404)
        return self.p.command(
            command_key or f'{visit_id}:candidate:{candidate_id}:{status}',
            {'type': 'review_visit_candidate_decision', 'visit_id': visit_id,
             'candidate_id': candidate_id, 'status': status}, execute)

    def supersede_candidate(self, visit_id: str, candidate_id: str, *,
                            superseded_by: str, reason: str,
                            command_key: str | None = None) -> dict[str, Any]:
        """把一条待确认候选**撤回**——它被另一条候选取代了（例如一次纠正）。

        与"放弃"分开：放弃是用户看过并决定不要；撤回是"用户后来的说法不再支持
        这一条"。两者都不写权威记录。撤回后它**不再是**待确认项，重新渲染、
        刷新、重启都不会把它再弹出来。
        """
        def execute():
            visit = self.get(visit_id)
            for candidate in visit.get('change_candidates') or []:
                if candidate['id'] != candidate_id:
                    continue
                if candidate['status'] != CANDIDATE_PENDING:
                    return visit
                candidate['status'] = CANDIDATE_SUPERSEDED
                candidate['superseded_by'] = superseded_by
                candidate['decided_at'] = utc_now()
                candidate['supersede_reason'] = reason
                candidate['revision'] = int(candidate.get('revision') or 1) + 1
                visit['updated_at'] = utc_now()
                visit['revision'] += 1
                self.p.save(KIND, visit)
                return visit
            raise ProductError('这条变更候选不存在', 404)
        return self.p.command(
            command_key or f'{visit_id}:candidate:{candidate_id}:supersede:{superseded_by}',
            {'type': 'review_visit_candidate_supersede', 'visit_id': visit_id,
             'candidate_id': candidate_id, 'superseded_by': superseded_by}, execute)

    def record_conflict(self, visit_id: str, candidate_id: str, *, conflict: dict[str, Any],
                        expected_revision: int | None = None,
                        command_key: str | None = None) -> dict[str, Any]:
        """把一条冲突记在候选上。

        **必须由自己的事务完成。** 触发冲突的那笔药物写入已经整体回滚，冲突说明
        不能跟着它一起消失——否则用户拿到一个 409，却看不到任何解释，刷新之后
        更是连痕迹都没有。
        """
        def execute():
            visit = self.get(visit_id)
            for candidate in visit.get('change_candidates') or []:
                if candidate['id'] != candidate_id:
                    continue
                current = int(candidate.get('revision') or 1)
                if expected_revision is not None and current != int(expected_revision):
                    # 候选在预检之后被改过：不覆盖更新的那一条。
                    raise ProductError('这条候选已经更新过，请刷新后重新核对', 409)
                if candidate['status'] != CANDIDATE_PENDING:
                    raise ProductError('这条候选已经处理过了', 409)
                candidate['conflict'] = {**dict(conflict), 'detected_at': utc_now()}
                candidate['revision'] = current + 1
                visit['updated_at'] = utc_now()
                visit['revision'] += 1
                self.p.save(KIND, visit)
                return visit
            raise ProductError('这条变更候选不存在', 404)
        return self.p.command(
            command_key or f'{visit_id}:candidate:{candidate_id}:conflict:'
                           f'{expected_revision if expected_revision is not None else "-"}',
            {'type': 'review_visit_candidate_conflict', 'visit_id': visit_id,
             'candidate_id': candidate_id, 'conflict': dict(conflict)}, execute)

    def clear_conflict(self, visit_id: str, candidate_id: str) -> None:
        """候选被重新声明之后，旧的冲突说明不再适用。"""
        def execute():
            visit = self.get(visit_id)
            for candidate in visit.get('change_candidates') or []:
                if candidate['id'] == candidate_id and candidate.get('conflict'):
                    candidate['conflict'] = None
                    visit['revision'] += 1
                    self.p.save(KIND, visit)
                    return visit
            return visit
        self.p.command(f'{visit_id}:candidate:{candidate_id}:conflict-clear',
                       {'type': 'review_visit_candidate_conflict_clear',
                        'visit_id': visit_id, 'candidate_id': candidate_id}, execute)

    def pending_candidates(self, visit_id: str) -> list[dict[str, Any]]:
        visit = self.get(visit_id)
        return [item for item in visit.get('change_candidates') or []
                if item['status'] == CANDIDATE_PENDING]

    def group_members(self, visit_id: str, group_id: str) -> list[dict[str, Any]]:
        visit = self.get(visit_id)
        return [item for item in visit.get('change_candidates') or []
                if (item.get('group') or {}).get('id') == group_id]


def _name_key(name: Any) -> str:
    """药名的归一化形式，与 `memory` 的 `medication_key` 同一口径。"""
    return re.sub(r'\s+', '', str(name or '')).lower()


def normalise_candidate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """把一条操作语义的候选规范化；表达不完整就拒绝，不替它猜。"""
    if not isinstance(spec, dict):
        raise ProductError('变更候选必须是结构化描述')
    operation = str(spec.get('operation') or '').strip()
    if operation not in CANDIDATE_OPERATIONS:
        raise ProductError('不支持的用药变更操作')
    target = spec.get('target')
    if not isinstance(target, dict):
        raise ProductError('变更候选的目标必须是结构化描述')
    name = str(target.get('name') or '').strip()
    if not name:
        raise ProductError('候选变更必须指明药名')
    if operation in OPERATIONS_NEEDING_TARGET and not target.get('record_id'):
        # 名称、别名只用于**解析与展示**。真正写进记录的是记录 ID + 版本，
        # 所以没有这两样的候选根本执行不了——现在拒绝，而不是写入时才发现。
        raise ProductError('这个操作必须指向一条具体的用药记录，不能只给药名')
    changes = spec.get('changes') or {}
    if not isinstance(changes, dict):
        raise ProductError('变更内容必须是结构化描述')
    if [field for field in changes if field not in CANDIDATE_FIELDS]:
        raise ProductError('候选只能针对可核对的用药字段')
    if operation != CANDIDATE_STOP and operation != CANDIDATE_CORRECTION:
        if not any(str(value or '').strip() for value in changes.values()):
            raise ProductError('这个操作必须给出新的值')
    group = spec.get('group')
    if group is not None:
        if not isinstance(group, dict) or not str(group.get('id') or '').strip():
            raise ProductError('换药组必须有标识')
        if str(group.get('role') or '') not in GROUP_ROLES:
            raise ProductError('换药组里的角色只能是换出或换入')
        group = {'id': str(group['id']).strip(), 'role': str(group['role'])}
    occurred = spec.get('occurred') or {}
    if not isinstance(occurred, dict):
        raise ProductError('发生时间必须是结构化描述')
    precision = str(occurred.get('precision') or 'unknown')
    if precision not in TIME_PRECISIONS:
        raise ProductError('不支持的时间精度')
    basis = str(occurred.get('basis') or 'unknown')
    if basis not in ('reported', 'reported_vague', 'unknown', 'recorded_time'):
        raise ProductError('不支持的用药时间来源')
    value = occurred.get('value')
    if basis == 'reported_vague' or precision in ('week', 'month', 'vague'):
        # "上周"不是一个时间戳。保留原话与精度，**不任意挑一天**。
        basis, value = 'reported_vague', None
    elif basis == 'reported' and not value:
        basis = 'unknown'
    origin = spec.get('origin') or {'kind': 'manual'}
    if not isinstance(origin, dict):
        raise ProductError('候选来源必须是结构化描述')
    return {
        'operation': operation,
        'target': {
            'name': name,
            'matched_by': str(target.get('matched_by') or 'unmatched'),
            'record_id': target.get('record_id'),
            'record_version': target.get('record_version'),
            'record_ref': target.get('record_ref'),
            'scope_id': target.get('scope_id') or SCOPE,
            'episode_id': target.get('episode_id'),
        },
        'changes': {str(k): v for k, v in changes.items()},
        'before': spec.get('before') or {},
        'before_ref': spec.get('before_ref'),
        'occurred': {'text': occurred.get('text'), 'value': value,
                     'precision': precision, 'basis': basis,
                     'tz': occurred.get('tz')},
        'group': group,
        'reported_overlap': spec.get('reported_overlap'),
        'origin': {'kind': str(origin.get('kind') or 'manual'),
                   'note_id': origin.get('note_id'),
                   'key': origin.get('key'),
                   'interpretation_revision': origin.get('interpretation_revision')},
    }


def candidate_identity(item: dict[str, Any]) -> str:
    """同一味药上的**同一件事**的合并身份：操作 + 药。

    刻意**不含**提议的值，也不含依据的记录版本：用户改口说同一件事的另一个值，
    是"更新那条待确认的候选"，不是"再排一条"。两条并存的候选在界面上互相矛盾，
    确认其中一条之后另一条必然对不上记录。

    版本进的是**命令身份**（`candidate_command_key`），不是合并身份。
    """
    target = item.get('target') or {}
    drug = _name_key(target.get('name')) or str(target.get('record_ref') or '')
    return f"{item.get('operation')}:{drug}"


def candidate_command_key(spec: dict[str, Any]) -> str:
    """创建候选的命令身份：**来源 + 解释版本 + 依据的记录版本 + 内容摘要**。

    只追加 `before` 值不够——撤销后重新声明、同值不同版本、重新解释是三种不同的
    请求，必须分得开，否则第二次声明会静默命中第一次的回执，用户的第二句话被丢掉。
    """
    origin = spec.get('origin') or {}
    target = spec.get('target') or {}
    occurred = spec.get('occurred') or {}
    payload = {
        'origin': str(origin.get('key') or origin.get('note_id') or 'manual'),
        'interpretation': str(origin.get('interpretation_revision') or ''),
        'operation': str(spec.get('operation') or ''),
        'target': str(target.get('record_ref') or _name_key(target.get('name'))),
        'changes': {str(k): str(v) for k, v in sorted((spec.get('changes') or {}).items())},
        'occurred': str(occurred.get('value') or occurred.get('text') or ''),
        'group': str((spec.get('group') or {}).get('id') or ''),
        'identity': candidate_identity(spec),
    }
    return hashlib.sha256(packed(payload).encode('utf-8')).hexdigest()[:32]


def medication_write_plan(candidate: dict[str, Any], memory) -> dict[str, Any]:
    """一条候选 → 一次受控写入的参数。

    每一个值要么来自候选本身（用户说过的话），要么来自**权威记录**（目标行现在的
    值）。没有第三个来源："自报的原来是 X" 不参与，未提及的字段继承目标行的当前
    值——否则一次只改频次的候选会把剂量一起抹掉。

    ``expect`` 里带的是**具体记录 ID + 版本 + 状态 + 作用域**，写入原语在同一事务
    内核对它。
    """
    from .memory import MedicationWriteConflict
    operation = candidate['operation']
    target = candidate.get('target') or {}
    changes = dict(candidate.get('changes') or {})
    occurred = candidate.get('occurred') or {}
    record_id = target.get('record_id')
    row = None
    if record_id is not None:
        row = memory.connection.execute(
            "SELECT * FROM medications WHERE id=?", (int(record_id),)).fetchone()
    if operation in OPERATIONS_NEEDING_TARGET and row is None:
        raise MedicationWriteConflict('这条候选指向的用药记录已经不存在')
    reported = occurred.get('basis') == 'reported'
    occurred_at = occurred.get('value') if reported else None
    plan: dict[str, Any] = {
        'action': operation,
        'name': target.get('name'),
        'ingredients': json.loads((row['ingredients_json'] if row else None) or '[]'),
        'occurred_at': occurred_at,
        'time_basis': occurred.get('basis'),
        'time_text': occurred.get('text'),
        'dose': None, 'route': None, 'schedule': None,
        'expect': {'record_id': record_id, 'record_version': target.get('record_version'),
                   'status': OPERATION_REQUIRES_STATUS.get(operation),
                   'scope_id': target.get('scope_id')},
    }
    if operation == CANDIDATE_ADD:
        for field in ('dose', 'route', 'schedule'):
            plan[field] = changes.get(field)
        if changes.get('start_at'):
            # 新增时的"开始时间"就是这次发生的时间——同一件事的两种说法。
            plan['occurred_at'] = str(changes['start_at'])
            plan['time_basis'] = 'reported'
    elif operation == CANDIDATE_DOSE_CHANGE:
        for field in ('dose', 'route', 'schedule'):
            plan[field] = changes.get(field) or (row[field] if row else None)
        if changes.get('start_at'):
            plan['occurred_at'] = str(changes['start_at'])
            plan['time_basis'] = 'reported'
    return plan


def candidate_change_text(candidate: dict[str, Any]) -> str:
    """一句话说清这条候选要改什么：操作 + 药 + 值 + 时间及其不确定性。"""
    drug = (candidate.get('target') or {}).get('name') or ''
    operation = candidate.get('operation')
    label = CANDIDATE_OPERATION_LABELS.get(operation, str(operation))
    parts = [f'{drug} · {label}']
    before = candidate.get('before') or {}
    rendered: list[str] = []
    for field, value in sorted((candidate.get('changes') or {}).items()):
        old = before.get(field) if isinstance(before, dict) else None
        rendered.append(f'{CANDIDATE_FIELD_LABELS.get(field, field)} '
                        f'{old if old is not None else "（未记录）"} → {value}')
    if rendered:
        parts.append('；'.join(rendered))
    occurred = candidate.get('occurred') or {}
    if occurred.get('basis') == 'reported_vague' and occurred.get('text'):
        # 原话保留，**不**解析成一个精确日期。
        parts.append(f'时间：{occurred["text"]}（未确定到具体日期）')
    elif occurred.get('value'):
        parts.append(f'时间：{occurred["value"]}')
    elif occurred.get('basis') == 'unknown':
        parts.append('时间未提供')
    return '，'.join(parts)


def group_statements(visit: dict[str, Any], group_id: str) -> list[dict[str, Any]]:
    """一组换药的**逐条**陈述。由各成员的状态**派生**，不是数两条是否 confirmed。

    这一条很重要：旧药已停、新药仍在计划中时，正确的话是
    「旧药停用已记录，新药开始尚未确认发生」——而不是"换药只登记了一半"。
    后半句来自那条**计划**，它根本不是候选（计划不进入可执行的确认列表）。
    """
    statements: list[dict[str, Any]] = []
    for candidate in visit.get('change_candidates') or []:
        if (candidate.get('group') or {}).get('id') != group_id:
            continue
        label = CANDIDATE_OPERATION_LABELS.get(candidate['operation'], candidate['operation'])
        drug = (candidate.get('target') or {}).get('name') or ''
        status = candidate['status']
        if status == CANDIDATE_CONFIRMED:
            text = f'{drug}的{label}已经登记进记录'
        elif status == CANDIDATE_SUPERSEDED:
            text = f'{drug}的{label}已被您后来的说法撤回，记录未变'
        elif status == CANDIDATE_DISMISSED:
            text = f'{drug}的{label}已放弃，记录未变'
        else:
            text = f'{drug}的{label}仍待您确认，记录未变'
        statements.append({'candidate_id': candidate['id'],
                           'role': (candidate.get('group') or {}).get('role'),
                           'status': status, 'text': text})
    for note in visit.get('change_notes') or []:
        for plan in note.get('plans') or []:
            if (plan.get('group') or {}).get('id') != group_id:
                continue
            label = CANDIDATE_OPERATION_LABELS.get(plan.get('operation'), plan.get('operation'))
            drug = (plan.get('target') or {}).get('name') or ''
            statements.append({
                'candidate_id': None,
                'role': (plan.get('group') or {}).get('role'),
                'status': 'planned',
                'text': f'{drug}的{label}仍在计划中，尚未确认发生',
            })
    return statements


def status_from_task(task: dict[str, Any] | None) -> str | None:
    """这次回访走到哪了——由**执行它的任务**的真实状态决定，不由回访记录自己说。

    分成两件事：`awaiting_user` 是"跑完了，在等用户回答"；`blocked` 是"这一轮没
    跑成"。把没跑成显示成"在等您"，用户会坐在那里等一个永远不会来的问题。
    """
    if not isinstance(task, dict):
        return None
    status = task.get('status')
    if status in ('queued', 'running', 'ready'):
        return STATUS_OPEN
    if status == 'waiting_input':
        return STATUS_AWAITING
    if status == 'completed':
        return STATUS_COMPLETED
    if status in ('failed', 'cancelled'):
        return STATUS_BLOCKED
    return None


#: 回访目标里**允许**的下一步。程序限定可选项，模型选具体走哪一个——
#: 代码不替它定"这位患者该问哪几题"。
VISIT_NEXT_STEPS = ('直接复用已有结论交付结果', '向用户询问一个具体缺失事实',
                    '读取相关材料或证据', '从用户描述中提出待确认变更',
                    '根据新证据重新核对某个判断', '说明当前需要等待什么')

REASON_LABELS = {REASON_DUE: '已确认的跟进安排到期', REASON_RECORD_CHANGE: '相关记录发生变化',
                 REASON_INPUT_ARRIVED: '收到了新的补充', REASON_USER_STARTED: '您主动发起'}

CASE_KIND_LABELS = {'interaction_risk': '药物相互作用风险', 'condition_risk': '患者个体风险',
                    'evidence_gap': '依据缺口', 'discrepancy': '记录不一致',
                    'source_invalidated': '来源失效'}


def sequence_of(product, case_id: str, visit: dict[str, Any]) -> int:
    """这是这位患者这件事项的第几次回访。

    优先读记录里**写死的** `sequence`。回退到按顺序数只对旧记录有效——那个数法在
    两次回访落在同一秒时是不对的（平局由随机 uuid 决定），所以它不是口径，只是
    存量兼容。
    """
    stored = visit.get('sequence')
    if isinstance(stored, int) and stored > 0:
        return stored
    ordered = ReviewVisitStore(product).for_case(case_id)
    for index, item in enumerate(ordered):
        if item['id'] == visit['id']:
            return index + 1
    return len(ordered) or 1


def _previous_visit(product, visit: dict[str, Any]) -> dict[str, Any] | None:
    previous_id = visit.get('previous_visit_id')
    if not previous_id:
        return None
    try:
        return ReviewVisitStore(product).get(previous_id)
    except ProductError:
        return None


def visit_intent(product, case: dict[str, Any], visit: dict[str, Any], *,
                 previous_task: dict[str, Any] | None = None) -> dict[str, Any]:
    """本次回访的执行意图，由**四类事实**推导。

    触发原因、上次未完成事项、上次之后的实际变化、已确认的跟进安排。

    刻意不要求"重新证明原有风险成立"：那是把第一次重做一遍。只有原依据真的
    失效、或上次之后出现了相关新证据时，才给出 ``recheck_reason``。
    """
    from . import followup_runtime as _follow_up
    previous = _previous_visit(product, visit)
    previous_result = (previous or {}).get('result') or {}
    unfinished = [str(item.get('text')) for item in (previous_result.get('unresolved') or [])
                  if isinstance(item, dict) and item.get('text')]
    entries = list(case.get('history') or [])
    start = int((visit.get('cursor') or {}).get('before') or 0)
    fresh = [_history_line(entry)['text'] for entry in news_entries(case, start)]
    recheck = None
    for entry in news_entries(case, start):
        if entry.get('event') == 'resolution_basis_retired':
            recheck = '原有处置依据已失效，需要按当前记录重新核对'
        elif entry.get('event') == 'answer_retired' and recheck is None:
            recheck = '记录变化使先前的一条依据不再适用，需要重新核对'
    follow_up = _follow_up.project_follow_up(case.get('follow_up'))
    intent = {
        'visit_id': visit['id'],
        'sequence': sequence_of(product, case['id'], visit),
        'reason': dict(visit['reason']),
        'previous_visit_id': visit.get('previous_visit_id'),
        'previous_unfinished': unfinished,
        'new_since_last_visit': fresh,
        'follow_up': _arrangement_view(follow_up, case),
        'recheck_reason': recheck,
        'allowed_next_steps': list(VISIT_NEXT_STEPS),
    }
    intent['goal'] = visit_goal(product, case, visit, intent=intent)
    return intent


def visit_goal(product, case: dict[str, Any], visit: dict[str, Any], *,
               intent: dict[str, Any] | None = None) -> str:
    """回访目标文本。上限 300 字，与 `_safety_case_goal` 同一口径。"""
    intent = intent or visit_intent(product, case, visit)
    kind = CASE_KIND_LABELS.get(case.get('case_type'), case.get('case_type'))
    parts = [f"这是同一件{kind}安全事项的第 {intent['sequence']} 次回访。",
             f"本次起因：{intent['reason']['detail']}。"]
    if intent['previous_unfinished']:
        parts.append('上次仍未完成：' + '；'.join(intent['previous_unfinished'][:3]) + '。')
    if intent['new_since_last_visit']:
        parts.append('上次之后新增：' + '；'.join(intent['new_since_last_visit'][:3]) + '。')
    else:
        parts.append(NO_NEW_RECORDS)
    follow = intent['follow_up'] or {}
    if follow.get('present'):
        parts.append('已确认的跟进安排：'
                     f"{follow.get('at') or follow.get('note') or '已登记'}"
                     f"（{'已确认' if follow.get('confirmed') else '尚未确认'}）。")
    if intent['recheck_reason']:
        parts.append('需要重新核对：' + intent['recheck_reason'] + '。')
    else:
        parts.append('原有结论仍然有效时直接复用，不要为了重新得到同一结论重复检索。')
    parts.append('请选择本次最值得执行的下一步：' + '／'.join(VISIT_NEXT_STEPS) + '。')
    return ''.join(parts)[:300]


def _reason_rank(kind: str) -> int:
    """理由的强弱。「到期」与「记录变化」是可指认的事实，压过"用户随手点进来"。"""
    return {REASON_DUE: 3, REASON_RECORD_CHANGE: 2, REASON_INPUT_ARRIVED: 1,
            REASON_USER_STARTED: 0}.get(kind, 0)


# ---- 本次为什么跟进（服务端按事实判定，不信调用方自报） ----------------------
def derive_reason(product, case: dict[str, Any], *, today: str | None = None) -> dict[str, Any]:
    """按**事实**判定这次为什么要跟进，给出可核对的 `refs`。

    优先级：到期 > 记录变化 > 新补充 > 用户主动。调用方可以为"用户主动发起"这种
    场景调低期望，但**不能**把到期说成主动——那会让真正该跟进的那件事永远不算数。
    """
    from . import followup_runtime as _follow_up
    follow_up = _follow_up.project_follow_up(case.get('follow_up'))
    if follow_up and follow_up.get('schedule_state') in ('due', 'triggered', 'blocked'):
        at = follow_up.get('at')
        return {'kind': REASON_DUE, 'refs': [r for r in [case.get('id')] if r],
                'detail': (f"已确认的跟进安排到了时间（{at}）"
                           if at else "已确认的跟进安排已经到期")}

    changes = changed_scopes(product, case)
    if changes:
        return {'kind': REASON_RECORD_CHANGE, 'refs': [],
                'detail': '相关记录发生变化：' + '、'.join(changes)}

    recent = [e for e in (case.get('history') or [])
              if e.get('event') in ('input_recorded', 'answer_retired')]
    if recent:
        return {'kind': REASON_INPUT_ARRIVED,
                'refs': [e.get('request_id') for e in recent[-3:] if e.get('request_id')],
                'detail': '上次之后收到了新的补充'}
    return {'kind': REASON_USER_STARTED, 'refs': [],
            'detail': '由用户主动发起；系统没有发现新的变化'}


def changed_scopes(product, case: dict[str, Any],
                   since: dict[str, Any] | None = None) -> list[str]:
    """相对 `since`（默认：事项记录的依据版本）变了哪些**范围**。

    返回的是范围名（`medications` / `semantic` / …），不是"最后几条事件"。
    """
    baseline = since if since is not None else (case.get('input_versions') or {})
    current = product.revisions()
    labels = {'medications': '用药记录', 'semantic': '相关背景'}
    return [labels.get(key, key) for key in sorted(current)
            if baseline.get(key) != current[key]]


# ---- 渲染结果（只从既有真相源取，不复制数据） --------------------------------
def render_result(product, case: dict[str, Any], visit: dict[str, Any], *,
                  history: Sequence[dict[str, Any]] | None = None,
                  task: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一次回访的结果渲染出来。**纯读**：不改任何东西。

    每条关键陈述都带 `basis`：`program_check`（程序核对出来的）/ `user_report`
    （用户报告的）/ `model_explanation`（模型的解释）/ `record`（权威记录里的）。
    三者混在一起，用户就没法知道自己看到的是哪一种。
    """
    from . import followup_runtime as _follow_up
    from . import safety_cases as sc

    cursor = visit.get('cursor') or {}
    start = int(cursor.get('before') or 0)
    # 只把**信息到达或记录改变**当成"上次之后发生了什么"。事项自身的生命周期
    # 转移不算——那是这些事件的后果，不是患者那里传来的消息。口径只有一处定义。
    if history is None:
        new_entries = news_entries(case, start)
    else:
        new_entries = [entry for entry in list(history)[start:]
                       if entry.get('event') in NEWS_EVENTS]
    inputs = case.get('required_inputs') or []
    open_inputs = [i for i in inputs if i.get('status') == 'open']
    unknown_inputs = [i for i in inputs if i.get('status') == sc.ANSWER_UNKNOWN]
    answered_inputs = [i for i in inputs if i.get('status') == 'answered']
    follow_up = _follow_up.project_follow_up(case.get('follow_up'))

    statements: list[dict[str, Any]] = []

    # —— 上次之后发生了什么。没有新记录时**只说信息状态**，不做任何性质判断。
    since_last: list[dict[str, Any]] = []
    for entry in new_entries:
        since_last.append(_history_line(entry))
    if not since_last:
        since_last = [{'text': NO_NEW_RECORDS, 'basis': {'kind': 'program_check',
                                                         'refs': []}}]

    # —— 已完成的跟进行动：本次回访里真的落地了的写入与核对。
    actions: list[dict[str, Any]] = []
    for candidate in visit.get('change_candidates') or []:
        if candidate['status'] == CANDIDATE_CONFIRMED:
            actions.append({
                'text': f'{candidate_change_text(candidate)}（已确认）',
                'basis': {'kind': 'record',
                          'refs': list((candidate.get('basis') or {}).get('refs') or [])}})
    if task:
        for run in (task.get('runs') or [])[-1:]:
            actions.append({'text': f"本次回访执行了一次核对（{run.get('status')}）",
                            'basis': {'kind': 'program_check', 'refs': []}})

    # —— 仍未解决：未决问题 + 明说不知道 + 还没做的跟进行动。三者含义不同，
    #    分别给出，不合并成一句"还有事项待处理"。
    unresolved: list[dict[str, Any]] = []
    for item in open_inputs:
        unresolved.append({'text': f"仍需要补充：{item.get('question')}",
                           'basis': {'kind': 'program_check',
                                     'refs': [item.get('request_id')]}})
    for item in unknown_inputs:
        unresolved.append({'text': f"您表示不清楚，系统转去找其他来源：{item.get('question')}",
                           'basis': {'kind': 'user_report',
                                     'refs': [item.get('request_id')]}})
    for candidate in visit.get('change_candidates') or []:
        if candidate['status'] != CANDIDATE_PENDING:
            continue
        text = f'待您确认的变更：{candidate_change_text(candidate)}'
        if candidate.get('conflict'):
            text += f"（记录已变化：{candidate['conflict'].get('detail') or '请重新核对'}）"
        unresolved.append({
            'text': text,
            'basis': {'kind': 'user_report' if candidate['source'] == SOURCE_USER_DECLARED
                      else 'model_explanation',
                      'refs': list((candidate.get('basis') or {}).get('refs') or [])}})
    # 换药是**一组有关联的**变更：逐条陈述，不合并成一个"换药完成/未完成"。
    seen_groups: list[str] = []
    for candidate in visit.get('change_candidates') or []:
        group_id = (candidate.get('group') or {}).get('id')
        if group_id and group_id not in seen_groups:
            seen_groups.append(group_id)
    for note in visit.get('change_notes') or []:
        for plan in note.get('plans') or []:
            group_id = (plan.get('group') or {}).get('id')
            if group_id and group_id not in seen_groups:
                seen_groups.append(group_id)
    groups = [{'group_id': group_id, 'statements': group_statements(visit, group_id)}
              for group_id in seen_groups]
    if follow_up and follow_up.get('schedule_state') == 'blocked':
        unresolved.append({'text': '跟进安排被阻塞：' + str(follow_up.get('blocked_reason')),
                           'basis': {'kind': 'program_check', 'refs': [case.get('id')]}})

    statements.append({'text': visit['reason']['detail'],
                       'basis': {'kind': 'program_check',
                                 'refs': list(visit['reason'].get('refs') or [])}})
    for item in answered_inputs[-3:]:
        statements.append({
            'text': f"已记录您关于「{item.get('question')}」的回答",
            'basis': {'kind': 'user_report', 'refs': [item.get('request_id')]}})

    # —— 复用了哪些已有信息。**不是**"又确认了一遍"：这些是上次就已经有依据、
    #    本次直接沿用的判断，用户要能看出"系统记得上次做过的事"。
    reused: list[dict[str, Any]] = []
    for item in answered_inputs:
        reused.append({'text': f"沿用了已有的回答：{item.get('question')}",
                       'basis': {'kind': 'record', 'refs': [item.get('request_id')]}})

    # —— 哪些判断需要重新核对。由**程序**判定（依据失效才会出现在这里），
    #    与模型的解释是两种来源，界面上分得开。
    recheck: list[dict[str, Any]] = []
    for item in inputs:
        if item.get('answer_invalidated') or item.get('reopened_reason'):
            recheck.append({
                'text': f"需要重新核对：{item.get('question')}"
                        f"（{item.get('reopened_reason') or '记录变化'}）",
                'basis': {'kind': 'program_check', 'refs': [item.get('request_id')]}})
    for entry in new_entries:
        if entry.get('event') == 'resolution_basis_retired':
            recheck.append({'text': '原有处置依据已失效，需按当前记录重新核对',
                            'basis': {'kind': 'program_check', 'refs': []}})

    result = {
        'why': dict(visit['reason']),
        'since_last': since_last,
        'actions': actions,
        'unresolved': unresolved,
        # 换药组的逐条陈述。**没有**一个"换药完成"的总结论——那会把"旧药停了、
        # 新药还没开始"渲染成"换药做完了"。
        'groups': groups,
        'reused': reused,
        'recheck': recheck,
        'end_reason': _end_reason(visit, task, open_inputs, unknown_inputs, new_entries),
        'answered_count': len(answered_inputs),
        'next_step': case.get('next_action_summary'),
        'next_arrangement': _arrangement_view(follow_up, case),
        'statements': statements,
        'first_visit': visit.get('previous_visit_id') is None,
        'rendered_at': utc_now(),
    }
    return result


def _end_reason(visit: dict[str, Any], task: dict[str, Any] | None,
                open_inputs: Sequence[dict[str, Any]],
                unknown_inputs: Sequence[dict[str, Any]],
                new_entries: Sequence[dict[str, Any]]) -> str:
    """本次为什么结束或等待——按**实际状态**说，不套一句通用的结尾。

    三件事分开讲：真的没有新情况、在等用户补什么、这一轮没跑成。
    把它们都说成"本次回访已完成"会让等待看起来像结论。
    """
    status = status_from_task(task)
    if status == STATUS_BLOCKED:
        reason = (task or {}).get('waiting_reason')
        return f'本次回访没有跑成：{reason}' if reason else '本次回访没有跑成，可以稍后重试。'
    if open_inputs:
        return f'本次在等您补充：{open_inputs[0].get("question")}'
    if unknown_inputs:
        return f'您表示不清楚，系统转去找其他来源：{unknown_inputs[0].get("question")}'
    if not new_entries:
        return '本次没有新的变化，已有结论仍然有效，因此结束。'
    return '本次已按新到的情况处理完，可以结束这一回。'


def history_line(entry: dict[str, Any]) -> dict[str, Any]:
    """一条事项历史 → 一句可读的"上次之后发生了什么"。**公开入口**。"""
    return _history_line(entry)


def arrangement_view(case: dict[str, Any]) -> dict[str, Any] | None:
    """这件事项的跟进安排及其**确认状态**。没有就是没有，不编一个出来。"""
    from . import followup_runtime as _follow_up
    return _arrangement_view(_follow_up.project_follow_up(case.get('follow_up')), case)


def _history_line(entry: dict[str, Any]) -> dict[str, Any]:
    """一条事项历史 → 一句可读的"上次之后发生了什么"。"""
    event = entry.get('event')
    at = entry.get('at')
    if event == 'input_recorded':
        return {'text': f"{at}：记录了您的一条补充（{entry.get('answer_kind') or '内容'}）",
                'basis': {'kind': 'user_report', 'refs': [entry.get('request_id')]}}
    if event == 'answer_retired':
        return {'text': f"{at}：记录变化，先前的一条回答需要重新核对",
                'basis': {'kind': 'program_check', 'refs': [entry.get('request_id')]}}
    if event == 'status_changed':
        return {'text': f"{at}：事项状态由 {entry.get('from')} 变为 {entry.get('to')}"
                        + (f"（{entry.get('why')}）" if entry.get('why') else ''),
                'basis': {'kind': 'program_check', 'refs': []}}
    if event == 'disposition':
        return {'text': f"{at}：登记了处置（{entry.get('disposition')}）",
                'basis': {'kind': 'record', 'refs': []}}
    return {'text': f"{at}：{event}", 'basis': {'kind': 'program_check', 'refs': []}}


def _arrangement_view(follow_up: dict[str, Any] | None,
                      case: dict[str, Any]) -> dict[str, Any] | None:
    """下一次跟进安排及其**确认状态**。没有就是没有，不编一个出来。"""
    if not follow_up:
        return {'present': False, 'confirmed': False, 'schedule_state': None,
                'at': None, 'owner': None, 'note': '这件事项目前没有登记跟进安排'}
    return {
        'present': True,
        'at': follow_up.get('at'), 'kind': follow_up.get('kind'),
        'owner': follow_up.get('owner'), 'note': follow_up.get('note'),
        'schedule_state': follow_up.get('schedule_state'),
        'confirmed': bool(follow_up.get('confirmed')),
        'confirmation_ref': follow_up.get('confirmation_ref'),
        'confirmed_at': follow_up.get('confirmed_at'),
        'last_triggered_at': follow_up.get('last_triggered_at'),
        'blocked_reason': follow_up.get('blocked_reason'),
    }


__all__ = [
    'KIND', 'REASON_DUE', 'REASON_RECORD_CHANGE', 'REASON_INPUT_ARRIVED',
    'REASON_USER_STARTED', 'REASON_KINDS', 'STATUS_OPEN', 'STATUS_AWAITING',
    'STATUS_COMPLETED', 'STATUS_BLOCKED', 'VISIT_STATUSES', 'CANDIDATE_PENDING',
    'CANDIDATE_CONFIRMED', 'CANDIDATE_DISMISSED', 'SOURCE_USER_DECLARED',
    'SOURCE_MODEL_PROPOSED', 'CANDIDATE_SOURCES', 'CANDIDATE_FIELDS',
    'NO_NEW_RECORDS', 'ReviewVisitStore', 'derive_reason', 'changed_scopes',
    'render_result', 'status_from_task', 'visit_intent', 'visit_goal',
    'VISIT_NEXT_STEPS', 'REASON_LABELS', 'CASE_KIND_LABELS',
]
