"""Explicit, immutable k=3 live cohort under the existing frozen protocol."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'docs/agent-capability-upgrade/closeout-2026-09-10/planner-reliability/acceptance-protocol.md'


def fingerprint():
    paths = sorted([*ROOT.glob('stage0/**/*.py'), *ROOT.glob('frontend/src/**/*.ts'),
                    *ROOT.glob('frontend/src/**/*.tsx'), *ROOT.glob('scripts/*.py')])
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in paths)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-live', action='store_true')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    if not args.enable_live:
        parser.error('Remote calls disabled. Explicit --enable-live is required.')
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)  # Including partial cohorts: never reuse.
    manifest = {'started_at': datetime.now(timezone.utc).isoformat(), 'planned_k': 3, 'actual_k': 0,
        'source_fingerprint': fingerprint(), 'protocol_sha256': hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        'dataset_sha256': hashlib.sha256((ROOT / 'stage0/agent_evals/dev.json').read_bytes()).hexdigest(),
        'configuration': {'provider': 'official_zhipu', 'model': 'glm-4.7-flash',
            'path': 'replay', 'seconds_per_run': 180, 'calls_per_run': 8,
            'planner_provider_retries': 1, 'independent_held_out': 'unavailable'}, 'runs': []}
    target = out / 'live-cohort.json'
    def save():
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'MEMORY_ENABLE_LLM': '0',
           'AGENT_MULTI_REVIEW_MODEL_ENABLED': '0', 'PLANNER_PROVIDER_RETRIES': '1',
           'PLANNER_PROVIDER_RETRY_BACKOFF_SECONDS': '1.5', 'PLANNER_ARG_AUTOCORRECT': '1',
           'PLANNER_SAFETY_REJECTION_LIMIT': '2', 'AGENT_EVAL_MAX_CYCLES': '12'}
    for index in range(1, 4):
        if fingerprint() != manifest['source_fingerprint']:
            manifest['stopped_reason'] = 'source_changed'
            break
        manifest['actual_k'] = index
        save()  # Admission persisted before dispatch.
        cmd = [sys.executable, '-m', 'stage0.agent_evals.run_eval', '--policy', 'gap', '--path', 'replay',
               '--live', '--out', str(out / f'live-final-{index}.json')]
        try:
            run = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, encoding='utf-8',
                                 errors='replace', timeout=240)
            code, log = run.returncode, run.stdout + run.stderr
        except subprocess.TimeoutExpired as exc:
            code = -1
            log = 'Acceptance process timeout; retained partial output.\n' + str(exc.stdout or '') + str(exc.stderr or '')
        (out / f'live-final-{index}.log').write_text(log, encoding='utf-8')
        manifest['runs'].append({'index': index, 'exit_code': code,
            'artifact_present': (out / f'live-final-{index}.json').exists()})
        print(json.dumps(manifest['runs'][-1]), flush=True)
        save()
    manifest['source_unchanged'] = fingerprint() == manifest['source_fingerprint']
    manifest['completed_at'] = datetime.now(timezone.utc).isoformat()
    manifest['collection_status'] = 'complete' if len(manifest['runs']) == 3 and manifest['source_unchanged'] else 'incomplete'
    manifest['quality_status'] = 'requires_metrics_and_protocol_evaluation'
    save()
    return 0 if manifest['collection_status'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
