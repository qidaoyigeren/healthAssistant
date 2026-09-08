"""Versioned, deterministic care-task contracts and immutable visit artifacts."""
from __future__ import annotations

import html
import uuid
from datetime import datetime

from .memory import utc_now
from .product import ProductError, SCOPE, packed

CONTRACTS = {
    'reconcile_material': {'version': 2, 'outputs': ['reconciliation', 'safety_checks'], 'tools': ['candidate_correct', 'reconciliation_read'], 'max_steps': 64},
    'current_medications': {'version': 1, 'outputs': ['medication_snapshot'], 'tools': ['memory_read'], 'max_steps': 1},
    'visit_summary': {'version': 1, 'outputs': ['visit_artifact'], 'tools': ['memory_read', 'summary_render'], 'max_steps': 1},
}


class CareTasks:
    def __init__(self, product):
        self.p = product

    def create(self, key, goal_type, case_id=None, due_at=None, budget=None):
        if goal_type not in CONTRACTS:
            raise ProductError('请选择材料核对、当前药单或就诊摘要；开放问题请使用照护助手')
        contract = CONTRACTS[goal_type]
        if budget is not None and (type(budget) is not int or not 1 <= budget <= contract['max_steps']):
            raise ProductError('任务处理次数超出契约范围')
        if due_at:
            try:
                if datetime.fromisoformat(due_at).tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ProductError('待办日期必须包含时区')
        def execute():
            if goal_type == 'reconcile_material':
                self.p.get(case_id, 'case')
                existing = [t for t in self.p.objects('care_task') if t.get('case_id') == case_id and t['status'] not in ('completed', 'cancelled', 'failed')]
                if existing:
                    return existing[0]
            task = {'id': f'task:{uuid.uuid4().hex}', 'scope_id': SCOPE, 'subject_id': SCOPE, 'goal_type': goal_type,
                'contract_version': contract['version'], 'base_revision': self.p.revisions(), 'required_outputs': contract['outputs'],
                'missing_inputs': [], 'waiting_reason': None, 'result_refs': [], 'due_at': due_at,
                'status': 'ready', 'revision': 1, 'case_id': case_id, 'runs': [], 'created_at': utc_now(),
                'budget': {'limit': budget or contract['max_steps'], 'spent': 0, 'unit': 'deterministic_steps'}, 'effects_retained': True}
            task['resource_budget'] = {'token_limit': 1_000_000, 'call_limit': 256,
                'tokens_reserved': 0, 'calls_reserved': 0, 'tokens_actual': 0, 'calls_actual': 0, 'child_run_ids': []}
            self.p.save('care_task', task)
            return task
        return self.p.command(key, {'type': 'care_task_create', 'goal_type': goal_type, 'case_id': case_id, 'due_at': due_at, 'budget': budget}, execute)

    def resume(self, task_id, key, revision, action='continue'):
        def execute():
            task = self.p.get(task_id, 'care_task')
            if task['revision'] != revision:
                raise ProductError('待办已被其他操作更新，请刷新', 409)
            if action == 'cancel':
                if task['status'] == 'completed':
                    raise ProductError('已完成待办不能取消', 409)
                task['status'] = 'cancelled'
                task['waiting_reason'] = '已取消后续处理；此前保存的记录仍然保留'
                for run_id in task.get('resource_budget', {}).get('child_run_ids', []):
                    run = self.p.memory.workflow_run_get(run_id)
                    if not run or run['status'] in ('succeeded', 'degraded', 'failed', 'cancelled'):
                        continue
                    # Persist into the EXISTING cancellation protocol before
                    # commit; a crash cannot leave a cancelled task scheduling
                    # new work on its owned child runs.
                    self.p.db.execute("INSERT OR IGNORE INTO run_cancel_requests(run_id,state,requested_by,reason,requested_at) VALUES(?,'requested','caregiver','care_task_cancelled',?)", (run_id, utc_now()))
                    self.p.db.execute("UPDATE workflow_runs SET status='cancelled',updated_at=? WHERE run_id=?", (utc_now(), run_id))
                    self.p.db.execute("UPDATE resume_tasks SET status='cancelled' WHERE run_id=? AND status='pending'", (run_id,))
                    self.p.db.execute("UPDATE review_cases SET status='cancelled',revision=revision+1 WHERE run_id=? AND status IN ('open','assigned')", (run_id,))
            else:
                if action != 'continue':
                    raise ProductError('任务状态由完成校验决定，不能直接指定完成')
                if task['status'] in ('cancelled', 'completed', 'failed'):
                    raise ProductError('此待办已结束', 409)
                contract = CONTRACTS[task['goal_type']]
                if task['contract_version'] != contract['version']:
                    raise ProductError('任务契约已更新，请创建新的待办', 409)
                if task['budget']['spent'] >= task['budget']['limit']:
                    task['status'] = 'failed'
                    task['waiting_reason'] = '已达到此待办的处理次数上限；已保存记录保留'
                else:
                    task['budget']['spent'] += 1
                    task['status'] = 'running'
                    task['runs'].append({'run_id': key, 'runner': 'deterministic-care-v1', 'started_at': utc_now(), 'input_revision': self.p.revisions()})
                    self._execute(task)
                    task['runs'][-1].update(status=task['status'], finished_at=utc_now())
            task['revision'] += 1
            self.p.save('care_task', task)
            return task
        result = self.p.command(key, {'type': 'care_task_resume', 'task_id': task_id, 'revision': revision, 'action': action}, execute)
        if action == 'cancel':
            from .harness.progress import cancel_event_for
            for run_id in result.get('resource_budget', {}).get('child_run_ids', []):
                cancel_event_for(run_id).set()
        return result

    def _execute(self, task):
        task['missing_inputs'] = []
        resources = task.get('resource_budget')
        if resources:
            owned = [self.p.memory.workflow_run_get(r) for r in resources['child_run_ids']]
            resources['tokens_actual'] = sum((r or {}).get('budget', {}).get('tokens_actual', 0) for r in owned)
            resources['calls_actual'] = sum((r or {}).get('budget', {}).get('calls_attempted', 0) for r in owned)
            resources['usage_unknown'] = any((r or {}).get('budget', {}).get('usage_unknown', False) for r in owned)
        if task['goal_type'] == 'reconcile_material':
            case = self.p.get(task['case_id'], 'case')
            if case['base_revision'] != self.p.revisions():
                task['status'] = 'waiting_input'
                task['waiting_reason'] = '患者记录已变化，请进入材料核对刷新差异'
                task['missing_inputs'] = ['重新核对当前患者记录']
                return
            pending = [i for i in case['items'] if i['status'] == 'pending']
            task['missing_inputs'] = [{'item_id': i['item_id'], 'issues': i['issues'] or ['请确认或保留此项']} for i in pending]
            if pending:
                task['status'] = 'waiting_input'
                task['waiting_reason'] = f'还有 {len(pending)} 项材料记录需要核对或补充'
                return
            checks = [i['safety_check'] for i in case['items'] if i.get('safety_check')]
            task['workflow_run_ids'] = [c['run_id'] for c in checks]
            runs = [self.p.memory.workflow_run_get(c['run_id']) for c in checks]
            if any(r and r['status'] == 'waiting_review' for r in runs):
                task['status'] = 'waiting_review'
                task['waiting_reason'] = '相关风险检查正在等待专业审核，请在风险与证据页面查看；到期不会自动批准'
                task['review_refs'] = [r['id'] for r in self.p.memory.review_cases() if r['run_id'] in task['workflow_run_ids']]
                return
            if any(r and r['status'] in ('failed', 'cancelled', 'degraded') for r in runs):
                task['status'] = 'failed'
                task['waiting_reason'] = '相关检查未完成；已保存记录仍保留，请查看检查结果'
                return
            if any(not r or r['status'] != 'succeeded' for r in runs):
                task['status'] = 'running'
                task['waiting_reason'] = '记录已保存，后台正在检查风险；稍后可继续查看结果'
                return
            task['result_refs'] = [case['id']]
        elif task['goal_type'] == 'current_medications':
            snapshot = {'id': f'medication-snapshot:{uuid.uuid4().hex}', 'created_at': utc_now(),
                'patient_revision': self.p.revisions(), 'medications': self.p.memory.current_medications()}
            self.p.save('medication_snapshot', snapshot)
            self.p.memory._audit('read_current_medications', 'care_task', None, {'task_id': task['id'], 'refs': [m['ref'] for m in snapshot['medications']]}, 'caregiver')
            task['result_refs'] = [snapshot['id']]
        elif task['goal_type'] == 'visit_summary':
            artifact = self.summary_tx()
            task['result_refs'] = [artifact['id']]
        # Completion is code-controlled and requires each output persisted.
        if not task['result_refs'] or any(not self.p.get(ref) for ref in task['result_refs']):
            raise ProductError('任务产物尚未保存，不能完成', 409)
        task['status'] = 'completed'
        task['waiting_reason'] = None
        task['base_revision'] = self.p.revisions()

    def summary_tx(self):
        def safe(value):
            # Escape raw HTML and Markdown link syntax from user material.
            return html.escape(str(value or '未记录'), quote=False).replace('[', '&#91;').replace(']', '&#93;').replace('\n', ' ')
        def reported(value):
            if isinstance(value, dict):
                if 'value' in value:
                    return f"{safe(value['value'])} {safe(value.get('unit')) if value.get('unit') else ''}".strip()
                return '；'.join(f'{safe(k)}：{reported(v)}' for k, v in value.items())
            if isinstance(value, list):
                return '、'.join(reported(v) for v in value)
            return safe(value)
        now = utc_now()
        meds = self.p.memory.current_medications()
        facts = self.p.memory.current_semantic()
        episodes = [self.p.memory._episode_row(r) for r in self.p.db.execute('SELECT * FROM episodic_memory ORDER BY id DESC LIMIT 30')]
        cases = self.p.objects('case')
        lines = ['# 就诊准备摘要', '', f'生成时间：{now}', '', '以下为照护者报告记录，供就诊核对使用。', '', '## 当前报告药单', '']
        refs = []
        for m in meds:
            lines.append(f"- {safe(m['display_name'])} · {safe(m['dose'])} · {safe(m['schedule'])}（来源：{m['ref']}；{safe(m.get('source_uri'))}）")
            refs.append(m['ref'])
        if not meds:
            lines.append('尚无已保存药单。')
        lines += ['', '## 用户报告情况', '']
        for f in facts:
            category = {'age': '年龄', 'allergy': '过敏报告', 'renal_function': '肾功能报告', 'hepatic_function': '肝功能报告', 'chronic_condition': '既往疾病报告'}.get(f['namespace'], f['namespace'])
            verified = {'recorded_as_reported': '按报告记录', 'verified': '已核实来源', 'disputed': '存在未决分歧'}.get(f.get('verification_status'), '核实状态待确认')
            lines.append(f"- {safe(category)}：{reported(f['value'])}（来源：{f['ref']}；{verified}）")
            refs.append(f['ref'])
        if not facts:
            lines.append('尚无已保存的报告情况。')
        lines += ['', '## 近期变化（最近 30 条事件）', '']
        for e in episodes:
            p = e['payload']
            kind = e['event_type']
            title = {'medication_add': '新增用药报告', 'medication_remove': '停用报告', 'medication_dose_change': '更新用药报告',
                     'warning': '风险检查提醒', 'measurement': '测量报告', 'symptom': '症状报告', 'procedure_exposure': '医疗操作报告'}.get(kind, '照护记录')
            if kind.startswith('medication_'):
                description = ' · '.join(safe(p.get(k)) for k in ('name', 'dose', 'schedule') if p.get(k))
            elif kind == 'warning':
                w = p.get('warning') or p
                description = f"{safe(w.get('drug_a'))} / {safe(w.get('drug_b'))}：{safe(w.get('effect') or w.get('source_text'))}"
            else:
                description = safe(p.get('text') or p.get('description') or p.get('name') or '详细报告见来源记录')
            lines.append(f"- {safe(e['occurred_at'])[:10]} · {title}：{description}（来源：{e['ref']}）")
            refs.append(e['ref'])
        lines += ['', '## 未核实问题与材料来源', '']
        for c in cases:
            for item in c['items']:
                if item['status'] == 'pending':
                    lines.append(f"- {safe((item.get('candidate') or {}).get('fields', {}).get('name'))}：{safe('；'.join(item['issues']) or '尚未确认')}（{c['document_id']}#{item['item_id']}）")
                    refs.append(c['document_id'])
        for row in self.p.db.execute("SELECT id,status,kind FROM conclusions WHERE status != 'current' ORDER BY id DESC LIMIT 20"):
            status = {'stale': '依据变化待重查', 'superseded': '已有后续记录', 'rechecked': '历史检查记录'}.get(row['status'], '状态需核实')
            lines.append(f"- 风险记录 {row['id']}：{status}，详情见风险与证据页面。")
        artifact = {'id': f'summary:{uuid.uuid4().hex}', 'created_at': now, 'patient_revision': self.p.revisions(),
                    'material_revision': self.p.memory.scope_revision('materials'),
                    'scope_id': SCOPE, 'format_version': 1, 'markdown': '\n'.join(lines), 'source_refs': sorted(set(refs))}
        self.p.save('summary', artifact)
        return artifact


def register_task_routes(app, product, access, invoke):
    from fastapi import Request
    from fastapi.responses import Response
    globals()['Request'] = Request
    tasks = CareTasks(product)

    @app.get('/v1/care-tasks')
    def list_tasks(request: Request):
        access(request)
        return {'items': product.objects('care_task'), 'notifications': 'in_app_only', 'contracts': CONTRACTS}

    @app.post('/v1/care-tasks')
    def create(request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.create(body.get('key'), body.get('goal_type'), body.get('case_id'), body.get('due_at'), body.get('budget')))

    @app.post('/v1/care-tasks/{task_id}/resume')
    def resume(task_id: str, request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.resume(task_id, body.get('key'), body.get('revision'), body.get('action', 'continue')))

    @app.get('/v1/visit-summaries')
    def summaries(request: Request):
        access(request)
        return {'items': [{**s, 'stale': s['patient_revision'] != product.revisions() or s.get('material_revision') != product.memory.scope_revision('materials')} for s in product.objects('summary')]}

    @app.get('/v1/visit-summaries/{artifact_id}/download')
    def download(artifact_id: str, request: Request, format: str = 'markdown'):
        access(request)
        artifact = invoke(lambda: product.get(artifact_id, 'summary'))
        if format not in ('markdown', 'html'):
            return invoke(lambda: (_ for _ in ()).throw(ProductError('格式必须是 markdown 或 html')))
        content = artifact['markdown']
        if format == 'html':
            blocks = []
            for line in content.splitlines():
                if not line:
                    continue
                tag, text = ('h1', line[2:]) if line.startswith('# ') else (('h2', line[3:]) if line.startswith('## ') else ('p', line))
                blocks.append(f'<{tag}>' + html.escape(html.unescape(text)) + f'</{tag}>')
            content = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>就诊准备摘要</title><style>body{max-width:900px;margin:40px auto;padding:0 24px;font:16px/1.8 sans-serif;color:#183b45;overflow-wrap:anywhere}h1{font-size:26px}h2{font-size:19px;border-bottom:1px solid #ccd9dc;margin-top:26px;break-after:avoid}p{margin:8px 0}</style><body>' + ''.join(blocks) + '</body></html>'
        return Response(content, media_type='text/html' if format == 'html' else 'text/markdown', headers={'Content-Disposition': f'attachment; filename="visit-summary.{"html" if format == "html" else "md"}"', 'X-Content-Type-Options': 'nosniff'})
