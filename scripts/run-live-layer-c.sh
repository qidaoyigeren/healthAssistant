#!/usr/bin/env bash
# Layer C entry: explicit real-model acceptance (k=3, frozen protocol).
set -e
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
exec .venv/Scripts/python.exe scripts/verify-agent-closeout.py --out output/planner-reliability-20260910/live --only-live --live-repeats 3
