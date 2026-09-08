import json
from pathlib import Path
root = Path('stage0/product_evals/tasks/dev')
header = 'name,dose,unit,schedule,date,subject\n'
rows = [
    ('normal', header + '氨氯地平,5,mg,每日一次,2026-09-08,local-demo\n', ['new']),
    ('brand', header + '络活喜,5,mg,每日一次,2026-09-08,local-demo\n', ['new']),
    ('unknown', header + '合成不明药,5,mg,每日一次,2026-09-08,local-demo\n', ['unresolved']),
    ('wrong_subject', header + '氨氯地平,5,mg,每日一次,2026-09-08,他人\n', ['unresolved']),
    ('bad_date', header + '氨氯地平,5,mg,每日一次,未知,local-demo\n', ['unresolved']),
    ('missing_unit', header + '氨氯地平,5,,每日一次,2026-09-08,local-demo\n', ['unresolved']),
    ('decimal_unclear', header + '氨氯地平,0.?,mg,每日一次,2026-09-08,local-demo\n', ['unresolved']),
    ('missing_name', header + ',5,mg,每日一次,2026-09-08,local-demo\n', ['unresolved']),
    ('empty', header, None),
    ('missing_header', 'name,dose\n氨氯地平,5\n', None),
]
tasks = []
for label, text, kinds in rows:
    tasks.append(('P2', label, {'action':'import_reconciliation','text':text}, {'http_status': 200 if kinds else 422, **({'kinds': kinds} if kinds else {})}))
for goal, action in [('current_medications','continue'),('visit_summary','continue'),('current_medications','cancel'),('visit_summary','cancel')]:
    tasks.append(('P3',goal+'_'+action,{'action':'care_task_contract','goal_type':goal,'task_action':action},{'status':'completed' if action=='continue' else 'cancelled'}))
for label, quote, expected in [('real_support','药甲与药乙合用增加出血风险','supported'),('negation','药甲与药乙未发现相互作用','contradicted'),('unrelated','药甲和药乙属于常见药物','insufficient'),('population','儿童使用药甲与药乙合用增加出血风险','insufficient')]:
    tasks.append(('P4',label,{'action':'claim_support','quote':quote},{'status':expected}))
for i,(phase,label,call,expected) in enumerate(tasks,13):
    task={'task_id':f'dev-product-{i:03d}','phase':phase,'capability':label,'group':f'synthetic-{phase.lower()}-contract-v1',
          'title':label,'inputs':{'seed':{},'call':call},'expected':{'type':'db_state',**expected},'requires':['fixture_db']}
    (root/f'dev-product-{i:03d}-{label}.task.json').write_text(json.dumps(task,ensure_ascii=False,indent=2),'utf-8')
print(len(tasks))
