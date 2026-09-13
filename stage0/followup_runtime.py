"""长期跟进 runtime：让一条已确认的安排在时间到达或相关记录变化时**真的**被恢复。

背景（这是本模块要修的缺陷）：`follow_up` 过去是**只写一次、不可改、不可取消、
无人执行**的声明，而且 `confirmed` 是**派生**的 `bool(at or condition)`——调用方
给了个时间就自动算"已确认"。三件事因此都做不到：

1. **有时间或条件 ≠ 已确认。** 时间是一个待办，确认是一个人对这项安排的承诺。
   `confirmed` 只能由一条**确认记录**产生（`confirmation_ref`），并且
   `confirmed_at` / `confirmed_by` 一并可追溯。
2. **安排会变。** 改期与取消必须让**旧版本**的触发失效——否则一条已经取消的安排
   仍在后台执行副作用。
3. **没人执行它。** 没有调度状态机，`condition` 是自由文本从不求值。

本模块只做三件事，且都复用仓库**既有**的持久运行机制：

* **校验与规范化**——`at` 必须带时区（naive → 422），`condition` 只接受白名单
  结构（未知类型 → 422，**不静默降级**）。
* **安排与确认的语义**——`build_arrangement` / `apply_confirmation` /
  `project_follow_up`。投影是只读的：存量记录里 `confirmed=True` 但拿不出确认记录
  的，一律按 `false` 读。
* **调度状态推进**——`scan_follow_ups` / `run_follow_ups`，由既有的
  `OutboxWorker.drain_once` 驱动。不新增调度框架，不新增后台模型循环。

状态机是 `scheduled → due → triggered`，失败进 `blocked` 并带 `blocked_reason`。
**"永远停在 scheduled"不算实现**——那等于把"有人会跟进"这句话挂在墙上。

关于"恰好一次"：这里不宣称物理意义上的 exactly-once。触发身份是幂等的业务写入
（`case_id` + 安排 `revision` + 触发实例），重复扫描与进程重启只会命中同一条记录；
用户看到的是一个结果，不是两个。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from .product import ProductError

#: `kind` 是"安排的种类"；`schedule_state` 是"这条安排现在走到哪了"。两件事。
FOLLOW_UP_KINDS = ('review_at', 'on_event', 'arrangement')

#: 安排的生命周期。`unscheduled` 是合法的终点（`kind='arrangement'` 的备忘可以
#: 永久停在那里），不是失败。
SCHEDULE_SCHEDULED = 'scheduled'
SCHEDULE_DUE = 'due'
SCHEDULE_TRIGGERED = 'triggered'
SCHEDULE_BLOCKED = 'blocked'
SCHEDULE_CANCELLED = 'cancelled'
SCHEDULE_UNSCHEDULED = 'unscheduled'
SCHEDULE_STATES = (SCHEDULE_SCHEDULED, SCHEDULE_DUE, SCHEDULE_TRIGGERED,
                   SCHEDULE_BLOCKED, SCHEDULE_CANCELLED, SCHEDULE_UNSCHEDULED)

#: 可以被确认的状态：还没有走完的安排。已触发/已取消/仅备忘都没有可确认的东西。
CONFIRMABLE_STATES = (SCHEDULE_SCHEDULED, SCHEDULE_DUE)

#: §4.3 白名单。值为该 kind 的必填键；`optional` 是允许出现（且会被保留）的键。
CONDITION_KINDS: dict[str, dict[str, tuple[str, ...]]] = {
    'conclusion_recorded': {'required': ('ref',), 'optional': ('conclusion_kind',)},
    'necessary_check': {'required': ('ref',), 'optional': ('check_id',)},
    'medication_change': {'required': ('ref',), 'optional': ()},
    'fact_change': {'required': ('ref',), 'optional': ()},
}

#: 后两类复用既有的必要检查触发器词汇（``safety_checks``）。
CONDITION_TO_CHECK_TRIGGER = {
    'medication_change': 'medication_set',
    'fact_change': 'condition_facts',
}

DEFAULT_OWNER = 'caregiver'
UNCONFIRMED_NOTE = '这是一项待确认的安排；时间或条件本身不构成确认'

LEASE_TTL_SECONDS = 300
MAX_ATTEMPTS = 3
MAX_JOBS = 8
TRIGGER_DEFERRED_REASON = '等待必要安全检查完成'


def _lease_ttl() -> int:
    try:
        return max(1, int(os.getenv('FOLLOW_UP_LEASE_TTL_SECONDS', str(LEASE_TTL_SECONDS))))
    except ValueError:
        return LEASE_TTL_SECONDS


# ---- 校验与规范化 -------------------------------------------------------------
def normalise_at(raw) -> str | None:
    """带时区的 ISO-8601 → `utc_now()` 同款的 `+00:00` 秒精度；其余一律 422。

    与 ``care_task.due_at`` 同一口径（"待办日期必须包含时区"）：没有时区的时间不是
    一个时刻，只是半句话——按本地时区猜一次，就会在跨时区时悄悄错几个小时。
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ProductError('跟进时间必须是带时区的 ISO-8601 字符串', 422)
    try:
        parsed = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ProductError('无法解析跟进时间；请使用带时区的 ISO-8601 格式', 422) from None
    if parsed.tzinfo is None:
        raise ProductError('跟进时间必须包含时区（例如 2026-10-01T00:00:00+00:00）', 422)
    return parsed.astimezone(timezone.utc).isoformat(timespec='seconds')


def normalise_condition(raw) -> dict | None:
    """只接受白名单结构。**未知类型 → 422，不静默降级。**

    静默降级会把一条永远不会触发的安排渲染成一条正常排上的安排——用户以为有人会
    在事情变化时回来找他，而系统里其实什么都没有。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProductError(
            '触发条件必须是结构化条件（kind + ref），不接受自然语言；'
            '系统不会执行用户写的表达式', 422)
    kind = raw.get('kind')
    if kind not in CONDITION_KINDS:
        raise ProductError(
            f"不支持的触发条件类型：{kind!r}；"
            f"可选值：{'、'.join(sorted(CONDITION_KINDS))}", 422)
    spec = CONDITION_KINDS[kind]
    condition: dict = {'kind': kind}
    for key in spec['required']:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProductError(f'触发条件 {kind} 缺少必填的 {key}', 422)
        condition[key] = value.strip()
    for key in spec['optional']:
        value = raw.get(key)
        if value is not None:
            condition[key] = value
    return condition


# ---- 安排与确认 ---------------------------------------------------------------
def build_arrangement(*, kind: str, at=None, condition=None, owner=None, note=None,
                      recorded_at: str | None = None,
                      revision: int = 1) -> dict:
    """构造一条**未确认**的安排。

    调用方给出时间或条件，这只说明"什么时候该看"；它**不**说明谁确认过。所以这里
    产出的 `confirmed` 恒为 `False`，`confirmed_at` / `confirmation_ref` 恒为
    `None`——确认只能由 `apply_confirmation` 产生（§4.5）。
    """
    from .memory import utc_now
    if kind not in FOLLOW_UP_KINDS:
        raise ProductError(f"不支持的安排类型：{kind!r}", 422)
    at_value = normalise_at(at) if kind == 'review_at' else None
    condition_value = normalise_condition(condition) if kind == 'on_event' else None
    # 说了 review_at 却没给时间（或说了 on_event 却没给条件）——那不是一条排上的
    # 安排，只是一条备忘。降级成 arrangement 是**可见**的：它停在 unscheduled。
    if kind == 'review_at' and at_value is None:
        kind = 'arrangement'
    elif kind == 'on_event' and condition_value is None:
        kind = 'arrangement'
    scheduled = bool(at_value or condition_value)
    return {
        'kind': kind,
        'at': at_value,
        'condition': condition_value,
        'owner': owner or DEFAULT_OWNER,
        'note': note or UNCONFIRMED_NOTE,
        'recorded_at': recorded_at or utc_now(),
        'confirmed': False,
        'confirmed_at': None,
        'confirmed_by': None,
        'confirmation_ref': None,
        'revision': revision,
        'schedule_state': SCHEDULE_SCHEDULED if scheduled else SCHEDULE_UNSCHEDULED,
        'last_triggered_at': None,
        'last_trigger_reason': None,
        'care_task_id': None,
        'blocked_reason': None,
    }


def apply_confirmation(follow_up: dict, *, by: str | None, confirmation_ref: str,
                       at: str, note: str | None = None) -> dict:
    """把一条确认记录落到安排上。`confirmed` 只能从这里产生。

    确认**不**推进安排 `revision`：确认改变的是"谁承诺了"，不是"安排是什么"。
    推进它会让一条刚刚确认的安排作废自己已经排好的触发。
    """
    updated = dict(follow_up)
    updated['confirmed'] = True
    updated['confirmed_at'] = at
    updated['confirmed_by'] = by
    updated['confirmation_ref'] = confirmation_ref
    if note:
        updated['note'] = note
    return updated


def project_follow_up(raw) -> dict | None:
    """读侧投影。**只做一件事**：把拿不出确认记录的 `confirmed=True` 读成 `false`。

    不加工、不猜测、不补默认值。存量数据里 `confirmed=true` 却没有 `confirmed_at`
    / `confirmation_ref` 的记录按 `false` 读——这是一次有意的、可见的行为回退：
    在此之前，调用方给一个时间就会被系统记成"已确认"，那些"确认"从来没有人做过。
    """
    if not isinstance(raw, dict):
        return None
    projected = dict(raw)
    if projected.get('confirmed') and not (projected.get('confirmed_at')
                                           and projected.get('confirmation_ref')):
        projected['confirmed'] = False
    return projected


def is_confirmed(follow_up) -> bool:
    projected = project_follow_up(follow_up)
    return bool(projected and projected.get('confirmed'))


def schedule_state(follow_up) -> str | None:
    projected = project_follow_up(follow_up)
    return (projected or {}).get('schedule_state')


# ---- 触发身份 -----------------------------------------------------------------
def trigger_key(case_id: str, follow_up: dict, instance: str) -> str:
    """幂等触发身份：事项 + 安排版本 + 触发实例。

    三者缺一不可。少了安排版本，改期后旧触发会继续执行；少了触发实例，同一版本的
    第二次触发会被当成第一次而丢掉；少了事项，两件事会互相顶掉。
    """
    return f"{case_id}:{int(follow_up.get('revision') or 1)}:{instance}"


def review_at_instance(at: str | None) -> str:
    return f'review_at:{at}'


# ---- 触发实例队列 -------------------------------------------------------------
def cancel_pending_runs(memory, case_id: str, *, reason: str) -> int:
    """作废某事项上**尚未执行**的触发实例。改期与取消共用这一条路径。

    已经 `done` 的行保持原样：那次调查确实发生过，把它抹掉等于篡改历史。被作废的
    是"还没执行、且现在已不适用"的触发——否则一条取消了的安排会继续在后台执行
    副作用。
    """
    with memory._lock, memory.connection:
        cursor = memory.connection.execute(
            "UPDATE follow_up_runs SET status='cancelled', lease_token=NULL, "
            "lease_expires_at=NULL, error=?, updated_at=? "
            "WHERE case_id=? AND status IN ('open','running')",
            (reason, _now(), case_id))
    return cursor.rowcount or 0


def _now() -> str:
    from .memory import utc_now
    return utc_now()


# ---- 条件求值 -----------------------------------------------------------------
def _norm_ref(text) -> str:
    import re
    return re.sub(r'\s+', '', str(text or '')).lower()


def _ref_matches(candidate, wanted) -> bool:
    """版本化引用按**头部**比较，这样 `...@1` 与 `...@v1` 指的是同一条记录。

    引用格式是 `<layer>:<kind>:<id>@<version>`；版本是快照，不是身份。把版本也
    算进匹配，会让一条"某结论被记录"的条件因为版本号写法不同而永远不触发。
    """
    left, right = _norm_ref(candidate), _norm_ref(wanted)
    if not left or not right:
        return False
    if left == right:
        return True
    return left.split('@')[0] == right.split('@')[0]


def _conclusion_ref(row) -> str:
    return f"memory:conclusion:{row['id']}@v1"


def _condition_satisfied(memory, condition: dict) -> str | None:
    """条件是否已经由**持久记录**满足。返回一句人话的原因，或 None。

    求值只读结构化的持久信号（`conclusions` / `necessary_checks`），不执行任何
    用户写的表达式，也不问模型。
    """
    kind = condition.get('kind')
    ref = condition.get('ref')
    if kind == 'conclusion_recorded':
        wanted_kind = condition.get('conclusion_kind')
        rows = memory.connection.execute(
            "SELECT id, kind FROM conclusions WHERE status='current' ORDER BY id").fetchall()
        for row in rows:
            if not _ref_matches(_conclusion_ref(row), ref):
                continue
            if wanted_kind and row['kind'] != wanted_kind:
                continue
            return f'关联结论已记录：{ref}'
        return None
    trigger_kind = CONDITION_TO_CHECK_TRIGGER.get(kind)
    if trigger_kind is None and kind != 'necessary_check':
        return None
    if kind == 'necessary_check':
        # "某项必要检查完成"跨**两种**触发类型：用药集合与患者事实。
        # 限定在其中一种，会让另一种下的条件永远不触发。
        rows = memory.connection.execute(
            "SELECT id, trigger_kind, trigger_ref, status FROM necessary_checks").fetchall()
    else:
        rows = memory.connection.execute(
            "SELECT id, trigger_kind, trigger_ref, status FROM necessary_checks "
            "WHERE trigger_kind=?", (trigger_kind,)).fetchall()
    for row in rows:
        if kind == 'necessary_check':
            check_id = condition.get('check_id')
            if check_id is not None and str(check_id) != str(row['id']):
                continue
            if not _ref_matches(row['trigger_ref'], ref):
                continue
            if row['status'] != 'done':
                continue
            return f'必要检查已完成：{ref}'
        if _ref_matches(row['trigger_ref'], ref):
            return ('用药集合已变化：' if kind == 'medication_change'
                    else '患者事实已变化：') + str(ref)
    return None


def evaluate_trigger(follow_up: dict, memory, *, now: str) -> dict | None:
    """这条安排现在该触发吗？返回 `{instance, reason}` 或 None。

    时间比较是**字符串**比较：`normalise_at` 与 `utc_now()` 产出的都是同一格式
    （`+00:00`、秒精度）的 UTC 时间戳，字典序即时间序——这也是既有队列比较租约与
    `next_attempt_at` 的口径。
    """
    kind = follow_up.get('kind')
    if kind == 'review_at':
        at = follow_up.get('at')
        if not at or str(at) > now:
            return None
        return {'instance': review_at_instance(at), 'reason': f'已到安排时间 {at}'}
    if kind == 'on_event':
        condition = follow_up.get('condition')
        if not isinstance(condition, dict):
            return None
        reason = _condition_satisfied(memory, condition)
        if reason is None:
            return None
        return {'instance': condition_instance(condition), 'reason': reason}
    # `kind='arrangement'` 是一条备忘：它停在 unscheduled，永远不会自己触发。
    return None


# ---- 队列 ---------------------------------------------------------------------
def pending_necessary_checks(memory) -> int:
    """还没跑完的确定性检查数。**它们必须先跑完**，模型调查才轮到。"""
    row = memory.connection.execute(
        "SELECT COUNT(*) FROM necessary_checks WHERE status IN ('open','running')"
    ).fetchone()
    return int(row[0] or 0)


def _recover_expired(memory, now: str) -> int:
    """崩溃的 worker 留下过期租约：记一次尝试并放回队列（三次后判失败）。

    与 `necessary_checks` / `dependency_tasks` 同一口径——失败保持可见，不会被
    静默丢掉。
    """
    rows = memory.connection.execute(
        "SELECT id, attempts FROM follow_up_runs WHERE status='running' "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at<?", (now,)).fetchall()
    for row in rows:
        attempts = int(row['attempts'] or 0) + 1
        memory.connection.execute(
            "UPDATE follow_up_runs SET status=?, attempts=?, lease_token=NULL, "
            "lease_expires_at=NULL, updated_at=? WHERE id=?",
            ('failed' if attempts >= MAX_ATTEMPTS else 'open', attempts, now, row['id']))
    return len(rows)


def _enqueue_trigger(memory, case_id: str, follow_up: dict, trigger: dict,
                     now: str) -> dict:
    """登记一条触发实例，并**如实回报那一行现在是什么状态**。

    `UNIQUE(case_id, schedule_revision, trigger_key)` 就是幂等：重复扫描命中同一行，
    不会长出新的一行。回报状态是必要的——一行可能已经 `done`（执行过了）或 `failed`
    （重试耗尽）。把这两种都当成"刚排上队"，界面上就会出现一条看起来还在排队、
    实际永远不会执行的安排。
    """
    with memory._lock, memory.connection:
        cursor = memory.connection.execute(
            """INSERT INTO follow_up_runs(scope_id,case_id,schedule_revision,trigger_key,
                 trigger_reason,status,attempts,lease_token,lease_expires_at,
                 care_task_id,result_json,error,created_at,updated_at)
               VALUES('local-demo',?,?,?,?, 'open',0,NULL,NULL,NULL,NULL,NULL,?,?)
               ON CONFLICT(case_id,schedule_revision,trigger_key) DO NOTHING""",
            (case_id, int(follow_up.get('revision') or 1),
             trigger_key(case_id, follow_up, trigger['instance']),
             trigger['reason'], now, now))
        created = bool(cursor.rowcount)
        key = trigger_key(case_id, follow_up, trigger['instance'])
        row = memory.connection.execute(
            "SELECT status, attempts, error, care_task_id FROM follow_up_runs "
            "WHERE case_id=? AND schedule_revision=? AND trigger_key=?",
            (case_id, int(follow_up.get('revision') or 1), key)).fetchone()
    return {'created': created,
            'status': (row['status'] if row else 'open'),
            'attempts': (int(row['attempts'] or 0) if row else 0),
            'error': (row['error'] if row else None),
            'care_task_id': (row['care_task_id'] if row else None)}


def scan_follow_ups(memory, product, *, now: str | None = None) -> list[dict]:
    """把"现在该触发了"的安排登记成触发实例。不执行调查，只排队。"""
    from .safety_cases import SafetyCaseStore
    now = now or _now()
    store = SafetyCaseStore(product)
    registered: list[dict] = []
    for case in store.objects():
        follow_up = project_follow_up(case.get('follow_up'))
        if not follow_up:
            continue
        # **不把 `confirmed` 当成可执行的前置条件。** CONTRACT §4.5 把"已安排"与
        # "已确认"定成两件事：确认记录回答的是"谁承诺了这件事"，不是"这条安排存不
        # 存在"。拿它做闸门会让每一条经由处置端点建立的安排在到期后**永远停在
        # scheduled**——而处置端点是本轮之前唯一的写入路径，它产出的 confirmed
        # 恒为 false，等于整个长期跟进对主要路径失效。
        if follow_up.get('schedule_state') not in (SCHEDULE_SCHEDULED, SCHEDULE_DUE,
                                                   SCHEDULE_BLOCKED):
            continue
        trigger = evaluate_trigger(follow_up, memory, now=now)
        if trigger is None:
            continue
        outcome = _enqueue_trigger(memory, case['id'], follow_up, trigger, now)
        # 状态必须跟着那一行的**真实**状态走，不能一律写成"排队中"。
        if outcome['status'] == 'done':
            state, blocked_reason = SCHEDULE_TRIGGERED, None
        elif outcome['status'] == 'failed':
            state = SCHEDULE_BLOCKED
            blocked_reason = outcome['error'] or '触发重试已达上限'
        elif outcome['status'] == 'cancelled':
            # 这一版已经被改期/取消作废了；不要把它重新盖回"排队中"。
            continue
        elif outcome['attempts'] > 0:
            # 已经失败过、正等着重试：保持 `blocked` 与它的原因。把它盖回"排队中"
            # 会让一条反复失败的安排看起来一切正常。
            continue
        else:
            state, blocked_reason = SCHEDULE_DUE, None
        _stamp(product, case['id'], expected_revision=follow_up.get('revision'),
               state=state, last_trigger_reason=trigger['reason'],
               care_task_id=outcome['care_task_id'], blocked_reason=blocked_reason,
               event='follow_up_due')
        registered.append({'case_id': case['id'], 'reason': trigger['reason'],
                           'created': outcome['created'], 'state': state})
    return registered


def _stamp(product, case_id: str, *, expected_revision, state: str,
           event: str, last_trigger_reason: str | None = None,
           last_triggered_at: str | None = None, care_task_id: str | None = None,
           blocked_reason: str | None = None) -> dict | None:
    """推进安排的状态。**只动 runtime 拥有的字段**，且只在安排版本仍然匹配时动。

    改期/取消会把安排 revision 推进一格；那时这里什么也不写——旧触发不该把
    "已取消"重新盖回"已触发"。
    """
    from .safety_cases import KIND, SafetyCaseStore
    store = SafetyCaseStore(product)
    with product.transaction():
        case = store.get(case_id)
        follow_up = case.get('follow_up')
        if not isinstance(follow_up, dict):
            return None
        if int(follow_up.get('revision') or 1) != int(expected_revision or 1):
            return None
        previous = follow_up.get('schedule_state')
        # 只在**真的有变化**时写：worker 每个周期都会扫一遍，一次没有变化的扫描
        # 不该推进事项 revision，也不该往历史里塞一条什么都没说的记录。
        unchanged = (
            previous == state
            and (last_trigger_reason is None
                 or follow_up.get('last_trigger_reason') == last_trigger_reason)
            and (last_triggered_at is None
                 or follow_up.get('last_triggered_at') == last_triggered_at)
            and (care_task_id is None or follow_up.get('care_task_id') == care_task_id)
            and follow_up.get('blocked_reason') == blocked_reason)
        if unchanged:
            return case
        follow_up['schedule_state'] = state
        if last_trigger_reason is not None:
            follow_up['last_trigger_reason'] = last_trigger_reason
        if last_triggered_at is not None:
            follow_up['last_triggered_at'] = last_triggered_at
        if care_task_id is not None:
            follow_up['care_task_id'] = care_task_id
        follow_up['blocked_reason'] = blocked_reason
        case['follow_up'] = follow_up
        case['history'].append({'at': _now(), 'event': event, 'schedule_state': state,
                                'previous_state': previous,
                                'reason': last_trigger_reason or blocked_reason})
        case['updated_at'] = _now()
        case['revision'] += 1
        product.save(KIND, case)
        return case


# ---- 执行 ---------------------------------------------------------------------
def _ref_head(ref) -> str:
    """引用的"版本号之前"部分：``memory:medication:45@v2`` → ``memory:medication:45``。

    版本的真实形状是 ``@v<数字>``（``answer_grounding.parse_versioned_ref`` 只认它）；
    解析不出来的原样返回——**不猜**它在指哪条记录。
    """
    from .answer_grounding import parse_versioned_ref
    parsed = parse_versioned_ref(ref)
    return parsed[0] if parsed else str(ref or '')


def recheck_answer_dependencies(product, case: dict, *, changed_refs=(),
                                reason: str | None = None) -> dict:
    """记录变了之后，请 A 的接口判定**哪几条**答案不再算已核对，并把结果搬回去。

    边界分工（CONTRACT §3.6）：B 只做两件事——把"哪些记录变了"交过去、把结果搬
    回来。**一条答案现在还成不成立、哪一条受影响，由 A 判定**：这里调用的
    `answer_grounding.revalidate` / `dependency_state` 就是答案写入时用的同一套
    判定，B **不复制**第二套规则，也不新建第二套版本机制。

    三条守住的性质，和"把所有已答问题一律重开"划清界限：

    * **只动受影响的。** 传了 `changed_refs` 就只处理依赖里确实牵涉到那些引用的
      答案。一次局部变化不该放大成一次全面重问。
    * **只降不升。** A 的 `revalidate` 只会走到 `stale` / `unsupported`。
    * **没有 assessment 的老答案一个字节都不写**（§3.4）：缺失就是"未核实"，
      给它们补一个状态等于凭空造出一条核对记录。

    A 的判定接口不可用时如实返回 `unavailable` 并且**不写任何东西**——编一个
    "未核实"不算核对，只会让界面显示一个没人做过的结论。
    """
    try:
        from . import answer_grounding as ag
    except ImportError:
        return {'status': 'unavailable',
                'reason': 'A 的答案依赖接口（stage0/answer_grounding.py）尚未交付；'
                          '本次不改变任何答案的核对状态'}
    if not hasattr(ag, 'revalidate'):
        return {'status': 'unavailable',
                'reason': 'A 的答案依赖接口尚未提供 revalidate；'
                          '本次不改变任何答案的核对状态'}

    from .safety_cases import KIND, SafetyCaseStore
    store = SafetyCaseStore(product)
    detail = reason or '来源记录发生变化，这条答案需要重新核对'
    changed_heads = {_ref_head(ref) for ref in changed_refs or ()}
    versions = ag.versions_from_snapshot(
        (product.memory.snapshot() or {}).get('medications') or [])

    checked = affected = 0
    updated: list[dict] = []
    unchecked: list[dict] = []
    projections: dict[str, list] = {}
    changed_tasks: list[dict] = []

    for task in product.objects('care_task'):
        if (task.get('goal_type') != 'safety_case'
                or task.get('safety_case_id') != case.get('id')):
            continue
        investigation = task.get('investigation')
        if not isinstance(investigation, dict):
            continue
        touched = False
        for question in investigation.get('questions') or []:
            answers = question.get('answers') or []
            for index, answer in enumerate(answers):
                assessment = ag.assessment_of(answer)
                if assessment is None:
                    continue                     # §3.4：缺失就是缺失，不补
                checked += 1
                deps = assessment.get('dependency_refs') or []
                if changed_heads and not ({_ref_head(ref) for ref in deps}
                                          & changed_heads):
                    continue                     # 与这次变化无关：不动
                state = ag.dependency_state(deps, current_version_of=versions)
                # 来源**还在不在**由调用方判定（`revalidate` 的口径）。本仓库里
                # 改剂量会**取代**旧记录：`memory:medication:1@v1` 直接不在当前
                # 用药集合里了，新记录换了 id（`memory:medication:2@v2`）。所以
                # "用药类依赖查不到"就是"这条答案依据的那条记录已经不在了" ——
                # 按 A 的口径走 `withdraw`，不是"没变"。
                #
                # 只对**真能查**的这一类下判断。其余依赖（如 `memory:conclusion:`）
                # 没有版本来源可查，如实记进 `unchecked`，**不猜**、也不当成没变。
                resolvable = [head for head in state['unknown']
                              if head.startswith('memory:medication:')]
                unresolvable = [head for head in state['unknown']
                                if not head.startswith('memory:medication:')]
                if unresolvable:
                    unchecked.append({'question_id': question.get('question_id'),
                                      'detail': '这些依赖没有可查的版本来源，'
                                                '本次无法核对：' + '、'.join(unresolvable)})
                fresh = ag.revalidate(assessment,
                                      source_available=not resolvable,
                                      current_version_of=versions, detail=detail)
                if fresh == assessment:
                    continue
                answers[index] = {**answer, 'assessment': fresh}
                affected += 1
                touched = True
                updated.append({'question_id': question.get('question_id'),
                                'from': assessment.get('status'),
                                'to': fresh.get('status')})
            if touched:
                from .care_tasks import safety_case_request_id
                projections[safety_case_request_id(case.get('id'), question)] = \
                    list(answers)
        if touched:
            changed_tasks.append(task)

    # 写回是**一个**事务：`product.save` 自己会隐式开事务，逐个写在事务外做，
    # 后面就再也拿不到 `transaction()`（它要求"由自己开启"）。
    if changed_tasks or projections:
        with product.transaction():
            for task in changed_tasks:
                task['revision'] = int(task.get('revision') or 0) + 1
                task['updated_at'] = _now()
                product.save('care_task', task)

            # 把改动搬到 C 真正消费的那份投影上。`answered_parts` 是**事项上的
            # 副本**（`care_tasks._sync_questions_to_case` 写入）：只改调查、不重
            # 投影，前端拿到的仍然是旧状态——"改了但用户看不见"和没改一样。
            if projections:
                stored = store.get(case['id'])
                changed_case = False
                for request in stored.get('required_inputs') or []:
                    parts = projections.get(request.get('request_id'))
                    if parts is not None:
                        request['answered_parts'] = parts
                        request['still_uncertain'] = list(
                            (parts[-1] or {}).get('still_uncertain') or [])
                        changed_case = True
                if changed_case:
                    stored['updated_at'] = _now()
                    stored['revision'] = int(stored.get('revision') or 0) + 1
                    product.save(KIND, stored)

    return {'status': 'checked' if checked else 'nothing_to_check',
            'reason': detail, 'checked': checked, 'affected': affected,
            'updated': updated, 'unchecked': unchecked}


def _active_investigation(product, case_id: str) -> dict | None:
    """这件事项上是否已经有一次在跑／待跑的调查。

    同一事项上不得同时存在两个互相覆盖的调查：它们各自花预算，最后在同一件事上
    互相改写状态。这里是**服务端**强制，而不是靠界面记得先查一遍。
    """
    for task in product.objects('care_task'):
        if (task.get('goal_type') == 'safety_case'
                and task.get('safety_case_id') == case_id
                and task['status'] not in ('completed', 'cancelled', 'failed')):
            return task
    return None


def _start_investigation(tasks, product, case: dict, row: dict) -> dict:
    """把一次触发落成一次**有界调查**（既有的 care_task 契约）。

    这里只受理与排队，不执行调查：调查本身由既有的 worker 消费 outbox 任务时跑，
    所以"等用户"与"还没到期"的路径不会碰模型预算。
    """
    running = _active_investigation(product, case['id'])
    if running is not None:
        return {'task': running, 'merged': True}
    key = f"followup:{row['id']}:{row['trigger_key']}"
    created = tasks.create(key, 'safety_case', case['id'])
    resumed = tasks.resume(created['id'], f'{key}:run', created['revision'],
                           'continue', enqueue=True)
    return {'task': resumed, 'merged': False}


def run_follow_ups(memory, *, product, tasks=None, now: str | None = None,
                   max_jobs: int = MAX_JOBS) -> dict:
    """消费长期跟进队列。由既有的 `OutboxWorker.drain_once` 驱动。

    **不新增调度框架，不新增后台模型循环**：租约、重试与状态列沿用既有队列的口径，
    入口就是那个唯一的 worker 周期。这里只做三件事：登记触发、认领触发、把认领到的
    触发落成一次调查。

    顺序是刻意的——调用点把本函数放在 `run_necessary_checks` **之后**，并且这里
    还会再挡一道：只要还有没跑完的确定性检查，任何一版模型调查都不消费。
    """
    from .safety_cases import SafetyCaseStore, STATUS_AWAITING_PROFESSIONAL, STATUS_AWAITING_USER
    now = now or _now()
    if tasks is None:
        from .care_tasks import CareTasks
        tasks = CareTasks(product)

    registered = scan_follow_ups(memory, product, now=now)
    with memory._lock, memory.connection:
        _recover_expired(memory, now)
        rows = [dict(row) for row in memory.connection.execute(
            "SELECT * FROM follow_up_runs WHERE status='open' ORDER BY id LIMIT ?",
            (max_jobs,))]

    store = SafetyCaseStore(product)
    triggered: list[dict] = []
    deferred: list[dict] = []
    blocked: list[dict] = []
    for row in rows:
        token = uuid.uuid4().hex
        expires = (_as_utc_plus(now, _lease_ttl()))
        with memory._lock, memory.connection:
            # 认领**不**计数：这里会"等必要检查跑完"再放回去，而等待不是失败。
            # 在认领时计次会让三次延迟把一次都没试过的触发直接判死。
            claimed = memory.connection.execute(
                "UPDATE follow_up_runs SET status='running', lease_token=?, "
                "lease_expires_at=?, updated_at=? "
                "WHERE id=? AND status='open'", (token, expires, now, row['id']))
            if claimed.rowcount != 1:
                continue

        try:
            case = store.get(row['case_id'])
        except Exception:
            _release(memory, row['id'], now, terminal=True, reason='事项不存在')
            continue
        follow_up = project_follow_up(case.get('follow_up'))
        revision = int((follow_up or {}).get('revision') or 1)
        if (not follow_up
                or follow_up.get('schedule_state') == SCHEDULE_CANCELLED
                or revision != int(row['schedule_revision'])):
            # 安排被改期或取消：旧触发不再执行副作用。
            # 这是**作废**，不是失败——记成失败会把它算进重试统计，也会让"最近一次
            # 执行失败了"这种界面提示出现在一件根本没执行过的事上。
            _release(memory, row['id'], now, status='cancelled',
                     reason='安排已改期或取消，旧触发作废')
            continue
        if pending_necessary_checks(memory):
            _release(memory, row['id'], now, reason=TRIGGER_DEFERRED_REASON)
            deferred.append({'case_id': row['case_id'], 'run_id': row['id'],
                             'reason': TRIGGER_DEFERRED_REASON})
            continue
        if case.get('current_status') in (STATUS_AWAITING_USER, STATUS_AWAITING_PROFESSIONAL):
            reason = '事项正在等待补充或专业复核，此时不启动新的模型调查'
            _release(memory, row['id'], now, reason=reason)
            deferred.append({'case_id': row['case_id'], 'run_id': row['id'],
                             'reason': reason})
            continue

        # 相关记录变化时，先把"答案是建立在哪些记录上"这件事交给约定接口复核。
        # 时间到了不算"记录变了"——那时没有可指认的变化，不该去打扰答案依赖。
        answer_recheck = None
        if follow_up.get('kind') == 'on_event':
            condition = follow_up.get('condition') or {}
            answer_recheck = recheck_answer_dependencies(
                product, case, changed_refs=[condition.get('ref')],
                reason=row.get('trigger_reason'))
        try:
            outcome = _start_investigation(tasks, product, case, row)
        except Exception as exc:
            message = f'{type(exc).__name__}: {exc}'
            attempts = int(row['attempts'] or 0) + 1
            _release(memory, row['id'], now, reason=message, attempts=attempts,
                     terminal=attempts >= MAX_ATTEMPTS)
            _stamp(product, row['case_id'], expected_revision=revision,
                   state=SCHEDULE_BLOCKED, event='follow_up_blocked',
                   blocked_reason=message)
            blocked.append({'case_id': row['case_id'], 'run_id': row['id'],
                            'error': message})
            continue

        task = outcome['task']
        with memory._lock, memory.connection:
            memory.connection.execute(
                "UPDATE follow_up_runs SET status='done', lease_token=NULL, "
                "lease_expires_at=NULL, care_task_id=?, result_json=?, updated_at=? "
                "WHERE id=?",
                (task['id'], packed_result(outcome, answer_recheck), now, row['id']))
        _stamp(product, row['case_id'], expected_revision=revision,
               state=SCHEDULE_TRIGGERED, event='follow_up_triggered',
               last_triggered_at=now, care_task_id=task['id'])
        triggered.append({'case_id': row['case_id'], 'run_id': row['id'],
                          'care_task_id': task['id'], 'merged': outcome['merged'],
                          'reason': row['trigger_reason']})

    return {'status': 'ok', 'registered': registered, 'triggered': triggered,
            'deferred': deferred, 'blocked': blocked,
            'pending': pending(memory)}


def _as_utc_plus(now: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).astimezone(
        timezone.utc).isoformat(timespec='seconds')


def _release(memory, run_id: int, now: str, *, reason: str, terminal: bool = False,
             attempts: int | None = None, status: str | None = None) -> None:
    """放回队列、判为失败、或作废。**原因一律留在 `error` 里**——不静默丢弃。

    `attempts` 只在该次尝试真的失败时递增；"等必要检查""等用户"这类延迟不计数。
    """
    target = status or ('failed' if terminal else 'open')
    with memory._lock, memory.connection:
        if attempts is None:
            memory.connection.execute(
                "UPDATE follow_up_runs SET status=?, lease_token=NULL, "
                "lease_expires_at=NULL, error=?, updated_at=? WHERE id=?",
                (target, reason, now, run_id))
        else:
            memory.connection.execute(
                "UPDATE follow_up_runs SET status=?, attempts=?, lease_token=NULL, "
                "lease_expires_at=NULL, error=?, updated_at=? WHERE id=?",
                (target, attempts, reason, now, run_id))


def packed_result(outcome: dict, answer_recheck: dict | None = None) -> str:
    """触发记录：调查、必要检查与模型预算**分别**留痕，不混成一个布尔量。"""
    import json
    return json.dumps({'care_task_id': outcome['task']['id'],
                       'merged': outcome['merged'],
                       'answer_recheck': answer_recheck}, ensure_ascii=False)


def pending(memory) -> dict[str, int]:
    row = memory.connection.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        "FROM follow_up_runs WHERE status IN ('open','running','failed')").fetchone()
    return {'unfinished': int(row['total'] or 0), 'failed': int(row['failed'] or 0)}


def condition_instance(condition: dict) -> str:
    parts = [str(condition.get('kind')), str(condition.get('ref'))]
    if condition.get('check_id'):
        parts.append(str(condition['check_id']))
    return 'on_event:' + ':'.join(parts)
