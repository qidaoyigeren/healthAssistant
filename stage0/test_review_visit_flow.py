"""回访的三个产品场景，走**真实 HTTP 端点 + 真实 worker**。

这一套回答的是"用户回来跟进时，系统能不能接着上次的事情继续提供帮助"，
所以它不走内部函数：起一个真 app、发真请求、跑真 outbox worker。

规模器是**脚本化的**（`proposal_provider`），不调模型——它验的是状态与恢复机制。
模型自己会不会这么选，属于有限真实验收（`scripts/review-visit-live-acceptance.py`），
**不能**拿这里的结果记成真实模型自主成功。

三个场景对应交付要求第八节：
  A 已有信息充分  → 复用已有答案，不重复要求用户填写；
  B 出现相关变化  → 识别缺口、提问、确认变更、检查重排队、同一事项更新；
  C 跟进行动未完成 → 保留未决，不把回访结束当成事项解决。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from stage0 import safety_cases as sc
from stage0.agent import DDITool, MedicationCoordinatorAgent
from stage0.care_tasks import CareTasks
from stage0.product import ProductStore

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / 'output' / 'visit'
REAL_PATIENT_DB = (ROOT / 'stage0' / 'memory.db').resolve()

SYNTHETIC_WARNING = {
    'drug_a': '合成药甲', 'drug_b': '合成药乙', 'severity': 'major',
    'mechanism': '合成机制', 'effect': '合成风险效应', 'management': None,
    'source_text': '合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）',
    'source_url': 'https://synthetic.invalid/label/a',
    'confidence': 'high', 'detection_path': 'synthetic_detector',
}


def synthetic_detect(medications):
    if {'合成药甲', '合成药乙'}.issubset(set(medications)):
        return [dict(SYNTHETIC_WARNING)]
    return []


class _Host:
    """最小产品宿主：真实服务 + 真实 worker + 隔离数据库。"""

    def __init__(self, directory: Path, *, proposal_provider=None) -> None:
        import stage0.server as server
        directory.mkdir(parents=True, exist_ok=True)
        db_path = (directory / 'memory.db').resolve()
        assert db_path != REAL_PATIENT_DB, f'拒绝连接真实患者库：{db_path}'
        self.db_path = db_path
        holder: dict = {}

        def factory():
            return MedicationCoordinatorAgent(
                holder['store'], ddi_tool=DDITool(synthetic_detect),
                llm_planner_enabled=proposal_provider is not None,
                proposal_provider=proposal_provider)

        self.app = server.create_app(db_path=db_path, worker_thread=False,
                                     agent_factory=factory)
        holder['store'] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.product = ProductStore(self.store)
        self.client = TestClient(self.app)

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.worker.stop()
            self.store.close()

    def rebuild(self, **kwargs) -> '_Host':
        """换掉宿主、**保留同一个数据库**——这就是"重启"。"""
        self.close()
        return type(self)(self.db_path.parent, **kwargs)

    # ---- 产品路径 --------------------------------------------------------
    def cases(self) -> list[dict]:
        return self.client.get('/v1/safety-cases').json()['items']

    def case(self, case_id: str) -> dict:
        return self.client.get(f'/v1/safety-cases/{case_id}').json()

    def submit_event(self, key: str, body: dict) -> dict:
        response = self.client.post('/v1/events', json=body,
                                    headers={'Idempotency-Key': key})
        assert response.status_code == 202, response.text
        self.worker.drain_once()
        return self.client.get(f'/v1/events/{key}').json()

    def add_medication(self, key: str, name: str) -> None:
        self.submit_event(key, {'session_id': 'visit', 'event_type': 'medication_change',
                                'text': f'新增{name}', 'source': 'caregiver',
                                'payload': {'action': 'add', 'medication': name}})

    def pump(self, rounds: int = 4, *, max_tasks: int = 6) -> None:
        for _ in range(rounds):
            self.worker.drain_once(max_tasks=max_tasks)

    def seed_one_case(self) -> dict:
        self.add_medication('seed-1', '合成药甲')
        self.add_medication('seed-2', '合成药乙')
        cases = self.cases()
        assert len(cases) == 1, cases
        return cases[0]

    def start_visit(self, case_id: str, key: str = 'visit-1') -> dict:
        response = self.client.post(
            f'/v1/safety-cases/{case_id}/visits',
            json={'key': key, 'expected_revision': self.case(case_id)['revision']})
        assert response.status_code == 200, response.text
        self.pump()
        return self.case(case_id)

    def answer(self, case_id: str, request_id: str, value: str, *,
               kind: str | None = None, key: str = 'answer-1') -> dict:
        body = {'key': key, 'expected_revision': self.case(case_id)['revision'],
                'request_id': request_id, 'value': value}
        if kind:
            body['answer_kind'] = kind
        response = self.client.post(f'/v1/safety-cases/{case_id}/answer', json=body)
        assert response.status_code == 200, response.text
        return response.json()


class _VisitCase(unittest.TestCase):
    """每个用例一个目录、一个数据库、一个宿主实例。"""

    proposal_provider = None

    def setUp(self):
        self.directory = OUTPUT_ROOT / self.id().rsplit('.', 2)[-2] / self.id().rsplit('.', 1)[-1]
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.api = self._build()

    def _build(self, **kwargs) -> _Host:
        kwargs.setdefault('proposal_provider', self.proposal_provider)
        return _Host(self.directory / 'host', **kwargs)

    def rebuild(self, **kwargs) -> _Host:
        """换掉宿主、**保留同一个数据库**——这就是"重启"。"""
        self.api.close()
        self.api = self._build(**kwargs)
        return self.api

    def tearDown(self):
        try:
            self.api.close()
        except Exception:
            pass


def _declare(statement, *, field=None, strategy='patient_record',
             target='patient_actual_state', subjects=('合成药乙',),
             why='它决定这条提示是否还成立') -> dict:
    return {'decision': 'tool', 'tool': 'plan_questions', 'gap_id': 'subquestions',
            'expected_observation': '问题集建立', 'expected_change': '按回答重新核对',
            'basis_refs': ['memory:conclusion:1@v1'],
            'arguments': {'questions': [{'statement': statement,
                                         'information_target': target,
                                         'strategy': strategy,
                                         'subject_refs': list(subjects),
                                         'target_field': field, 'why': why}]}}


class ScenarioAReusesWhatIsKnown(_VisitCase):
    """"已有信息充分"：复用已有有效答案，形成简短结果，不重复要求用户填写。"""

    @staticmethod
    def proposal_provider(payload):
        # 本轮不声明任何问题：已有信息足够，不制造补问。
        return {'decision': 'respond'}

    def test_a_visit_with_enough_information_asks_nothing_new(self):
        case = self.api.seed_one_case()
        case_id = case['case_id']

        # 先制造一条**已经有答案**的问题：用户答过，事项上有 answered_inputs。
        self.api.cases_store = sc.SafetyCaseStore(self.api.product)
        declared = _declare('合成药乙目前的服用频次是什么？', field='schedule',
                            strategy='ask_user')

        first = self._run_with([declared])
        answerable = first['required_inputs']
        self.assertTrue(answerable, '脚本化调查应当提出一条待回答的问题')
        self.api.answer(case_id, answerable[0]['request_id'], '每日一次')

        # 开始回访：这一次不再声明新问题。
        view = self.api.start_visit(case_id, key='visit-enough')
        visit = view['visit']
        self.assertIsNotNone(visit, '回访没有建起来')
        self.assertEqual([], visit['focus'],
                         f'信息已经足够，却又提出了问题：{visit["focus"]}')
        self.assertEqual([], view['required_inputs'],
                         '信息已经足够，却又要求用户补充')

        result = visit['result']
        self.assertIsNotNone(result, '回访没有产生结果')
        self.assertGreaterEqual(result['answered_count'], 1,
                                '已经有答案，结果里却看不出复用')
        self.assertNotIn('新增的问题', str(result))

    def _run_with(self, declarations):
        """用一次性的脚本规划器跑一轮调查，返回最新的事项视图。"""
        case = self.api.seed_one_case() if not self.api.cases() else self.api.cases()[0]

        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                for declaration in declarations:
                    return declaration
            return {'decision': 'respond'}

        self.api = self.api.rebuild(proposal_provider=provider)
        tasks = CareTasks(self.api.product)
        task = tasks.create('scenario-a', 'safety_case', case['case_id'])
        tasks.resume(task['id'], 'scenario-a-run', task['revision'], 'continue',
                     enqueue=True)
        self.api.pump()
        return self.api.case(case['case_id'])


class CursorConsumptionTests(_VisitCase):
    """游标只在**成功消费**时推进，且只推到手交给模型的那个位置。"""

    @staticmethod
    def always_invalid(payload):
        """每一轮都提一个不允许的动作：这一轮**必然跑不成**。"""
        return {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'no_such_gap',
                'expected_observation': 'x', 'arguments': {'query': 'snapshot'}}

    def test_a_run_that_failed_leaves_its_events_unconsumed(self):
        """真跑一轮**没跑成**的调查：游标不能动，而历史确实长过。

        这一条是对照旧行为写的——旧代码无条件把游标推到当时的长度，
        于是失败那轮没看过的事件下一轮再也不会被当成"新增"。
        """
        case = self.api.seed_one_case()
        self.api = self.api.rebuild(proposal_provider=self.always_invalid)
        self.api.start_visit(case['case_id'], key='cursor-fail')

        task = [t for t in self.api.product.objects('care_task') if t.get('visit_id')][-1]
        raw = sc.SafetyCaseStore(self.api.product).get(case['case_id'])
        self.assertNotIn(task.get('investigation', {}).get('termination_reason'),
                         ('checks_completed', 'waiting_input', 'waiting_review'))
        self.assertTrue(raw['history'], '事项应当有历史')
        self.assertEqual(0, task.get('case_history_cursor') or 0,
                         '这一轮没跑成，却把没看过的事件标成了已消费')

    def test_a_successful_run_consumes_only_what_it_was_handed(self):
        case = self.api.seed_one_case()
        self.api.start_visit(case['case_id'], key='cursor-1')
        task = [t for t in self.api.product.objects('care_task') if t.get('visit_id')][-1]
        raw = sc.SafetyCaseStore(self.api.product).get(case['case_id'])
        self.assertIsNotNone(task.get('case_history_cursor'))
        self.assertLessEqual(task['case_history_cursor'], len(raw['history']))

    def test_a_failed_run_does_not_consume_the_events_it_never_read(self):
        """一轮没跑成，它没看过的事件必须留在游标之后。

        规则本身在 `consumed_cursor` 里，这里直接打它——失败的每一种收尾都要
        原样返回，跑成的那一种也不能越过运行期间新增的事件。
        """
        from stage0.care_tasks import CONSUMED_TERMINATIONS, consumed_cursor
        for termination in ('unrecoverable_failure', 'no_progress', 'cancelled',
                            'budget_insufficient', 'evidence_unavailable', None):
            self.assertNotIn(termination, CONSUMED_TERMINATIONS)
            self.assertEqual(
                7, consumed_cursor(7, 12, termination, 20),
                f'{termination!r} 没跑成，却把没看过的事件标成了已消费')

        for termination in CONSUMED_TERMINATIONS:
            # 跑成了：停在**建上下文时**的位置，不越过运行期间新增的事件。
            self.assertEqual(12, consumed_cursor(7, 12, termination, 20))
            # 历史反而变短（回滚/清理）时也不越界。
            self.assertEqual(9, consumed_cursor(7, 12, termination, 9))


class ReuseIsAnAction(_VisitCase):
    """有效答案可以直接结束当前事实问题，不强制先规划、再搜索、再提问。"""

    @staticmethod
    def always_respond(payload):
        """"复用已有结论并交付"——模型第一轮就选它。"""
        return {'decision': 'respond'}

    def _declare_then_answer(self, case_id):
        declared = _declare('合成药乙目前的服用频次是什么？', field='schedule',
                            strategy='ask_user')
        first = self._run_with([declared], case_id)
        request = first['required_inputs'][0]['request_id']
        self.api.answer(case_id, request, '每日一次')
        return request

    def _run_with(self, declarations, case_id):
        case = self.api.cases()

        def provider(payload):
            inv = payload['investigation']
            if not (inv.get('questions') or []):
                for declaration in declarations:
                    return declaration
            return {'decision': 'respond'}

        self.api = self.api.rebuild(proposal_provider=provider)
        tasks = CareTasks(self.api.product)
        task = tasks.create('reuse-seed', 'safety_case', case_id)
        tasks.resume(task['id'], 'reuse-seed-run', task['revision'], 'continue',
                     enqueue=True)
        self.api.pump()
        return self.api.case(case_id)

    def test_the_model_may_choose_to_reuse_rather_than_search(self):
        case = self.api.seed_one_case()
        case_id = case['case_id']
        self._declare_then_answer(case_id)

        # 第二次进来：这一次模型什么都不做，直接交付。
        self.api = self.api.rebuild(proposal_provider=self.always_respond)
        self.api.start_visit(case_id, key='reuse-visit')
        task = [t for t in self.api.product.objects('care_task')
                if t.get('visit_id')][-1]
        inv = task.get('investigation') or {}

        # 交付里**有模型的一次选择**——不是代码在它开口前就收尾了。
        self.assertGreaterEqual(inv.get('model_decisions') or 0, 1,
                                '全程没有发生模型决策，这不是 Agent 的决定')
        self.assertEqual('checks_completed', inv.get('termination_reason'),
                         f'无事可做的回访被记成了 {inv.get("termination_reason")!r}')
        self.assertEqual('completed', task['status'])
        # 也没有为了"重新得到同一结论"去检索。
        self.assertEqual([], list(inv.get('queries') or []))


class TheModelSeesTheVisit(_VisitCase):
    """回访摘要真的进到模型上下文里，而且只放引用。"""

    def _context(self, case_id):
        case = sc.SafetyCaseStore(self.api.product).get(case_id)
        task = next(t for t in self.api.product.objects('care_task')
                    if t.get('visit_id'))
        tasks = CareTasks(self.api.product)
        return tasks._safety_case_context(task, case, {'max_steps': 16}), task

    def test_the_context_carries_the_visit_summary(self):
        case = self.api.seed_one_case()
        view = self.api.start_visit(case['case_id'], key='visit-ctx')
        context, task = self._context(case['case_id'])

        visit = context['visit']
        self.assertIsNotNone(visit, '回访任务的上下文里没有回访摘要')
        self.assertEqual(view['visit']['visit_id'], visit['visit_id'])
        self.assertEqual(task['visit_id'], visit['visit_id'])
        self.assertEqual(1, visit['sequence'])
        self.assertEqual(view['visit']['reason'], visit['reason'])
        for key in ('reusable_answers', 'retired_answers', 'pending_candidates',
                    'confirmed_follow_up', 'open_questions', 'allowed_actions',
                    'new_since_last_visit', 'previous_result'):
            self.assertIn(key, visit)

    def test_no_new_records_is_stated_as_an_information_state(self):
        """没有新记录 ≠ 情况稳定。这一句是全项目唯一允许的写法。"""
        from stage0 import review_visits as rv
        case = self.api.seed_one_case()
        view = self.api.start_visit(case['case_id'], key='visit-quiet')
        visit_id = view['visit']['visit_id']
        # 把游标推到当前历史末尾——此刻**确实**没有新记录，这条规则才有东西可判。
        raw = sc.SafetyCaseStore(self.api.product).get(case['case_id'])
        rv.ReviewVisitStore(self.api.product).save_result(
            visit_id, {'unresolved': []}, cursor_after=len(raw.get('history') or []))

        context, _ = self._context(case['case_id'])
        new = context['visit']['new_since_last_visit']
        self.assertEqual([], new['events'])
        self.assertEqual([], new['changed_scopes'])
        self.assertEqual(rv.NO_NEW_RECORDS, new['statement'])
        # 系统的信息状态，不是一句没有人做过的判断。这句原话本身**否定**了
        # "风险已经解除"，所以不能按关键词一刀切——要禁止的是**肯定**的那两种说法。
        self.assertNotIn('情况稳定', new['statement'])
        self.assertNotIn('风险已解除', new['statement'])

    def test_the_summary_does_not_copy_patient_facts(self):
        """只放引用：摘要里不得出现药名原文或证据正文。"""
        import json
        case = self.api.seed_one_case()
        self.api.start_visit(case['case_id'], key='visit-copy')
        context, _ = self._context(case['case_id'])
        blob = json.dumps(context['visit'], ensure_ascii=False)
        for medication in self.api.product.memory.current_medications():
            self.assertNotIn(medication.get('display_name') or '\0', blob)


class ScenarioBRelatedChange(_VisitCase):
    """"出现相关变化"：识别缺口 → 提问 → 确认变更 → 检查重排队 → 同一事项更新。"""

    @staticmethod
    def proposal_provider(payload):
        inv = payload['investigation']
        questions = inv.get('questions') or []
        if not questions:
            return _declare('合成药乙目前的剂量是多少？', field='dose',
                            strategy='ask_user')
        # 用户答了之后，这一轮有**它还没看过的新信息**；不先把它读进来，
        # 这一轮既结束不了（respond 会被判 investigation_not_terminal），
        # 也做不了别的。真实模型也得先看一眼记录。
        if inv.get('new_information_pending'):
            for gap in inv.get('open_gaps') or []:
                if 'memory_read' in (gap.get('closable_by') or []):
                    return {'decision': 'tool', 'tool': 'memory_read',
                            'gap_id': gap['gap_id'],
                            'expected_observation': '把权威记录读进这一轮',
                            'arguments': {'query': 'snapshot'}}
        return {'decision': 'respond'}

    def test_a_confirmed_change_updates_the_same_case_and_requeues_checks(self):
        from stage0 import safety_checks
        case = self.api.seed_one_case()
        case_id = case['case_id']
        view = self.api.start_visit(case_id, key='visit-change')

        visit = view['visit']
        request_id = view['required_inputs'][0]['request_id'] if view['required_inputs'] else None
        self.assertTrue(request_id, f'没有提出问题：{view["required_inputs"]}')

        # 用户回答"情况有变化" —— 它**不是**一个值，而是一条待确认的线索。
        self.api.answer(case_id, request_id, '情况有变化', kind='changed',
                        key='answer-changed')
        # 回答会把调查**唤醒继续**（"根据新结果调整回访重点"）。让它跑完，
        # 否则任务在跑，"确认"这一步会被正确地挡下（409）。
        self.api.pump()
        after_answer = self.api.case(case_id)
        still_open = [item for item in after_answer['required_inputs']
                      if item['request_id'] == request_id]
        self.assertTrue(still_open, '"情况有变化"不该把这条问题关掉')
        self.assertEqual('changed', still_open[0]['answer_kind'])

        # 用户结构化声明一条变更候选。
        before = {m['display_name']: m['dose']
                  for m in self.api.store.current_medications()}
        visit_id = after_answer['visit']['visit_id']
        proposed = self.api.client.post(
            f'/v1/safety-cases/{case_id}/visits/{visit_id}/candidates',
            json={'key': 'candidate-1', 'name': '合成药乙', 'field': 'dose',
                  'value': '10mg'})
        self.assertEqual(200, proposed.status_code, proposed.text)
        pending = proposed.json()['visit']['pending_candidates']
        self.assertEqual(1, len(pending), pending)
        self.assertEqual(before['合成药乙'], pending[0]['before'],
                         'before 必须取自当前权威记录，不采信调用方自报')
        self.assertEqual('user_declared', pending[0]['source'])
        self.assertEqual(before, {m['display_name']: m['dose']
                                  for m in self.api.store.current_medications()},
                         '候选未经确认就改了权威记录')

        # 确认 → 走既有权威入口写入，必要检查重新排队。
        confirmed = self.api.client.post(
            f'/v1/safety-cases/{case_id}/visits/{visit_id}'
            f'/candidates/{pending[0]["id"]}/confirm',
            json={'key': 'confirm-1'})
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        after = {m['display_name']: m['dose']
                 for m in self.api.store.current_medications()}
        self.assertEqual('10mg', after['合成药乙'], '确认之后记录没有更新')
        self.assertTrue(safety_checks.pending(self.api.store)['unfinished'],
                        '确认用药变更之后，必要检查没有重新排队')

        # 同一件事项更新，并且**说得清楚这次补充改变了什么**。
        final = self.api.case(case_id)
        self.assertEqual(case_id, final['case_id'], '变更不该产生第二件事项')
        actions = final['visit']['result']['actions']
        self.assertTrue(any('10mg' in item['text'] for item in actions), actions)
        self.assertTrue(any(item['basis']['kind'] == 'record' for item in actions),
                        f'写入的依据类型应当是权威记录：{actions}')

    def test_a_dismissed_candidate_writes_nothing(self):
        case = self.api.seed_one_case()
        case_id = case['case_id']
        view = self.api.start_visit(case_id, key='visit-dismiss')
        visit_id = view['visit']['visit_id']
        before = {m['display_name']: m['dose']
                  for m in self.api.store.current_medications()}
        proposed = self.api.client.post(
            f'/v1/safety-cases/{case_id}/visits/{visit_id}/candidates',
            json={'key': 'candidate-2', 'name': '合成药甲', 'field': 'dose',
                  'value': '99mg'})
        candidate_id = proposed.json()['visit']['pending_candidates'][0]['id']
        dismissed = self.api.client.post(
            f'/v1/safety-cases/{case_id}/visits/{visit_id}/candidates/{candidate_id}/dismiss',
            json={'key': 'dismiss-1'})
        self.assertEqual(200, dismissed.status_code, dismissed.text)
        self.assertEqual(before, {m['display_name']: m['dose']
                                  for m in self.api.store.current_medications()})
        self.assertEqual([], dismissed.json()['visit']['pending_candidates'])


class ScenarioCFollowUpNotDone(_VisitCase):
    """"跟进行动尚未完成"：保留未决，**不把回访结束当成事项解决**。"""

    proposal_provider = None

    def test_not_done_keeps_the_question_open_and_the_case_unresolved(self):
        case = self.api.seed_one_case()
        case_id = case['case_id']
        store = sc.SafetyCaseStore(self.api.product)
        # 一条**跟进行动**类的问题：只有这一类，"已完成"才等于把它答上。
        store.require_input(case_id, request_id='req-action',
                            question='两周后的复查做了吗？',
                            question_kind=sc.QUESTION_KIND_FOLLOW_UP_ACTION)

        self.api.answer(case_id, 'req-action', '还没做', kind='not_done',
                        key='answer-not-done')
        view = self.api.case(case_id)
        item = next(i for i in view['required_inputs'] if i['request_id'] == 'req-action')
        self.assertEqual('open', item['status'], '"尚未完成"不该把问题关掉')
        self.assertEqual('not_done', item['deferred_kind'])
        self.assertNotEqual(sc.STATUS_RESOLVED, view['status'],
                            '一次"还没做"的回答把事项关掉了')

        # 关闭仍然被挡住——未决就得继续挡住。
        evidence = self.api.client.get(
            f'/v1/safety-cases/{case_id}/closure-evidence').json()
        self.assertIn('req-action', evidence['blocking_inputs'])

    def test_done_only_closes_a_follow_up_action_question(self):
        """「已完成」对**事实问题**不成立——那是在说一件跟问题无关的事。"""
        case = self.api.seed_one_case()
        case_id = case['case_id']
        store = sc.SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id='req-fact',
                            question='现在吃多少？', fields=['dose'])

        self.api.answer(case_id, 'req-fact', '已完成', kind='done', key='answer-done')
        view = self.api.case(case_id)
        item = next(i for i in view['required_inputs'] if i['request_id'] == 'req-fact')
        self.assertEqual('open', item['status'],
                         '一条事实问题被"已完成"关掉了')

    def test_unknown_keeps_the_uncertainty_and_stops_asking_this_user(self):
        case = self.api.seed_one_case()
        case_id = case['case_id']
        store = sc.SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id='req-unknown', question='最近有没有出血？')

        self.api.answer(case_id, 'req-unknown', '不清楚', kind='unknown', key='a-unknown')
        view = self.api.case(case_id)
        unknown = [i for i in view['unanswered_by_user']
                   if i['request_id'] == 'req-unknown']
        self.assertTrue(unknown, '明说不知道应当进 unanswered_by_user')
        self.assertTrue(unknown[0].get('needs_alternative_evidence'),
                        '明说不知道之后应当转去找替代证据')
        self.assertNotEqual(sc.STATUS_RESOLVED, view['status'],
                            '不确定没有消失，事项不得被关掉')

    def test_a_declined_question_stays_open_without_being_treated_as_unknown(self):
        """「暂不回答」≠「不知道」≠「解决了」。三者必须分得开。"""
        case = self.api.seed_one_case()
        case_id = case['case_id']
        store = sc.SafetyCaseStore(self.api.product)
        store.require_input(case_id, request_id='req-later', question='能不能确认一下剂量？')

        self.api.answer(case_id, 'req-later', '暂不回答', kind='declined', key='a-declined')
        view = self.api.case(case_id)
        item = next(i for i in view['required_inputs'] if i['request_id'] == 'req-later')
        self.assertEqual('open', item['status'], '"暂不回答"不该关掉问题')
        self.assertEqual('declined', item['deferred_kind'])
        self.assertFalse(
            [i for i in view['unanswered_by_user'] if i['request_id'] == 'req-later'],
            '"暂不回答"不是"明说不知道"——它还要再等这位用户')


if __name__ == '__main__':
    unittest.main()
