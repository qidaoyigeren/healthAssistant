"""Reproducible local acceptance. Never treats missing browser/OTel QA as pass.

python -m stage0.run_harness_acceptance --out docs/harness-upgrade/final-acceptance
Browser fixture/CLI steps are documented in that directory's README.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_fingerprints():
    files = list((ROOT / "stage0").rglob("*.py")) + list((ROOT / "frontend" / "src").rglob("*.ts*"))
    files += [ROOT / "scripts/harness-browser-acceptance.js", ROOT / "frontend/package-lock.json",
              ROOT / "requirements-harness-observability.txt"]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs/harness-upgrade/final-acceptance")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--unit-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.unit_only:
        names = ["stage0." + path.stem for path in sorted((ROOT / "stage0").glob("test_*.py"))]
        # stage0 is a namespace package: unittest discover -s stage0 -t .
        # rejects it. Load qualified module names without changing packaging.
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
        write_json(out / "unittest-summary.json", {"tests": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
            "successful": result.wasSuccessful() and not result.skipped})
        return 0 if result.wasSuccessful() and not result.skipped else 1

    started = time.perf_counter()
    before = source_fingerprints()
    checks = []
    py = sys.executable
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    commands = [
        ("unit", [py, "-m", "stage0.run_harness_acceptance", "--unit-only", "--out", str(out)], ROOT),
        ("p1-eval", [py, "-m", "stage0.harness_eval", "--out", str(out / "p1-eval.json")], ROOT),
        ("p2-eval", [py, "-m", "stage0.harness_p2_eval", "--repeat", str(args.repeat), "--out", str(out / "p2-eval.json")], ROOT),
        ("p3-eval", [py, "-m", "stage0.harness_p3_eval", "--repeat", str(args.repeat), "--out", str(out / "p3-eval.json")], ROOT),
        ("pip-check", [py, "-m", "pip", "check"], ROOT),
        ("frontend-build", [npm or "npm", "run", "build"], ROOT / "frontend"),
    ]
    for name, command, cwd in commands:
        tick = time.perf_counter()
        try:
            with (out / f"{name}.log").open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=600, env={**os.environ, "PYTHONUTF8": "1"})
            code = completed.returncode
            entry = {"name": name, "status": "pass" if code == 0 else "fail", "exit_code": code}
        except (OSError, subprocess.TimeoutExpired) as exc:
            entry = {"name": name, "status": "fail", "error": str(exc)}
        entry.update(seconds=round(time.perf_counter() - tick, 3), log=f"{name}.log")
        checks.append(entry)
        print(json.dumps(entry), flush=True)

    try:
        transcript = (out / "browser-acceptance.log").read_text(encoding="utf-8-sig")
        marker = "### Result\n"
        browser, _ = json.JSONDecoder().raw_decode(transcript.split(marker, 1)[1].lstrip())
        screenshots = [ROOT / f"output/playwright/harness-{name}.png" for name in ("progress", "cancelled", "completed")]
        browser_sources = [ROOT / name for name in before
                           if not name.startswith("stage0/test_") and "run_harness_acceptance" not in name]
        fresh = (out / "browser-acceptance.log").stat().st_mtime_ns >= max(p.stat().st_mtime_ns for p in browser_sources)
        passed = (browser["status"] == "pass" and all(browser["checks"].values())
                  and all(p.is_file() for p in screenshots) and fresh)
        write_json(out / "browser-acceptance.json", browser)
        checks.append({"name": "browser", "status": "pass" if passed else "fail",
                       "artifact_newer_than_sources": fresh,
                       "screenshots": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in screenshots if p.is_file()},
                       "note": "local synthetic FastAPI + Vite + real Edge; independently invoked CLI"})
    except (OSError, ValueError, KeyError, IndexError) as exc:
        checks.append({"name": "browser", "status": "pending", "reason": type(exc).__name__})
    after = source_fingerprints()
    checks.append({"name": "source_unchanged_during_acceptance", "status": "pass" if before == after else "fail"})
    packages = {}
    for name in ("langgraph", "langgraph-checkpoint-sqlite", "openai", "pydantic", "fastapi",
                 "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "missing"
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "scope": "local-synthetic-acceptance",
              "status": "pass" if all(c["status"] == "pass" for c in checks) else "incomplete",
              "seconds": round(time.perf_counter() - started, 3), "checks": checks,
              "python": sys.version, "platform": platform.platform(), "packages": packages,
              "source_fingerprints": after,
              "excluded": ["live_model_quality_and_cost", "independent_held_out_dataset", "clinical_signoff", "P4_production_migration"]}
    write_json(out / "acceptance-summary.json", report)
    print(f"Acceptance: {report['status']} ({report['seconds']}s)", flush=True)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
