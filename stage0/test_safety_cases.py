"""安全事项契约：身份、生命周期、依据与关闭条件。

这些用例锁的是**安全不变量**，不是实现细节：同一件事不因复检而变成新卡片、
不同用药分期不被药名合并、"已读"不能关闭事项、旧版本的检查结果不能批准新状态。
"""
import tempfile
import unittest
from pathlib import Path

from stage0.memory import MemoryStore
from stage0.product import ProductError, ProductStore
from stage0 import safety_cases as sc


class SafetyCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / 'memory.db', llm_enabled=False)
        self.product = ProductStore(self.memory)
        self.cases = sc.SafetyCaseStore(self.product)

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    # ---- helpers ---------------------------------------------------------
    def add_drug(self, name, *, key=None, dose='5mg'):
        return self.memory.apply_medication_change(
            action='add', name=name, ingredients=[], session_id='s', turn_id=key or name,
            source='caregiver', dose=dose)

    def warn(self, text, meds=(), key='w1'):
        return self.memory.record_conclusion(
            session_id='s', turn_id=key, kind='warning', text=text,
            memory_refs=list(meds), source_refs=[{'uri': 'label://x', 'text': text}])

    def observe(self, conclusion, key='obs1'):
        return sc.observe_conclusion(self.product, conclusion, key=key)

    # ---- 身份与去重 -------------------------------------------------------
    def test_the_same_risk_on_a_later_recheck_updates_one_case(self):
        """每天复检一次同一风险，不应该变成每天一张新卡片。"""
        first = self.add_drug('华法林')['medication']
        second = self.add_drug('阿司匹林')['medication']
        meds = [first['ref'], second['ref']]
        a = self.observe(self.warn('阿司匹林 × 华法林：出血风险升高。', meds, 'w1'), 'k1')
        b = self.observe(self.warn('阿司匹林 × 华法林：出血风险升高。', meds, 'w2'), 'k2')
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(1, len(self.cases.objects()))
        self.assertEqual(2, len(b['linked_conclusion_refs']))

    def test_a_dose_change_stays_the_same_episode_but_a_restart_does_not(self):
        """剂量变更仍是同一次用药；停药后重新启用是新的用药分期。"""
        first = self.add_drug('华法林')['medication']
        other = self.add_drug('阿司匹林')['medication']
        text = '阿司匹林 × 华法林：出血风险升高。'
        original = self.observe(self.warn(text, [first['ref'], other['ref']], 'w1'), 'k1')

        self.memory.apply_medication_change(action='dose_change', name='华法林', ingredients=[],
                                            session_id='s', turn_id='d1', source='caregiver', dose='3mg')
        same = self.observe(self.warn(text, [first['ref'], other['ref']], 'w2'), 'k2')
        self.assertEqual(original['id'], same['id'], '剂量变更不应产生新事项')

        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='r1', source='caregiver')
        restarted = self.add_drug('华法林', key='re-add')['medication']
        after = self.observe(self.warn(text, [restarted['ref'], other['ref']], 'w3'), 'k3')
        self.assertNotEqual(original['id'], after['id'], '停药后重新启用是新的用药分期')
        self.assertEqual(2, len(self.cases.objects()))

    def test_unrelated_drugs_do_not_land_in_the_same_case(self):
        a = self.add_drug('华法林')['medication']
        b = self.add_drug('阿司匹林')['medication']
        c = self.add_drug('二甲双胍')['medication']
        one = self.observe(self.warn('阿司匹林 × 华法林：出血风险。', [a['ref'], b['ref']], 'w1'), 'k1')
        two = self.observe(self.warn('二甲双胍 × 华法林：低血糖风险。', [a['ref'], c['ref']], 'w2'), 'k2')
        self.assertNotEqual(one['id'], two['id'])

    def test_a_case_references_facts_and_never_copies_them(self):
        """事项只保存引用——没有第二个真相源。"""
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        blob = str(case)
        for leaked in ('5mg', 'display_name', 'ingredients'):
            self.assertNotIn(leaked, blob)
        self.assertIn(med['ref'], case['related_medication_refs'])

    # ---- 已读不是状态 -----------------------------------------------------
    def test_marking_seen_never_closes_or_downgrades(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        seen = self.cases.mark_seen(case['id'])
        self.assertIsNotNone(seen['user_seen_at'])
        self.assertEqual(sc.STATUS_OPEN, seen['current_status'])
        self.assertIsNone(seen['resolution_basis'])

    # ---- 关闭条件 ---------------------------------------------------------
    def _resolve(self, case):
        """把事项关掉（调用方身份来自认证上下文，测试里显式给出角色）。"""
        return self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_RESOLVED, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver',))

    def test_a_live_risk_cannot_close_the_case_however_current_it_is(self):
        """**检查做过了、版本也对得上，仍然不能关闭。**

        一条 `current` 的风险结论只证明"检查跑了、风险还在"，不证明"触发条件没了"。
        把它当成关闭依据，就是把"还在的风险"读成"已处理"。
        """
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        with self.assertRaises(ProductError) as err:
            self._resolve(case)
        self.assertEqual(409, err.exception.status)
        self.assertIn('风险存在', str(err.exception))
        self.assertEqual(sc.STATUS_OPEN, self.cases.get(case['id'])['current_status'])
        self.assertIsNone(self.cases.get(case['id'])['resolution_basis'])

    def test_an_eliminated_trigger_closes_the_case_with_recorded_evidence(self):
        """停药 → 触发条件客观消失 → 可以关闭，且依据里记着**凭什么**。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        # 重查把旧结论换成后继版本——事项必须跟着链头走，而不是盯着过期记录。
        self.memory.recheck_pending()
        closed = self._resolve(self.cases.get(case['id']))
        self.assertEqual(sc.STATUS_RESOLVED, closed['current_status'])
        basis = closed['resolution_basis']
        self.assertEqual(sc.BASIS_CHECK, basis['kind'])
        self.assertTrue(basis['conclusion_refs'])
        self.assertTrue(basis['evidence']['eliminated'])
        self.assertFalse(basis['evidence']['still_present'])
        self.assertTrue(any('不在当前用药中' in reason
                            for item in basis['evidence']['checked']
                            for reason in item['reasons']))

    def test_an_open_question_blocks_closing_even_with_an_eliminated_trigger(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        self.cases.require_input(case['id'], request_id='q1', question='还有别的不舒服吗？')
        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        self.memory.recheck_pending()
        with self.assertRaises(ProductError) as err:
            self._resolve(self.cases.get(case['id']))
        self.assertEqual(409, err.exception.status)
        self.assertIn('未决问题', str(err.exception))

    def test_closing_requires_the_actor_to_have_the_role(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        self.memory.recheck_pending()
        with self.assertRaises(ProductError) as err:
            self.cases.disposition(case['id'], expected_revision=self.cases.get(case['id'])['revision'],
                                   disposition=sc.DISPOSITION_RESOLVED, basis_kind=sc.BASIS_CHECK,
                                   actor='someone-else', roles=('viewer',))
        self.assertEqual(403, err.exception.status)
        self.assertNotEqual(sc.STATUS_RESOLVED, self.cases.get(case['id'])['current_status'])

    def test_the_recorded_actor_is_the_authenticated_principal(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        self.memory.recheck_pending()
        closed = self.cases.disposition(
            case['id'], expected_revision=self.cases.get(case['id'])['revision'],
            disposition=sc.DISPOSITION_RESOLVED, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver', 'ops'))
        entry = [e for e in closed['history'] if e['event'] == 'disposition'][-1]
        self.assertEqual('local-demo-caregiver', entry['actor'])
        self.assertEqual(['caregiver', 'ops'], entry['roles'])

    # ---- 持续跟进 ≠ 等待专业人员 -------------------------------------------
    def test_monitoring_is_its_own_state_not_a_wait_for_a_professional(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_MONITORING, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver',))
        self.assertEqual(sc.STATUS_MONITORING, monitored['current_status'])
        self.assertNotEqual(sc.STATUS_AWAITING_PROFESSIONAL, monitored['current_status'])
        self.assertEqual('caregiver', monitored['responsible_party'])
        # 风险仍在：处置说明里必须这么说，不能让用户以为风险消失了。
        self.assertIn('风险仍然存在', monitored['next_action_summary'])
        self.assertIn(monitored['current_status'], sc.UNSETTLED_STATUSES)
        # 记下来的依据是"凭什么说风险还在"，而不是任何形式的关闭依据。
        basis = monitored['resolution_basis']
        self.assertEqual('monitoring_arrangement', basis['kind'])
        self.assertTrue(basis['still_present'])
        self.assertIn('不能关闭', basis['why_not_closed'])
        self.assertNotIn(basis['kind'], sc.CLOSING_BASES)

    def test_monitoring_does_not_require_a_closing_basis(self):
        """持续跟进没有"依据"可填——要求它填一个只会让界面以为这里有一种依据。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_MONITORING, basis_kind='',
            actor='local-demo-caregiver', roles=('caregiver',))
        self.assertEqual(sc.STATUS_MONITORING, monitored['current_status'])
        self.assertEqual('monitoring_arrangement', monitored['resolution_basis']['kind'])

    def test_monitoring_without_a_credible_basis_records_an_unconfirmed_arrangement(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_MONITORING, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver',))
        follow_up = monitored['follow_up']
        self.assertFalse(follow_up['confirmed'])
        self.assertIn('待确认', follow_up['note'])
        self.assertIsNone(follow_up['at'])

    def test_monitoring_with_an_explicit_schedule_keeps_it_unconfirmed(self):
        """安排留下来了，但**给了一个时间不等于有人确认过**（CONTRACT §4.5）。

        原用例断言的是 `confirmed` 由 `at` 派生（`bool(at or condition)`）——
        那正是 §4.5 点名要修掉的缺陷。这里改成断言目标语义：时间与负责人原样
        保留，确认仍然为空，直到有一条真实的确认记录。
        """
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        monitored = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_MONITORING, basis_kind=sc.BASIS_CHECK,
            actor='local-demo-caregiver', roles=('caregiver',),
            follow_up={'kind': 'review_at', 'at': '2026-10-01T00:00:00+00:00',
                       'owner': 'caregiver'})
        follow_up = monitored['follow_up']
        self.assertFalse(follow_up['confirmed'])
        self.assertIsNone(follow_up['confirmed_at'])
        self.assertIsNone(follow_up['confirmation_ref'])
        self.assertEqual('scheduled', follow_up['schedule_state'],
                         '已安排与已确认是两件事：给了时间就应当是可调度的安排')
        self.assertEqual('2026-10-01T00:00:00+00:00', follow_up['at'])
        self.assertEqual('caregiver', follow_up['owner'])

    def test_a_model_cannot_invent_a_monitoring_cycle(self):
        """没给依据的 `review_at` 不能被当成一个真实的复查周期。"""
        follow_up = sc._normalise_follow_up({'kind': 'review_at', 'owner': 'caregiver'})
        self.assertFalse(follow_up['confirmed'])
        self.assertIsNone(follow_up['at'])
        self.assertEqual('arrangement', follow_up['kind'])

    def test_projected_answers_do_not_collide_across_runs(self):
        """已答问题的投影是**幂等**的，但每跑一次调查集合都可能变。

        `ProductStore.command` 对"同一个 key 配不同内容"返回 409——投影用固定
        key 的话，第二轮调查会直接撞上去。所以幂等键必须带调用方的判别项。
        """
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(
            self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        prefix = f"case:{case['id']}"
        one = self.cases.project_answered_questions(
            case['id'], [{'request_id': f'{prefix}:q1', 'status': 'answered'}],
            command_key='run-1')
        self.assertEqual(1, len(one['required_inputs']))

        two = self.cases.project_answered_questions(
            case['id'],
            [{'request_id': f'{prefix}:q1', 'status': 'answered'},
             {'request_id': f'{prefix}:q2', 'status': 'answered'}],
            command_key='run-2')
        self.assertEqual(2, len(two['required_inputs']),
                         '第二轮调查多答上一条，投影必须跟得上')

        replay = self.cases.project_answered_questions(
            case['id'],
            [{'request_id': f'{prefix}:q1', 'status': 'answered'},
             {'request_id': f'{prefix}:q2', 'status': 'answered'}],
            command_key='run-2')
        self.assertEqual(2, len(replay['required_inputs']),
                         '重放同一次运行不该重复登记')

    def test_an_old_check_cannot_approve_a_new_medication_state(self):
        """检查之后又改过记录：旧结论不能用来关闭事项。

        拒绝可以来两条路（结论已失效，或版本对不上），两条都是**拒绝**；
        这里锁的是"被拒"，不是拒绝的具体措辞。
        """
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        self.add_drug('新药', key='new')
        before = self.cases.get(case['id'])
        with self.assertRaises(ProductError) as err:
            self.cases.disposition(case['id'], expected_revision=before['revision'],
                                   disposition=sc.DISPOSITION_RESOLVED,
                                   basis_kind=sc.BASIS_CHECK, actor='caregiver',
                                   roles=('caregiver',))
        self.assertEqual(409, err.exception.status)
        self.assertNotEqual(sc.STATUS_RESOLVED, self.cases.get(case['id'])['current_status'])

    def test_a_stale_conclusion_cannot_close_the_case(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        # A later medication change invalidates the conclusion it depends on.
        self.add_drug('阿司匹林', key='asa')
        stale = self.memory.connection.execute(
            "SELECT status FROM conclusions WHERE id=?", (int(case['linked_conclusion_refs'][0].split(':')[2].split('@')[0]),)
        ).fetchone()
        self.assertEqual('stale', stale['status'])
        with self.assertRaises(ProductError):
            self.cases.disposition(case['id'], expected_revision=case['revision'],
                                   disposition=sc.DISPOSITION_RESOLVED,
                                   basis_kind=sc.BASIS_CHECK, actor='caregiver')

    def test_a_user_relayed_doctor_opinion_is_not_a_closing_basis(self):
        """用户转述医生意见：不能关闭，只能被如实记下并转人工复核。

        "关闭"这个请求被**明确拒绝**，而不是被悄悄降级成一个没人请求过的结果。
        """
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        with self.assertRaises(ProductError) as err:
            self.cases.disposition(
                case['id'], expected_revision=case['revision'],
                disposition=sc.DISPOSITION_RESOLVED, basis_kind=sc.BASIS_USER_REPORTED,
                actor='caregiver', roles=('caregiver',))
        self.assertEqual(409, err.exception.status)
        self.assertEqual(sc.STATUS_OPEN, self.cases.get(case['id'])['current_status'])

        escalated = self.cases.disposition(
            case['id'], expected_revision=case['revision'],
            disposition=sc.DISPOSITION_ESCALATED, basis_kind=sc.BASIS_USER_REPORTED,
            actor='caregiver', roles=('caregiver',))
        self.assertEqual(sc.STATUS_AWAITING_PROFESSIONAL, escalated['current_status'])
        self.assertEqual(sc.BASIS_USER_REPORTED, escalated['resolution_basis']['kind'])
        self.assertIn('未经核实', escalated['resolution_basis']['note'])

    def _review_decision(self, *, action='close_with_safe_guidance', actor_id='local-reviewer',
                         outcome='applied', summary=None, linked_case=True):
        """造一条复核决定。默认是"看起来合法"的那种，便于逐个证伪它的每一道门。"""
        review = self.memory.open_review_case(
            logic_key=f'review:{action}:{actor_id}:{linked_case}',
            run_id='run:synthetic-review', thread_id='run:synthetic-review',
            reason_codes=['evidence_conflict'],
            summary=summary if summary is not None else {'verification_status': 'incomplete'})
        review = self.memory.claim_review_case(review['id'],
                                               expected_revision=review['revision'],
                                               assignee=actor_id)
        record = self.memory.record_review_decision(
            case_id=review['id'], expected_revision=review['revision'], action=action,
            idempotency_key=f'key:{action}:{actor_id}:{linked_case}',
            actor_id=actor_id)
        if outcome != 'recorded':
            self.memory.set_review_decision_outcome(record['decision_id'], outcome=outcome)
        return review, record

    def _link(self, case, review):
        def execute():
            item = self.cases.get(case['id'])
            linked = item.setdefault('linked_review_case_ids', [])
            if str(review['id']) not in linked:
                linked.append(str(review['id']))
                item['revision'] += 1
                self.p_save(item)
            return item
        return self.cases.p.command(f"{case['id']}:link:{review['id']}",
                                    {'type': 'safety_case_link_review'}, execute)

    def p_save(self, item):
        self.product.save(sc.KIND, item)

    def test_professional_basis_requires_an_applied_decision(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        with self.assertRaises(ProductError):
            self.cases.disposition(case['id'], expected_revision=case['revision'],
                                   disposition=sc.DISPOSITION_RESOLVED,
                                   basis_kind=sc.BASIS_PROFESSIONAL, actor='reviewer', roles=('reviewer',),
                                   decision_id='does-not-exist')

    def _resolve_with_decision(self, case, decision_id):
        return self.cases.disposition(
            case['id'], expected_revision=self.cases.get(case['id'])['revision'],
            disposition=sc.DISPOSITION_RESOLVED, basis_kind=sc.BASIS_PROFESSIONAL,
            actor='local-reviewer', roles=('reviewer',), decision_id=decision_id)

    def test_an_applied_decision_for_another_case_cannot_close_this_one(self):
        """不能仅凭任意一条 applied 的复核决定，关掉另一件事项。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        _review, record = self._review_decision()          # 属于别的事项，未与本事项关联
        with self.assertRaises(ProductError) as err:
            self._resolve_with_decision(case, record['decision_id'])
        self.assertEqual(409, err.exception.status)
        self.assertIn('不是针对本事项', str(err.exception))

    def test_a_review_action_that_does_not_permit_closure_cannot_close(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        review, record = self._review_decision(action='request_more_info')
        self._link(case, review)
        with self.assertRaises(ProductError) as err:
            self._resolve_with_decision(case, record['decision_id'])
        self.assertIn('不允许完成事项', str(err.exception))

    def test_a_simulated_review_cannot_be_dressed_up_as_professional_confirmation(self):
        """本地模拟工作台的决定不是专业医疗确认——这是这条路径上最危险的谎。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        review, record = self._review_decision(summary={'simulated': True})
        self._link(case, review)
        with self.assertRaises(ProductError) as err:
            self._resolve_with_decision(case, record['decision_id'])
        self.assertIn('模拟工作台', str(err.exception))
        self.assertNotEqual(sc.STATUS_RESOLVED, self.cases.get(case['id'])['current_status'])

    def test_a_review_whose_facts_moved_cannot_approve_the_new_state(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        review, record = self._review_decision()
        self._link(case, review)
        self.add_drug('二甲双胍', key='metformin')       # 决定作出之后事实变了
        with self.assertRaises(ProductError) as err:
            self._resolve_with_decision(case, record['decision_id'])
        self.assertIn('不再适用', str(err.exception))

    def test_disposition_is_revision_guarded(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        with self.assertRaises(ProductError) as err:
            self.cases.disposition(case['id'], expected_revision=case['revision'] + 5,
                                   disposition=sc.DISPOSITION_RESOLVED,
                                   basis_kind=sc.BASIS_CHECK, actor='caregiver',
                                   roles=('caregiver',))
        self.assertEqual(409, err.exception.status)

    # ---- 补充只关闭它回答的那一条 -------------------------------------------
    def test_input_closes_only_the_request_it_answers(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        case = self.cases.require_input(case['id'], request_id='q1', question='最近一次化验是什么时候？')
        case = self.cases.require_input(case['id'], request_id='q2', question='是否同时服用其他药物？')
        self.assertEqual(sc.STATUS_AWAITING_USER, case['current_status'])
        answered = self.cases.record_input(case['id'], request_id='q1', answer_ref='answer:1',
                                           value='上周')
        self.assertEqual('answered', next(i for i in answered['required_inputs']
                                          if i['request_id'] == 'q1')['status'])
        self.assertEqual('open', next(i for i in answered['required_inputs']
                                      if i['request_id'] == 'q2')['status'])
        self.assertEqual(sc.STATUS_AWAITING_USER, answered['current_status'])

    def test_an_empty_answer_closes_nothing(self):
        """空值不是回答。它不能把缺口标成已解决。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        case = self.cases.require_input(case['id'], request_id='q1', question='最近有没有出血？')
        for empty in (None, '', '   '):
            case = self.cases.record_input(case['id'], request_id='q1', answer_ref='r', value=empty,
                                           command_key=f'empty-{empty!r}')
        item = next(i for i in case['required_inputs'] if i['request_id'] == 'q1')
        self.assertEqual('open', item['status'], '空回答不得关闭请求')
        self.assertEqual(sc.STATUS_AWAITING_USER, case['current_status'])
        kinds = [e['answer_kind'] for e in case['history'] if e['event'] == 'input_recorded']
        self.assertTrue(all(k == sc.ANSWER_EMPTY for k in kinds), kinds)

    def test_saying_i_do_not_know_stops_asking_but_keeps_the_uncertainty(self):
        """「不知道」结束这一轮追问，但**不消除**不确定性，也不允许关闭。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        case = self.cases.require_input(case['id'], request_id='q1', question='最近有没有出血？')
        case = self.cases.record_input(case['id'], request_id='q1', answer_ref='r', value='不知道')
        item = next(i for i in case['required_inputs'] if i['request_id'] == 'q1')
        self.assertEqual(sc.ANSWER_UNKNOWN, item['status'])
        self.assertTrue(item['needs_alternative_evidence'])
        # 不再等用户，改由系统找替代证据。
        self.assertNotEqual(sc.STATUS_AWAITING_USER, case['current_status'])
        self.assertEqual('agent', case['responsible_party'])
        # 但仍然阻止关闭——不确定性没有消失。
        evidence = self.cases.closure_evidence(case)
        self.assertIn('q1', evidence['blocking_inputs'])

    def test_an_unknown_answer_does_not_unblock_closing(self):
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        self.cases.require_input(case['id'], request_id='q1', question='最近有没有出血？')
        self.cases.record_input(case['id'], request_id='q1', answer_ref='r', value='记不清')
        self.memory.apply_medication_change(action='remove', name='华法林', ingredients=[],
                                            session_id='s', turn_id='stop', source='caregiver')
        self.memory.recheck_pending()
        with self.assertRaises(ProductError) as err:
            self._resolve(self.cases.get(case['id']))
        self.assertIn('未决问题', str(err.exception))

    def test_the_classifier_separates_content_from_absence(self):
        for unknown in ('不知道', '不清楚。', '  记不清 ', '说不好'):
            self.assertEqual(sc.ANSWER_UNKNOWN, sc.classify_answer(unknown), unknown)
        for empty in (None, '', '   ', '\n'):
            self.assertEqual(sc.ANSWER_EMPTY, sc.classify_answer(empty), repr(empty))
        self.assertEqual(sc.ANSWER_PROVIDED, sc.classify_answer('上周三开始有点牙龈出血'))
        # "不知道"出现在其它句子里时不算"明说不知道"。
        self.assertEqual(sc.ANSWER_PROVIDED, sc.classify_answer('我不知道是不是，但昨天有一点'))

    def test_an_answer_stops_applying_when_the_facts_move_and_the_question_reopens(self):
        """旧答案不能永久挡住重新核对：事实变了就重新打开那条问题并说明原因。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        case = self.cases.require_input(case['id'], request_id='q1', question='最近有没有出血？')
        case = self.cases.record_input(case['id'], request_id='q1', answer_ref='r',
                                       value='上周有一点牙龈出血')
        self.assertEqual('answered', next(i for i in case['required_inputs']
                                          if i['request_id'] == 'q1')['status'])

        # 记录变化 → 那条回答不再适用于当前状态。
        self.add_drug('二甲双胍', key='metformin')
        synced = self.cases.sync(case['id'])
        item = next(i for i in synced['required_inputs'] if i['request_id'] == 'q1')
        self.assertEqual('open', item['status'])
        self.assertIn('变化', item['reopened_reason'])
        self.assertIsNone(item['answer_ref'])
        self.assertTrue(any(e['event'] == 'answer_retired' for e in synced['history']))
        # 事项必须先重新核对依据，再谈补充——旧答案不会被无声地继续使用。
        self.assertEqual(sc.STATUS_NEEDS_RECHECK, synced['current_status'])

    def test_re_answering_after_a_fact_change_keeps_the_same_request_id(self):
        """重新核对不会生成一条新问题——身份还是那一个。"""
        a = self.add_drug('阿司匹林')['medication']
        b = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('阿司匹林×华法林：出血风险升高。', [a['ref'], b['ref']], 'w1'), 'k1')
        self.cases.require_input(case['id'], request_id='q1', question='最近有没有出血？')
        self.cases.record_input(case['id'], request_id='q1', answer_ref='r', value='没有')
        self.add_drug('二甲双胍', key='metformin')
        reopened = self.cases.record_input(case['id'], request_id='q1', answer_ref='r2',
                                           value='这周有一点')
        ids = [i['request_id'] for i in reopened['required_inputs']]
        self.assertEqual(['q1'], ids)
        self.assertEqual('answered', next(i for i in reopened['required_inputs']
                                          if i['request_id'] == 'q1')['status'])

    def test_an_answer_that_matches_nothing_closes_nothing(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        case = self.cases.require_input(case['id'], request_id='q1', question='问题一')
        stray = self.cases.record_input(case['id'], request_id='not-a-request',
                                        answer_ref='answer:x', value='随便说说')
        self.assertEqual('open', next(i for i in stray['required_inputs']
                                      if i['request_id'] == 'q1')['status'])
        self.assertTrue(any(e['event'] == 'input_recorded' and not e['answered']
                            for e in stray['history']))

    def test_a_professional_input_request_routes_to_the_professional(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        case = self.cases.require_input(case['id'], request_id='p1', question='该剂量是否需要调整？',
                                        for_professional=True)
        self.assertEqual(sc.STATUS_AWAITING_PROFESSIONAL, case['current_status'])
        self.assertEqual('professional', case['responsible_party'])

    # ---- 依据变化重新打开 ---------------------------------------------------
    def _condition_case(self, *, value=30):
        """一个由"用药 + 肾功能事实"共同构成触发条件的个体风险事项。"""
        from stage0.memory import SemanticFact
        fact = self.memory.write_semantic_fact(
            SemanticFact('renal_function', 'renal_status', {'value': value}),
            source='caregiver')
        med = self.add_drug('华法林')['medication']
        conclusion = self.memory.record_conclusion(
            session_id='s', turn_id='w1', kind='condition_warning',
            text='华法林×患者个体风险：肾功能:30，需由医生/药师复核适用性（major / medium）',
            memory_refs=[med['ref'], fact['item']['ref']],
            source_refs=[{'uri': 'label://x', 'text': 'x'}])
        return self.observe(conclusion, 'k1'), fact

    def test_a_resolved_case_reopens_when_the_same_trigger_returns(self):
        """按依据关闭之后，**同一分期**的触发条件重新成立 → 重开同一件事。

        这里用"事实被撤回 → 关闭；事实重新上报 → 重开"，因为换药（停药重启）
        按设计就是新的用药分期、新的事项，不是同一件事。
        """
        case, fact = self._condition_case()
        self.memory.retract_semantic_fact(fact['item']['ref'], reason='报告有误')
        self.memory.recheck_pending()
        closed = self._resolve(self.cases.get(case['id']))
        self.assertEqual(sc.STATUS_RESOLVED, closed['current_status'])

        from stage0.memory import SemanticFact
        again = self.memory.write_semantic_fact(
            SemanticFact('renal_function', 'renal_status', {'value': 25}),
            source='caregiver')
        reopened = self.observe(self.memory.record_conclusion(
            session_id='s', turn_id='w2', kind='condition_warning',
            text='华法林×患者个体风险：肾功能:25，需由医生/药师复核适用性（major / medium）',
            memory_refs=[self.memory.current_medications()[0]['ref'], again['item']['ref']],
            source_refs=[{'uri': 'label://x', 'text': 'x'}]), 'k2')
        self.assertEqual(closed['id'], reopened['id'], '重新打开的是同一件事')
        self.assertEqual(sc.STATUS_NEEDS_RECHECK, reopened['current_status'])
        self.assertIsNone(reopened['resolution_basis'])

    def test_sync_moves_the_case_when_the_facts_behind_it_move(self):
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        self.assertEqual(sc.STATUS_OPEN, case['current_status'])
        self.add_drug('阿司匹林', key='asa')
        synced = self.cases.sync(case['id'])
        self.assertEqual(sc.STATUS_NEEDS_RECHECK, synced['current_status'])
        self.assertTrue(any(e['event'] == 'status_changed' for e in synced['history']))

    def test_every_status_transition_is_recorded_with_its_reason(self):
        """"为何重新复核"必须能从历史里读出来，而不是只看到当前状态。"""
        med = self.add_drug('华法林')['medication']
        case = self.observe(self.warn('华法林 单独提示。', [med['ref']], 'w1'), 'k1')
        # 让事项先进入 waiting 状态，再让依据失效——两次迁移都要留下痕迹。
        case = self.cases.require_input(case['id'], request_id='q1', question='问题一')
        self.assertEqual(sc.STATUS_AWAITING_USER, case['current_status'])
        self.add_drug('阿司匹林', key='asa')
        synced = self.cases.sync(case['id'])
        changes = [e for e in synced['history'] if e['event'] == 'status_changed']
        self.assertEqual(2, len(changes), synced['history'])
        self.assertEqual((sc.STATUS_OPEN, sc.STATUS_AWAITING_USER),
                         (changes[0]['from'], changes[0]['to']))
        self.assertEqual((sc.STATUS_AWAITING_USER, sc.STATUS_NEEDS_RECHECK),
                         (changes[1]['from'], changes[1]['to']))
        self.assertTrue(all(entry.get('why') for entry in changes),
                        '每次迁移都要说明为什么')

    # ---- 分期锚点本身 ------------------------------------------------------
    def test_incarnation_walks_back_over_dose_changes_only(self):
        first = self.add_drug('华法林')['medication']
        second = self.memory.apply_medication_change(
            action='dose_change', name='华法林', ingredients=[], session_id='s',
            turn_id='d1', source='caregiver', dose='3mg')['medication']
        self.assertEqual(
            sc.incarnation_id(self.memory.connection, first['id']),
            sc.incarnation_id(self.memory.connection, second['id']))

    # ---- 契约面 ------------------------------------------------------------
    def test_status_labels_cover_every_status(self):
        self.assertEqual(set(sc.STATUSES), set(sc.STATUS_LABELS))

    def test_unknown_case_type_is_refused(self):
        with self.assertRaises(ProductError):
            self.cases.open_or_update('k', case_type='made_up', subject_keys=['x'],
                                      anchor='a', trigger={'kind': 'test'})


class InvestigationContextAnswerTests(SafetyCaseTests):
    """已回答的问题要带上**答案本身**，不只是"这条答过"。

    只报一个 `answer_kind` 时，模型读得到"有人答过"，读不到"答的是什么、
    依据是什么"——于是下一轮回访只能把同一件事重新查一遍。
    """

    def _answered_case(self):
        case = self.observe(self.warn(
            '合成药甲 × 合成药乙：出血风险升高。',
            [self.add_drug('合成药甲', key='m1')['medication']['ref'],
             self.add_drug('合成药乙', key='m2')['medication']['ref']], 'w1'), 'k1')
        request_id = f"case:{case['id']}:q:dose"
        self.cases.require_input(case['id'], request_id=request_id,
                                 question='当前的剂量是多少？', fields=['dose'],
                                 question_kind='patient_actual_state',
                                 subject_refs=[case['related_medication_refs'][0]],
                                 command_key='req-1')
        self.cases.record_input(case['id'], request_id=request_id,
                                answer_ref='answer-1', value='5mg',
                                answer_kind=sc.ANSWER_PROVIDED, command_key='ans-1')
        return self.cases.get(case['id']), request_id

    def test_an_answered_question_carries_its_answer_and_source(self):
        case, request_id = self._answered_case()
        answered = self.cases.investigation_context(case)['questions']['answered']
        item = next(entry for entry in answered if entry['request_id'] == request_id)
        self.assertEqual('5mg', item['value'])
        self.assertIsNotNone(item['source'])
        self.assertIsNotNone(item['answered_against'])

    def test_an_open_question_is_not_reported_as_answered(self):
        case, _ = self._answered_case()
        case = self.cases.get(case['id'])
        open_id = f"case:{case['id']}:q:open"
        self.cases.require_input(case['id'], request_id=open_id,
                                 question='上次复查是什么时候？', fields=['start_at'],
                                 command_key='req-2')
        context = self.cases.investigation_context(self.cases.get(case['id']))
        self.assertNotIn(open_id,
                         {item['request_id'] for item in context['questions']['answered']})
        self.assertIn(open_id,
                      {item['request_id'] for item in context['questions']['open']})


if __name__ == '__main__':
    unittest.main()
