"""Failure delivery: what the product owes the user when a check or a tool fails.

Two separate contracts live here.

1. The final safety check must not classify a QUESTION as a risk assertion.
   A sub-question only labels evidence to gather; the report renders it under
   "就诊时可以向医生或药师确认什么".  Reading it as an uncited warning both
   blocks a correct report and hides the real unsupported assertions behind a
   false positive.

2. When the check (or composition, or a tool) DOES fail, the turn must still
   deliver a structured partial result: task status, actions taken, request
   ledger, valid evidence, the stop reason, and whatever is safe to show.  It
   must never report success, and it must never lose an already-sent request.
"""
import contextlib
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _nullcontext():
    return contextlib.nullcontext()

from stage0.agent import CareEvent
from stage0.care_tasks import CareTasks
from stage0.memory import MemoryStore
from stage0.product import ProductStore
from stage0.response_safety import check_composed_response


def check(text, **overrides):
    kwargs = {'warnings': [], 'escalation_required': True, 'refusal_required': False}
    kwargs.update(overrides)
    return check_composed_response(text, **kwargs)


class OpenQuestionIsNotARiskAssertion(unittest.TestCase):
    """The false positive that blocked the previous round's third live run."""

    # Verbatim from the blocked report: a MODEL sub-question about the label's
    # interaction section, rendered into section 5 with no citation by design
    # (there is no evidence to cite -- that is the point of the section).
    BLOCKED = '氨氯地平在适应症中是否有提及与克拉霉素的相互作用？'

    def test_the_blocked_sentence_is_not_evidence_of_a_risk_claim(self):
        self.assertNotIn('unrecorded_or_uncited_warning', check(self.BLOCKED))

    def test_a_question_ending_the_report_is_still_not_a_claim(self):
        report = '# 报告\n\n## 5. 就诊时可以向医生或药师确认什么\n\n' + self.BLOCKED \
                 + '\n\n本系统不做诊断。建议咨询医生/药师。'
        self.assertNotIn('unrecorded_or_uncited_warning', check(report))

    def test_statement_form_of_the_same_content_is_still_blocked(self):
        # Only the ASKING form is exempt.  Asserting the interaction without a
        # source is exactly what the rule exists to stop.
        self.assertIn('unrecorded_or_uncited_warning',
                      check('氨氯地平与克拉霉素存在相互作用。\n建议咨询医生/药师。'))

    def test_a_question_mark_alone_does_not_make_a_question(self):
        # Rhetorical: no interrogative marker, so the sentence still asserts.
        self.assertIn('unrecorded_or_uncited_warning',
                      check('氨氯地平与克拉霉素合用可增加低血压风险？\n建议咨询医生/药师。'))
        self.assertIn('unrecorded_or_uncited_warning',
                      check('氨氯地平与克拉霉素合用会出血？\n建议咨询医生/药师。'))

    def test_an_assertive_clause_cannot_hide_behind_a_trailing_question(self):
        self.assertIn('unrecorded_or_uncited_warning',
                      check('氨氯地平与克拉霉素存在相互作用；是否有风险？\n建议咨询医生/药师。'))

    def test_an_unmarked_warning_clause_beside_a_real_question_is_still_blocked(self):
        self.assertIn('unrecorded_or_uncited_warning',
                      check('氨氯地平与克拉霉素存在相互作用，是否需要监测？\n建议咨询医生/药师。'))

    def test_a_lower_risk_question_is_also_exempt_not_only_the_frozen_one(self):
        self.assertEqual(check('患者是否需要监测血压？\n建议咨询医生/药师。'), [])
        self.assertEqual(check('该材料是否提到肾功能不全时的剂量调整？\n建议咨询医生/药师。'), [])

    def test_uncited_warning_hard_gates_are_untouched(self):
        # The exemption must not touch fabricated references or missing escalation.
        self.assertIn('fabricated_citation', check(self.BLOCKED + '\n来源：https://elsewhere.test/x\n建议咨询医生/药师。'))
        self.assertIn('missing_escalation', check(self.BLOCKED))


def assert_question_provider(statement):
    """A planner that declares one sub-question and then stops, so the
    investigation ends with an unverified claim about `statement`."""
    def provider(payload):
        investigation = payload.get('investigation') or {}
        if not investigation.get('authority_read'):
            return {'decision': 'tool', 'tool': 'memory_read', 'gap_id': 'authority',
                    'expected_observation': '完整权威快照', 'arguments': {'query': 'snapshot'}}
        if not investigation.get('claims'):
            return {'decision': 'tool', 'tool': 'plan_questions', 'gap_id': 'subquestions',
                    'expected_observation': '声明子问题',
                    'arguments': {'questions': [{'statement': statement,
                                                 'entities': ['氨氯地平', '克拉霉素']}]}}
        return {'decision': 'respond'}
    return provider


class BlockedReportIsDeliveredAsAFailure(unittest.TestCase):
    """A report that fails its own final check must not become an unhandled
    exception: that loses the turn's whole record, and the runner then exports
    a result the evaluator cannot read."""

    ENV = {'AGENT_INVESTIGATION_ENABLED': '1', 'AGENT_NO_PROGRESS_LIMIT': '2',
           'MEMORY_ENABLE_LLM': '0', 'AGENT_LLM_VERIFIER': '0', 'AGENT_SUBQUESTION_PLANNER': 'model'}

    def run_turn(self, provider):
        from stage0.harness_eval import make_agent
        with tempfile.TemporaryDirectory() as directory:
            with MemoryStore(Path(directory) / 'memory.db') as store:
                for name in ('氨氯地平', '克拉霉素'):
                    store.apply_medication_change(action='add', name=name, ingredients=[],
                                                  session_id='s', turn_id='t', source='fixture')
                agent = make_agent(store, provider=provider)
                with patch.dict('os.environ', self.ENV):
                    return agent.handle(CareEvent('user_message', '核查用药'), session_id='s', turn_id='t')

    def test_an_unsupported_assertion_blocks_but_still_delivers_a_turn(self):
        response = self.run_turn(assert_question_provider('氨氯地平与克拉霉素存在相互作用'))
        bundle = response.answer_bundle or {}
        # The turn completed and carries the record it is owed.
        self.assertTrue(response.tool_trace, '失败回合必须保留已执行动作')
        self.assertIsNotNone(bundle.get('investigation'), '失败回合必须保留调查状态')
        self.assertIn('evidence_refs', bundle)
        # ... but it does not claim success.
        self.assertNotEqual(bundle.get('answer_status'), 'bounded_report')
        self.assertNotEqual(bundle.get('execution_status'), 'finished')
        self.assertNotEqual(bundle.get('goal_status'), 'completed')
        self.assertNotIn('存在相互作用', response.text,
                         '被拒绝的正文不得原样交付')
        blocked = [entry for entry in response.tool_trace if entry.get('phase') == 'respond_blocked']
        self.assertTrue(blocked, '拒绝必须留下可审计的 trace 记录')
        # The rejection is recorded, not hidden: the audit keeps the text that
        # was refused, and the stop reason that produced it.
        self.assertIn('unrecorded_or_uncited_warning', blocked[-1]['errors'])
        self.assertIn('存在相互作用', blocked[-1]['rejected_text'])
        self.assertEqual(blocked[-1]['stop_reason'], 'no_progress')
        self.assertEqual(blocked[-1]['notice_errors'], [],
                         '替换后的通知本身也必须过同一道校验')

    def test_the_delivered_failure_text_passes_the_same_gate(self):
        response = self.run_turn(assert_question_provider('氨氯地平与克拉霉素存在相互作用'))
        self.assertEqual(check(response.text), [],
                         '失败通知本身必须能过安全校验，否则用户拿到的是被替换过的文本')

    def test_a_question_form_sub_question_is_delivered_normally(self):
        # The mirror image: the same path with an ASKING sub-question is a
        # successful bounded report, so the tests above are about the check
        # and not about the scenario being unreachable.
        response = self.run_turn(assert_question_provider('氨氯地平是否与克拉霉素存在相互作用？'))
        bundle = response.answer_bundle or {}
        self.assertEqual(bundle.get('answer_status'), 'bounded_report')
        self.assertIn('是否与克拉霉素存在相互作用', response.text)


class RunnerOutcomeShapeTests(unittest.TestCase):
    """A turn that raises must not change the SHAPE of what the runner records.

    The previous round's export crashed because the exception branch built its
    own smaller dict: the evaluator read `tool_trace`, the key was not there,
    and a real result — with its ledger, its wire files and its SQLite backup
    all intact — became unreadable.  The keys a consumer reads must not depend
    on whether the turn happened to raise.
    """

    def _task(self):
        from stage0.agent_evals import run_visitprep as runner
        return copy.deepcopy(json.loads(runner.DATA.read_text(encoding='utf-8'))[0])

    def _run(self, *, break_report=False, artifact_dir=None):
        from stage0.agent_evals import run_visitprep as runner
        from stage0.agent_evals.batch_budget import BatchAllowance
        from stage0 import investigation as investigation_module
        context = (patch.object(investigation_module.InvestigationState, 'report_text',
                                side_effect=RuntimeError('injected report failure'))
                   if break_report else _nullcontext())
        with context:
            return runner.run_task(self._task(), 'scripted', allowance=BatchAllowance(10),
                                   artifact_dir=artifact_dir)

    def test_an_injected_failure_is_still_readable_by_a_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(break_report=True, artifact_dir=Path(directory) / 'run')
        observed = result['observed']
        self.assertIn('injected report failure', observed['error'])
        self.assertIn('tool_trace', observed)
        self.assertIn('investigation', observed)
        self.assertIn('audit_trail', observed)

    def test_failure_and_success_outcomes_carry_the_same_keys(self):
        good = self._run()['observed']
        bad = self._run(break_report=True)['observed']
        self.assertEqual(set(good) - set(bad), set(),
                         '异常路径缺少正常路径的键；评测据此读取会直接抛 KeyError')

    def test_a_sent_request_survives_the_failure_in_the_persisted_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(break_report=True, artifact_dir=Path(directory) / 'run')
            observed = result['observed']
            self.assertGreater(observed['provider_attempts_sent'], 0,
                               '异常退出也必须留下真实发送过的尝试数')
            self.assertTrue(observed['request_ledger'], '账本不能因为异常而丢失')
            self.assertEqual(len(observed['request_ledger']), observed['provider_attempts_sent'])

    def test_the_partial_trace_of_the_failed_turn_is_recovered(self):
        result = self._run(break_report=True)['observed']
        self.assertTrue(result['tool_trace'],
                        '已执行的动作应当能从持久轨迹里取回，而不是记成"什么都没做"')


class ProductFaultInjectionTests(unittest.TestCase):
    """Fault injection over the normal persisted-task entry point.

    An execution failure must not cost the ledger, must not park the task in a
    state nothing will ever move it out of, and must not disable cancellation.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-fault-')
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / 'memory.db', llm_enabled=False)
        from stage0 import test_agent_open_tasks as support
        support.seed(self.store)
        from stage0 import test_agent_open_tasks as support
        self.agent_factory = lambda: support.make_agent(self.store)
        self.tasks = CareTasks(ProductStore(self.store), agent_factory=self.agent_factory)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _worker(self, agent=None):
        from stage0.server import OutboxWorker
        return OutboxWorker(self.store, runner_factory=lambda: _ExplodingRunner(agent))

    def _waiting_task(self):
        """Reach the point where the worker is asked to run the turn, then stop.

        The default ``resume`` drains the queue inline; the failure is injected
        on the SECOND execution — the one the worker drives — so the first,
        successful turn still seeds the ledger and the patient state.
        """
        task = self.tasks.create('create', 'evidence_review', goal='核查当前用药相互作用证据与适用条件')
        task = self.tasks.resume(task['id'], 'resume-1', task['revision'])
        self.assertEqual('waiting_input', task['status'])
        self.tasks.record_input(task['id'], 'input-1', task['revision'],
                                semantic={'renal_function': {'value': 55, 'unit': 'ml/min'}})
        return ProductStore(self.store).get(task['id'], 'care_task')

    def _drain_with_failure(self):
        """Claim exactly the named task through the normal worker entry point."""
        task = self._waiting_task()
        self._worker(None)._auto_resume_care_task(
            {'task_id': task['id'], 'expected_revision': task['revision']}, 'injected-1')
        return task

    def test_an_execution_failure_does_not_park_the_task_in_running(self):
        task = self._drain_with_failure()
        current = ProductStore(self.store).get(task['id'], 'care_task')
        self.assertNotEqual('running', current['status'], '执行失败后任务不能永远停在 running')
        self.assertIn(current['status'], {'failed', 'waiting_input', 'cancelled'})

    def test_the_failure_is_recorded_rather_than_silently_swallowed(self):
        task = self._drain_with_failure()
        current = ProductStore(self.store).get(task['id'], 'care_task')
        self.assertTrue(current.get('runs'), '任务必须留下自己被尝试过的运行记录')
        self.assertTrue(any(run.get('finished_at') for run in current['runs']),
                        '尝试过的运行必须有结束时间，否则看板上会一直显示"进行中"')

    def test_cancel_still_works_after_an_execution_failure(self):
        task = self._drain_with_failure()
        current = ProductStore(self.store).get(task['id'], 'care_task')
        cancelled = self.tasks.resume(task['id'], 'cancel-1', current['revision'], action='cancel')
        self.assertEqual('cancelled', cancelled['status'])


class _ExplodingRunner:
    """A runner that fails at execution — a tool crash, an unrecoverable store
    error, any of the failures that are not the agent's own decision."""

    def __init__(self, agent):
        self.agent = agent

    def run(self, **kwargs):
        raise RuntimeError('injected execution failure')

    def run_pending_rechecks(self):
        return []


if __name__ == '__main__':
    unittest.main()
