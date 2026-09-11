"""Shared active-run accounting. Estimates are admission units, not billing guarantees.

I/O attempts are reserved durably before dispatch. Unknown remote outcomes keep
their reservation and stop further calls on this run. Sync I/O cannot be killed;
timeout is forwarded to the transport and late results are discarded.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import inspect
import json
import math
import os
import sys
import time
import uuid

# Some existing Stage 0 detector imports are script-style. Both names MUST
# share the same ContextVars, otherwise tool-internal I/O bypasses the run.
sys.modules.setdefault('stage0.turn_budget', sys.modules[__name__])
sys.modules.setdefault('turn_budget', sys.modules[__name__])


class BudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('budget_exhausted:' + reason)


@dataclass(frozen=True)
class TurnBudget:
    max_cycles: int = 16
    wall_clock_seconds: float = 120.0
    token_budget: int = 150_000
    call_budget: int = 32

    @classmethod
    def from_env(cls, max_cycles):
        def number(name, default, cast):
            try:
                value = cast(os.getenv(name, str(default)))
                return max(0, value) if math.isfinite(value) else default
            except ValueError:
                return default
        return cls(max(0, max_cycles), number('AGENT_TURN_BUDGET_SECONDS', 120., float),
                   number('AGENT_TURN_TOKEN_BUDGET', 150_000, int),
                   number('AGENT_TURN_CALL_BUDGET', 32, int))


LIMITS = ('max_cycles', 'wall_clock_seconds', 'token_budget', 'call_budget')
# ``refused_calls`` counts attempts a provider definitively refused BEFORE
# executing (see ``call(refund_if=...)``).  It only ever advances, so the
# monotonic ``merge_budget`` contract is preserved across checkpoints and
# restarts; the refund is expressed as an offset in ``exhausted()`` rather
# than by decrementing ``calls_attempted``.
COUNTERS = ('consumed_seconds', 'tokens_estimated', 'tokens_actual', 'tokens_charged',
            'calls_attempted', 'refused_calls', 'cycles_consumed')
CURRENT = ContextVar('turn_budget', default=None)
LEASE = ContextVar('turn_lease', default=None)


def merge_budget(stored, candidate):
    """Run owns immutable limits; checkpoints can only advance counters."""
    merged = dict(stored or {})
    for key, value in (candidate or {}).items():
        if value is None:
            continue
        if key in LIMITS and merged.get(key) is not None:
            continue
        if key == 'usage_unknown':
            merged[key] = bool(merged.get(key)) or bool(value)
            continue
        merged[key] = max(merged.get(key) or 0, value) if key in COUNTERS else value
    return merged


def initial_budget(max_cycles=16, saved=None):
    return merge_budget(saved, {**asdict(TurnBudget.from_env(max_cycles)),
                               **{k: 0 for k in COUNTERS}, 'accounting_version': 2})


def check_lease():
    guard = LEASE.get()
    if guard:
        guard()


@contextmanager
def lease_scope(guard):
    token = LEASE.set(guard)
    try:
        check_lease()
        yield
    finally:
        LEASE.reset(token)


class BudgetSession:
    def __init__(self, memory, run_id, max_cycles=16, saved=None):
        self.memory, self.run_id = memory, run_id
        run = memory.workflow_run_get(run_id) or {}
        if not run:
            raise RuntimeError('budget requires a persisted workflow run')
        stored = run.get('budget', {})
        self.data = initial_budget(max_cycles, merge_budget(stored, saved))
        # Pre-Harness active runs have no complete call ledger. Fail closed for
        # further model calls instead of pretending their historical usage is 0.
        self.unknown = (bool(stored and stored.get('accounting_version') != 2)
                        or bool(saved and saved.get('accounting_version') != 2)
                        or run.get('state_schema_version') != '2'
                        or bool(self.data.get('usage_unknown')))
        self.unknown |= bool(memory.unsettled_llm_attempts(run_id))
        self.data['usage_unknown'] = self.unknown
        self.started = time.perf_counter()
        self.base_seconds = self.data['consumed_seconds']
        self.reason = 'usage_unknown' if self.unknown else None

    def sync(self):
        self.data['consumed_seconds'] = max(self.data['consumed_seconds'],
            self.base_seconds + time.perf_counter() - self.started)
        self.memory.workflow_run_update(self.run_id, budget=self.data)
        return dict(self.data)

    def exhausted(self, *, cycle=None):
        self.sync()
        if self.reason:
            return self.reason
        # Equal means exhausted, including explicit zero on the first cycle.
        if self.data['consumed_seconds'] >= self.data['wall_clock_seconds']:
            return 'wall_clock'
        if self.data['tokens_charged'] >= self.data['token_budget']:
            return 'tokens'
        # Definitive refusals ran no model, so they do not consume the model
        # call budget.  ``refused_calls`` is capped at one call budget when it
        # is granted (see ``call``), which keeps the budget a hard ceiling:
        # total attempts never exceed twice it, however saturated the provider
        # is.  The min() only defends against pre-existing stored values.
        refunded = min(self.data.get('refused_calls', 0), self.data['call_budget'])
        if self.data['calls_attempted'] - refunded >= self.data['call_budget']:
            return 'calls'
        if cycle is not None and max(cycle, self.data['cycles_consumed']) >= self.data['max_cycles']:
            return 'cycles'
        return None

    def gate(self, state, *, check_cycles=True):
        reason = self.exhausted(cycle=state.cycle if check_cycles else None)
        if reason:
            self.reason = reason
            state.degraded_reason = 'budget_exhausted:' + reason
            if not any(t.get('phase') == 'budget' and t.get('exhausted') == reason for t in state.trace):
                state.trace.append({'phase': 'budget', 'cycle': state.cycle, 'exhausted': reason,
                    'tokens_estimated': self.data['tokens_estimated'],
                    'elapsed_seconds': self.data['consumed_seconds'],
                    'limits': {k: self.data[k] for k in LIMITS}})
        return reason

    def cycle(self, state):
        state.cycle = max(state.cycle, self.data['cycles_consumed']) + 1
        self.data['cycles_consumed'] = state.cycle
        self.sync()

    def call(self, kind, callback, payload, timeout=60., output_limit=4096, *,
             token_metered=True, refund_if=None):
        reason = self.exhausted()
        if reason:
            self.reason = reason
            raise BudgetExceeded(reason)
        check_lease()
        remaining = self.data['wall_clock_seconds'] - self.data['consumed_seconds']
        planning_phase = kind not in {'composer', 'response_composer', 'verifier', 'response_verifier'}
        if planning_phase:
            remaining -= self.data.get('wrap_up_seconds_reserved', 0)
        # Reserve a bounded local safety/template margin, never for extra LLMs.
        margin = min(0.05, self.data['wall_clock_seconds'] * .05)
        allowed = min(timeout, remaining - margin)
        if allowed <= 0:
            self.reason = 'wall_clock'
            raise BudgetExceeded(self.reason)
        tokens_left = self.data['token_budget'] - self.data['tokens_charged']
        cap = min(output_limit, tokens_left)
        estimate = max(1, math.ceil(len(json.dumps(payload, ensure_ascii=False, default=str)) / 1.5)) if token_metered else 0
        reserved_tokens = self.data.get('wrap_up_tokens_reserved', 0) if planning_phase else 0
        if reserved_tokens and token_metered:
            cap = min(cap, max(0, tokens_left - estimate - reserved_tokens))
            if cap <= 0:
                self.reason = 'tokens_reserved_for_wrapup'
                raise BudgetExceeded(self.reason)
        attempt_id = uuid.uuid4().hex
        self.data['calls_attempted'] += 1
        self.sync()
        self.memory.reserve_llm_attempt(attempt_id, self.run_id, kind,
            min(tokens_left, estimate + cap), allowed, self.data)
        started = time.perf_counter()
        allowed = min(allowed, self.data['wall_clock_seconds'] - self.base_seconds
                      - (started - self.started) - margin)
        if allowed <= 0:
            self._settle(attempt_id, 0, 0, 'not_dispatched')
            self.reason = 'wall_clock'
            raise BudgetExceeded(self.reason)
        try:
            result = callback(allowed, cap)
        except Exception as exc:
            # Transport uncertainty is retained; no automatic remote retry.
            uncertain = isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
                'APITimeoutError', 'APIConnectionError'}
            # A *definitive* refusal (the caller supplies the predicate) was
            # rejected before running, so it is not charged against the call
            # budget.  The ledger row is still written: rate-limit history is
            # evidence and must not be lost.
            refused = (not uncertain) and refund_if is not None and bool(refund_if(exc))
            # ``refused_calls`` counts REFUNDS GRANTED, not refusals seen (the
            # ledger row below records every refusal).  Capping it at one call
            # budget keeps the budget a hard ceiling, so a provider that always
            # refuses still exhausts the turn instead of retrying forever.
            refunded = refused and self.data.get('refused_calls', 0) < self.data['call_budget']
            if refunded:
                self.data['refused_calls'] = self.data.get('refused_calls', 0) + 1
            self._settle(attempt_id, estimate, None,
                         'refused' if refused else ('unknown' if uncertain else 'failed_estimate'),
                         refund=refunded)
            if uncertain:
                self.reason = 'usage_unknown'
                self.data['usage_unknown'] = True
            raise
        # BaseException/process death deliberately leaves the durable reservation.
        usage = getattr(result, 'usage', None)
        actual = usage.get('total_tokens') if isinstance(usage, dict) else getattr(usage, 'total_tokens', None)
        if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
            actual = None
        estimated = estimate + max(1, math.ceil(len(str(getattr(result, 'choices', result))) / 1.5)) if token_metered else 0
        if not token_metered:
            actual = 0  # HTTP lookup, not an unknown LLM usage total.
        late = time.perf_counter() - started >= allowed
        self._settle(attempt_id, estimated, actual, 'late' if late else ('actual' if actual is not None else 'estimate'))
        check_lease()
        if late:
            self.reason = 'wall_clock'
            raise BudgetExceeded(self.reason)
        return result

    def _settle(self, attempt_id, estimated, actual, status, *, refund=False):
        # The estimate is always OBSERVED (it describes what we tried to send);
        # a refunded refusal simply does not CHARGE it.  Nothing is ever
        # decremented, so the durable counters stay monotonic across
        # checkpoints and restarts.
        self.data['tokens_estimated'] += estimated
        if actual is not None:
            self.data['tokens_actual'] += actual
        if not refund:
            self.data['tokens_charged'] += actual if actual is not None else estimated
            quality = 'actual' if actual is not None else 'estimate'
            previous = self.data.get('usage_quality')
            self.data['usage_quality'] = quality if previous in (None, quality) else 'mixed'
        self.sync()
        self.memory.settle_llm_attempt(attempt_id, status, actual, estimated, self.data)


@contextmanager
def budget_scope(memory, run_id, max_cycles=16, saved=None):
    active = CURRENT.get()
    if active is not None:
        if active.run_id != run_id:
            raise RuntimeError('nested budget run mismatch')
        yield active
        return
    session = BudgetSession(memory, run_id, max_cycles, saved)
    token = CURRENT.set(session)
    try:
        session.sync()
        yield session
    finally:
        try:
            session.sync()
        finally:
            CURRENT.reset(token)


def provider_call(kind, provider, payload, timeout=60.):
    session = CURRENT.get()
    if session is None:
        return provider(payload)
    def invoke(allowed, cap):
        params = inspect.signature(provider).parameters
        kwargs = {'timeout': allowed} if 'timeout' in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()) else {}
        return provider(payload, **kwargs)
    return session.call(kind, invoke, payload, timeout)


def completion_call(kind, client, **kwargs):
    # ``refund_if`` is our own policy knob, never a provider parameter.
    refund_if = kwargs.pop('refund_if', None)
    session = CURRENT.get()
    if session is None:
        return client.chat.completions.create(**kwargs)
    def invoke(allowed, cap):
        configured = client.with_options(max_retries=0) if hasattr(client, 'with_options') else client
        options = dict(kwargs, timeout=allowed)
        key = 'max_completion_tokens' if 'max_completion_tokens' in options else 'max_tokens'
        options[key] = min(options.get(key, cap), cap)
        return configured.chat.completions.create(**options)
    configured_timeout = getattr(client, 'timeout', 60.)
    configured_timeout = getattr(configured_timeout, 'read', configured_timeout)
    timeout = kwargs.pop('timeout', configured_timeout)
    timeout = 60. if timeout is None else float(timeout)
    return session.call(kind, invoke, kwargs, timeout,
                        kwargs.get('max_completion_tokens', kwargs.get('max_tokens', 4096)),
                        refund_if=refund_if)


def network_call(kind, callback, *, timeout=30.):
    session = CURRENT.get()
    if session is None:
        return callback(timeout)
    return session.call(kind, lambda allowed, cap: callback(allowed), {}, timeout,
                        output_limit=0, token_metered=False)
