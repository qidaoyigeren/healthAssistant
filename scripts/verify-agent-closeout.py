"""Reproducible closeout: isolated unittest processes and frozen-source evals.

Default is offline. --live-repeats explicitly opts into the already configured
official free Zhipu evaluation; no retries to replace failures, no overwritten logs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def fingerprint():
    paths = sorted([*ROOT.glob('stage0/**/*.py'), *ROOT.glob('frontend/src/**/*.ts'),
                    *ROOT.glob('frontend/src/**/*.tsx')])
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in paths)).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--live-repeats', type=int, default=0, choices=range(4))
    parser.add_argument('--only-live', action='store_true')
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / ('live-cohort.json' if args.only_live else 'engineering.json')
    if report_path.exists():
        raise RuntimeError('Preserve existing acceptance; choose a new output directory')
    version = fingerprint()
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'MEMORY_ENABLE_LLM': '0',
           'AGENT_LLM_PLANNER': '0', 'AGENT_MULTI_REVIEW_MODEL_ENABLED': '0'}
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'source_fingerprint': version,
              'suites': [], 'evals': [], 'live': [], 'independent_held_out': 'unavailable'}

    def run(name, cmd, expected=0, timeout=240):
        try:
            completed = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                encoding='utf-8', errors='replace', timeout=timeout)
            content = completed.stdout + completed.stderr
            code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            content, code = str(exc), -1
        (out / f'{name}.log').write_text(content, encoding='utf-8')
        count = re.search(r'Ran (\d+) tests?', content)
        row = {'name': name, 'command': cmd, 'exit_code': code, 'expected_exit': expected,
               'passed': code == expected, 'tests': int(count[1]) if count else None}
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return row

    if not args.only_live:
        for file in sorted((ROOT / 'stage0').glob('test_*.py')):
            result['suites'].append(run(file.stem, [sys.executable, '-m', 'unittest', f'stage0.{file.stem}', '-q']))
        for policy, path, negative in [('baseline', 'replay', False), ('gap', 'replay', False),
                                       ('gap', 'tools', False), ('gap', 'replay', True)]:
            name = f'{policy}-{path}' + ('-negative' if negative else '')
            cmd = [sys.executable, '-m', 'stage0.agent_evals.run_eval', '--policy', policy,
                   '--path', path, '--out', str(out / f'{name}.json')]
            if negative:
                cmd.append('--negative-control')
            row = run(name, cmd, expected=1 if policy == 'baseline' or negative else 0)
            result['evals'].append(row)
    for index in range(1, args.live_repeats + 1):
        name = f'live-final-{index}'
        cmd = [sys.executable, '-m', 'stage0.agent_evals.run_eval', '--policy', 'gap', '--path', 'replay',
               '--live', '--out', str(out / f'{name}.json')]
        result['live'].append(run(name, cmd))
    result['source_unchanged'] = version == fingerprint()
    result['status'] = 'pass' if result['source_unchanged'] and all(
        r['passed'] for k in ('suites', 'evals', 'live') for r in result[k]) else 'fail'
    result['tests_executed'] = sum(r['tests'] or 0 for r in result['suites'])
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
