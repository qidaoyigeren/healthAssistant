"""本轮完成标准：**同一件安全事项第二次回访时，系统利用第一次的结果和期间变化，
执行有区别、有依据的下一步。**

这一套是整轮的判据，所以它不测某个函数，而是把两次回访**真的**跑一遍：真实
HTTP 端点、真实 worker、真实任务。规模器仍是脚本化的（`proposal_provider`），
它验的是状态与恢复机制；模型自己会不会这么选，属于有限真实验收
（`scripts/review-visit-live-acceptance.py`），**不能**拿这里的结果记成真实模型
自主成功。

三个用例各钉住判据的一面：

* 期间变了 → 第二次**聚焦那个变化**，并复用第一次的结论；
* 期间没变 → 第二次**不凭空产生新问题**；
* 变化与那条回答无关 → **不重开**它，用户不必重答一遍。
"""
from __future__ import annotations

import shutil
import unittest

from stage0 import review_visits as rv
from stage0 import safety_cases as sc
from stage0.care_tasks import CareTasks
from stage0.test_review_visit_flow import OUTPUT_ROOT, _Host, _declare


class _TwoVisits(unittest.TestCase):
    """两次回访的公共装置：第一次留下结论，第二次看它有没有用上。"""

    case_id: str = ''

    def setUp(self):
        self.directory = OUTPUT_ROOT / 'change-based' / self.id().rsplit('.', 1)[-1]
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.api = _Host(self.directory / 'host')
        self.case_id = self.api.seed_one_case()['case_id']

    def tearDown(self):
        self.api.close()

    # ---- 装置 -------------------------------------------------------------
    def _run_first_visit(self, case_id: str) -> str:
        """第一次回访：模型问一条事实问题，用户答了。返回那条 request_id。"""
        declared = _declare('合成药乙目前的服用频次是什么？', field='schedule',
                            strategy='ask_user')

        def provider(payload):
            inv = payload['investigation']
            return declared if not (inv.get('questions') or []) else {'decision': 'respond'}

        self.api = self.api.rebuild(proposal_provider=provider)
        view = self.api.start_visit(case_id, key='visit-first')
        self.assertEqual('waiting_input', self._task()['status'], '第一次回访应当停在等补充上')

        request_id = view['required_inputs'][0]['request_id']
        self.api.answer(case_id, request_id, '每日一次')
        self.api.pump()

        visit_id = view['visit']['visit_id']
        self.visits().set_status(visit_id, rv.STATUS_COMPLETED)
        return request_id

    def _task(self) -> dict:
        """**当前这一次**回访的任务。

        不能取"最后一个带 visit_id 的任务"：任务列表的顺序取决于存储，那样取到
        的可能正是上一次回访的任务，于是读出来的上下文属于另一访。
        """
        open_visit = self.visits().open_for_case(self.case_id)
        target = open_visit['id'] if open_visit else self.visits().for_case(self.case_id)[-1]['id']
        return next(t for t in self.api.product.objects('care_task')
                    if t.get('visit_id') == target)

    def visits(self) -> rv.ReviewVisitStore:
        return rv.ReviewVisitStore(self.api.product)

    def _second_visit_context(self, case_id: str):
        """第二次回访的**模型上下文**——它这一轮真正看到的东西。"""
        case = sc.SafetyCaseStore(self.api.product).get(case_id)
        return CareTasks(self.api.product)._safety_case_context(
            self._task(), case, {'max_steps': 16})


class SecondVisitDiffersTests(_TwoVisits):
    """判据本体。"""

    def test_the_second_visit_carries_the_first_result_and_follows_the_change(self):
        """期间变了：第二次带着第一次的结果开工，并聚焦那个变化。"""
        case_id = self.case_id
        request_id = self._run_first_visit(case_id)

        # 期间发生一次**相关**变化：药单动了。那条回答问的正是用药，所以它
        # **应当**被判为不再适用——这不是缺陷，正是"变化影响到了什么"的答案。
        self.api.add_medication('change-1', '合成药丙')
        self.api.pump()

        self.api = self.api.rebuild(proposal_provider=lambda payload: {'decision': 'respond'})
        self.api.start_visit(case_id, key='visit-second')
        context = self._second_visit_context(case_id)['visit']

        # (0) 这确实是**第二次**，而且它知道自己接着谁。
        self.assertEqual(2, context['sequence'])
        self.assertIsNotNone(context['previous_visit_id'])

        # (a) 第一次的**结果**传下来了：它的结论与收尾时刻都在，不是重新查一遍。
        previous = context['previous_result']
        self.assertIsNotNone(previous, '第二次看不到第一次的结果')
        self.assertTrue(previous['next_step'], f'第一次的结果里没有结论：{previous}')
        self.assertTrue(previous['closed_at'])

        # (b) 聚焦**实际变化**：上次之后真的事件在，且说的正是这次变化，
        #     不是"最后几条历史"照抄一遍。变化按**游标之后**算，不看最后 N 条。
        events = context['new_since_last_visit']['events']
        self.assertTrue(events, '期间改了药单，第二次却说"上次之后没有新记录"')
        self.assertTrue(any('重新核对' in line for line in events),
                        f'记录变化没有作为事件传下来：{events}')

        # (c) 受影响的那条答案被**如实标成需要重核**，而不是当成没答过、
        #     更不是悄悄留着继续当依据。没受影响的不会被拖下水。
        retired = [item['request_id'] for item in context['retired_answers']]
        self.assertIn(request_id, retired,
                      f'药单变了，那条问剂量的回答却既没重核也没作废：{retired}')
        self.assertEqual([request_id], retired, '重开的应当是受影响的那一条，不是全部')

    def test_a_second_visit_over_an_unchanged_record_asks_nothing_new(self):
        """不变时不该凭空产生新问题——"没有新记录"是一句信息状态，不是一句判断。"""
        case_id = self.case_id
        request_id = self._run_first_visit(case_id)

        first = self.visits().for_case(case_id)[-1]
        raw = sc.SafetyCaseStore(self.api.product).get(case_id)
        # 把第一次的游标推到当前历史末尾：第二次看到的就是"这中间没有新记录"。
        self.visits().save_result(first['id'], {'unresolved': []},
                                  cursor_after=len(raw.get('history') or []))

        self.api = self.api.rebuild(proposal_provider=lambda payload: {'decision': 'respond'})
        self.api.start_visit(case_id, key='visit-quiet')
        context = self._second_visit_context(case_id)['visit']

        self.assertEqual([], context['new_since_last_visit']['events'])
        self.assertEqual([], context['new_since_last_visit']['changed_scopes'])
        self.assertEqual(rv.NO_NEW_RECORDS, context['new_since_last_visit']['statement'])
        self.assertIn(request_id,
                      {item['request_id'] for item in context['reusable_answers']})

    def test_the_second_visit_delivers_instead_of_re_running_the_first(self):
        """有区别、有依据的下一步：第二次直接交付，不重新检索同一结论。"""
        case_id = self.case_id
        self._run_first_visit(case_id)
        first_queries = list((self._task().get('investigation') or {}).get('queries') or [])

        self.api = self.api.rebuild(proposal_provider=lambda payload: {'decision': 'respond'})
        self.api.start_visit(case_id, key='visit-deliver')
        task = self._task()
        inv = task.get('investigation') or {}

        self.assertEqual('checks_completed', inv.get('termination_reason'),
                         f'第二次回访没有以交付收尾：{inv.get("termination_reason")!r}')
        self.assertGreaterEqual(inv.get('model_decisions') or 0, 1)
        # 没有为了重新得到同一结论再查一遍。
        self.assertEqual(first_queries, list(inv.get('queries') or []))
        # 结果里读得出复用了什么。
        result = self.visits().for_case(case_id)[-1]['result']
        self.assertTrue(result['reused'], '结果里看不出复用了任何已有信息')
        self.assertTrue(result['end_reason'])


class BetweenVisitsChangeTests(_TwoVisits):
    """两次回访**之间**发生的改动，第二次必须看得见。

    这一条是真实模型那一轮暴露出来的：药单明明改过，第二次回访的摘要里
    `changed_scopes` 与 `events` 都是空的——它看到的"上次之后"什么都没有。
    """

    def test_a_change_made_between_visits_is_visible_to_the_second(self):
        case_id = self.case_id
        self._run_first_visit(case_id)
        first = self.visits().for_case(case_id)[-1]
        self.visits().set_status(first['id'], rv.STATUS_COMPLETED)

        # 第一次收尾之后、第二次开始之前，药单变了。
        self.api.add_medication('between-1', '合成药丙')
        self.api.pump()

        self.api = self.api.rebuild(proposal_provider=lambda payload: {'decision': 'respond'})
        self.api.start_visit(case_id, key='visit-after-change')
        context = self._second_visit_context(case_id)['visit']

        self.assertIn('用药记录', context['new_since_last_visit']['changed_scopes'],
                      '两次回访之间改了药单，第二次却说没有范围变化')
        self.assertIsNone(context['new_since_last_visit']['statement'],
                          '有变化时不该报"系统尚未收到新记录"')

    def test_a_visit_without_a_saved_end_position_does_not_reset_the_next_start(self):
        """上一次没落 `after` 时，起点退回它开始的位置，**不是 0**。

        这是**存储层**的契约，所以直接在存储层验：走 HTTP 的话，读一次事项视图
        就会把结果重渲染一遍、顺手把 `after` 补回来，模拟不出这个状态。
        """
        case_id = self.case_id
        self._run_first_visit(case_id)
        first = self.visits().for_case(case_id)[-1]
        raw = self.visits().get(first['id'])
        raw['cursor'] = {**raw['cursor'], 'after': None}
        # 裸 save 会留下一个未提交的隐式事务，后面的命令就开不了自己的事务。
        with self.api.product.transaction():
            self.api.product.save(rv.KIND, raw)

        case = sc.SafetyCaseStore(self.api.product).get(case_id)
        # `history_length=99`：起点若来自"现在有多少条历史"，一眼就能看出来。
        second, created = self.visits().open_or_continue(
            'store-level-2', case_id=case_id,
            reason=rv.derive_reason(self.api.product, case),
            actor='caregiver', history_length=99)

        self.assertTrue(created, '第一次已收尾，第二次应当新开一访')
        self.assertNotEqual(0, second['cursor']['before'],
                            '起点退回了 0——整件事项的来龙去脉会被当成"上次之后"重报一遍')
        self.assertEqual(first['cursor']['opened_at_history'], second['cursor']['before'])


class UnfinishedCarriesForwardTests(_TwoVisits):
    """上次**没做完**的事，第二次要接着办，而不是从头问一遍。"""

    def test_what_the_first_visit_left_open_reaches_the_second(self):
        case_id = self.case_id
        declared = _declare('两周后的复查做了吗？', field='schedule', strategy='ask_user')

        def provider(payload):
            inv = payload['investigation']
            return declared if not (inv.get('questions') or []) else {'decision': 'respond'}

        self.api = self.api.rebuild(proposal_provider=provider)
        view = self.api.start_visit(case_id, key='visit-open')
        request_id = view['required_inputs'][0]['request_id']
        # 「暂不回答」：推迟 ≠ 不知道 ≠ 解决。它**留着**，下一次接着办。
        self.api.answer(case_id, request_id, '暂不回答', kind='declined', key='a-declined')
        self.api.pump()
        self.visits().set_status(view['visit']['visit_id'], rv.STATUS_COMPLETED)

        self.api = self.api.rebuild(proposal_provider=lambda payload: {'decision': 'respond'})
        self.api.start_visit(case_id, key='visit-again')
        context = self._second_visit_context(case_id)['visit']

        previous = context['previous_result']
        self.assertIsNotNone(previous)
        self.assertTrue(previous['unresolved'],
                        f'上次留下的未决事项没有传到第二次：{previous}')
        # 未决项引用的是**事项上那条请求**，不是复制一份问题正文。
        self.assertTrue(any(request_id in (item.get('basis') or {}).get('refs', [])
                            for item in previous['unresolved']),
                        f'上次未决的那一条没传下来：{previous["unresolved"]}')
        # 它仍是**未决**，没有被偷偷记成已完成。
        self.assertNotIn('completed', {item.get('status') for item in
                                       context['reusable_answers']})


class UnrelatedChangeTests(_TwoVisits):
    """无关变化不该让用户把已经答过的问题重答一遍。"""

    def test_a_change_to_another_scope_does_not_reopen_the_answer(self):
        case_id = self.case_id
        request_id = self._run_first_visit(case_id)

        # 把这条回答的依赖范围收窄到"相关背景"，再只改用药记录。
        store = sc.SafetyCaseStore(self.api.product)
        fresh = store.get(case_id)
        item = next(i for i in fresh['required_inputs'] if i['request_id'] == request_id)
        item['subject_refs'] = ['memory:conclusion:1@v1']
        item['answered_against'] = self.api.product.revisions()
        self.api.product.save(sc.KIND, fresh)

        self.api.add_medication('change-2', '合成药丙')
        self.api.pump()

        after = store.get(case_id)
        reopened = next(i for i in after['required_inputs']
                        if i['request_id'] == request_id)
        self.assertEqual('answered', reopened['status'],
                         '只改了用药记录，却把一条只依赖背景的回答重新打开了')


if __name__ == '__main__':
    unittest.main()
