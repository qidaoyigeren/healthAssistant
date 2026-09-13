"""长期跟进 runtime：安排语义、调度状态机与触发身份。

这些用例锁的是**长期服务的可靠性**，不是实现细节：

* 有时间或条件 **不等于** 已确认（`confirmed` 只能来自确认记录）；
* `at` 必须带时区，`condition` 只接受白名单结构——未知类型必须 422，不许静默降级；
* 未到期不执行、到期才触发；重复扫描与进程重启不产生重复调查；
* 改期/取消让旧安排失效，旧触发不能再执行副作用；
* 相关变化触发、无关变化不触发；
* 必要安全检查先于依赖它的模型调查；
* provider 不可用时，安排仍在、安全结果仍在，任务可恢复。

全程离线：不调用真实模型，不使用 sleep（时间由显式 `now` 与显式时间戳控制）。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stage0.memory import MemoryStore
from stage0.product import ProductError, ProductStore
from stage0 import followup_runtime as fr
from stage0 import safety_cases as sc


def _ago(**kwargs) -> str:
    """相对现在的过去时刻（秒精度、带时区）——用来表达"已到期"。"""
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat(timespec='seconds')


def _ahead(**kwargs) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat(timespec='seconds')


class FollowUpValidationTests(unittest.TestCase):
    """§4.3 白名单与 §4.4 时区：两者都是**拒绝**而不是降级。"""

    # ---- §4.4 at 的时区 ---------------------------------------------------
    def test_a_naive_time_is_rejected(self):
        """没有时区的时间不是一个时刻，只是半句话。"""
        with self.assertRaises(ProductError) as caught:
            fr.normalise_at('2026-10-01T00:00:00')
        self.assertEqual(422, caught.exception.status)

    def test_a_time_without_any_offset_is_rejected(self):
        with self.assertRaises(ProductError) as caught:
            fr.normalise_at('2026-10-01')
        self.assertEqual(422, caught.exception.status)

    def test_an_offset_time_is_normalised_to_seconds_precision_utc(self):
        self.assertEqual('2026-10-01T00:00:00+00:00', fr.normalise_at('2026-10-01T08:00:00+08:00'))

    def test_a_zulu_millisecond_time_is_accepted_and_normalised(self):
        """前端送 `Date.toISOString()`：毫秒 + Z。服务端负责规范化，不是拒绝。"""
        self.assertEqual('2026-10-01T00:00:00+00:00', fr.normalise_at('2026-10-01T00:00:00.000Z'))

    def test_a_non_string_time_is_rejected(self):
        for bad in (123, [], {}):
            with self.assertRaises(ProductError) as caught:
                fr.normalise_at(bad)
            self.assertEqual(422, caught.exception.status)

    def test_a_nonsense_string_is_rejected_not_silently_dropped(self):
        with self.assertRaises(ProductError) as caught:
            fr.normalise_at('下周三')
        self.assertEqual(422, caught.exception.status)

    # ---- §4.3 condition 白名单 ---------------------------------------------
    def test_the_four_whitelisted_condition_kinds_are_accepted(self):
        for kind in ('conclusion_recorded', 'necessary_check',
                     'medication_change', 'fact_change'):
            parsed = fr.normalise_condition({'kind': kind, 'ref': 'memory:medication:45@2'})
            self.assertEqual(kind, parsed['kind'])
            self.assertEqual('memory:medication:45@2', parsed['ref'])

    def test_an_unknown_condition_kind_is_rejected_not_downgraded(self):
        """静默降级会让一条永远不会触发的安排看起来是正常的。"""
        with self.assertRaises(ProductError) as caught:
            fr.normalise_condition({'kind': 'whenever_the_doctor_says_so', 'ref': 'x'})
        self.assertEqual(422, caught.exception.status)

    def test_a_free_text_condition_is_rejected(self):
        """自然语言条件不被执行——只接受结构化白名单。"""
        with self.assertRaises(ProductError) as caught:
            fr.normalise_condition('等医生看完再说')
        self.assertEqual(422, caught.exception.status)

    def test_a_condition_missing_its_ref_is_rejected(self):
        with self.assertRaises(ProductError) as caught:
            fr.normalise_condition({'kind': 'conclusion_recorded'})
        self.assertEqual(422, caught.exception.status)

    def test_optional_keys_are_preserved_but_unknown_keys_do_not_leak_through(self):
        parsed = fr.normalise_condition({'kind': 'conclusion_recorded',
                                         'ref': 'memory:conclusion:123@1',
                                         'conclusion_kind': 'warning',
                                         'evil': 'x'})
        self.assertEqual('warning', parsed.get('conclusion_kind'))
        self.assertNotIn('evil', parsed)


class FollowUpConfirmationTests(unittest.TestCase):
    """§4.5：有时间或条件 ≠ 已确认。存量记录按 false 读。"""

    def test_a_time_alone_does_not_confirm_an_arrangement(self):
        follow_up = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        self.assertFalse(follow_up['confirmed'])
        self.assertIsNone(follow_up['confirmed_at'])
        self.assertIsNone(follow_up['confirmation_ref'])

    def test_a_condition_alone_does_not_confirm_an_arrangement(self):
        follow_up = fr.build_arrangement(
            kind='on_event', condition={'kind': 'fact_change', 'ref': 'memory:fact:1@1'})
        self.assertFalse(follow_up['confirmed'])

    def test_a_scheduled_arrangement_is_not_unscheduled(self):
        """"已安排"与"已确认"是两件事——未确认不等于没排上。"""
        follow_up = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        self.assertEqual('scheduled', follow_up['schedule_state'])

    def test_an_arrangement_with_neither_time_nor_condition_is_unscheduled(self):
        follow_up = fr.build_arrangement(kind='arrangement')
        self.assertEqual('unscheduled', follow_up['schedule_state'])

    def test_a_legacy_record_claiming_confirmation_without_a_record_reads_as_unconfirmed(self):
        """存量 `confirmed=True` 但没有确认时间/引用 = 损坏记录，按 false 读。"""
        legacy = {'kind': 'review_at', 'at': '2026-10-01T00:00:00+00:00',
                  'condition': None, 'owner': 'caregiver', 'note': None,
                  'recorded_at': '2026-09-01T00:00:00+00:00', 'confirmed': True}
        projected = fr.project_follow_up(legacy)
        self.assertFalse(projected['confirmed'])
        # 投影不补默认值：存量记录本来就没有这两个键，读了也是没有。
        self.assertIsNone(projected.get('confirmed_at'))
        self.assertIsNone(projected.get('confirmation_ref'))

    def test_a_fully_recorded_confirmation_reads_as_confirmed(self):
        follow_up = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        confirmed = fr.apply_confirmation(follow_up, by='caregiver-1',
                                          confirmation_ref='confirmation:abc',
                                          at='2026-09-13T00:00:00+00:00')
        self.assertTrue(confirmed['confirmed'])
        self.assertEqual('caregiver-1', confirmed['confirmed_by'])
        self.assertEqual('confirmation:abc', confirmed['confirmation_ref'])
        self.assertTrue(fr.project_follow_up(confirmed)['confirmed'])

    def test_a_confirmation_missing_its_reference_reads_as_unconfirmed(self):
        broken = {'kind': 'review_at', 'at': '2026-10-01T00:00:00+00:00',
                  'confirmed': True, 'confirmed_at': '2026-09-13T00:00:00+00:00',
                  'confirmed_by': 'x', 'confirmation_ref': None,
                  'recorded_at': '2026-09-01T00:00:00+00:00'}
        self.assertFalse(fr.project_follow_up(broken)['confirmed'])

    def test_projection_does_not_invent_fields_on_a_missing_arrangement(self):
        self.assertIsNone(fr.project_follow_up(None))

    def test_projection_adds_no_default_to_an_arrangement_it_does_not_understand(self):
        """不加工、不猜测、不补默认值：读不懂就按未确认读，并保留原键。"""
        weird = {'kind': 'review_at', 'at': '2026-10-01T00:00:00+00:00',
                 'confirmed': True, 'recorded_at': 'x', 'custom': 7}
        projected = fr.project_follow_up(weird)
        self.assertFalse(projected['confirmed'])
        self.assertEqual(7, projected['custom'])


class FollowUpTriggerIdentityTests(unittest.TestCase):
    """幂等触发身份至少包含：事项、安排版本、触发实例。"""

    def test_the_same_trigger_instance_has_one_identity(self):
        follow_up = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        a = fr.trigger_key('safety-case:1', follow_up, 'review_at:2026-10-01T00:00:00+00:00')
        b = fr.trigger_key('safety-case:1', follow_up, 'review_at:2026-10-01T00:00:00+00:00')
        self.assertEqual(a, b)

    def test_a_different_arrangement_version_is_a_different_identity(self):
        first = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        second = {**first, 'revision': first['revision'] + 1}
        self.assertNotEqual(
            fr.trigger_key('safety-case:1', first, 'i'),
            fr.trigger_key('safety-case:1', second, 'i'))

    def test_a_different_case_is_a_different_identity(self):
        follow_up = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        self.assertNotEqual(
            fr.trigger_key('safety-case:1', follow_up, 'i'),
            fr.trigger_key('safety-case:2', follow_up, 'i'))

    def test_a_different_instance_is_a_different_identity(self):
        follow_up = fr.build_arrangement(kind='review_at', at='2026-10-01T00:00:00+00:00')
        self.assertNotEqual(
            fr.trigger_key('safety-case:1', follow_up, 'i1'),
            fr.trigger_key('safety-case:1', follow_up, 'i2'))


class _StoreFixture(unittest.TestCase):
    """隔离数据库 + 一件真实的安全事项。沿用 `test_safety_cases.py` 的脚手架。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='followup-runtime-')
        self.memory = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.product = ProductStore(self.memory)
        self.cases = sc.SafetyCaseStore(self.product)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def add_drug(self, name, *, key=None, dose='5mg'):
        return self.memory.apply_medication_change(
            action='add', name=name, ingredients=[], session_id='s',
            turn_id=key or name, source='caregiver', dose=dose)

    def open_case(self, key='obs1') -> dict:
        """建一次，之后每次返回**当前**状态——否则第二次调用会撞幂等键。"""
        opened = getattr(self, '_opened_cases', None)
        if opened is None:
            opened = self._opened_cases = {}
        if key not in opened:
            first = self.add_drug('华法林', key='w1')['medication']
            second = self.add_drug('阿司匹林', key='w2')['medication']
            text = '阿司匹林 × 华法林：出血风险升高。'
            conclusion = self.memory.record_conclusion(
                session_id='s', turn_id=key, kind='warning', text=text,
                memory_refs=[first['ref'], second['ref']],
                source_refs=[{'uri': 'label://x', 'text': text}])
            opened[key] = sc.observe_conclusion(self.product, conclusion, key=key)['id']
        return self.cases.get(opened[key])


class FollowUpActionTests(_StoreFixture):
    """§4.6 的三个动作：安排/改期、取消、确认。"""

    def arrange(self, case, *, at=None, kind='review_at', condition=None, key='sched1',
                revision=None):
        return self.cases.schedule_follow_up(
            case['id'], expected_revision=case['revision'] if revision is None else revision,
            kind=kind, at=at, condition=condition, command_key=key)

    # ---- 安排 --------------------------------------------------------------
    def test_scheduling_records_an_arrangement_that_is_not_yet_confirmed(self):
        case = self.open_case()
        updated = self.arrange(case, at='2026-10-01T00:00:00+00:00')
        follow_up = updated['follow_up']
        self.assertEqual('review_at', follow_up['kind'])
        self.assertEqual('2026-10-01T00:00:00+00:00', follow_up['at'])
        self.assertFalse(follow_up['confirmed'])
        self.assertIsNone(follow_up['confirmation_ref'])
        self.assertEqual('scheduled', follow_up['schedule_state'])

    def test_scheduling_rejects_a_naive_time(self):
        case = self.open_case()
        with self.assertRaises(ProductError):
            self.arrange(case, at='2026-10-01T00:00:00')

    def test_scheduling_rejects_an_unknown_condition_kind(self):
        case = self.open_case()
        with self.assertRaises(ProductError):
            self.arrange(case, kind='on_event',
                         condition={'kind': 'vibes', 'ref': 'memory:fact:1@1'})

    def test_scheduling_a_resolved_case_is_refused(self):
        """终态不可再安排——否则用户会以为一件已经关闭的事又有人在跟。"""
        case = self.open_case()
        # 合法地把事项推到终态：停药 → 触发条件客观消除 → 有依据的关闭。
        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        self.memory.recheck_pending()
        resolved = self.cases.disposition(
            case['id'], expected_revision=self.cases.get(case['id'])['revision'],
            disposition=sc.DISPOSITION_RESOLVED, basis_kind=sc.BASIS_CHECK,
            actor='caregiver-1', roles=('caregiver',))
        self.assertEqual(sc.STATUS_RESOLVED, resolved['current_status'])
        with self.assertRaises(ProductError) as caught:
            self.arrange(resolved, at='2026-10-01T00:00:00+00:00')
        self.assertEqual(409, caught.exception.status)

    def test_scheduling_with_a_stale_revision_is_refused(self):
        case = self.open_case()
        with self.assertRaises(ProductError) as caught:
            self.arrange(case, at='2026-10-01T00:00:00+00:00', revision=case['revision'] + 5)
        self.assertEqual(409, caught.exception.status)

    # ---- 改期让旧安排失效 ---------------------------------------------------
    def test_rescheduling_advances_the_arrangement_version(self):
        case = self.open_case()
        first = self.arrange(case, at='2026-10-01T00:00:00+00:00', key='s1')
        second = self.arrange(first, at='2026-11-01T00:00:00+00:00', key='s2')
        self.assertGreater(second['follow_up']['revision'], first['follow_up']['revision'])
        self.assertEqual('2026-11-01T00:00:00+00:00', second['follow_up']['at'])

    # ---- 取消保留历史 -------------------------------------------------------
    def test_cancelling_keeps_the_history_and_only_the_state_says_cancelled(self):
        case = self.open_case()
        scheduled = self.arrange(case, at='2026-10-01T00:00:00+00:00')
        cancelled = self.cases.cancel_follow_up(
            scheduled['id'], expected_revision=scheduled['revision'],
            reason='医生已另行安排', actor='caregiver-1', command_key='cancel1')
        follow_up = cancelled['follow_up']
        self.assertEqual('cancelled', follow_up['schedule_state'])
        # 取消**不清空**历史字段。
        self.assertEqual('2026-10-01T00:00:00+00:00', follow_up['at'])
        self.assertEqual('caregiver', follow_up['owner'])
        self.assertGreater(follow_up['revision'], scheduled['follow_up']['revision'])

    # ---- 确认 --------------------------------------------------------------
    def test_confirming_an_arrangement_records_who_and_when(self):
        case = self.open_case()
        scheduled = self.arrange(case, at='2026-10-01T00:00:00+00:00')
        confirmed = self.cases.confirm_follow_up(
            scheduled['id'], expected_revision=scheduled['revision'],
            actor='caregiver-1', note='我负责这个复查', command_key='conf1')
        follow_up = confirmed['follow_up']
        self.assertTrue(follow_up['confirmed'])
        self.assertEqual('caregiver-1', follow_up['confirmed_by'])
        self.assertIsNotNone(follow_up['confirmed_at'])
        self.assertIsNotNone(follow_up['confirmation_ref'])

    def test_confirming_does_not_change_the_arrangement_version(self):
        """确认改变的是"谁承诺了"，不是"安排是什么"——旧触发不该因此作废。"""
        case = self.open_case()
        scheduled = self.arrange(case, at='2026-10-01T00:00:00+00:00')
        confirmed = self.cases.confirm_follow_up(
            scheduled['id'], expected_revision=scheduled['revision'],
            actor='caregiver-1', command_key='conf1')
        self.assertEqual(scheduled['follow_up']['revision'], confirmed['follow_up']['revision'])

    def test_confirming_without_an_arrangement_is_refused(self):
        """没有安排就没有可确认的东西。"""
        case = self.open_case()
        with self.assertRaises(ProductError) as caught:
            self.cases.confirm_follow_up(case['id'], expected_revision=case['revision'],
                                         actor='caregiver-1', command_key='conf-none')
        self.assertEqual(409, caught.exception.status)

    def test_confirming_a_cancelled_arrangement_is_refused(self):
        case = self.open_case()
        scheduled = self.arrange(case, at='2026-10-01T00:00:00+00:00')
        cancelled = self.cases.cancel_follow_up(
            scheduled['id'], expected_revision=scheduled['revision'],
            actor='caregiver-1', command_key='cancel1')
        with self.assertRaises(ProductError) as caught:
            self.cases.confirm_follow_up(cancelled['id'],
                                         expected_revision=cancelled['revision'],
                                         actor='caregiver-1', command_key='conf-cancelled')
        self.assertEqual(409, caught.exception.status)

    # ---- 三个动作都写 history ----------------------------------------------
    def test_all_three_actions_are_recorded_in_history(self):
        case = self.open_case()
        scheduled = self.arrange(case, at='2026-10-01T00:00:00+00:00')
        confirmed = self.cases.confirm_follow_up(
            scheduled['id'], expected_revision=scheduled['revision'],
            actor='caregiver-1', command_key='conf1')
        cancelled = self.cases.cancel_follow_up(
            confirmed['id'], expected_revision=confirmed['revision'],
            actor='caregiver-1', command_key='cancel1')
        events = [entry['event'] for entry in cancelled['history']]
        self.assertIn('follow_up_scheduled', events)
        self.assertIn('follow_up_confirmed', events)
        self.assertIn('follow_up_cancelled', events)

    # ---- 处置路径不再自动确认 -----------------------------------------------
    def test_a_monitoring_disposition_no_longer_auto_confirms(self):
        """§4.5 的核心：处置时给一个时间，不等于有人做过确认。"""
        case = self.open_case()
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition='accepted_monitoring', basis_kind='monitoring_arrangement',
            actor='caregiver-1', roles=('caregiver',),
            follow_up={'kind': 'review_at', 'at': '2026-10-01T00:00:00+00:00'})
        follow_up = monitored['follow_up']
        self.assertFalse(follow_up['confirmed'])
        self.assertIsNone(follow_up['confirmation_ref'])
        self.assertEqual('scheduled', follow_up['schedule_state'])


class _RuntimeFixture(_StoreFixture):
    """带运行时的夹具：一件已确认的安排 + 一个可观察的 worker。"""

    def setUp(self):
        super().setUp()
        from stage0.care_tasks import CareTasks
        self.tasks = CareTasks(self.product)

    # ---- 观察面 ------------------------------------------------------------
    def settle_checks(self) -> None:
        """把已经排上的必要检查跑完。

        建药本身会排一次确定性检查——这是既有行为，也正是"检查先于调查"的由来。
        用**空的**检测器跑，是为了不让本套用例依赖真实相互作用数据：这里验的是
        runtime 的调度，不是检测器。
        """
        from stage0 import safety_checks as checks
        for _ in range(5):
            report = checks.run_necessary_checks(
                self.memory, detector=lambda medications: [], rag_tool=None,
                product=self.product)
            if not report['completed'] and not checks.pending(self.memory)['unfinished']:
                break
            if not checks.pending(self.memory)['unfinished']:
                break

    def open_case(self, key='obs1') -> dict:
        case = super().open_case(key)
        self.settle_checks()
        return self.cases.get(case['id'])

    def care_tasks(self, case_id=None) -> list[dict]:
        items = [t for t in self.product.objects('care_task')
                 if t.get('goal_type') == 'safety_case']
        if case_id is not None:
            items = [t for t in items if t.get('safety_case_id') == case_id]
        return items

    def run_rows(self, case_id: str) -> list[dict]:
        return [dict(row) for row in self.memory.connection.execute(
            'SELECT * FROM follow_up_runs WHERE case_id=? ORDER BY id', (case_id,))]

    def outbox_rows(self) -> list[dict]:
        return [dict(row) for row in self.memory.connection.execute(
            'SELECT * FROM outbox_tasks ORDER BY id')]

    def sweep(self, *, now=None, tasks=None):
        return fr.run_follow_ups(self.memory, product=self.product,
                                 tasks=tasks or self.tasks, now=now)

    # ---- 脚手架 ------------------------------------------------------------
    def confirmed_arrangement(self, *, at=None, condition=None, kind='review_at',
                              case=None, prefix='fa'):
        """安排 + 确认 + 返回最新的事项。确认是**单独一步**，因为它是一次单独的承诺。"""
        case = case or self.open_case()
        if case.get('follow_up') is None:
            case = self.cases.schedule_follow_up(
                case['id'], expected_revision=case['revision'], kind=kind, at=at,
                condition=condition, command_key=f'{prefix}-schedule')
        return self.cases.confirm_follow_up(
            case['id'], expected_revision=case['revision'], actor='caregiver-1',
            command_key=f'{prefix}-confirm')

    def register_change(self, *, kind: str, ref: str, done=True, suffix='1'):
        """登记一次"记录变化"——这正是 runtime 读取的持久信号。

        直接写队列表是因为本用例要验的是 **runtime 怎么读它**；检查本身怎么跑由
        `test_safety_checks.py` 覆盖。`done=False` 表示检查还排着队。
        """
        from stage0 import safety_checks as checks
        checks.enqueue(self.memory, trigger_kind=kind, trigger_ref=ref,
                       subject_key=f'subject-{suffix}', reason='测试用变化')
        self.memory.connection.execute(
            "UPDATE necessary_checks SET status=? WHERE trigger_ref=? AND subject_key=?",
            ('done' if done else 'open', ref, f'subject-{suffix}'))
        self.memory.connection.commit()


class FollowUpRuntimeTimingTests(_RuntimeFixture):
    """到期才触发。等待中的安排**不消费模型预算**。"""

    def test_an_arrangement_with_nothing_to_fire_on_never_triggers(self):
        """没有可触发的东西（`kind='arrangement'` 备忘，无时间无条件）就不开工。

        > 集成说明：本用例原先断言的是"**未确认**的安排永不执行"
        > （`test_an_unconfirmed_arrangement_never_triggers`）。那一条**与冻结契约
        > 冲突**，集成时按契约改掉了：CONTRACT §4.5 把"已安排"与"已确认"定成两件
        > 事，§4.7 又要求 `schedule_state` 必须能前进、**不得**"永远停在 scheduled
        > 来伪装成功"。若执行以 `confirmed` 为前置条件，则经由处置端点建立的安排
        > （本轮之前**唯一**的写入路径，其 `confirmed` 恒为 false）在到期后永远
        > 停在 scheduled——那正是 §4.7 点名禁止的形态。
        > "别在没承诺时花模型预算"这个关切由**别的**东西承担：未到期不执行
        > （下一条用例）、取消即不再执行、以及本条的"没有可触发的东西就不开工"。
        """
        case = self.open_case()
        scheduled = self.cases.schedule_follow_up(
            case['id'], expected_revision=case['revision'], kind='arrangement',
            command_key='s1')
        self.sweep()
        self.assertEqual([], self.care_tasks(scheduled['id']))
        self.assertEqual([], self.run_rows(scheduled['id']))
        self.assertEqual('unscheduled',
                         self.cases.get(scheduled['id'])['follow_up']['schedule_state'])

    def test_an_arrangement_that_is_not_due_yet_does_not_run(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ahead(hours=2), prefix='future')
        self.sweep()
        self.assertEqual([], self.care_tasks(confirmed['id']))
        self.assertEqual('scheduled', self.cases.get(confirmed['id'])['follow_up']['schedule_state'])

    def test_a_due_arrangement_triggers_an_investigation(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='due')
        report = self.sweep()
        self.assertEqual(1, len(report['triggered']))
        tasks = self.care_tasks(confirmed['id'])
        self.assertEqual(1, len(tasks), '到期应当产生一次调查')
        follow_up = self.cases.get(confirmed['id'])['follow_up']
        self.assertEqual('triggered', follow_up['schedule_state'])
        self.assertIsNotNone(follow_up['last_triggered_at'])
        self.assertEqual(tasks[0]['id'], follow_up['care_task_id'])

    def test_an_arrangement_that_is_not_confirmed_still_runs(self):
        """已安排 ≠ 已确认（CONTRACT §4.5）。

        确认记录回答的是"谁承诺了这件事"，不是"这条安排存不存在"。拿 `confirmed`
        当可执行的前置条件，会让每一条经由处置端点建立的安排在到期后**永远停在
        scheduled**——而处置端点是本轮之前唯一的写入路径，它产出的 confirmed
        恒为 false，等于长期跟进对主要路径整个失效。
        """
        case = self.open_case()
        scheduled = self.cases.schedule_follow_up(
            case['id'], expected_revision=case['revision'], kind='review_at',
            at=_ago(minutes=5), command_key='unconfirmed-schedule')
        self.assertFalse(scheduled['follow_up']['confirmed'],
                         '只给了时间，不该被算成已确认')
        self.assertEqual('scheduled', scheduled['follow_up']['schedule_state'])

        report = self.sweep()
        self.assertEqual(1, len(report['triggered']),
                         f'未确认但已到期的安排没有执行：{report}')
        follow_up = self.cases.get(case['id'])['follow_up']
        self.assertNotEqual('scheduled', follow_up['schedule_state'],
                            '未确认的安排永远停在 scheduled——"有人会跟进"只是墙上的字')

    def test_the_trigger_time_is_controllable_not_wall_clock_guessed(self):
        """同一个安排，在"还没到"和"已过点"两个时刻扫描，结果不同。"""
        case = self.open_case()
        at = _ahead(hours=1)
        confirmed = self.confirmed_arrangement(at=at, prefix='ctl')
        self.sweep()
        self.assertEqual([], self.care_tasks(confirmed['id']))
        # 把"现在"推到安排时间之后——不是 sleep，是显式时间。
        later = (datetime.fromisoformat(at) + timedelta(minutes=1)).isoformat(timespec='seconds')
        report = self.sweep(now=later)
        self.assertEqual(1, len(report['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))


class FollowUpRuntimeIdempotenceTests(_RuntimeFixture):
    """重复扫描与进程重启不得创建重复调查。"""

    def test_scanning_twice_does_not_start_two_investigations(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='twice')
        self.sweep()
        self.sweep()
        self.sweep()
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))
        self.assertEqual(1, len(self.run_rows(confirmed['id'])))

    def test_a_restarted_process_recovers_a_claimed_but_unfinished_trigger(self):
        """worker 崩溃留下过期租约：恢复的是**同一件**任务，不是新起一件。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='crash')
        self.sweep()
        row = self.run_rows(confirmed['id'])[0]
        # 模拟崩溃：任务被认领后进程死掉，租约过期。
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec='seconds')
        self.memory.connection.execute(
            "UPDATE follow_up_runs SET status='running', lease_token='dead', "
            "lease_expires_at=? WHERE id=?", (past, row['id']))
        self.memory.connection.commit()
        self.sweep()
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])),
                         '恢复不得创建第二件调查')
        self.assertEqual('done', self.run_rows(confirmed['id'])[0]['status'])

    def test_a_sweep_that_changes_nothing_does_not_churn_the_case(self):
        """worker 每 0.2 秒扫一次；一次没变化的扫描不该推进 revision、也不该刷历史。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ahead(hours=2), prefix='quiet')
        before = self.cases.get(confirmed['id'])
        before_events = [e['event'] for e in before['history']]
        for _ in range(5):
            self.sweep()
        after = self.cases.get(confirmed['id'])
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual(before_events, [e['event'] for e in after['history']],
                         '没有变化的扫描不该往历史里写任何东西')

    def test_a_sweep_that_changes_nothing_does_not_churn_a_blocked_case(self):
        """阻塞状态也不该被反复重写——否则历史会被"还是阻塞着"刷满。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='quiet-b')

        class ExplodingTasks:
            def create(self, *a, **k):
                raise RuntimeError('provider unavailable')

        self.sweep(tasks=ExplodingTasks())
        blocked = self.cases.get(confirmed['id'])
        for _ in range(4):
            self.sweep(tasks=ExplodingTasks())
        after = self.cases.get(confirmed['id'])
        self.assertEqual('blocked', after['follow_up']['schedule_state'])
        self.assertLessEqual(len(after['history']) - len(blocked['history']),
                             fr.MAX_ATTEMPTS,
                             '重试期间不该每个周期都往历史里写一条')

    def test_the_trigger_identity_is_stored_not_remembered_in_process(self):
        """身份写在数据库里：换一个进程、换一次扫描，认的是同一行。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='ident')
        self.sweep()
        row = self.run_rows(confirmed['id'])[0]
        self.assertEqual(confirmed['id'], row['case_id'])
        self.assertEqual(confirmed['follow_up']['revision'], row['schedule_revision'])
        self.assertTrue(row['trigger_key'])


class FollowUpRuntimeInvalidationTests(_RuntimeFixture):
    """改期或取消后，旧安排不能继续执行副作用。"""

    def test_cancelling_an_arrangement_stops_it_from_ever_triggering(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ahead(hours=1), prefix='cancel')
        cancelled = self.cases.cancel_follow_up(
            confirmed['id'], expected_revision=confirmed['revision'],
            reason='医生已另行安排', actor='caregiver-1', command_key='c1')
        # 时间已经过了，但安排已经取消。
        later = _ahead(hours=2)
        report = self.sweep(now=later)
        self.assertEqual([], report['triggered'])
        self.assertEqual([], self.care_tasks(cancelled['id']))
        self.assertEqual('cancelled',
                         self.cases.get(cancelled['id'])['follow_up']['schedule_state'])

    def test_rescheduling_invalidates_a_trigger_that_was_already_queued(self):
        """改期之后，**旧版本**已排队但尚未执行的触发被作废；新版本按新时间走。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='resched')
        # 只排队、不执行：这正是"已排上但还没跑"的那个窗口。
        fr.scan_follow_ups(self.memory, self.product)
        self.assertEqual(1, len(self.run_rows(confirmed['id'])))
        self.assertEqual('open', self.run_rows(confirmed['id'])[0]['status'])

        rescheduled = self.cases.schedule_follow_up(
            self.cases.get(confirmed['id'])['id'],
            expected_revision=self.cases.get(confirmed['id'])['revision'],
            kind='review_at', at=_ahead(hours=3), command_key='resched-again')
        self.assertGreater(rescheduled['follow_up']['revision'],
                           confirmed['follow_up']['revision'])
        self.assertEqual('cancelled', self.run_rows(confirmed['id'])[0]['status'],
                         '改期必须让尚未执行的旧触发作废')

        # 新版本还没到期：不触发，也不会补跑旧版本。
        report = self.sweep()
        self.assertEqual([], report['triggered'])
        self.assertEqual([], self.care_tasks(confirmed['id']))

    def test_a_stale_trigger_that_comes_back_is_voided_not_counted_as_a_failure(self):
        """作废不是失败：它没执行过，不该算进重试统计，也不该显示成"执行失败"。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='stale')
        fr.scan_follow_ups(self.memory, self.product)
        run_id = self.run_rows(confirmed['id'])[0]['id']
        self.cases.schedule_follow_up(
            self.cases.get(confirmed['id'])['id'],
            expected_revision=self.cases.get(confirmed['id'])['revision'],
            kind='review_at', at=_ahead(hours=3), command_key='stale-resched')
        # 模拟这条已经被作废的触发又被"捡回来"（例如租约恢复把它放回队列）。
        self.memory.connection.execute(
            "UPDATE follow_up_runs SET status='open', attempts=0 WHERE id=?", (run_id,))
        self.memory.connection.commit()

        self.sweep()

        row = self.run_rows(confirmed['id'])[0]
        self.assertEqual('cancelled', row['status'], '作废不应记成 failed')
        self.assertEqual(0, row['attempts'])
        self.assertEqual([], self.care_tasks(confirmed['id']))

    def test_a_cancelled_arrangement_cannot_be_confirmed_again(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ahead(hours=1), prefix='cc')
        cancelled = self.cases.cancel_follow_up(
            confirmed['id'], expected_revision=confirmed['revision'],
            actor='caregiver-1', command_key='cc-cancel')
        # 换一个幂等键：沿用同一个键会命中回执重放，测的就不是"能不能确认"了。
        with self.assertRaises(ProductError) as caught:
            self.cases.confirm_follow_up(cancelled['id'],
                                         expected_revision=cancelled['revision'],
                                         actor='caregiver-1', command_key='cc-confirm-again')
        self.assertEqual(409, caught.exception.status)


class FollowUpRuntimeConditionTests(_RuntimeFixture):
    """相关变化触发，无关变化不触发。条件由**持久信号**求值，不执行自然语言。"""

    def test_a_relevant_record_change_triggers_the_arrangement(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'medication_change', 'ref': '华法林'},
            prefix='rel')
        self.register_change(kind='medication_set', ref='华法林')
        report = self.sweep()
        self.assertEqual(1, len(report['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))
        self.assertEqual('triggered',
                         self.cases.get(confirmed['id'])['follow_up']['schedule_state'])

    def test_an_unrelated_record_change_does_not_trigger_the_arrangement(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'medication_change', 'ref': '布洛芬'},
            prefix='unrel')
        self.register_change(kind='medication_set', ref='华法林')
        report = self.sweep()
        self.assertEqual([], report['triggered'])
        self.assertEqual([], self.care_tasks(confirmed['id']))
        self.assertEqual('scheduled',
                         self.cases.get(confirmed['id'])['follow_up']['schedule_state'])

    def test_a_fact_change_is_matched_on_its_own_namespace(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'fact_change', 'ref': 'renal_function:egfr'},
            prefix='fact')
        self.register_change(kind='condition_facts', ref='renal_function:egfr')
        self.assertEqual(1, len(self.sweep()['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))

    def test_a_necessary_check_condition_matches_checks_of_either_trigger_kind(self):
        """`necessary_check` 说的是"某项必要检查完成"，不是"某项**用药**检查完成"。

        只认用药类触发会让"患者事实变化后的检查完成"这类条件永远不触发——一条
        永远不触发的安排看起来和一条正常排上的安排一模一样。
        """
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event',
            condition={'kind': 'necessary_check', 'ref': 'renal_function:egfr'},
            prefix='check-any')
        self.register_change(kind='condition_facts', ref='renal_function:egfr', done=True)
        report = self.sweep()
        self.assertEqual(1, len(report['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))

    def test_a_necessary_check_condition_waits_until_the_check_is_done(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event',
            condition={'kind': 'necessary_check', 'ref': 'renal_function:egfr'},
            prefix='check-wait')
        self.register_change(kind='condition_facts', ref='renal_function:egfr', done=False)
        self.assertEqual([], self.sweep()['triggered'])
        self.memory.connection.execute(
            "UPDATE necessary_checks SET status='done' WHERE trigger_ref='renal_function:egfr'")
        self.memory.connection.commit()
        self.assertEqual(1, len(self.sweep()['triggered']))

    def test_a_conclusion_being_recorded_can_be_the_condition(self):
        case = self.open_case()
        conclusion = self.memory.record_conclusion(
            session_id='s', turn_id='later', kind='warning',
            text='华法林：需要复查凝血功能。',
            memory_refs=case['related_medication_refs'][:1],
            source_refs=[{'uri': 'label://y', 'text': 'x'}])
        ref = f"memory:conclusion:{conclusion['id']}@v1"
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'conclusion_recorded', 'ref': ref},
            prefix='concl')
        report = self.sweep()
        self.assertEqual(1, len(report['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))


class FollowUpRuntimeOrderingTests(_RuntimeFixture):
    """必要安全检查先于依赖它的模型调查。"""

    def test_a_pending_necessary_check_defers_the_investigation(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'medication_change', 'ref': '华法林'},
            prefix='order')
        self.register_change(kind='medication_set', ref='华法林', done=False)

        report = self.sweep()
        self.assertEqual([], report['triggered'])
        self.assertEqual([], self.care_tasks(confirmed['id']),
                         '检查还没跑完，就不能消费这一版的模型调查')
        self.assertIn(confirmed['id'], [item['case_id'] for item in report['deferred']])

        # 检查跑完之后，同一个触发才被消费——没有第二次扫描的必要条件。
        self.memory.connection.execute(
            "UPDATE necessary_checks SET status='done' WHERE trigger_ref='华法林'")
        self.memory.connection.commit()
        report = self.sweep()
        self.assertEqual(1, len(report['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))

    def test_waiting_for_a_check_does_not_spend_the_retry_allowance(self):
        """等检查不是失败。延迟计次会让一次都没试过的触发被"等"死。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'medication_change', 'ref': '华法林'},
            prefix='patient')
        self.register_change(kind='medication_set', ref='华法林', done=False)

        for _ in range(fr.MAX_ATTEMPTS + 3):
            self.sweep()
        row = self.run_rows(confirmed['id'])[0]
        self.assertEqual('open', row['status'], '等待不该把它判为失败')
        self.assertEqual(0, row['attempts'], '等待不该消耗重试次数')

        self.memory.connection.execute(
            "UPDATE necessary_checks SET status='done' WHERE trigger_ref='华法林'")
        self.memory.connection.commit()
        self.assertEqual(1, len(self.sweep()['triggered']))
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))

    def test_a_case_waiting_on_the_user_does_not_consume_model_budget(self):
        case = self.open_case()
        self.cases.require_input(
            case['id'], request_id='q1', question='停药多久了？',
            command_key='q1-key')
        refreshed = self.cases.get(case['id'])
        confirmed = self.confirmed_arrangement(at=_ago(minutes=1), case=refreshed,
                                               prefix='waiting')
        report = self.sweep()
        self.assertEqual([], report['triggered'])
        self.assertEqual([], self.care_tasks(confirmed['id']))
        self.assertIn(confirmed['id'], [item['case_id'] for item in report['deferred']])


class FollowUpRuntimeFailureTests(_RuntimeFixture):
    """模型失败不影响安排持久化与已有安全结果。"""

    def test_a_failed_investigation_leaves_the_arrangement_and_the_safety_result(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='fail')
        before = list(case.get('linked_conclusion_refs') or [])

        class ExplodingTasks:
            def create(self, *a, **k):
                raise RuntimeError('provider unavailable')

        scheduled_at = confirmed['follow_up']['at']
        report = self.sweep(tasks=ExplodingTasks())
        self.assertEqual(1, len(report['blocked']))
        follow_up = self.cases.get(confirmed['id'])['follow_up']
        self.assertEqual('blocked', follow_up['schedule_state'])
        self.assertTrue(follow_up['blocked_reason'])
        # 安排还在（时间没有被改写、确认没有被撤销），安全结果还在。
        self.assertEqual(scheduled_at, follow_up['at'])
        self.assertTrue(follow_up['confirmed'])
        self.assertIsNotNone(follow_up['confirmation_ref'])
        self.assertEqual(before, self.cases.get(confirmed['id'])['linked_conclusion_refs'])

    def test_a_blocked_trigger_recovers_when_the_provider_comes_back(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='recover')

        class ExplodingTasks:
            def create(self, *a, **k):
                raise RuntimeError('provider unavailable')

        self.sweep(tasks=ExplodingTasks())
        self.assertEqual('blocked',
                         self.cases.get(confirmed['id'])['follow_up']['schedule_state'])
        # 同一行仍然在队列里，provider 回来后继续，而不是重新排一条。
        self.sweep()
        self.assertEqual(1, len(self.care_tasks(confirmed['id'])))
        self.assertEqual('triggered',
                         self.cases.get(confirmed['id'])['follow_up']['schedule_state'])

    def test_a_failed_trigger_is_counted_and_eventually_stops_retrying_loudly(self):
        """重试有上限，且失败**保持可见**——不会被静默丢掉。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='cap')

        class ExplodingTasks:
            def create(self, *a, **k):
                raise RuntimeError('provider unavailable')

        for _ in range(fr.MAX_ATTEMPTS):
            self.sweep(tasks=ExplodingTasks())
        row = self.run_rows(confirmed['id'])[0]
        self.assertEqual('failed', row['status'])
        self.assertGreaterEqual(row['attempts'], fr.MAX_ATTEMPTS)
        self.assertTrue(row['error'])

    def test_an_exhausted_trigger_does_not_look_merely_queued(self):
        """重试耗尽后，安排必须**仍然显示为阻塞**。

        否则它会退回到一个"已排队"的样子，而对应的触发行已经 `failed`、永远不会再
        执行——那是"永远停在 scheduled"换了一身衣服，同样是把没人会做的事显示成
        有人会做。
        """
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='exhaust')

        class ExplodingTasks:
            def create(self, *a, **k):
                raise RuntimeError('provider unavailable')

        for _ in range(fr.MAX_ATTEMPTS):
            self.sweep(tasks=ExplodingTasks())
        # 再扫几次：耗尽之后不该有任何东西把它"洗白"成排队中。
        for _ in range(3):
            report = self.sweep(tasks=ExplodingTasks())
            self.assertEqual([], report['triggered'])

        follow_up = self.cases.get(confirmed['id'])['follow_up']
        self.assertEqual('blocked', follow_up['schedule_state'],
                         '重试耗尽后不得回到"已排队"的样子')
        self.assertTrue(follow_up['blocked_reason'])


class FollowUpStatusSurfaceTests(_RuntimeFixture):
    """对外状态：下一次跟进、触发原因、是否排队、正在处理、逾期、失败、可恢复。"""

    def test_the_case_view_reports_the_next_follow_up_and_why_it_fired(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='surface')
        self.sweep()
        view = sc.case_view(self.cases, self.cases.get(confirmed['id']))
        follow_up = view['follow_up']
        self.assertEqual('triggered', follow_up['schedule_state'])
        self.assertTrue(follow_up['last_trigger_reason'])
        self.assertTrue(follow_up['care_task_id'])

    def test_the_case_view_reports_a_blocked_arrangement_with_its_reason(self):
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='surface-b')

        class ExplodingTasks:
            def create(self, *a, **k):
                raise RuntimeError('provider unavailable')

        self.sweep(tasks=ExplodingTasks())
        view = sc.case_view(self.cases, self.cases.get(confirmed['id']))
        self.assertEqual('blocked', view['follow_up']['schedule_state'])
        self.assertIn('provider', view['follow_up']['blocked_reason'])

    def test_a_legacy_confirmed_record_reads_as_unconfirmed_through_the_case_view(self):
        """§4.5 的存量回退必须在**对外视图**上成立，而不只是在函数里。"""
        case = self.open_case()
        stored = self.cases.get(case['id'])
        stored['follow_up'] = {'kind': 'review_at', 'at': '2026-10-01T00:00:00+00:00',
                               'condition': None, 'owner': 'caregiver', 'note': None,
                               'recorded_at': '2026-09-01T00:00:00+00:00',
                               'confirmed': True}
        with self.product.transaction():
            self.product.save(sc.KIND, stored)
        view = sc.case_view(self.cases, self.cases.get(case['id']))
        self.assertFalse(view['follow_up']['confirmed'])


class FollowUpRuntimeSchedulingTests(_RuntimeFixture):
    """同一事项不得同时启动多个相互覆盖的调查。"""

    def test_an_active_investigation_is_not_duplicated_by_a_second_arrangement(self):
        case = self.open_case()
        first = self.confirmed_arrangement(at=_ago(minutes=5), prefix='one')
        self.sweep()
        # 同一事项上再来一条安排：不应并起第二次调查。
        again = self.cases.schedule_follow_up(
            self.cases.get(first['id'])['id'],
            expected_revision=self.cases.get(first['id'])['revision'],
            kind='review_at', at=_ago(minutes=5), command_key='second-arrangement')
        self.cases.confirm_follow_up(
            again['id'], expected_revision=again['revision'], actor='caregiver-1',
            command_key='second-confirm')
        self.sweep()
        self.assertEqual(1, len(self.care_tasks(first['id'])),
                         '同一事项上不得有两条互相覆盖的调查')


class FollowUpWorkerWiringTests(unittest.TestCase):
    """接进**既有** worker。

    "有一个待办字段"不等于"已经具备长期服务"。这一组用例存在的唯一目的，是证明
    `follow_up` 真的挂在 `OutboxWorker.drain_once` 的周期上——不点"调查"也会被恢复。
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        from stage0 import server
        from stage0.agent import DDITool, MedicationCoordinatorAgent

        self.temp = tempfile.TemporaryDirectory(prefix='followup-worker-')
        self.directory = Path(self.temp.name)
        holder: dict = {}

        def factory():
            return MedicationCoordinatorAgent(
                holder['store'], ddi_tool=DDITool(lambda medications: []),
                rag_tool=None, llm_planner_enabled=False)

        self.app = server.create_app(db_path=self.directory / 'memory.db',
                                     worker_thread=False, agent_factory=factory)
        holder['store'] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)
        self.product = ProductStore(self.store)

    def tearDown(self):
        self.client.close()
        self.worker.stop()
        self.store.close()
        self.temp.cleanup()

    def _seed_case(self) -> dict:
        from stage0 import safety_cases as safety
        memory = self.store
        first = memory.apply_medication_change(
            action='add', name='合成药甲', ingredients=[], session_id='s',
            turn_id='a', source='caregiver', dose='5mg')['medication']
        second = memory.apply_medication_change(
            action='add', name='合成药乙', ingredients=[], session_id='s',
            turn_id='b', source='caregiver', dose='5mg')['medication']
        text = '合成药甲 × 合成药乙：合成风险。'
        conclusion = memory.record_conclusion(
            session_id='s', turn_id='w', kind='warning', text=text,
            memory_refs=[first['ref'], second['ref']],
            source_refs=[{'uri': 'synthetic.invalid/label', 'text': text}])
        return safety.observe_conclusion(self.product, conclusion, key='synthetic')

    def test_the_worker_cycle_triggers_a_due_follow_up_without_a_user_click(self):
        """用户不再点"调查"：worker 的一次周期就把到期安排恢复成一次调查。"""
        cases = sc.SafetyCaseStore(self.product)
        case = self._seed_case()
        due = _ago(minutes=5)
        scheduled = cases.schedule_follow_up(
            case['id'], expected_revision=cases.get(case['id'])['revision'],
            kind='review_at', at=due, command_key='worker-schedule')
        cases.confirm_follow_up(scheduled['id'],
                                expected_revision=scheduled['revision'],
                                actor='caregiver-1', command_key='worker-confirm')

        self.worker.drain_once()

        started = [t for t in self.product.objects('care_task')
                   if t.get('goal_type') == 'safety_case'
                   and t.get('safety_case_id') == case['id']]
        self.assertEqual(1, len(started), 'worker 周期应当恢复这件已确认的安排')
        follow_up = cases.get(case['id'])['follow_up']
        self.assertEqual('triggered', follow_up['schedule_state'])
        self.assertEqual(started[0]['id'], follow_up['care_task_id'])

    def test_the_worker_cycle_leaves_a_not_yet_due_follow_up_alone(self):
        cases = sc.SafetyCaseStore(self.product)
        case = self._seed_case()
        scheduled = cases.schedule_follow_up(
            case['id'], expected_revision=cases.get(case['id'])['revision'],
            kind='review_at', at=_ahead(hours=6), command_key='worker-future')
        cases.confirm_follow_up(scheduled['id'],
                                expected_revision=scheduled['revision'],
                                actor='caregiver-1', command_key='worker-future-confirm')

        self.worker.drain_once()

        started = [t for t in self.product.objects('care_task')
                   if t.get('goal_type') == 'safety_case'
                   and t.get('safety_case_id') == case['id']]
        self.assertEqual([], started)
        self.assertEqual('scheduled', cases.get(case['id'])['follow_up']['schedule_state'])

    def test_the_worker_runs_the_necessary_checks_before_it_consumes_the_follow_up(self):
        """端到端顺序：确定性检查先跑完，模型调查才被消费。

        建药本身排了一次必要检查；`drain_once` 先跑检查再走跟进周期。因此在**同一
        个**周期结束时，检查已经 `done`，而调查是之后才起的。
        """
        from stage0 import safety_checks as checks
        cases = sc.SafetyCaseStore(self.product)
        case = self._seed_case()
        due = _ago(minutes=5)
        scheduled = cases.schedule_follow_up(
            case['id'], expected_revision=cases.get(case['id'])['revision'],
            kind='review_at', at=due, command_key='worker-order')
        cases.confirm_follow_up(scheduled['id'],
                                expected_revision=scheduled['revision'],
                                actor='caregiver-1', command_key='worker-order-confirm')

        self.worker.drain_once()

        self.assertEqual(0, checks.pending(self.store)['unfinished'],
                         '周期结束时不应还有没跑完的必要检查')
        started = [t for t in self.product.objects('care_task')
                   if t.get('goal_type') == 'safety_case'
                   and t.get('safety_case_id') == case['id']]
        self.assertEqual(1, len(started))


class FollowUpHttpTests(unittest.TestCase):
    """冻结的端点形状：三个动作、幂等键、CAS、以及"请求体不得自称已确认"。"""

    def setUp(self):
        from fastapi.testclient import TestClient
        from stage0 import server
        self.temp = tempfile.TemporaryDirectory(prefix='followup-http-')
        self.directory = Path(self.temp.name)
        self.app = server.create_app(db_path=self.directory / 'memory.db',
                                     worker_thread=False)
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)
        self.product = ProductStore(self.store)
        self.cases = sc.SafetyCaseStore(self.product)

    def tearDown(self):
        self.client.close()
        self.worker.stop()
        self.store.close()
        self.temp.cleanup()

    def _case(self) -> dict:
        memory = self.store
        first = memory.apply_medication_change(
            action='add', name='合成药甲', ingredients=[], session_id='s',
            turn_id='a', source='caregiver', dose='5mg')['medication']
        second = memory.apply_medication_change(
            action='add', name='合成药乙', ingredients=[], session_id='s',
            turn_id='b', source='caregiver', dose='5mg')['medication']
        text = '合成药甲 × 合成药乙：合成风险。'
        conclusion = memory.record_conclusion(
            session_id='s', turn_id='w', kind='warning', text=text,
            memory_refs=[first['ref'], second['ref']],
            source_refs=[{'uri': 'synthetic.invalid/label', 'text': text}])
        return sc.observe_conclusion(self.product, conclusion, key='http-case')

    def _post(self, path, body):
        return self.client.post(path, json=body)

    def test_scheduling_returns_a_case_view_and_ignores_a_self_declared_confirmation(self):
        case = self._case()
        response = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-1', 'expected_revision': case['revision'],
            'action': 'schedule', 'kind': 'review_at',
            'at': '2026-10-01T00:00:00+00:00',
            # 请求体自称已确认：必须被忽略，且不得因此置真。
            'confirmed': True})
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertFalse(body['follow_up']['confirmed'])
        self.assertIsNone(body['follow_up']['confirmation_ref'])

    def test_a_naive_time_is_rejected_with_422(self):
        case = self._case()
        response = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-2', 'expected_revision': case['revision'],
            'action': 'schedule', 'kind': 'review_at', 'at': '2026-10-01T00:00:00'})
        self.assertEqual(422, response.status_code, response.text)

    def test_an_unknown_condition_kind_is_rejected_with_422(self):
        case = self._case()
        response = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-3', 'expected_revision': case['revision'],
            'action': 'schedule', 'kind': 'on_event',
            'condition': {'kind': 'we_ll_see', 'ref': 'memory:fact:1@1'}})
        self.assertEqual(422, response.status_code, response.text)

    def test_confirmation_records_who_confirmed_it(self):
        case = self._case()
        scheduled = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-4', 'expected_revision': case['revision'],
            'action': 'schedule', 'kind': 'review_at',
            'at': '2026-10-01T00:00:00+00:00'}).json()
        response = self._post(
            f"/v1/safety-cases/{case['case_id']}/follow-up/confirmation",
            {'key': 'http-5', 'expected_revision': scheduled['revision'],
             # 自报的确认人必须被忽略：身份来自认证上下文。
             'confirmed_by': 'somebody-else'})
        self.assertEqual(200, response.status_code, response.text)
        follow_up = response.json()['follow_up']
        self.assertTrue(follow_up['confirmed'])
        self.assertNotEqual('somebody-else', follow_up['confirmed_by'])
        self.assertIsNotNone(follow_up['confirmation_ref'])

    def test_cancelling_marks_the_state_and_keeps_the_history(self):
        case = self._case()
        scheduled = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-6', 'expected_revision': case['revision'],
            'action': 'schedule', 'kind': 'review_at',
            'at': '2026-10-01T00:00:00+00:00'}).json()
        response = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-7', 'expected_revision': scheduled['revision'],
            'action': 'cancel', 'reason': '医生已另行安排'})
        self.assertEqual(200, response.status_code, response.text)
        follow_up = response.json()['follow_up']
        self.assertEqual('cancelled', follow_up['schedule_state'])
        self.assertEqual('2026-10-01T00:00:00+00:00', follow_up['at'])

    def test_a_stale_revision_is_rejected_with_409(self):
        case = self._case()
        response = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", {
            'key': 'http-8', 'expected_revision': case['revision'] + 7,
            'action': 'schedule', 'kind': 'review_at',
            'at': '2026-10-01T00:00:00+00:00'})
        self.assertEqual(409, response.status_code, response.text)

    def test_the_same_idempotency_key_replays_instead_of_scheduling_twice(self):
        case = self._case()
        body = {'key': 'http-9', 'expected_revision': case['revision'],
                'action': 'schedule', 'kind': 'review_at',
                'at': '2026-10-01T00:00:00+00:00'}
        first = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", body)
        replay = self._post(f"/v1/safety-cases/{case['case_id']}/follow-up", body)
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual(first.json()['follow_up']['revision'],
                         replay.json()['follow_up']['revision'])

    def test_confirming_without_an_arrangement_is_rejected_with_409(self):
        case = self._case()
        response = self._post(
            f"/v1/safety-cases/{case['case_id']}/follow-up/confirmation",
            {'key': 'http-10', 'expected_revision': case['revision']})
        self.assertEqual(409, response.status_code, response.text)


class AssessmentProjectionTests(_StoreFixture):
    """接口一（assessment）：B **原样投影**，不加工、不猜测、不补默认值。"""

    def _case_with_answer(self, assessment_marker) -> dict:
        case = self.open_case()
        stored = self.cases.get(case['id'])
        answer = {'value': '每天一次', 'field': 'schedule', 'source': 'user_answer',
                  'provenance': 'user_reported', 'answer_ref': 'answer:1',
                  'origin': 'user', 'still_uncertain': []}
        if assessment_marker is not None:
            answer['assessment'] = assessment_marker
        stored['required_inputs'] = [{
            'request_id': 'r1', 'status': 'answered', 'question': '多久一次？',
            'answers': [answer], 'answered_parts': [answer],
            'answered_against': self.product.revisions()}]
        with self.product.transaction():
            self.product.save(sc.KIND, stored)
        return self.cases.get(case['id'])

    def test_an_assessment_is_projected_verbatim(self):
        marker = {'status': 'verified', 'reason': '引用片段与已读回的证据原文逐字一致',
                  'source_ref': 'ev-8842', 'locator': '第 3 段第 2 行',
                  'dependency_refs': ['memory:medication:45@2']}
        case = self._case_with_answer(marker)
        view = sc.case_view(self.cases, case)
        projected = view['answered_inputs'][0]['answered_parts'][0]['assessment']
        self.assertEqual(marker, projected, 'assessment 必须原样透传')

    def test_a_missing_assessment_is_not_filled_with_a_default(self):
        """缺失即未核实。**不得**默认 verified，也不得补任何 status。"""
        case = self._case_with_answer(None)
        view = sc.case_view(self.cases, case)
        answer = view['answered_inputs'][0]['answered_parts'][0]
        self.assertNotIn('assessment', answer)

    def test_projection_does_not_add_an_assessment_to_a_whole_case(self):
        case = self._case_with_answer({'status': 'candidate', 'reason': '有候选依据，尚未核对',
                                       'source_ref': None, 'locator': None,
                                       'dependency_refs': []})
        blob = str(sc.case_view(self.cases, case))
        self.assertNotIn('"status": "verified"', blob)


class AnswerDependencyBoundaryTests(_RuntimeFixture):
    """B 只定义**调用边界**并把结果搬回去，不复制一套答案判定规则。

    集成后 A 的判定接口（`answer_grounding.revalidate` / `dependency_state`）已经
    在位，所以这一组验的是**交接真的发生**、且**只降级受影响的那一条**——不是
    "接口还在不在"。
    """

    def _case_with_a_dependent_answer(self, drug_ref: str) -> dict:
        """在事项上挂一条已核对的答案，依赖 `drug_ref` 的那个版本。"""
        case = self.open_case()
        request_id = f"case:{case['id']}:q:1"
        answer = {
            'value': '5mg', 'field': 'dose', 'source': 'patient_record',
            'provenance': 'authoritative_record', 'answer_ref': 'answer:1',
            'origin': 'model', 'still_uncertain': [],
            'assessment': {'status': 'verified',
                           'reason': '与当前权威记录逐字一致',
                           'source_ref': drug_ref, 'locator': 'dose',
                           'dependency_refs': [drug_ref]}}
        task = {'id': 'task-answer-1', 'kind': 'care_task',
                'goal_type': 'safety_case', 'safety_case_id': case['id'],
                'status': 'completed', 'revision': 1,
                'created_at': fr._now(), 'updated_at': fr._now(),
                'investigation': {'questions': [
                    {'question_id': 'q:1', 'statement': '合成药甲的剂量是多少？',
                     'target_field': 'dose', 'answers': [answer]}]}}
        stored = self.cases.get(case['id'])
        stored['required_inputs'] = [{
            'request_id': request_id, 'status': 'answered',
            'question': '合成药甲的剂量是多少？', 'answers': [answer],
            'answered_parts': [answer],
            'answered_against': self.product.revisions()}]
        with self.product.transaction():
            self.product.save('care_task', task)
            self.product.save(sc.KIND, stored)
        return self.cases.get(case['id'])

    def test_only_the_answer_whose_dependency_changed_is_downgraded(self):
        """依赖的那条记录变了 ⇒ 那条答案不再算已核对，并且**前端看得见**。

        本仓库里改剂量是**取代**：`memory:medication:1@v1` 不再是当前记录，
        新记录换了 id。所以这条走 A 的 `withdraw`（来源已不可用）→ `unsupported`；
        若只是同一条记录版本前进，同一段代码走 `retire` → `stale`。
        两种情况共同的性质是**不再是 `verified`**，这正是要守的那条。
        """
        drug_ref = self.add_drug('卡马西平', key='dep-1')['medication']['ref']
        case = self._case_with_a_dependent_answer(drug_ref)
        # 记录的版本前进一格：那条答案依赖的已经不是当前版本了。
        self.memory.apply_medication_change(
            action='dose_change', name='卡马西平', dose='200mg',
            ingredients=[], session_id='s', turn_id='dep-1-change',
            source='caregiver')

        result = fr.recheck_answer_dependencies(
            self.product, case, changed_refs=[drug_ref], reason='用药变化')
        self.assertEqual(1, result['affected'],
                         f'改了一条依赖，却降级了 {result["affected"]} 条答案：{result}')
        self.assertIn(result['updated'][0]['to'], ('stale', 'unsupported'))
        self.assertNotEqual('verified', result['updated'][0]['to'])

        projected = sc.case_view(self.cases, self.cases.get(case['id']))
        parts = projected['answered_inputs'][0]['answered_parts'][0]
        self.assertNotEqual('verified', parts['assessment']['status'],
                            '降级只写进调查、没有搬到事项投影，前端看到的仍是"已核对"')

    def test_a_change_no_answer_depends_on_is_left_alone(self):
        """与这次变化无关的答案不动——一次局部变化不该放大成一次全面重问。"""
        case = self._case_with_a_dependent_answer(
            self.add_drug('丙戊酸钠', key='dep-2')['medication']['ref'])
        result = fr.recheck_answer_dependencies(
            self.product, case, changed_refs=['memory:conclusion:999@1'],
            reason='另一个结论被记录')
        self.assertEqual(0, result['affected'], result)
        projected = sc.case_view(self.cases, self.cases.get(case['id']))
        parts = projected['answered_inputs'][0]['answered_parts'][0]
        self.assertEqual('verified', parts['assessment']['status'],
                         '不相关的答案被降级了——相关判断必须由 A 的接口给出')

    def test_the_boundary_does_not_write_any_assessment_of_its_own(self):
        """B 不产生 assessment——那是 A 的产物。缺失就保持缺失。"""
        case = self.open_case()
        fr.recheck_answer_dependencies(self.product, case, changed_refs=['x'],
                                       reason='相关变化')
        view = sc.case_view(self.cases, self.cases.get(case['id']))
        self.assertNotIn('assessment', str(view))

    def test_the_boundary_is_asked_when_a_relevant_change_fires_a_trigger(self):
        """相关变化触发时**确实**调用了约定接口——边界是活的，不是写在文档里。"""
        case = self.open_case()
        confirmed = self.confirmed_arrangement(
            kind='on_event', condition={'kind': 'medication_change', 'ref': '华法林'},
            prefix='boundary')
        self.register_change(kind='medication_set', ref='华法林')
        seen: list = []
        original = fr.recheck_answer_dependencies

        def spy(product, case_obj, *, changed_refs=(), reason=None):
            seen.append((case_obj['id'], list(changed_refs)))
            return original(product, case_obj, changed_refs=changed_refs, reason=reason)

        fr.recheck_answer_dependencies = spy
        try:
            self.sweep()
        finally:
            fr.recheck_answer_dependencies = original
        self.assertEqual(1, len(seen), '触发时应当向 A 的接口提请重新核对')
        self.assertEqual(confirmed['id'], seen[0][0])
        self.assertIn('华法林', seen[0][1])

    def test_a_time_based_trigger_does_not_claim_a_record_changed(self):
        """时间到了不等于"有记录变了"：没有可指认的变化就不去打扰答案依赖。

        否则每到一个复查时间点，所有答案都会被提请重新核对一次——那正是"把所有
        已答问题全部重开"的另一种写法。
        """
        case = self.open_case()
        confirmed = self.confirmed_arrangement(at=_ago(minutes=5), prefix='timed')
        seen: list = []
        original = fr.recheck_answer_dependencies

        def spy(product, case_obj, *, changed_refs=(), reason=None):
            seen.append(list(changed_refs))
            return original(product, case_obj, changed_refs=changed_refs, reason=reason)

        fr.recheck_answer_dependencies = spy
        try:
            report = self.sweep()
        finally:
            fr.recheck_answer_dependencies = original
        self.assertEqual([], seen, '纯时间触发不应提请重新核对任何答案依赖')
        self.assertEqual(1, len(report['triggered']), '调查本身照常进行')


if __name__ == '__main__':
    unittest.main()
