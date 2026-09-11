"""A5 demo driver — API part (UI screenshots are taken by agent-capability-demo.js).

Orchestrates with REAL process restarts: phase 1 creates the open review and
verifies it waits; then the caller restarts the server; phase 2 supplements
the fact and resumes, recording observed server state to demo-result.json.
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

API = 'http://127.0.0.1:8000'
OUT = Path(__file__).resolve().parents[1] / 'docs/agent-capability-upgrade/A5/demo-result.json'


def call(method, path, body=None):
    headers = {'Content-Type': 'application/json'}
    if body is not None and method == 'POST':
        headers['Idempotency-Key'] = f"demo-{uuid.uuid4().hex[:12]}"
    request = urllib.request.Request(API + path, data=json.dumps(body).encode('utf-8') if body is not None else None,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8'))


def wait_event(accepted, seconds=120):
    url = accepted.get('status_url') or f"/v1/events/{accepted.get('event_key')}"
    payload = {}
    for _ in range(seconds * 5):
        status, payload = call('GET', url)
        if payload.get('status') in ('committed', 'failed'):
            return payload
        time.sleep(0.2)
    raise RuntimeError('event did not settle: ' + json.dumps(payload, ensure_ascii=False)[:200])


phase = sys.argv[1]
result = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {'steps': []}

if phase == '1':
    # Seed two real-corpus medications; one dose deliberately lacks a unit so
    # the review MUST wait for a recorded fact.
    for index, (name, dose, schedule) in enumerate([
            ('氨氯地平', '5', '每日一次'), ('克拉霉素', '250mg', '每日两次')]):
        status, accepted = call('POST', '/v1/events', {
            'event_type': 'medication_change', 'text': f'添加{name} {dose} {schedule}',
            'payload': {'medication': name, 'action': 'add', 'dose': dose, 'schedule': schedule},
            'session_id': 'demo'})
        outcome = wait_event(accepted)
        result['steps'].append({'step': f'seed_{name}', 'status': outcome.get('status'),
                                'run_status': (outcome.get('response') or {}).get('run_status')})
    status, task = call('POST', '/v1/care-tasks', {'key': f"demo-create-{uuid.uuid4().hex[:8]}",
                                                   'goal_type': 'evidence_review',
                                                   'goal': '核查当前用药剂量与相互作用证据'})
    resumed = call('POST', f"/v1/care-tasks/{task['id']}/resume",
                   {'key': f"demo-resume-{uuid.uuid4().hex[:8]}", 'revision': task['revision'], 'action': 'continue'})[1]
    result['steps'].append({'step': 'review_started',
                            'status': resumed['status'],
                            'questions': resumed.get('missing_inputs'),
                            'subgoals': len(resumed.get('subgoals', []))})
    result['task_id'] = task['id']
    result['revision_after_wait'] = resumed['revision']
    result['partial_report_ref'] = (resumed.get('partial_report_refs') or [None])[0]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'phase': 1, 'status': resumed['status'],
                      'questions': resumed.get('missing_inputs')}, ensure_ascii=False))

elif phase == '2':
    # Called AFTER a real server restart against the same SQLite file.
    revision = result['revision_after_wait']
    status, record = call('POST', f"/v1/care-tasks/{result['task_id']}/input", {
        'key': f"demo-input-{uuid.uuid4().hex[:8]}",
        'revision': revision,
        'medications': [{'name': '氨氯地平', 'dose': '5mg', 'schedule': '每日一次'}]})
    result['steps'].append({'step': 'fact_supplemented_after_restart', 'status': status,
                            'applied': record.get('applied_medications')})
    status, task = call('POST', f"/v1/care-tasks/{result['task_id']}/resume",
                        {'key': f"demo-resume2-{uuid.uuid4().hex[:8]}",
                         'revision': revision + 1, 'action': 'continue'})
    investigation = task.get('investigation') or {}
    result['steps'].append({'step': 'incremental_recheck_after_restart',
                            'status': task['status'],
                            'termination': investigation.get('termination_reason'),
                            'invalidations': investigation.get('invalidations'),
                            'queries': investigation.get('queries'),
                            'evidence_refs': investigation.get('evidence_refs'),
                            'budget': task.get('budget')})
    result['final_status'] = task['status']
    result['budget'] = task.get('budget')
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'phase': 2, 'status': task['status'],
                      'termination': investigation.get('termination_reason')}, ensure_ascii=False))
