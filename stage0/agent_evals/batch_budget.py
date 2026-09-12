"""批次级调用额度：把剩余额度送进**真实请求入口**，而不是在任务之间估算。

单次任务的额度由 ``BudgetSession.call`` 在**每次发送前**取用（重试也各取一次），
所以一个任务不可能越过交给它的额度。

记账读**持久账本** ``llm_attempts``：该行由 ``reserve_llm_attempt`` 在 dispatch
**之前**写入，因此任务抛异常、甚至进程被杀，已发生的调用记录都还在。

本模块不维护可递减的余额——本仓的预算计数器只增不减（``merge_budget`` 对
``COUNTERS`` 取 ``max()``），余额一旦可减，就会被 checkpoint 恢复撤销。
"""
from __future__ import annotations


class BatchAllowance:
    """A batch's call allowance, charged from the durable ledger between tasks."""

    def __init__(self, cap: int):
        self.cap = max(0, int(cap))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.cap - self.spent)

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def charge(self, attempts) -> None:
        # 负数是退款形状的记账，而额度只增不减：忽略它，否则一次"退款"就能
        # 把已经用掉的额度买回来，上限随之失效。
        self.spent += max(0, int(attempts or 0))

    def to_dict(self) -> dict:
        return {'cap': self.cap, 'spent': self.spent, 'remaining': self.remaining()}


def attempts_sent(memory, run_id: str) -> int:
    """Dispatch attempts recorded for a run. Written before the call is made."""
    row = memory.connection.execute(
        'SELECT COUNT(*) FROM llm_attempts WHERE run_id=?', (run_id,)).fetchone()
    return int(row[0]) if row else 0
