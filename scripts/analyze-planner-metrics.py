"""Compute the frozen acceptance-protocol metrics from live/replay run files.

Usage:
  python scripts/analyze-planner-metrics.py docs/.../live-final-1.json ... [--out report.json]

Emits per-run and cohort aggregates exactly as defined in
docs/agent-capability-upgrade/closeout-2026-09-10/planner-reliability/acceptance-protocol.md:
proposal acceptance, gap advancement, provider availability, rejection
categories (including correctable-by-v2 validator false rejections), token
accounting and latencies (per-run values + median; never a fabricated P95).
Offline: reads artifacts only, makes no calls.
"""
import argparse
import glob
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

CORRECTABLE_TOOLS = {'ddi_check', 'memory_read'}


def correctable_rejection(errors, proposal=None):
    """A v2-correctable rejection: every schema error is a pure missing-key
    omission on an argument the code owns authoritatively."""
    schema = [e for e in errors if e.get('code') == 'missing_required_arguments']
    if not schema or len(schema) != len(errors):
        return False
    proposal = proposal or {}
    owned = {'medications'} if proposal.get('tool') == 'ddi_check' else (
        {'query'} if proposal.get('tool') == 'memory_read' and proposal.get('gap_id') == 'authority' else set())
    return all(e.get('message', '').endswith(' is required')
               and e.get('message', '').split('.')[-1].removesuffix(' is required') in owned
               for e in schema)


def analyze_run(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    task = raw['tasks'][0]
    observed = task['observed']
    trace = [e for r in observed.get('responses', []) for e in r.get('tool_trace', [])]
    plans = [e for e in trace if e.get('phase') == 'plan']
    meta = []
    for e in plans:
        pl = e.get('planner') or {}
        if not pl or pl.get('fallback_kind') == 'circuit_break' and not pl.get('proposal'):
            continue
        if not pl.get('model'):
            continue
        meta.append(pl)
    # De-duplicate a final deterministic response that repeats cached meta.
    deduped = []
    for pl in meta:
        if not deduped or pl != deduped[-1]:
            deduped.append(pl)
    meta = deduped
    provider_errors = sum(1 for pl in meta if pl.get('fallback_kind') == 'emergency'
                          and any(e.get('category') == 'provider_error' for e in (pl.get('validation') or {}).get('errors', [])))
    rejected = [pl for pl in meta if (pl.get('validation') or {}).get('status') == 'safety_rejected'
                and pl.get('proposal') is not None]
    accepted = [pl for pl in meta if pl.get('source') == 'llm']
    safety_rej = sum(1 for pl in rejected for e in (pl.get('validation') or {}).get('errors', [])
                     if e.get('category') == 'safety')
    param_rej = sum(1 for pl in rejected for e in (pl.get('validation') or {}).get('errors', [])
                    if e.get('code') == 'missing_required_arguments')
    false_rej = sum(1 for pl in rejected if correctable_rejection((pl.get('validation') or {}).get('errors', []), pl.get('proposal')))
    # Gap advancement: an accepted proposal whose following observation was ok
    # and either carried new evidence refs or advanced contract state.
    advanced = 0
    advancement_measured = any(e.get('phase') == 'investigation_progress' for e in trace)
    substantive = 0
    for pl in accepted:
        index = next((i for i, e in enumerate(trace)
                      if e.get('phase') == 'plan' and (e.get('planner') or {}) == pl), None)
        if index is None:
            continue
        end = next((j for j in range(index + 1, len(trace)) if trace[j].get('phase') == 'plan'), len(trace))
        following = trace[index + 1:end]
        executed = [e for e in following if e.get('phase') == 'act']
        if any(e.get('tool') in {'rag_search', 'read_evidence', 'ask_clarification', 'ddi_check'} for e in executed):
            substantive += 1
        changes = [e for e in following if e.get('phase') == 'investigation_progress']
        if executed and any(e.get('new_evidence_refs') or e.get('new_read_refs') or e.get('gap_changes') for e in changes):
            advanced += 1
    # Protocol v3 evidence: a proposal the model produced correctly vs one that
    # only ran because code supplied a missing authoritative argument.  The
    # latter must never be counted as autonomous planning.
    autocorrected = sum(1 for pl in accepted if pl.get('argument_corrections'))
    parse_failures = sum(1 for pl in meta
                         for e in (pl.get('validation') or {}).get('errors', [])
                         if e.get('category') == 'parse' or e.get('code') in
                         {'empty_response', 'malformed_json', 'response_shape_error'})
    progress = [e for e in trace if e.get('phase') == 'investigation_progress']
    new_evidence = sum(len(e.get('new_evidence_refs') or []) for e in progress)
    new_reads = sum(len(e.get('new_read_refs') or []) for e in progress)
    gap_changes = sum(len(e.get('gap_changes') or []) for e in progress)
    provider_attempts = [a for pl in meta for a in pl.get('provider_attempts', [])]
    ledger = observed.get('usage_ledger', [])
    planner_latencies = [pl.get('latency_ms') for pl in meta if isinstance(pl.get('latency_ms'), (int, float))]
    # Fallback detection must not depend on the (once-buggy) report fields:
    # any emergency/circuit-break planner fallback in the trace counts, so a
    # pre-fix artifact cannot pass as fallback-free.
    fallback_in_trace = any(
        (e.get('planner') or {}).get('fallback_kind') in {'emergency', 'circuit_break'} for e in plans)
    reported_degraded = bool(any(observed.get('degradation_reasons') or [])
                             or [(r.get('answer_bundle') or {}).get('coverage', {}).get('degraded_reason') for r in observed.get('responses', [])
                                 if (r.get('answer_bundle') or {}).get('coverage', {}).get('degraded_reason')]
                             or [r.get('audit_trail', {}).get('response_fallback_reason') for r in observed.get('responses', [])
                                 if r.get('audit_trail', {}).get('response_fallback_reason')])
    return {
        'file': str(path),
        'task_id': task['task_id'],
        'rubric_passed': task['score']['passed'],
        'score_failures': task['score']['failures'],
        'goal_status': observed.get('goal_status'),
        'execution_status': observed.get('execution_status'),
        'fallback_free': not (fallback_in_trace or reported_degraded),
        'autonomous_success': bool(
            substantive and task['score']['passed']
            and observed.get('goal_status') == 'completed'
            and not fallback_in_trace and not reported_degraded),
        'attempts': len(ledger) or raw.get('provider_availability', {}).get('attempts', 0),
        'actual_responses': sum(1 for a in ledger if a.get('status') in {'actual', 'estimate', 'late'}),
        'usage_unknown': sum(1 for a in ledger if a.get('usage_tokens') is None),
        'known_tokens': sum(a.get('usage_tokens') or 0 for a in ledger),
        'provider_errors': provider_errors,
        'model_proposals_with_response': len(rejected) + len(accepted),
        'accepted': len(accepted),
        'rejected': len(rejected),
        'accepted_ratio': round(len(accepted) / (len(rejected) + len(accepted)), 4) if (rejected or accepted) else None,
        'substantive_model_actions': substantive,
        'advanced_ratio': round(advanced / len(accepted), 4) if accepted and advancement_measured else None,
        'advancement_measurement': 'state_diff' if advancement_measured else 'unavailable_in_historical_trace',
        'protocol_version': raw.get('protocol_version'),
        'autocorrected_proposals': autocorrected,
        'parse_failures': parse_failures,
        'new_evidence_refs': new_evidence,
        'new_read_refs': new_reads,
        'gap_changes': gap_changes,
        'provider_attempt_outcomes': provider_attempts,
        'tool_choice_degraded_attempts': sum(a.get('outcome') == 'tool_choice_degraded'
                                             for a in provider_attempts),
        'rate_limit_attempts': sum(a.get('outcome') == 'rate_limit' for pl in meta for a in pl.get('provider_attempts', [])) if any('provider_attempts' in pl for pl in meta) else None,
        'timeout_attempts': sum(a.get('outcome') == 'timeout' for pl in meta for a in pl.get('provider_attempts', [])) if any('provider_attempts' in pl for pl in meta) else None,
        'first_backend_progress_ms': observed.get('first_backend_progress_ms'),
        'first_browser_visible_feedback_ms': None,
        'safety_rejections': safety_rej,
        'parameter_rejections': param_rej,
        'validator_false_rejections_v2_correctable': false_rej,
        'planner_latency_ms_each': planner_latencies,
        'planner_latency_ms_median': round(statistics.median(planner_latencies), 1) if planner_latencies else None,
        'wall_ms': observed.get('latency_ms'),
        'response_source': [r.get('audit_trail', {}).get('response_source') for r in observed.get('responses', [])],
        'degradation_reasons': observed.get('degradation_reasons'),
        'duplicate_effects': observed.get('duplicate_effects', 0),
        'false_completion': task['score'].get('false_completion'),
    }


def cohort(runs):
    total_props = sum(r['model_proposals_with_response'] for r in runs)
    accepted = sum(r['accepted'] for r in runs)
    lat = [v for r in runs for v in r['planner_latency_ms_each']]
    return {
        'runs': len(runs),
        'rubric_passed': sum(r['rubric_passed'] for r in runs),
        'fallback_free': sum(r['fallback_free'] for r in runs),
        'autonomous_success': sum(r['autonomous_success'] for r in runs),
        'attempts': sum(r['attempts'] for r in runs),
        'actual_responses': sum(r['actual_responses'] for r in runs),
        'usage_unknown': sum(r['usage_unknown'] for r in runs),
        'known_tokens': sum(r['known_tokens'] for r in runs),
        'provider_actual_response_ratio': round(sum(r['actual_responses'] for r in runs) / sum(r['attempts'] for r in runs), 4) if sum(r['attempts'] for r in runs) else None,
        'model_proposals_with_response': total_props,
        'accepted': accepted,
        'accepted_ratio': round(accepted / total_props, 4) if total_props else None,
        'validator_false_rejections_v2_correctable': sum(r['validator_false_rejections_v2_correctable'] for r in runs),
        'safety_rejections': sum(r['safety_rejections'] for r in runs),
        'parameter_rejections': sum(r['parameter_rejections'] for r in runs),
        'parse_failures': sum(r['parse_failures'] for r in runs),
        'autocorrected_proposals': sum(r['autocorrected_proposals'] for r in runs),
        'new_evidence_refs': sum(r['new_evidence_refs'] for r in runs),
        'new_read_refs': sum(r['new_read_refs'] for r in runs),
        'gap_changes': sum(r['gap_changes'] for r in runs),
        'tool_choice_degraded_attempts': sum(r['tool_choice_degraded_attempts'] for r in runs),
        'rate_limit_attempts': sum(r['rate_limit_attempts'] or 0 for r in runs),
        'timeout_attempts': sum(r['timeout_attempts'] or 0 for r in runs),
        'wall_ms_each': [r['wall_ms'] for r in runs],
        'wall_ms_median': round(statistics.median([r['wall_ms'] for r in runs if r['wall_ms'] is not None]), 1) if any(r['wall_ms'] is not None for r in runs) else None,
        'planner_latency_ms_median': round(statistics.median(lat), 1) if lat else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+')
    parser.add_argument('--out')
    args = parser.parse_args()
    files = [p for pattern in args.files for p in sorted(glob.glob(pattern))]
    if not files or len(files) != len(set(files)):
        parser.error('Input artifacts missing or duplicated')
    if args.out and Path(args.out).exists():
        parser.error('Preserve existing metrics; choose a new output file')
    runs = [analyze_run(f) for f in files]
    report = {'runs': runs, 'cohort': cohort(runs)}
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
