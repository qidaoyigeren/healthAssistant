"""批次调用上限的边界：cap=1、多次重试、跨任务共享、异常退出仍留账。

上限针对的是**发送尝试**，不是计费调用：429 重试各自取额，所以"上限"是真的
上限，而不是一个可以在退避后翻倍的名义值。
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from stage0.agent_evals.batch_budget import BatchAllowance, attempts_sent
from stage0.memory import MemoryStore


class BatchAllowanceTest(unittest.TestCase):
    def test_cap_one_admits_exactly_one_charge(self):
        allowance = BatchAllowance(1)
        self.assertFalse(allowance.exhausted())
        allowance.charge(1)
        self.assertTrue(allowance.exhausted())
        self.assertEqual(allowance.remaining(), 0)

    def test_cross_task_charges_accumulate(self):
        allowance = BatchAllowance(5)
        allowance.charge(2)
        allowance.charge(2)
        self.assertEqual(allowance.remaining(), 1)
        allowance.charge(1)
        self.assertTrue(allowance.exhausted())

    def test_a_zero_cap_is_exhausted_before_anything_runs(self):
        self.assertTrue(BatchAllowance(0).exhausted())

    def test_remaining_never_goes_negative(self):
        allowance = BatchAllowance(1)
        allowance.charge(9)
        self.assertEqual(allowance.remaining(), 0)

    def test_a_negative_charge_cannot_buy_back_allowance(self):
        """退款式记账会让上限失效：额度必须只增不减。"""
        allowance = BatchAllowance(2)
        allowance.charge(2)
        allowance.charge(-5)
        self.assertTrue(allowance.exhausted())
        self.assertEqual(allowance.spent, 2)


class LedgerTest(unittest.TestCase):
    def test_the_ledger_counts_attempts_written_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                store.workflow_run_start(run_id='r1', thread_id='r1', event_id=None,
                                         idempotency_key=None, graph_version='legacy')
                budget = {'cycles_consumed': 0}
                store.reserve_llm_attempt('a1', 'r1', 'planner', 10, 1.0, budget)
                store.reserve_llm_attempt('a2', 'r1', 'planner', 10, 1.0, budget)
                self.assertEqual(attempts_sent(store, 'r1'), 2)
                self.assertEqual(attempts_sent(store, 'other'), 0)
            finally:
                store.close()

    def test_an_unsettled_attempt_still_counts(self):
        """异常退出时预留行仍在——这正是"已发生的调用不得丢失"的依据。"""
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / 'memory.db', llm_enabled=False)
            try:
                store.workflow_run_start(run_id='r1', thread_id='r1', event_id=None,
                                         idempotency_key=None, graph_version='legacy')
                store.reserve_llm_attempt('a1', 'r1', 'planner', 10, 1.0, {'cycles_consumed': 0})
                self.assertEqual(len(store.unsettled_llm_attempts('r1')), 1)
                self.assertEqual(attempts_sent(store, 'r1'), 1)
            finally:
                store.close()


class RunTaskWiringTest(unittest.TestCase):
    """额度必须进入**真实请求入口**，而不是在任务之间估算。"""

    def test_a_batch_constraint_disables_refusal_refunds(self):
        """退款允许尝试数达到 2×call_budget，"批次上限"就不再是发送上限。"""
        from stage0.agent_evals import run_visitprep
        env = run_visitprep._budget_env(7, BatchAllowance(cap=7))
        self.assertEqual(env['AGENT_TURN_CALL_BUDGET'], '7')
        self.assertEqual(env['PLANNER_PROVIDER_REFUND_REFUSALS'], '0')

    def test_without_a_batch_the_standalone_budget_is_unchanged(self):
        from stage0.agent_evals import run_visitprep
        env = run_visitprep._budget_env(32, None)
        self.assertEqual(env['AGENT_TURN_CALL_BUDGET'], '32')
        self.assertNotIn('PLANNER_PROVIDER_REFUND_REFUSALS', env)

    def test_the_outcome_carries_the_ledger_count_not_the_trace_count(self):
        """实际发送的尝试数来自**持久账本**，不是 planner trace。

        离线替身走的是真实请求入口（``session.call``），每次发送前都写一行
        ``llm_attempts``；而它的 trace 里 ``provider_attempts`` 是空的，从
        trace 累加会把这个回合记成"零次调用"。两者必须能分辨。
        """
        import json
        from stage0.agent_evals import run_visitprep

        task = json.loads(run_visitprep.DATA.read_text(encoding='utf-8'))[0]
        result = run_visitprep.run_task(task, 'scripted')
        observed = result['observed']
        self.assertGreaterEqual(observed['provider_attempts_sent'], 1)
        self.assertEqual(observed['planner_calls'], 0,
                         '该替身的 trace 不含 provider_attempts；两者相等说明口径又退回 trace')


if __name__ == '__main__':
    unittest.main()
