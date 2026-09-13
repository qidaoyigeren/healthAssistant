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
#: 主线产品验收套件。它由下面带 `--report` 的显式调用执行，所以不进 glob 循环。
MAINLINE_SUITE = 'test_safety_mainline_e2e'


def fingerprint():
    """App scope (stage0 + frontend/src, NO scripts/) — see
    stage0/source_fingerprint.py.

    NOTE: narrower than the live-acceptance entries' repo scope, so the two
    digests are not comparable.  Scope is explicit now precisely so that is
    visible instead of being discovered as a false 'source changed' alarm."""
    sys.path.insert(0, str(ROOT))
    from stage0.source_fingerprint import SCOPE_APP, source_fingerprint
    return source_fingerprint(SCOPE_APP)


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
            if file.stem == MAINLINE_SUITE:
                continue  # 由下面带 --report 的那一次权威执行覆盖
            result['suites'].append(run(file.stem, [sys.executable, '-m', 'unittest', f'stage0.{file.stem}', '-q']))
        # 主线产品验收：按**类别**报告（必要检查 / 事项 / 调查 / 等待与处置 /
        # 模型与程序的分工 / 请求与故障可追溯），而不是合并成一个通过率。
        mainline_report = out / 'safety-mainline.json'
        row = run('safety-mainline-acceptance',
                  [sys.executable, '-m', 'stage0.test_safety_mainline_e2e',
                   '--report', str(mainline_report)], timeout=600)
        result['mainline'] = row
        if mainline_report.exists():
            result['mainline']['categories'] = json.loads(
                mainline_report.read_text(encoding='utf-8')).get('categories', {})
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
        r['passed'] for k in ('suites', 'evals', 'live') for r in result[k]
    ) and (args.only_live or result.get('mainline', {}).get('passed')) else 'fail'
    result['tests_executed'] = sum(r['tests'] or 0 for r in result['suites'])
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
