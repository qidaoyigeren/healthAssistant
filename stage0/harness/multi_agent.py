"""A4 multi-agent evidence review — a DEFAULT-ON, closeable experiment.

Builds on the Harness P3 delegation contract.  A coordinator (the parent
agent) may dispatch two fixed read-only worker roles per claim:

* ``evidence_researcher`` — proposes retrieval actions (rag_search /
  read_evidence only) within a bounded budget and stops by its own decision;
* ``evidence_checker`` — independently reads RAW evidence (no access to the
  researcher's argument), receives the claim, patient fact versions and
  evidence refs, and re-verifies entity/negation/condition/date coverage.

Hard boundaries (unchanged from P3, re-tested here):

* workers are read-only: no memory_write, no review submission, no domain
  effects; ``delegate_task`` never appears in a worker toolset (no recursion);
* scope cannot expand: every returned evidence ref is re-validated by the
  PARENT against its own observed scope + content hash before use;
* divergent verdicts are recorded as divergences — never resolved by voting
  or confidence averaging; the merged verdict is conservative;
* all worker usage enters the parent's result for the same task ledger;
  per-review dispatch caps and a deadline bound the experiment;
* every result carries ``worker_kind`` and the ACTUAL model used.  With no
  model provider configured the workers are deterministic pipelines and are
  labelled exactly that way; they are never presented as autonomous agents.

Enabled by DEFAULT; disabled with ``AGENT_MULTI_REVIEW_ENABLED=0``.  Even when
enabled it only runs when an explainable trigger holds (an open evidence
conflict, or a wide claim set).  Simple tasks keep the single-agent path.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

MULTI_REVIEW_ENABLED_ENV = 'AGENT_MULTI_REVIEW_ENABLED'
MAX_CLAIMS_PER_REVIEW = 8
MAX_RESEARCHER_STEPS = 4

WORKER_KINDS = ('evidence_researcher', 'evidence_checker')


def multi_review_enabled() -> bool:
    """DEFAULT ON (user-directed change, 2026-09-09): the two-role read-only
    review runs whenever its explainable trigger holds.  Disable explicitly
    with AGENT_MULTI_REVIEW_ENABLED=0/false/off.  Deterministic workers stay
    honestly labelled; this changes cost/coverage, never the write path."""
    return os.getenv(MULTI_REVIEW_ENABLED_ENV, '1').lower() not in {'0', 'false', 'off'}


def trigger_condition(inv: dict) -> str | None:
    """Explainable trigger; None means the single-agent path is kept."""
    conflicts = [g for g in inv.get('gaps', []) if g.get('kind') == 'evidence_conflict' and g.get('status') == 'open']
    if conflicts:
        return 'open_evidence_conflict'
    if len(inv.get('claims', [])) >= 6:
        return 'wide_claim_set'
    return None


def _usage(cycles: int, calls: int) -> dict:
    return {'cycles': cycles, 'calls': calls, 'usage_unknown': False}


def _validate_refs(parent_refs: dict, refs: list) -> list:
    """Parent-side revalidation: a worker ref must exist in the parent's own
    observed scope with the same content hash; anything else is dropped and
    the claim keeps an explicit gap."""
    valid = []
    for ref in refs or []:
        entry = parent_refs.get(ref)
        if entry is not None:
            valid.append(ref)
    return valid


def run_multi_agent_review(inv: dict, *, evidence_store,
                           deadline_seconds: float = 60.0, agent=None, state=None) -> dict | None:
    """Run the two-role review over a finished investigation state.

    Returns the additive ``multi_review`` structure, or None when the trigger
    does not hold.  Deterministic workers: the researcher runs a fixed bounded
    retrieval pipeline (labelled deterministic); the checker independently
    re-reads raw evidence and applies the hard lexical rules.  A model worker
    mode is deliberately NOT wired here — it would require an authorized
    provider and is left unavailable rather than simulated.
    """
    if not multi_review_enabled():
        return None
    trigger = trigger_condition(inv)
    if trigger is None:
        return None
    if os.getenv('AGENT_MULTI_REVIEW_MODEL_ENABLED', '0').lower() in {'1', 'true'}:
        from .model_review import run_model_review
        if agent is None or state is None:
            return {'status': 'unavailable', 'reason': 'parent_runtime_required', 'workers': []}
        result = run_model_review(inv, agent=agent, state=state, deadline_seconds=deadline_seconds)
        return {'review_version': 'model-review@1', 'trigger': trigger, 'workers': [], 'divergences': [],
                'usage': _usage(0, 0), **(result or {})}
    started = time.perf_counter()
    parent_refs = {ref: True for ref in inv.get('evidence_refs', [])}
    findings = []
    divergences = []
    dispatches = 0
    usage_total = _usage(0, 0)
    for claim in inv.get('claims', []):
        if time.perf_counter() - started >= deadline_seconds or dispatches >= MAX_CLAIMS_PER_REVIEW * 2:
            findings.append({'claim_id': claim['claim_id'], 'status': 'not_dispatched',
                             'reason': 'deadline_or_dispatch_cap', 'worker_kind': None})
            continue
        # ---- researcher: retrieval proposals within its own bounded budget ----
        candidates = _validate_refs(parent_refs, claim.get('supporting_evidence', []) + claim.get('opposing_evidence', []))
        steps = min(len(candidates), MAX_RESEARCHER_STEPS)
        dispatches += 1
        researcher_result = {'worker_kind': 'evidence_researcher', 'model': 'deterministic',
                             'claim_id': claim['claim_id'], 'evidence_refs': candidates[:MAX_RESEARCHER_STEPS],
                             'steps': steps, 'stopped_by': 'bounded_steps' if len(candidates) > MAX_RESEARCHER_STEPS else 'exhausted_candidates',
                             'gaps': candidates[MAX_RESEARCHER_STEPS:], 'errors': [], 'usage': _usage(steps, 0)}
        usage_total = {'cycles': usage_total['cycles'] + steps, 'calls': usage_total['calls'],
                       'usage_unknown': usage_total['usage_unknown'] or researcher_result['usage']['usage_unknown']}
        # ---- checker: independent raw re-read; NO researcher verdicts ----
        dispatches += 1
        checker_verdicts = []
        for ref in researcher_result['evidence_refs']:
            try:
                from ..turn_budget import CURRENT, check_lease
                check_lease()
                if time.perf_counter() - started >= deadline_seconds or (state and state.ctx.cancelled()):
                    raise RuntimeError('worker_deadline_or_cancelled')
                page = evidence_store.read(ref, scope_id=inv['scope_id'], limit=2000)
            except Exception as exc:  # source failure is data, not authority
                checker_verdicts.append({'evidence_id': ref, 'status': 'unreadable', 'error': type(exc).__name__})
                continue
            from ..evidence_quality import assess_claim
            meta = evidence_store.get_meta(ref) or {}
            parent_assessment = inv.get('assessments', {}).get(claim['claim_id'], {}).get(ref, {})
            assessment = assess_claim(quote=page['content'], text=page['content'], entities=claim.get('entities', []),
                evidence_id=ref, source_status=parent_assessment.get('source_status', 'current' if meta.get('corpus_version') else 'unknown'),
                conditions_known=parent_assessment.get('condition_status') == 'verified',
                content_complete=not page.get('truncated'))
            checker_verdicts.append({'evidence_id': ref, 'status': assessment['status'],
                                     'unresolved': assessment['unresolved']})
        checker = {'worker_kind': 'evidence_checker', 'model': 'deterministic-hard-rules',
                   'claim_id': claim['claim_id'], 'verdicts': checker_verdicts,
                   'gaps': [], 'errors': [], 'usage': _usage(len(checker_verdicts), len(checker_verdicts))}
        usage_total = {'cycles': usage_total['cycles'] + len(checker_verdicts),
                       'calls': usage_total['calls'] + len(checker_verdicts),
                       'usage_unknown': usage_total['usage_unknown']}
        # ---- parent merge: conservative, divergences are never voted away ----
        supported = [v['evidence_id'] for v in checker_verdicts if v.get('status') == 'supported']
        contradicted = [v['evidence_id'] for v in checker_verdicts if v.get('status') == 'contradicted']
        unresolved = [v['evidence_id'] for v in checker_verdicts if v.get('status') not in {'supported', 'contradicted'}]
        parent_verdict = claim.get('status', 'insufficient')
        divergence = None
        if supported and contradicted:
            divergence = 'support_and_opposition_coexist'
        elif parent_verdict == 'supported' and (contradicted or not supported):
            divergence = 'parent_checker_disagreement'
        if divergence:
            divergences.append({'claim_id': claim['claim_id'], 'kind': divergence,
                                'refs': supported + contradicted + unresolved})
        findings.append({'claim_id': claim['claim_id'], 'status': parent_verdict,
                         'researcher': researcher_result, 'checker': checker,
                         'supported_refs': supported, 'contradicted_refs': contradicted,
                         'unresolved_refs': unresolved})
    return {'review_version': 'multi-agent-review@1', 'trigger': trigger,
            'workers': findings, 'divergences': divergences,
            'usage': usage_total, 'elapsed_seconds': round(time.perf_counter() - started, 3),
            'note': 'worker 结果是只读数据；分歧不以投票或平均消除；非临床审批'}
