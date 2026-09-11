"""A5: run the authorized live evaluation k times (declared k=3).

Each run: one exposed dev task, official Zhipu glm-4.7-flash free tier,
180 s / 8 calls per run. Results are preserved per-run; sampling never
repeats into an existing result file.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/agent-capability-upgrade/A5'
K = 3

exit_codes = []
for k in range(1, K + 1):
    out = OUT / f'live-run-{k}.json'
    if out.exists():
        print(f'run {k}: result exists, preserving')
        continue
    completed = subprocess.run(
        [sys.executable, '-m', 'stage0.agent_evals.run_eval', '--policy', 'gap',
         '--path', 'replay', '--live', '--out', str(out)],
        cwd=ROOT, capture_output=True, text=True)
    exit_codes.append(completed.returncode)
    print(f'run {k}: exit={completed.returncode}')
    tail = (completed.stdout or '').strip().splitlines()
    if tail:
        print('  ', tail[-1][:220])
    if completed.returncode != 0 and (completed.stderr or '').strip():
        print('  stderr tail:', (completed.stderr or '').strip().splitlines()[-1][:220])
sys.exit(0 if all(code == 0 for code in exit_codes) else 1)
