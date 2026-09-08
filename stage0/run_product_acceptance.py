"""P0/P1 local product acceptance; browser evidence is a mandatory separate gate."""
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
from datetime import datetime, timezone
from pathlib import Path

from stage0.run_harness_acceptance import ROOT, source_fingerprints, write_json

REQUIRED_BROWSER = (
    'isolated_synthetic_fixture', 'alert_detail_explanation_visible',
    'evidence_original_rendered_with_exact_highlight', 'fact_detail_visible',
    'unknown_source_version_not_detector_name',
    'amlodipine_listed_for_selection', 'dose_change_committed',
    'impact_is_run_attributed', 'impact_has_real_invalidations',
    'impact_count_matches_details', 'change_impact_card_renders',
    'impact_survives_reload', 'no_react_pageerrors', 'no_http_500',
)
FULL_BROWSER = ('isolated_fixture', 'real_ocr_import', 'real_bbox', 'unreviewed_ocr_blocked',
    'partial_confirmation_survives_reload', 'original_and_correction_preserved', 'domain_receipt_and_safety_run',
    'not_listed_keeps_authority', 'missing_name_waits', 'task_survives_new_session', 'supplement_resumes_same_case',
    'background_safety_check_committed', 'task_code_verified_completion', 'cumulative_task_budget',
    'html_summary_download', 'summary_has_patient_sources', 'mobile_no_horizontal_overflow', 'no_react_errors', 'no_server_errors')


def fingerprints():
    result = source_fingerprints()
    for relative in ('scripts/product-p1-browser-acceptance.js', 'scripts/product-full-browser-acceptance.js',
                     'frontend/vite.config.ts', 'requirements-product-ocr.txt'):
        result[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    for root, pattern in [('stage0/product_evals/tasks', '*.json'), ('frontend/src', '*.css'), ('docs/product-upgrade/p5/samples', '*')]:
        for path in (ROOT / root).rglob(pattern):
            if path.is_file():
                result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def parse_browser(transcript):
    if '### Error' in transcript:
        raise ValueError('browser CLI reported an error')
    body, _ = json.JSONDecoder().raw_decode(transcript.split('### Result\n', 1)[1].lstrip())
    if not all(body.get('checks', {}).get(key) is True for key in REQUIRED_BROWSER):
        raise ValueError('required browser checks missing or failed')
    if body.get('errors') or body.get('serverErrors'):
        raise ValueError('browser errors present')
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/product-upgrade/closeout')
    parser.add_argument('--browser-log', type=Path)
    parser.add_argument('--full', action='store_true', help='Require P0-P6 engineering, real local OCR and full browser gates')
    parser.add_argument('--full-browser-log', type=Path)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    before = fingerprints()
    checks = []
    py = sys.executable
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm') or 'npm'
    commands = [
        ('unit', [py, '-m', 'stage0.run_harness_acceptance', '--unit-only', '--out', str(out)], ROOT, True),
        ('product-p1', [py, '-m', 'stage0.product_evals.run_eval', '--suite', 'dev', '--phase', 'P1', '--out', str(out / 'product-p1.json')], ROOT, True),
        ('product-dev-all', [py, '-m', 'stage0.product_evals.run_eval', '--out', str(out / 'product-dev-all.json')], ROOT, args.full),
        ('held-out', [py, '-m', 'stage0.product_evals.run_eval', '--suite', 'held_out', '--out', str(out / 'held-out.json')], ROOT, False),
        ('harness-p1', [py, '-m', 'stage0.harness_eval', '--out', str(out / 'harness-p1.json')], ROOT, True),
        ('pip-check', [py, '-m', 'pip', 'check'], ROOT, True),
        ('frontend-build', [npm, 'run', 'build'], ROOT / 'frontend', True),
    ]
    if args.full:
        commands += [
            ('quality-ablation', [py, '-m', 'stage0.product_quality_eval', '--out', str(out / 'quality-ablation.json')], ROOT, True),
            ('real-ocr', [py, '-m', 'stage0.product_ocr_eval', '--out', str(out / 'real-ocr.json')], ROOT, True),
        ]
    for name, command, cwd, required in commands:
        started = time.monotonic()
        entry = {'name': name, 'required': required, 'command': command, 'log': f'{name}.log'}
        try:
            with (out / entry['log']).open('w', encoding='utf-8') as log:
                code = subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=600, env={**os.environ, 'PYTHONUTF8': '1'}).returncode
            entry.update(exit_code=code, status='pass' if code == 0 else ('unavailable' if code == 2 and not required else 'fail'))
        except (OSError, subprocess.TimeoutExpired) as exc:
            entry.update(status='fail', error=str(exc))
        entry['seconds'] = round(time.monotonic() - started, 3)
        checks.append(entry)
        print(json.dumps(entry), flush=True)
    try:
        logpath = args.browser_log or out / 'browser.log'
        browser = parse_browser(logpath.read_text(encoding='utf-8-sig'))
        runtime = [ROOT / relative for relative in before if not relative.startswith('stage0/test_')]
        newest_source = max(path.stat().st_mtime for path in runtime)
        screenshots = {}
        for label in ('evidence-drawer', 'fact-detail', 'change-impact'):
            path = ROOT / f'output/playwright/product-p1-{label}.png'
            if path.stat().st_mtime < newest_source:
                raise ValueError(f'stale screenshot: {path.name}')
            screenshots[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if logpath.stat().st_mtime < newest_source:
            raise ValueError('stale browser transcript')
        browser['screenshots'] = screenshots
        write_json(out / 'browser.json', browser)
        checks.append({'name': 'browser', 'required': True, 'status': 'pass', 'checks': len(REQUIRED_BROWSER)})
    except (OSError, ValueError, IndexError, KeyError) as exc:
        checks.append({'name': 'browser', 'required': True, 'status': 'fail', 'error': str(exc)})
    if args.full:
        try:
            path = args.full_browser_log or out / 'full-browser.log'
            transcript = path.read_text('utf-8-sig')
            if '### Error' in transcript:
                raise ValueError('full browser CLI failed')
            full, _ = json.JSONDecoder().raw_decode(transcript.split('### Result\n', 1)[1].lstrip())
            if not all(full.get('checks', {}).get(k) is True for k in FULL_BROWSER) or full.get('errors') or full.get('serverErrors'):
                raise ValueError('required full browser checks absent or failed')
            runtime = [ROOT / relative for relative in before if not relative.startswith('stage0/test_')]
            newest = max(p.stat().st_mtime for p in runtime)
            artifacts = [ROOT / f'output/playwright/{name}' for name in ('product-p5-ocr-source.png', 'product-p3-waiting-task.png',
                'product-p3-visit-summary.png', 'product-p6-mobile.png', 'product-visit-summary.html')]
            if any(not p.is_file() or p.stat().st_mtime < newest for p in [path, *artifacts]):
                raise ValueError('full browser artifacts missing or older than sources')
            full['artifact_sha256'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}
            write_json(out / 'full-browser.json', full)
            checks.append({'name': 'full-browser', 'required': True, 'status': 'pass', 'checks': len(FULL_BROWSER)})
        except (OSError, ValueError, IndexError, KeyError) as exc:
            checks.append({'name': 'full-browser', 'required': True, 'status': 'fail', 'error': str(exc)})
    checks.append({'name': 'source_unchanged', 'required': True,
                   'status': 'pass' if before == fingerprints() else 'fail'})
    # Missing future-phase datasets remain visible without masquerading as
    # failure of the implemented P0/P1 local acceptance scope.
    okay = all(item['status'] == 'pass' for item in checks if item['required'])
    okay = okay and not any(item['status'] == 'fail' for item in checks)
    report = {'scope': 'P0-P1 local synthetic engineering acceptance',
              'status': 'pass' if okay else 'fail', 'checks': checks,
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version, 'platform': platform.platform(), 'source_sha256': before,
              'stages': {'P0': 'local_verified' if okay else 'incomplete',
                         'P1': 'local_verified' if okay else 'incomplete',
                         'P2': 'not_implemented', 'P3': 'not_implemented',
                         'P4': 'not_implemented', 'P5': 'not_implemented',
                         'P6': 'implemented_scope_acceptance_only'},
              'real_model_quality': 'unavailable', 'independent_domain_review': 'unavailable'}
    if args.full:
        report['scope'] = 'P0-P6 local synthetic engineering and actual local OCR acceptance'
        report['stages'] = {f'P{i}': 'local_verified' if okay else 'incomplete' for i in range(7)}
        report['external_tracks'] = {'real_model_quality': 'unavailable_no_opt_in_experiment',
            'independent_domain_review': 'unavailable_no_review_dataset', 'independent_held_out': 'unavailable'}
        report['optional_followups'] = {'Q1_independent_model_delegation': 'not_selected', 'Q2_GEPA': 'not_selected'}
    write_json(out / 'acceptance-summary.json', report)
    print(json.dumps({'status': report['status'], 'out': str(out)}), flush=True)
    return 0 if okay else 1


if __name__ == '__main__':
    raise SystemExit(main())
