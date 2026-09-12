"""Recompute the planning-latency baseline from artifacts already on disk.

One command, offline, read-only.  It exists because "the planner takes ~30s"
was, until now, a number living in prose: no artifact said which call was slow,
how much of a turn was remote, or what a millisecond of latency bought.

WHAT IT DEFINES (the metric other phases must reuse -- do not build a second one)

  unit: one model call
    prompt_tokens / completion_tokens   as the provider reported them, or
                                        ``unavailable``.  Never estimated: a
                                        total is not a split, and back-filling
                                        one would manufacture the very finding
                                        this file is meant to test.
    wall_ms                             wall clock as the caller sees it
    outcome                             response | rate_limit | timeout | ...

  unit: one turn (one run artifact)
    calls            number of model calls
    turn_wall_ms     whole turn
    first_progress_ms  first backend progress event -- the earliest thing a
                     user could see.  This is NOT the turn wall and the two are
                     never averaged together.
    remote_ms        sum of per-call wall -- what fraction of the turn was
                     waiting on somebody else's GPU

  derived
    ms_per_output_token = wall_ms / completion_tokens, over the SCORABLE calls
    scorable            = outcome == 'response' AND completion_tokens > 0.
                          Everything else stays in the counts as latency
                          evidence but is excluded from the ratio, and the
                          exclusion is reported, not silently dropped.

  Per-call planner latency is NOT recomputed here: it is imported verbatim from
  ``scripts/analyze-planner-metrics.py`` so this baseline and the frozen
  acceptance metrics can never drift apart.

Usage:
  python scripts/latency-baseline.py --out docs/agent-capability-upgrade/latency-2026-09-11/L0/baseline.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import importlib.util
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOCS = ROOT / 'docs' / 'agent-capability-upgrade'

# The three cohorts that exist as of 2026-09-11.  Each label is the batch, and
# the glob must resolve to run artifacts only.
COHORTS = (
    ('planner-closeout-2026-09-11', 'planner-closeout-2026-09-11/live/live-final-*.json'),
    ('planner-closeout-v3-2026-09-11', 'planner-closeout-v3-2026-09-11/live/live-final-*.json'),
    ('provider-switch-2026-09-11', 'provider-switch-2026-09-11/live/live-final-*.json'),
)
PROBE = 'latency-2026-09-11/latency.json'

# Numbers quoted in docs/latency-optimization-prompts-2026-09-11.md, verbatim.
# They are re-derived below rather than trusted; a mismatch is a finding.
DOC_CLAIMS = (
    ('zhipu', 'real_payload', 4224, 49, 1.5),
    ('tokendance', 'tiny_prompt', 437, 1451, 40.4),
    ('tokendance', 'real_payload', 4219, 1162, 31.6),
    ('tokendance', 'real_payload_encoding_default', 4219, 2440, 55.9),
)


def load_acceptance_analyzer():
    """Import the frozen analyzer so ``planner_latency_ms_each`` has one owner.

    The filename is hyphenated, so a plain import cannot reach it.  Reusing the
    function (rather than re-deriving planner latency here) is what makes this
    baseline comparable with the acceptance metrics instead of merely similar.
    """
    path = ROOT / 'scripts' / 'analyze-planner-metrics.py'
    spec = importlib.util.spec_from_file_location('acceptance_metrics', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ratio(wall_ms, completion_tokens):
    """ms of wall clock per output token, or ``unavailable``.

    The denominator is the point of the whole file: a turn that got slower
    because the model wrote 5x more does not have a latency bug, it has an
    output-discipline bug, and only this ratio tells them apart.
    """
    if wall_ms is None or not completion_tokens:
        return None
    return round(wall_ms / completion_tokens, 2)


def ols(points):
    """Least-squares slope/intercept of y on x; ``None`` when degenerate."""
    n = len(points)
    if n < 3:
        return None
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    if sxx == 0:
        return None
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    slope = sxy / sxx
    sst = sum((p[1] - mean_y) ** 2 for p in points)
    r2 = (sxy ** 2) / (sxx * sst) if sst else None
    return {'slope': round(slope, 3), 'intercept': round(mean_y - slope * mean_x, 1),
            'r_squared': None if r2 is None else round(r2, 4), 'n': n}


def inference_section(records):
    """Prefill vs decode, which existing artifacts cannot separate.

    A non-streamed call returns ONE number that contains queueing + prefill +
    decode.  No artifact records a time-to-first-token, so the split is not
    measurable here -- only inferable, by regressing wall clock on each token
    count across the probe's calls.  Reported as `inference`, with n, because a
    slope from five heterogeneous gateway samples is a hint and not a
    measurement, and a later streaming measurement should replace it.
    """
    out = []
    for provider in sorted({r['provider'] for r in records}):
        scorable = [c for r in records if r['provider'] == provider
                    for c in r['calls']
                    if c['outcome'] == 'response' and c['completion_tokens']]
        if len(scorable) < 3:
            out.append({'provider': provider, 'status': 'insufficient_samples',
                        'scorable_calls': len(scorable),
                        'note': 'a slope needs at least three calls'})
            continue
        on_completion = ols([(c['completion_tokens'], c['wall_ms']) for c in scorable])
        on_prompt = ols([(c['prompt_tokens'], c['wall_ms']) for c in scorable]) \
            if len({c['prompt_tokens'] for c in scorable}) > 1 else None
        out.append({
            'provider': provider, 'status': 'inference', 'scorable_calls': len(scorable),
            'wall_ms_on_completion_tokens': on_completion,
            'wall_ms_on_prompt_tokens': on_prompt,
            'reading': ('decode-dominated if the completion slope is large and the prompt '
                        'slope is flat or undefined'),
            'caveat': 'non-streamed samples only; queueing and prefill are folded into the '
                      'intercept, which is why the intercept is not read as prefill',
        })
    return out


def probe_section(payload):
    """Per-call view of the decomposition probe, plus doc reconciliation."""
    records = []
    for record in payload.get('records', []):
        calls = []
        for call in record.get('calls', []):
            calls.append({
                'call': call.get('call'),
                'outcome': call.get('outcome'),
                'error_type': call.get('error_type'),
                'wall_ms': call.get('latency_ms'),
                'prompt_tokens': call.get('prompt_tokens'),
                'completion_tokens': call.get('completion_tokens'),
                'total_tokens': call.get('total_tokens'),
                'ms_per_output_token': ratio(call.get('latency_ms'), call.get('completion_tokens')),
            })
        scorable = [c for c in calls if c['outcome'] == 'response' and c['completion_tokens']]
        records.append({
            'provider': record.get('provider'),
            'model': record.get('model'),
            'label': record.get('label'),
            'thinking': record.get('thinking'),
            'payload_chars': record.get('payload_chars'),
            'calls': calls,
            'scorable_calls': len(scorable),
            'excluded_calls': len(calls) - len(scorable),
            'ms_per_output_token_each': [c['ms_per_output_token'] for c in scorable],
            'published_median_latency_ms': record.get('median_latency_ms'),
            'published_median_completion_tokens': record.get('median_completion_tokens'),
        })

    def find(provider, label, call_number):
        for record in records:
            if record['provider'] == provider and record['label'] == label:
                for call in record['calls']:
                    if call['call'] == call_number:
                        return call
        return None

    # Which single call each documented row quotes.  The probe ran twice per
    # row; the row cites the call that actually returned tokens.  For
    # `real_payload_encoding_default` BOTH calls returned, and the row quotes
    # call 2 while the artifact's own `median_*` fields also resolve to call 2
    # (an even-sized sample takes the upper of the two middle values, so with
    # n=2 "median" is the maximum -- worth knowing before treating any
    # two-sample median in this repository as central).
    quoted_call = {
        ('zhipu', 'real_payload'): 2,
        ('tokendance', 'tiny_prompt'): 2,
        ('tokendance', 'real_payload'): 1,
        ('tokendance', 'real_payload_encoding_default'): 2,
    }
    reconciliation = []
    for provider, label, prompt_tok, completion_tok, seconds in DOC_CLAIMS:
        call = find(provider, label, quoted_call[(provider, label)])
        if call is None:
            reconciliation.append({'provider': provider, 'label': label,
                                   'status': 'call missing from artifact'})
            continue
        observed_seconds = None if call['wall_ms'] is None else round(call['wall_ms'] / 1000, 1)
        mismatches = []
        if call['prompt_tokens'] != prompt_tok:
            mismatches.append(f"prompt_tokens {call['prompt_tokens']} != {prompt_tok}")
        if call['completion_tokens'] != completion_tok:
            mismatches.append(f"completion_tokens {call['completion_tokens']} != {completion_tok}")
        if observed_seconds != seconds:
            mismatches.append(f"latency {observed_seconds}s != {seconds}s")
        reconciliation.append({
            'provider': provider, 'label': label, 'quoted_call': quoted_call[(provider, label)],
            'doc': {'prompt_tokens': prompt_tok, 'completion_tokens': completion_tok,
                    'latency_s': seconds},
            'recomputed': {'prompt_tokens': call['prompt_tokens'],
                           'completion_tokens': call['completion_tokens'],
                           'latency_s': observed_seconds},
            'status': 'reproduced' if not mismatches else 'DIFFERS',
            'differences': mismatches,
        })
    return {'source': PROBE, 'records': records, 'doc_reconciliation': reconciliation}


def decompose(run, analyzer_row):
    """Split one turn's wall clock into remote compute vs everything else.

    Three of the four requested components are separable from existing
    artifacts; the fourth is not, and says so:

      vendor queue      remote_ms - sum(provider_attempts latencies).  Time the
                        caller spent inside the planner that was NOT inside an
                        HTTP attempt: rate-limit backoff, retry gaps, and the
                        provider's own queueing inside an attempt are NOT
                        separable from each other and are reported together.
      model decode      not separable from prefill without a streaming
                        measurement -- a non-streamed call returns one number
                        that contains both.  Marked `inference` and only
                        estimated from the probe's across-call slope.
      local tool exec   turn_wall - remote_ms, minus response composition,
                        serialization and checkpoint writes, which are not
                        instrumented.  An upper bound, not a measurement.
    """
    remote_ms = sum(analyzer_row['planner_latency_ms_each'] or [])
    attempts = [a for pl in _planner_metas(run) for a in (pl.get('provider_attempts') or [])]
    attempt_ms = sum(a.get('latency_ms') or 0 for a in attempts)
    turn_wall = analyzer_row['wall_ms']
    return {
        'turn_wall_ms': turn_wall,
        'remote_ms': round(remote_ms, 1),
        'remote_share': round(remote_ms / turn_wall, 4) if turn_wall else None,
        'model_call_count': len(analyzer_row['planner_latency_ms_each'] or []),
        'provider_attempt_ms': round(attempt_ms, 1),
        'provider_attempt_count': len(attempts),
        'vendor_queue_and_backoff_ms': round(remote_ms - attempt_ms, 1),
        'local_and_uninstrumented_ms': round(turn_wall - remote_ms, 1) if turn_wall else None,
        'first_progress_ms': analyzer_row['first_backend_progress_ms'],
        'decode_vs_prefill': 'not separable without streaming; see probe slope (inference)',
        'local_execution': 'upper bound only; response composition and checkpoint writes uninstrumented',
    }


def _planner_metas(run):
    trace = [e for r in run['tasks'][0]['observed'].get('responses', [])
             for e in r.get('tool_trace', [])]
    return [e.get('planner') or {} for e in trace if e.get('phase') == 'plan']


def comparable(ledger, wall_ms):
    """Is this run's wall clock a measurement of a healthy turn?

    A run whose model calls returned no usage is not a FAST turn -- it is a
    turn that lost its model calls and fell back.  Its wall clock is short for
    the same reason a crash is quick, so folding it into a latency median
    understates latency and looks like an improvement that did not happen.
    The `planner-closeout-v3` cohort is exactly this case: two of three runs
    lost most of their model calls.

    The test is per LEDGER ROW, not per turn outcome: a call that reported no
    usage took some amount of wall clock that no longer means "model latency"
    (it is a timeout, a retry gap, or an error path), and one such call is
    enough to make the turn's wall clock incomparable.  A definitively refused
    call would be different -- it returns fast and says so -- but no cohort here
    contains one.
    """
    if wall_ms is None:
        return False, 'no turn wall recorded'
    if not ledger:
        return False, 'no model calls'
    unmeasured = [a for a in ledger if a.get('usage_tokens') is None]
    if unmeasured:
        statuses = sorted({a.get('status') for a in unmeasured})
        return False, (f'{len(unmeasured)} of {len(ledger)} model calls reported no usage '
                       f'(statuses: {", ".join(str(s) for s in statuses)})')
    return True, None


def batch_section(analyzer, cohorts_spec=COHORTS):
    cohorts = []
    for label, pattern in cohorts_spec:
        files = sorted(glob.glob(str(DOCS / pattern)))
        runs = []
        for path in files:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
            row = analyzer.analyze_run(path)
            ledger = raw['tasks'][0]['observed'].get('usage_ledger', [])
            split_known = sum(1 for a in ledger if a.get('prompt_tokens') is not None
                              or a.get('completion_tokens') is not None)
            ok, why = comparable(ledger, row['wall_ms'])
            # Per-call token detail, straight off the ledger.  This is the L0
            # requirement that a metric point at ONE call: the totals above are
            # a turn, these rows are the calls that make it up.  Fields the
            # provider did not report stay null.
            token_calls = [{'kind': a.get('kind'), 'cycle': a.get('cycle'),
                            'status': a.get('status'),
                            'prompt_tokens': a.get('prompt_tokens'),
                            'completion_tokens': a.get('completion_tokens'),
                            'reasoning_tokens': a.get('reasoning_tokens'),
                            'usage_tokens': a.get('usage_tokens')} for a in ledger]
            split_rows = [c for c in token_calls if c['completion_tokens']]
            known_completion = sum(c['completion_tokens'] for c in split_rows) if split_rows else None
            known_prompt = sum(c['prompt_tokens'] or 0 for c in split_rows) if split_rows else None
            known_reasoning = (sum(c['reasoning_tokens'] or 0 for c in split_rows)
                               if split_rows and any(c['reasoning_tokens'] is not None for c in split_rows)
                               else None)
            planner_ms = row['planner_latency_ms_each'] or []
            # Aggregate ratio, only when the two sides count the same calls --
            # otherwise it would divide a latency for N calls by tokens for M.
            aggregate_ratio = (round(sum(planner_ms) / known_completion, 2)
                               if known_completion and len(split_rows) == len(planner_ms) else None)
            runs.append({
                'file': Path(path).name,
                'task_id': row['task_id'],
                'goal_status': row['goal_status'],
                'latency_comparable': ok,
                'latency_exclusion_reason': why,
                'planner_latency_ms_each': row['planner_latency_ms_each'],
                'planner_calls': len(row['planner_latency_ms_each'] or []),
                'turn_wall_ms': row['wall_ms'],
                'model_calls': row['attempts'],
                'actual_responses': row['actual_responses'],
                'usage_unknown': row['usage_unknown'],
                'known_total_tokens': row['known_tokens'],
                # Null in every 2026-09-11 cohort -- their ledger recorded one
                # total and the split did not exist yet.  Null is the honest
                # value there and is never inferred from the total.  Cohorts
                # recorded after the L0 change carry real numbers here.
                'known_completion_tokens': known_completion,
                'known_prompt_tokens': known_prompt,
                'known_reasoning_tokens': known_reasoning,
                'calls_with_token_split': split_known,
                'token_calls': token_calls,
                'ms_per_output_token_aggregate': aggregate_ratio,
                'decomposition': decompose(raw, row),
            })
        good = [r for r in runs if r['latency_comparable']]
        split_runs = [r for r in runs if r['known_completion_tokens']]
        all_completion = [c['completion_tokens'] for r in runs for c in r['token_calls']
                          if c['completion_tokens']]
        comp_latencies = [v for r in good for v in (r['planner_latency_ms_each'] or [])]
        comp_walls = [r['turn_wall_ms'] for r in good if r['turn_wall_ms'] is not None]
        all_walls = [r['turn_wall_ms'] for r in runs if r['turn_wall_ms'] is not None]
        cohorts.append({
            'cohort': label,
            'runs': len(runs),
            'latency_comparable_runs': len(good),
            'excluded_runs': [{'file': r['file'], 'why': r['latency_exclusion_reason']}
                              for r in runs if not r['latency_comparable']],
            'planner_calls': sum(r['planner_calls'] for r in runs),
            'comparable_planner_calls': len(comp_latencies),
            # The published figure: healthy runs only.  The unfiltered median is
            # kept beside it so the exclusion is auditable, never silent.
            'planner_latency_ms_median': round(statistics.median(comp_latencies), 1) if comp_latencies else None,
            'planner_latency_ms_median_including_failed_runs': (
                round(statistics.median([v for r in runs for v in (r['planner_latency_ms_each'] or [])]), 1)
                if any(r['planner_latency_ms_each'] for r in runs) else None),
            'planner_latency_ms_each_comparable': comp_latencies,
            'turn_wall_ms_each_comparable': comp_walls,
            'turn_wall_ms_median': round(statistics.median(comp_walls), 1) if comp_walls else None,
            'turn_wall_ms_median_including_failed_runs': (
                round(statistics.median(all_walls), 1) if all_walls else None),
            'calls_per_turn': ([r['planner_calls'] for r in good] or None),
            'token_split': 'recorded' if split_runs else 'unavailable',
            'calls_with_token_split': sum(r['calls_with_token_split'] for r in runs),
            'completion_tokens_each': all_completion,
            'completion_tokens_median': (round(statistics.median(all_completion), 1)
                                         if all_completion else None),
            'reasoning_tokens_each': [c['reasoning_tokens'] for r in runs for c in r['token_calls']
                                      if c['completion_tokens']],
            'ms_per_output_token_aggregate_each': [r['ms_per_output_token_aggregate'] for r in good
                                                   if r['ms_per_output_token_aggregate']],
            'detail': runs,
        })
    return cohorts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True,
                        help='new file; an existing one is never overwritten')
    parser.add_argument('--probe', default=PROBE)
    # Later phases compare a NEW cohort against this baseline.  Letting them
    # name it here keeps one implementation of the口径: a second script that
    # "just reads the same fields" is how two subtly different numbers get
    # published for the same thing.
    parser.add_argument('--cohort', action='append', default=None, metavar='LABEL=GLOB',
                        help='cohort to include, repeatable; defaults to the three '
                             '2026-09-11 cohorts')
    args = parser.parse_args()
    cohorts_spec = COHORTS
    if args.cohort:
        cohorts_spec = []
        for entry in args.cohort:
            if '=' not in entry:
                parser.error(f'--cohort expects LABEL=GLOB, got {entry!r}')
            label, _, pattern = entry.partition('=')
            cohorts_spec.append((label.strip(), pattern.strip()))
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    if out.exists():
        parser.error(f'{out} exists; preserve the previous baseline and pick a new name')

    probe_path = Path(args.probe)
    if not probe_path.is_absolute():
        probe_path = DOCS / probe_path
    analyzer = load_acceptance_analyzer()
    probe = probe_section(json.loads(probe_path.read_text(encoding='utf-8')))
    probe['prefill_vs_decode'] = inference_section(probe['records'])
    cohorts = batch_section(analyzer, cohorts_spec)

    scorable = [c for r in probe['records'] for c in r['calls']
                if c['outcome'] == 'response' and c['completion_tokens']]
    ratios = [c['ms_per_output_token'] for c in scorable]
    report = {
        'started_at': datetime.now(timezone.utc).isoformat(),
        'note': 'synthetic content only; recomputed from artifacts, no calls made',
        'metric': {
            'per_call_fields': ['prompt_tokens', 'completion_tokens', 'wall_ms', 'outcome'],
            'per_turn_fields': ['model_call_count', 'turn_wall_ms', 'first_progress_ms', 'remote_ms'],
            'primary_predictor': 'completion_tokens',
            'ratio': 'ms_per_output_token = wall_ms / completion_tokens',
            'scorable': "outcome == 'response' AND completion_tokens > 0",
            'excluded': 'refusals, timeouts and calls with no reported split stay in the counts, out of the ratio',
            'unavailable_policy': 'a missing field is null and named; it is never estimated',
            'first_feedback_vs_result': 'first_progress_ms and turn_wall_ms are separate measurements; browser-visible feedback is not recorded by any artifact yet',
        },
        'probe': probe,
        'batches': cohorts,
        'summary': {
            'scorable_calls': len(scorable),
            'excluded_calls': sum(len(r['calls']) for r in probe['records']) - len(scorable),
            'ms_per_output_token_each': ratios,
            'ms_per_output_token_min': min(ratios) if ratios else None,
            'ms_per_output_token_max': max(ratios) if ratios else None,
            'batches_with_token_split': sum(1 for c in cohorts if c['token_split'] != 'unavailable'),
        },
        'unavailable': [
            {'field': 'prompt_tokens / completion_tokens',
             'where': 'all three 2026-09-11 cohorts',
             'why': 'llm_attempts recorded one total; the planner trace recorded latency only',
             'remedy': 'L0 adds the split to the ledger and to provider_attempts; applies to future batches',
             'backfill': 'none -- historical splits are not recoverable from a total'},
            {'field': 'browser-visible first feedback',
             'where': 'every artifact',
             'why': 'only the backend progress event is sampled (first_backend_progress_ms)',
             'remedy': 'out of scope for L0; recorded here so it is not confused with the turn wall'},
        ],
        'completed_at': datetime.now(timezone.utc).isoformat(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

    for cohort in cohorts:
        print(json.dumps({k: cohort[k] for k in
                          ('cohort', 'runs', 'latency_comparable_runs', 'comparable_planner_calls',
                           'planner_latency_ms_median', 'turn_wall_ms_median',
                           'planner_latency_ms_median_including_failed_runs',
                           'turn_wall_ms_median_including_failed_runs',
                           'calls_per_turn', 'token_split', 'completion_tokens_median',
                           'reasoning_tokens_each', 'ms_per_output_token_aggregate_each')},
                         ensure_ascii=False))
    print(json.dumps(report['summary'], ensure_ascii=False))
    print('->', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
