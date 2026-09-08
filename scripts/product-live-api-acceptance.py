"""Run bounded real HTTP events against the opt-in isolated live host."""
import argparse
import json
import time
from pathlib import Path
import urllib.request
import urllib.error

parser = argparse.ArgumentParser()
parser.add_argument('--origin', default='http://127.0.0.1:8012')
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--phase', choices=['seed', 'question'], default='seed')
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)


def request(path, body=None, key=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Idempotency-Key'] = key
    req = urllib.request.Request(args.origin + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc
    return response.status, json.load(response)


identity = request('/v1/acceptance/live-identity')[1]
assert identity.get('live_provider') and identity.get('isolated') and identity.get('response_substitutes') == []
scenarios = ([('live-seed-amlo', 'medication_change', '新增氨氯地平', {'action':'add','medication':'氨氯地平','dose':'5mg'}),
              ('live-seed-clari', 'medication_change', '新增克拉霉素', {'action':'add','medication':'克拉霉素','dose':'250mg'})]
             if args.phase == 'seed' else
             [('live-evidence-question', 'user_message', '请检索现有药品说明书，解释氨氯地平与克拉霉素合用的相互作用依据，引用原文并说明证据缺口。不要提出调整剂量或停药建议。', {})])
results = []
for key, kind, text, payload in scenarios:
    body = {'event_type':kind, 'text':text, 'payload':payload, 'session_id':'live-acceptance', 'source':'caregiver', 'occurred_at':None}
    code, accepted = request('/v1/events', body, key)
    print(json.dumps({'key':key, 'accepted_http':code, 'accepted':accepted}, ensure_ascii=False), flush=True)
    if code not in (200, 202):
        raise RuntimeError(f'Event acceptance failed: {code}')
    start = time.monotonic()
    for attempt in range(150):
        code, result = request('/v1/events/' + key)
        if result.get('status') in ('committed', 'failed'):
            break
        if attempt % 10 == 0:
            print(json.dumps({'key':key,'waiting':result.get('status'),'seconds':round(time.monotonic()-start)}, ensure_ascii=False), flush=True)
        time.sleep(2)
    record = {'key':key, 'http_status':code, 'seconds':round(time.monotonic()-start,3), 'response':result}
    (args.out / (key + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    results.append(record)
    print(json.dumps({'key':key,'status':result.get('status'),'run_status':result.get('result',{}).get('run_status'),'seconds':record['seconds']}, ensure_ascii=False), flush=True)
    if result.get('status') != 'committed':
        raise RuntimeError(f'Event did not commit: {key}')
(args.out / (args.phase + '-api.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
