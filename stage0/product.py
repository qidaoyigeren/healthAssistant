"""Patient-scoped material reconciliation. Candidates are never current facts.

All commands use BEGIN IMMEDIATE and the existing domain projection. A receipt,
the domain effects, and the workflow item commit together (including on restart).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any

from .memory import utc_now
from .normalize import load_mapping

SCOPE = 'local-demo'
FIELDS = ('name', 'dose', 'unit', 'schedule', 'date', 'subject', 'route', 'form', 'strength')
TEMPLATE = 'name,dose,unit,schedule,date,subject,route,form,strength\n氨氯地平,5,mg,每日一次,2026-09-07,local-demo,口服,片,5mg\n'


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256((value if isinstance(value, bytes) else value.encode('utf-8'))).hexdigest()


class ProductError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class ProductStore:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.connection
        self.fault_hook = None
        self.names = {}
        for row in load_mapping():
            for name in [row['generic_cn'], *row.get('brands', []), *row.get('aliases', [])]:
                self.names[name.strip().lower()] = row
        from .harness.progress import ProgressEventStore
        ProgressEventStore(self.db, memory._lock)
        with memory._lock, self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS product_objects (
                    object_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS product_objects_kind ON product_objects(scope_id,kind);
            ''')
            self.db.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('product_schema_version','1')")

    @contextmanager
    def transaction(self):
        with self.memory._lock:
            if self.db.in_transaction:
                raise RuntimeError('product commands require an owned transaction')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def revisions(self):
        return {key: self.memory.scope_revision(key) for key in ('medications', 'semantic')}

    def get(self, object_id, kind=None):
        if not isinstance(object_id, str):
            raise ProductError('记录标识无效')
        with self.memory._lock:
            row = self.db.execute('SELECT * FROM product_objects WHERE object_id=? AND scope_id=?', (object_id, SCOPE)).fetchone()
        if not row or (kind and row['kind'] != kind):
            raise ProductError('记录不存在或不属于当前患者', 404)
        return json.loads(row['body_json'])

    def save(self, kind, item):
        self.db.execute('INSERT INTO product_objects VALUES(?,?,?,?,?) ON CONFLICT(object_id) DO UPDATE SET body_json=excluded.body_json',
                        (item['id'], SCOPE, kind, packed(item), item.get('created_at', utc_now())))

    def objects(self, kind):
        with self.memory._lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT body_json FROM product_objects WHERE scope_id=? AND kind=? ORDER BY created_at DESC', (SCOPE, kind))]

    def command(self, key, payload, execute):
        if not isinstance(key, str) or not key.strip() or len(key) > 160:
            raise ProductError('请提供有效的提交标识')
        operation_id, input_hash = f'product:{key}', digest(packed(payload))
        with self.transaction():
            prior = self.db.execute('SELECT * FROM operation_receipts WHERE scope_id=? AND operation_id=?', (SCOPE, operation_id)).fetchone()
            if prior:
                if prior['input_hash'] != input_hash:
                    raise ProductError('同一提交标识不能用于不同内容', 409)
                return json.loads(prior['result_json'])
            result = execute()
            result['receipt_id'] = operation_id
            now = utc_now()
            self.db.execute('''INSERT INTO operation_receipts(scope_id,operation_id,event_id,run_id,operation_type,input_hash,status,result_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'succeeded',?,?,?)''',
                (SCOPE, operation_id, key, key, payload['type'], input_hash, packed(result), now, now))
            self.memory._audit(payload['type'], 'product', None, {'operation_id': operation_id, 'object_id': result.get('id')}, 'caregiver')
            return result

    def parse_csv(self, text):
        if not isinstance(text, str):
            raise ProductError('CSV 内容必须为文本')
        if len(text.encode('utf-8')) > 512_000:
            raise ProductError('CSV 超过 500 KB 限制')
        reader = csv.reader(io.StringIO(text.lstrip('\ufeff')), strict=True)
        try:
            header = next(reader)
            if len(set(header)) != len(header) or not set(FIELDS[:6]).issubset(header):
                raise ProductError('第 1 行缺少模板字段或存在重复字段')
            candidates = []
            while True:
                line = reader.line_num + 1
                row = next(reader, None)
                if row is None:
                    break
                if not any(row):
                    continue
                if len(row) != len(header):
                    raise ProductError(f'第 {line} 行列数不匹配，请按模板修正')
                values = dict(zip(header, row))
                candidates.append({'fields': {f: values.get(f, '').strip() or None for f in FIELDS},
                    'locations': {f: {'line': line, 'end_line': reader.line_num, 'column': header.index(f) + 1,
                                      'coordinate_system': 'csv_field'} for f in FIELDS if f in header},
                    'original_fields': dict(values), 'corrections': []})
                if len(candidates) > 200:
                    raise ProductError('最多支持 200 行药品')
        except (csv.Error, StopIteration) as exc:
            raise ProductError(f'CSV 解析失败，第 {reader.line_num} 行：{exc}') from exc
        if not candidates:
            raise ProductError('材料中没有药品行')
        return candidates

    def import_csv(self, key, text):
        if not isinstance(text, str):
            raise ProductError('CSV 内容必须为文本')
        def execute():
            return self.stage_candidates(text.encode('utf-8'), self.parse_csv(text), 'csv-v1', 'text/csv')
        return self.command(key, {'type': 'material_import', 'text': text}, execute)

    def stage_candidates(self, raw, candidates, parser_version, mime, extra=None):
        # Caller owns the transaction; also used by the OCR adapter.
        sha = digest(raw)
        document_id = f'document:{sha}'
        if not self.db.execute('SELECT 1 FROM product_objects WHERE object_id=?', (document_id,)).fetchone():
            import base64
            self.save('document', {'id': document_id, 'scope_id': SCOPE, 'sha256': sha, 'mime': mime,
                'raw_base64': base64.b64encode(raw).decode(), 'created_at': utc_now(), **(extra or {})})
        case_id = f'case:{uuid.uuid4().hex}'
        case = {'id': case_id, 'case_id': case_id, 'scope_id': SCOPE, 'subject_id': SCOPE,
                'document_id': document_id, 'parser_version': parser_version, 'created_at': utc_now(),
                'base_revision': self.revisions(), 'status': 'open', 'items': [], 'decision_log': []}
        for candidate in candidates:
            case['items'].append({'item_id': uuid.uuid4().hex, 'candidate': candidate, 'status': 'pending', 'receipt_id': None})
        self.recompute(case)
        self.save('case', case)
        self.memory._bump_scope_tx('materials')
        return case

    def validate(self, candidate):
        f = candidate['fields']
        issues = []
        if candidate.get('ocr_reviewed') is False:
            issues.append('请对照原件核实 OCR 字段，尤其小数点、单位和患者归属')
        if f.get('subject') != SCOPE:
            issues.append('请核实材料属于当前患者并补充主体')
        if not f.get('name'):
            issues.append('缺少药名')
        elif f['name'].lower() not in self.names and not candidate.get('name_confirmed'):
            issues.append('药名未精确匹配，请按原文核实名称')
        if not f.get('dose') or not re.fullmatch(r'\d+(?:\.\d+)?', f['dose']):
            issues.append('剂量数字不明确')
        if not f.get('unit'):
            issues.append('缺少剂量单位')
        if not f.get('schedule'):
            issues.append('缺少频次')
        try:
            date.fromisoformat(f.get('date') or '')
        except ValueError:
            issues.append('缺少有效日期（YYYY-MM-DD）')
        return issues

    def recompute(self, case):
        meds = self.memory.current_medications()
        case['items'] = [i for i in case['items'] if i.get('candidate') is not None or i['status'] != 'pending']
        seen = set()
        matched_refs = set()
        for item in case['items']:
            candidate = item.get('candidate')
            if not candidate:
                continue
            f = candidate['fields']
            name = (f.get('name') or '').lower()
            exact = [m for m in meds if m['display_name'].lower() == name]
            alias = self.names.get(name)
            identity = alias['generic_cn'] if alias else name
            related = [m for m in meds if alias and self.names.get(m['display_name'].lower(), {}).get('generic_cn') == alias['generic_cn']]
            target = candidate.get('target_ref')
            if target:
                exact = [m for m in meds if m['ref'] == target]
            matches = exact or related
            matched_refs.update(m['ref'] for m in matches)
            if item['status'] != 'pending':
                continue
            item['issues'] = self.validate(candidate)
            item['old_fact_refs'] = [m['ref'] for m in matches]
            item['current'] = matches
            item['kind'] = 'new'
            if target and not exact:
                item['issues'].append('选中的旧记录已变化，请重新选择')
            if item['issues']:
                item['kind'] = 'unresolved'
            elif identity in seen or (matches and not exact) or len(exact) > 1:
                item['kind'] = 'possible_duplicate'
            elif exact:
                current = exact[0]
                # No ingredient-based formulation/unit conversion is inferred.
                item['kind'] = 'same' if (current['dose'] == f"{f['dose']}{f['unit']}" and current['schedule'] == f['schedule'] and (current['route'] or '') == (f.get('route') or '')) else 'changed'
                if f.get('strength') or f.get('form'):
                    if not candidate.get('presentation_confirmed'):
                        item['issues'].append('请核实剂型、规格与所选记录一致')
            seen.add(identity)
        for med in meds:
            if med['ref'] not in matched_refs:
                stable_id = digest(case['id'] + med['ref'])[:32]
                if not any(i['item_id'] == stable_id for i in case['items']):
                    case['items'].append({'item_id': stable_id, 'candidate': None, 'kind': 'not_listed', 'status': 'pending',
                        'current': [med], 'old_fact_refs': [med['ref']], 'issues': ['材料未列出不代表已停用'], 'receipt_id': None})
        case['status'] = 'completed' if all(i['status'] != 'pending' for i in case['items']) else ('partial' if any(i['status'] != 'pending' for i in case['items']) else 'open')

    def case(self, case_id):
        with self.memory._lock:
            case = self.get(case_id, 'case')
            case['stale'] = case['base_revision'] != self.revisions()
            for item in case['items']:
                if item.get('safety_check'):
                    run = self.memory.workflow_run_get(item['safety_check']['run_id'])
                    item['safety_check']['status'] = run['status'] if run else 'unavailable'
            return case

    def refresh(self, case_id):
        with self.transaction():
            case = self.get(case_id, 'case')
            case['base_revision'] = self.revisions()
            # Explicit refresh removes confirmations tied to old facts.
            for item in case['items']:
                if item['status'] == 'pending' and item.get('candidate'):
                    item['candidate'].pop('target_ref', None)
                    item['candidate'].pop('presentation_confirmed', None)
            self.recompute(case)
            self.save('case', case)
            return case

    def decide(self, case_id, item_id, key, expected_revision, action, corrections, task_context=None):
        if not isinstance(corrections, dict) or (task_context is not None and not isinstance(task_context, dict)):
            raise ProductError('补充信息与待办标识格式无效')
        payload = {'type': 'reconciliation_decision', 'case_id': case_id, 'item_id': item_id,
                   'expected_revision': expected_revision, 'action': action, 'corrections': corrections, 'task_context': task_context}
        def execute():
            case = self.get(case_id, 'case')
            task = None
            if not task_context and any(t.get('case_id') == case_id and t['status'] not in ('completed', 'cancelled', 'failed') for t in self.objects('care_task')):
                raise ProductError('此材料已有照护待办，请从待办上下文继续，避免绕过累计预算', 409)
            if task_context:
                from .care_tasks import CONTRACTS
                task = self.get(task_context.get('task_id'), 'care_task')
                if task['case_id'] != case_id or task['revision'] != task_context.get('revision') or task['status'] in ('completed', 'cancelled', 'failed'):
                    raise ProductError('待办已变化或已结束，请刷新后继续', 409)
                if task['contract_version'] != CONTRACTS[task['goal_type']]['version'] or task['budget']['spent'] >= task['budget']['limit']:
                    raise ProductError('待办契约不兼容或累计预算已用完', 409)
                task['budget']['spent'] += 1
            if expected_revision != self.revisions() or case['base_revision'] != expected_revision:
                raise ProductError('患者记录已变化，请刷新差异后重新核对', 409)
            item = next((i for i in case['items'] if i['item_id'] == item_id), None)
            if not item or item['status'] != 'pending':
                raise ProductError('此项已处理或不存在，请刷新核对单', 409)
            if action not in ('accept', 'keep', 'correct'):
                raise ProductError('不支持的处理动作')
            candidate = item.get('candidate')
            if corrections:
                if not candidate:
                    raise ProductError('此项没有来源候选')
                if set(corrections) - set(FIELDS) - {'name_confirmed', 'presentation_confirmed', 'target_ref', 'ocr_reviewed'}:
                    raise ProductError('存在不支持的更正字段')
                candidate['corrections'].append({'at': utc_now(), 'before': dict(candidate['fields']), 'changes': corrections})
                for field, value in corrections.items():
                    if field in FIELDS:
                        if value is not None and not isinstance(value, str):
                            raise ProductError('候选字段必须是文本或空白')
                        candidate['fields'][field] = value.strip() or None if value is not None else None
                    else:
                        if field.endswith('_confirmed') or field == 'ocr_reviewed':
                            if type(value) is not bool:
                                raise ProductError('核实选项必须为明确的勾选值')
                        candidate[field] = value
                self.recompute(case)
            result = None
            if action == 'accept':
                if not candidate or item['kind'] == 'not_listed':
                    raise ProductError('材料未列出不能确认成停药；请保留当前记录')
                if item['issues'] or item['kind'] == 'possible_duplicate':
                    raise ProductError('请先补充或核实：' + '；'.join(item['issues'] or ['可能重复，请选择当前记录']))
                f = candidate['fields']
                target = item['current'][0] if item['current'] else None
                from .turn_budget import initial_budget
                child_budget = initial_budget()
                if task:
                    resources = task.get('resource_budget')
                    if not resources:
                        raise ProductError('旧任务缺少累计资源预算，请建立新的待办', 409)
                    for counter, maximum, limit in [('tokens_reserved', 'token_limit', 'token_budget'), ('calls_reserved', 'call_limit', 'call_budget')]:
                        remaining = resources[maximum] - resources[counter]
                        if remaining <= 0:
                            raise ProductError('此待办累计检查预算已用完；此前保存结果保留', 409)
                        child_budget[limit] = min(child_budget[limit], remaining)
                        resources[counter] += child_budget[limit]
                result = self.memory._apply_medication_change_tx(action='dose_change' if target else 'add',
                    name=target['display_name'] if target else f['name'], ingredients=self.names.get(f['name'].lower(), {}).get('ingredients', []),
                    session_id=case_id, turn_id=key, source='caregiver', occurred_at=f['date'],
                    dose=f"{f['dose']}{f['unit']}", schedule=f['schedule'], route=f.get('route'),
                    source_uri=f"{case['document_id']}#{item_id}")
                if self.fault_hook:
                    self.fault_hook()
                # Enqueue an audited safety-only event in the SAME transaction.
                # It must never replay a medication mutation against a later state.
                check_key = 'material-check-' + digest(key)[:32]
                run_id, event_id = uuid.uuid4().hex, uuid.uuid4().hex
                event = {'event_type': 'medication_recheck', 'text': '核对已确认材料对应的当前用药风险',
                         'payload': {'medication': target['display_name'] if target else f['name'], 'action': 'add',
                                     'material_case_id': case_id, 'material_item_id': item_id},
                         'source': 'caregiver', 'occurred_at': None, 'session_id': case_id}
                self.memory._accept_api_event_tx(idempotency_key=check_key, request_hash=digest(packed(event)),
                    event_id=event_id, run_id=run_id, task_payload={'event': event, 'session_id': case_id, 'turn_id': run_id,
                    'idempotency_key': check_key, 'event_key': f'api:{check_key}', 'event_id': event_id, 'run_id': run_id, 'trace_id': run_id})
                if task:
                    self.db.execute('UPDATE workflow_runs SET budget_json=? WHERE run_id=?', (packed(child_budget), run_id))
                    task['resource_budget']['child_run_ids'].append(run_id)
                item['safety_check'] = {'run_id': run_id, 'event_key': check_key, 'status_url': f'/v1/events/{check_key}'}
            if action != 'correct':
                item['status'] = 'accepted' if action == 'accept' else 'kept'
                item['receipt_id'] = f'product:{key}'
                item['result'] = result
            case['decision_log'].append({'item_id': item_id, 'action': action, 'at': utc_now(), 'receipt_id': f'product:{key}'})
            case['base_revision'] = self.revisions()
            self.recompute(case)
            self.save('case', case)
            if task:
                from .care_tasks import CareTasks
                task['runs'].append({'run_id': key, 'runner': 'reconciliation-supplement-v1', 'item_id': item_id, 'event_id': key, 'at': utc_now()})
                task['revision'] += 1
                CareTasks(self)._execute(task)
                self.save('care_task', task)
            self.memory._bump_scope_tx('materials')
            return case
        return self.command(key, payload, execute)


def register_product_routes(app, store, principal, authorize_scope, require_role, api_error):
    from fastapi import Request
    # Make annotation resolvable under future annotations for FastAPI.
    globals()['Request'] = Request
    product = ProductStore(store)
    app.state.product = product

    def access(request, write=False):
        p = principal(request)
        authorize_scope(p, SCOPE)
        if write:
            require_role(p, 'caregiver', 'ops')
            import os
            if os.getenv('STAGE0_PRODUCT_WRITES', '1') == '0':
                raise api_error(503, 'product_read_only', 'validation', '新产品功能当前处于只读模式，已有材料和结果仍可查看')

    def invoke(fn):
        try:
            return fn()
        except ProductError as exc:
            raise api_error(exc.status, 'product_validation', 'validation', str(exc)) from exc

    @app.get('/v1/materials/template')
    def template(request: Request):
        access(request)
        return {'csv': TEMPLATE, 'subject_id': SCOPE, 'parser_version': 'csv-v1'}

    @app.get('/v1/reconciliations')
    def cases(request: Request):
        access(request)
        return {'items': product.objects('case')}

    @app.post('/v1/materials/csv')
    def import_csv(request: Request, body: dict):
        access(request, True)
        return invoke(lambda: product.import_csv(body.get('key'), body.get('text', '')))

    @app.get('/v1/reconciliations/{case_id}')
    def get_case(case_id: str, request: Request):
        access(request)
        return invoke(lambda: product.case(case_id))

    @app.post('/v1/reconciliations/{case_id}/refresh')
    def refresh(case_id: str, request: Request):
        access(request, True)
        return invoke(lambda: product.refresh(case_id))

    @app.post('/v1/reconciliations/{case_id}/items/{item_id}')
    def decide(case_id: str, item_id: str, request: Request, body: dict):
        access(request, True)
        return invoke(lambda: product.decide(case_id, item_id, body.get('key'), body.get('expected_revision'), body.get('action'), body.get('corrections', {}), body.get('task_context')))

    from .care_tasks import register_task_routes
    register_task_routes(app, product, access, invoke)
    from .document_parser import register_document_routes
    register_document_routes(app, product, access, invoke)
    from .evidence_quality import register_quality_routes
    from .harness.evidence import EvidenceStore
    register_quality_routes(app, product, access, invoke, EvidenceStore(store.connection, store._lock), principal, require_role)
