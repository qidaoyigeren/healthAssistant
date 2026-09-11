"""A5 ablation runner — engineering replay over the frozen A0 dev set.

Runs the attributable comparisons on the local deterministic path (no remote
calls):
  1. baseline  : frozen pre-A1 planner policy (no gap-driven planning)
  2. gap       : A1 gap-driven planning (current source)
  3. gap-tight : A1 with a tight per-run cycle budget (budget sensitivity)

Usage:  .venv/Scripts/python.exe scripts/agent-capability-ablation.py --out output/a5-ablation.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/agent-capability-upgrade/A5'

RUNS = [
    {'name': 'baseline', 'args': ['--policy', 'baseline', '--path', 'replay']},
    {'name': 'gap', 'args': ['--policy', 'gap', '--path', 'replay']},
    {'name': 'gap_tools', 'args': ['--policy', 'gap', '--path', 'tools']},
    {'name': 'gap_tight_budget', 'args': ['--policy', 'gap', '--path', 'replay'], 'env': {'AGENT_EVAL_MAX_CYCLES': '4'}},
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(OUT / 'ablation.json'))
    args = parser.parse_args()
    results = []
    for run in RUNS:
        out_path = OUT / f"ablation-{run['name']}.json"
        import os
        env = {**os.environ, **run.get('env', {})}
        completed = subprocess.run(
            [sys.executable, '-m', 'stage0.agent_evals.run_eval', *run['args'], '--out', str(out_path)],
            cwd=ROOT, capture_output=True, text=True, env=env)
        payload = json.loads(out_path.read_text(encoding='utf-8')) if out_path.exists() else {}
        summary = payload.get('summary', {'total': None, 'passed': None})
        results.append({'name': run['name'], 'exit_code': completed.returncode,
                        'summary': summary, 'artifact': str(out_path.relative_to(ROOT))})
    # Real model quality / provider availability are not sampled here; this
    # file records engineering ablations only.
    report = {'protocol': 'agent-acceptance@1', 'stage': 'A5', 'kind': 'engineering_ablation',
              'real_model_quality': 'unavailable', 'provider_availability': 'unavailable',
              'independent_held_out': 'unavailable', 'runs': results}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if all(r['exit_code'] == 0 for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
