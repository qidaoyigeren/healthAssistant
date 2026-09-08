"""Opt-in live-provider acceptance host; synthetic inputs and isolated storage.

This host uses create_app's real default agent, detector and retriever. It does
not inject model/tool responses. --serve makes paid calls when events arrive.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def report(out):
    db = sqlite3.connect(f'{(out / "memory.db").as_uri()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    data = {'generated_at': datetime.now(timezone.utc).isoformat(), 'track': 'live_provider_synthetic_inputs'}
    for table in ('workflow_runs', 'llm_attempts', 'call_spans', 'audit_log', 'evidence_records', 'product_objects'):
        if table in tables:
            rows = [dict(r) for r in db.execute(f'SELECT * FROM {table}')]
            write(out / f'{table}.json', rows)
            data[table + '_count'] = len(rows)
    attempts = [dict(r) for r in db.execute('SELECT * FROM llm_attempts')]
    data['model_calls'] = {'attempts': len(attempts), 'by_kind': {}, 'by_status': {},
                           'actual_tokens': sum(r.get('usage_tokens') or 0 for r in attempts),
                           'billing_cost': 'unavailable_no_verified_provider_price'}
    for row in attempts:
        for field in ('kind', 'status'):
            bucket = data['model_calls']['by_' + field]
            bucket[row[field]] = bucket.get(row[field], 0) + 1
    data['runs'] = [{'run_id': r['run_id'], 'status': r['status'], 'graph_version': r['graph_version'],
                     'budget': json.loads(r['budget_json'] or '{}')} for r in db.execute('SELECT * FROM workflow_runs')]
    data['source_sha256'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (ROOT / 'stage0').rglob('*.py')}
    db.close()
    write(out / 'live-summary.json', data)
    print(json.dumps(data['model_calls'], ensure_ascii=False), flush=True)


def serve(out, port, resume):
    if (out / 'memory.db').exists() and not resume:
        raise SystemExit('Existing acceptance database; use a new --out or explicit --resume')
    from stage0 import extract_ddi
    config = extract_ddi.resolve_llm_config()
    settings = {'AGENT_LLM_PLANNER': '1', 'AGENT_GRAPH_RUNNER': '1',
                'STAGE0_REVIEW_ENABLED': '0', 'DDI_ENGINE_ENABLE_LLM': '1',
                'DDI_ENGINE_ENABLE_RAG': '1', 'DDI_ENGINE_LIVE_KEGG': '1',
                'AGENT_TURN_BUDGET_SECONDS': '180', 'AGENT_TURN_TOKEN_BUDGET': '100000',
                'AGENT_TURN_CALL_BUDGET': '12', 'TOKENDANCE_TIMEOUT_SECONDS': '45',
                'LLM_TIMEOUT_SECONDS': '45', 'STAGE0_OTEL_EXPORT': '0'}
    os.environ.update(settings)
    for env, filename in [('DDI_ENGINE_PAIR_INDEX_PATH', 'ddi_pair_index.json'),
                          ('DDI_ENGINE_KEGG_CACHE_PATH', 'kegg_ddi_cache.json'),
                          ('DDI_ENGINE_FALLBACK_CACHE_PATH', 'ddi_fallback_cache.json')]:
        target = out / filename
        original = ROOT / 'stage0/data' / filename
        if not target.exists() and original.exists():
            shutil.copy2(original, target)
        os.environ[env] = str(target)
    from stage0.server import create_app
    import uvicorn
    app = create_app(db_path=out / 'memory.db', checkpoint_path=str(out / 'checkpoints.db'), auth_mode='local-demo')
    identity = {'isolated': True, 'live_provider': True, 'synthetic_inputs': True,
                'response_substitutes': [], 'provider': config['provider'], 'model': config['model'],
                'settings': settings, 'corpus': 'existing local corpus; KEGG live on cache miss'}
    write(out / 'environment.json', identity)
    @app.get('/v1/acceptance/live-identity')
    def live_identity():
        return identity
    print(json.dumps(identity, ensure_ascii=False), flush=True)
    try:
        uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')
    finally:
        app.state.worker.stop()
        app.state.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8012)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.serve:
        serve(out, args.port, args.resume)
    else:
        report(out)


if __name__ == '__main__':
    main()
