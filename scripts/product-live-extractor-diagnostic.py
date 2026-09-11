"""One live diagnostic call: save raw completion fields, never provider secrets."""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stage0 import extract_ddi as extractor
from stage0.memory import MemoryStore
from stage0.turn_budget import budget_scope, completion_call

out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True,exist_ok=True)
store = MemoryStore(out / 'memory.db')
store.workflow_run_start(run_id='diagnostic',graph_version='live-completion-diagnostic')
config = extractor.resolve_llm_config()
text = '本品能增强华法林等抗凝药物的作用。与土霉素合用可干扰甲硝唑清除阴道滴虫的作用。'
try:
    with budget_scope(store,'diagnostic'):
        response = completion_call('ddi_extractor_diagnostic', extractor.create_llm_client(config),
            model=config['model'],messages=[{'role':'system','content':extractor.PROMPT},
                {'role':'user','content':extractor._production_user_content('甲硝唑片',text)}],
            tools=[extractor.TOOL],tool_choice={'type':'function','function':{'name':'record_ddi_triples'}},
            temperature=0,**extractor.llm_completion_options())
    result = {'model':config['model'],'choices':[c.model_dump() for c in response.choices],
              'usage':response.usage.model_dump() if response.usage else None,
              'options':extractor.llm_completion_options()}
    (out / 'completion.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'finish_reason':response.choices[0].finish_reason,'usage':result['usage'],
                      'tool_calls':len(response.choices[0].message.tool_calls or [])},ensure_ascii=False))
finally:
    store.close()
