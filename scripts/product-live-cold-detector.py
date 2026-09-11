"""Supplemental cold-cache component test with real retrieval/extraction/KEGG."""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
os.environ.update({'DDI_ENGINE_PAIR_INDEX_PATH': str(out / 'pairs.json'),
                   'DDI_ENGINE_KEGG_CACHE_PATH': str(out / 'kegg.json'),
                   'DDI_ENGINE_FALLBACK_CACHE_PATH': str(out / 'fallback.json'),
                   'DDI_ENGINE_ENABLE_LLM': '1', 'DDI_ENGINE_ENABLE_RAG': '1', 'DDI_ENGINE_LIVE_KEGG': '1',
                   'AGENT_TURN_BUDGET_SECONDS': '180', 'AGENT_TURN_CALL_BUDGET': '6',
                   'AGENT_TURN_TOKEN_BUDGET': '40000'})
from stage0 import ddi_engine as detector
from stage0.memory import MemoryStore
from stage0.turn_budget import budget_scope

pair = ['甲硝唑', '华法林']
ingredients = detector.normalize_medications(pair)
assert not detector._evidence_index().get(detector._pair_name_key(*ingredients)), 'pair must have no warm extraction evidence'
store = MemoryStore(out / 'memory.db')
run_id = 'live-cold-detector'
store.workflow_run_start(run_id=run_id, graph_version='component-cold-detector')
started = time.perf_counter()
try:
    with budget_scope(store, run_id):
        warnings = detector.detect(pair)
    attempts = [dict(r) for r in store.connection.execute('SELECT * FROM llm_attempts')]
    result = {'track':'supplemental_component_live_cold_cache', 'medications':pair,
              'seconds':time.perf_counter()-started, 'warnings':warnings,
              'research':detector.EVIDENCE_TRACE.get(), 'attempts':attempts,
              'budget':store.workflow_run_get(run_id)['budget']}
    result['checks'] = {'fresh_extractor_call':any(r['kind']=='ddi_extractor' and r['status']=='actual' for r in attempts),
                        'real_kegg_http':any(r['kind']=='kegg_http' and r['status']=='actual' for r in attempts),
                        'grounded_warning':any(w.get('source_text') and w.get('source_url') for w in warnings),
                        'complete_accounting':not result['budget'].get('usage_unknown')}
    result['status'] = ('pass' if all(result['checks'].values()) else
                        'degraded' if result['checks']['grounded_warning'] else 'fail')
    store.workflow_run_update(run_id, status='succeeded' if result['status']=='pass' else result['status'].replace('fail','failed'))
    (out / 'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':result['checks'],'seconds':result['seconds']},ensure_ascii=False),flush=True)
finally:
    store.close()
