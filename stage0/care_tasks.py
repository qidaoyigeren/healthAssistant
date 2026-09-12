"""Versioned, deterministic care-task contracts and immutable visit artifacts."""
from __future__ import annotations

import html
import os
import uuid
from datetime import datetime

from .memory import utc_now
from .product import ProductError, SCOPE, packed

# A run that degraded because the model provider never answered is a TRANSIENT
# condition: the bounded rule-based report is real and grounded, it simply was
# not model-planned, and retrying later is worth offering.  Anything else
# (contract, safety, budget, persistent schema failure) is not offered a retry
# because repeating it would not change the outcome.
TRANSIENT_PROVIDER_REASONS = ('provider_error', 'usage_unknown')
# Caregiver-facing copy: name the state in plain words, say what was saved, and
# say what can be done next — never surface an internal reason token.
PROVIDER_OUTAGE_LABEL = '本次未经模型核查：模型服务暂时不可用。以下为依据已保存记录整理的部分结果，可稍后重新核查。'


def provider_outage(degraded_reason: str | None) -> bool:
    return bool(degraded_reason) and any(
        token in degraded_reason for token in TRANSIENT_PROVIDER_REASONS)

CONTRACTS = {
    'reconcile_material': {'version': 2, 'outputs': ['reconciliation', 'safety_checks'], 'tools': ['candidate_correct', 'reconciliation_read'], 'max_steps': 64},
    'current_medications': {'version': 1, 'outputs': ['medication_snapshot'], 'tools': ['memory_read'], 'max_steps': 1},
    'visit_summary': {'version': 1, 'outputs': ['visit_artifact'], 'tools': ['memory_read', 'summary_render'], 'max_steps': 1},
    # A2: bounded open contract — evidence review over EXISTING patient
    # records and materials, producing a bounded report plus questions to
    # confirm.  Not a generic arbitrary-goal platform.
    'evidence_review': {'version': 1, 'outputs': ['investigation_report'],
                        'tools': ['memory_read', 'rag_search', 'read_evidence', 'ask_clarification', 'ddi_check', 'memory_write'],
                        'max_steps': 24},
}


class CareTasks:
    def __init__(self, product, agent_factory=None):
        self.p = product
        self._agent_factory = agent_factory
        self._agent = None

    def agent(self):
        """Lazily built review agent for the evidence_review contract.  The
        factory (when provided) is the server's configured agent factory so
        provider settings match the interactive path."""
        if self._agent is None:
            if self._agent_factory is not None:
                self._agent = self._agent_factory()
            else:
                from .agent import MedicationCoordinatorAgent
                self._agent = MedicationCoordinatorAgent(self.p.memory)
        return self._agent

    def create(self, key, goal_type, case_id=None, due_at=None, budget=None, goal=None):
        if goal_type not in CONTRACTS:
            raise ProductError('请选择材料核对、当前药单、就诊摘要或证据核查')
        contract = CONTRACTS[goal_type]
        if budget is not None and (type(budget) is not int or not 1 <= budget <= contract['max_steps']):
            raise ProductError('任务处理次数超出契约范围')
        if goal_type == 'evidence_review':
            if not isinstance(goal, str) or not 4 <= len(goal.strip()) <= 300:
                raise ProductError('请描述要核查的开放问题（4-300 字）')
            goal = goal.strip()
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
            if goal_type == 'evidence_review':
                # A2 open-goal persistence: goal text, per-scope input versions
                # for incremental replanning, serialized investigation state and
                # code-validated additional questions (model/user may propose,
                # code owns the contract).
                task['goal'] = goal
                task['input_versions'] = {**self.p.revisions(), 'materials': self.p.memory.scope_revision('materials')}
                task['investigation'] = None
                task['subgoals'] = []
                task['additional_questions'] = []
                task['invalidations'] = []
            self.p.save('care_task', task)
            return task
        return self.p.command(key, {'type': 'care_task_create', 'goal_type': goal_type, 'case_id': case_id, 'due_at': due_at, 'budget': budget, 'goal': goal}, execute)

    def resume(self, task_id, key, revision, action='continue', *, enqueue=False):
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
                    # The one exception is an explicit retry of a run that
                    # degraded because the provider was unavailable: it is
                    # transient, the result is labelled as not model-checked,
                    # and the task budget below still bounds how many tries a
                    # caregiver gets.  A cancelled task is never reopened.
                    if not (task.get('retry_available') and task['status'] in ('completed', 'failed')):
                        raise ProductError('此待办已结束', 409)
                    task['retry_available'] = False
                    task['degraded_label'] = None
                if task['goal_type'] == 'evidence_review' and task['status'] == 'running':
                    raise ProductError('此核查已在队列中或正在执行', 409)
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
                    if task['goal_type'] == 'evidence_review':
                        self._enqueue_review_tx(task)
                    else:
                        self._execute(task)
                    task['runs'][-1].update(status=task['status'])
                    if task['status'] != 'running':
                        task['runs'][-1]['finished_at'] = utc_now()
            task['revision'] += 1
            self.p.save('care_task', task)
            return task
        result = self.p.command(key, {'type': 'care_task_resume', 'task_id': task_id, 'revision': revision, 'action': action}, execute)
        if action == 'cancel':
            from .harness.progress import cancel_event_for
            for run_id in result.get('resource_budget', {}).get('child_run_ids', []):
                cancel_event_for(run_id).set()
        elif result['goal_type'] == 'evidence_review' and not enqueue:
            # Local callers may wait, but no database transaction spans execution.
            # HTTP callers always enqueue and return immediately.
            from .server import OutboxWorker
            from .graph_runner import LegacyAgentRunner
            OutboxWorker(self.p.memory, runner_factory=lambda: LegacyAgentRunner(self.agent())).drain_once()
            current = self.p.get(task_id, 'care_task')
            return {**current, 'receipt_id': result['receipt_id']}
        return result

    def _enqueue_review_tx(self, task):
        from .turn_budget import TurnBudget
        from dataclasses import asdict
        resources = task['resource_budget']
        limits = asdict(TurnBudget.from_env(CONTRACTS['evidence_review']['max_steps']))
        limits['token_budget'] = min(limits['token_budget'], max(0, resources['token_limit'] - resources['tokens_reserved']))
        limits['call_budget'] = min(limits['call_budget'], max(0, resources['call_limit'] - resources['calls_reserved']))
        if not limits['token_budget'] or not limits['call_budget']:
            task.update(status='failed', waiting_reason='任务累计资源预算已用尽；已保存结果保留')
            return
        run_id = f"care-task:{task['id']}:{len(task['runs'])}"
        resources['tokens_reserved'] += limits['token_budget']
        resources['calls_reserved'] += limits['call_budget']
        resources['child_run_ids'].append(run_id)
        task['active_run_id'] = run_id
        task['runs'][-1].update(workflow_run_id=run_id, runner='care-task-evidence-review@1')
        # Reservation and queue acceptance share the product command transaction.
        payload = {'operation': 'care-task-evidence-review@1', 'care_task_id': task['id'], 'run_id': run_id, 'limits': limits}
        now = utc_now()
        self.p.db.execute("""INSERT INTO outbox_tasks(task_type,payload_json,dedup_key,status,attempts,created_at,updated_at)
            VALUES('process_event',?,?,'open',0,?,?)""", (packed(payload), run_id, now, now))

    def execute_queued(self, queue):
        """Lease-fenced execution; short transactions only at acceptance/publication."""
        from .turn_budget import check_lease
        payload = queue['payload']
        task = self.p.get(payload['care_task_id'], 'care_task')
        run_id = payload['run_id']
        if task.get('active_run_id') == run_id and task['status'] == 'running':
            task['_queued_limits'] = payload['limits']
            self._execute_evidence_review(task, run_id=run_id, publish=False)
        with self.p.transaction():
            check_lease()
            current = self.p.get(task['id'], 'care_task')
            if current.get('active_run_id') == run_id and current['status'] == 'running':
                artifact = task.pop('_artifact', None)
                task.pop('_queued_limits', None)
                if artifact:
                    if artifact['investigation']['patient_version'] != self.p.revisions():
                        artifact['partial'] = True
                        task.update(status='ready', waiting_reason='记录在核查期间发生变化，请继续以重查相关依据',
                                    result_refs=[])
                        task.setdefault('partial_report_refs', []).append(artifact['id'])
                    self.p.save('investigation_report', artifact)
                task['revision'] = current['revision'] + 1
                task['runs'][-1].update(status=task['status'], finished_at=utc_now())
                self.p.save('care_task', task)
            # A concurrent cancel wins; its terminal task is never overwritten.
            self.p.db.execute("UPDATE outbox_tasks SET status='done',result_json=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?",
                (packed({'care_task_id': task['id']}), utc_now(), queue['id']))
        return {'task_id': queue['id'], 'status': 'done', 'care_task_status': self.p.get(task['id'])['status']}

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

    # ---- A2: evidence_review open contract ---------------------------------
    def _execute_evidence_review(self, task, *, run_id=None, publish=True):
        """One bounded review run over the persisted investigation state.

        The task owns status, artifacts and the cumulative resource budget;
        the investigation engine owns coverage semantics.  Domain effects can
        only be produced through the agent's existing write policy (recorded
        warnings) — this executor itself only reads and saves artifacts."""
        contract = CONTRACTS['evidence_review']
        versions = {**self.p.revisions(), 'materials': self.p.memory.scope_revision('materials')}
        previous = task.get('input_versions') or versions
        if versions != previous:
            changed = [k for k in versions if versions[k] != previous.get(k)]
            task['invalidations'].append({'at': utc_now(), 'changed': changed,
                'policy': 'medications→全量重查；semantic→适用条件重核、材料证据复用；materials→来源重校验'})
        task['missing_inputs'] = []
        # Protocol v2: the review must be able to SEE the caregiver's uploaded
        # materials (their staged candidates and the deterministic diff against
        # the authoritative list) — otherwise "多份材料之间有什么差异" is
        # answered by code reciting a diff rather than by investigation.
        agent = self.agent()
        from .product import MaterialIndex
        agent.attach_material_index(MaterialIndex(self.p))
        result = agent.run_open_review(
            task['goal'], run_id=run_id or f"care-task:{task['id']}:{len(task['runs'])}", scope_id=SCOPE,
            initial_state=task.get('investigation'), max_cycles=contract['max_steps'],
            saved_budget=task.get('_queued_limits'))
        inv = result['investigation']
        task['investigation'] = inv
        resources = task.get('resource_budget')
        if resources:
            if result['run_id'] not in resources['child_run_ids']:
                resources['child_run_ids'].append(result['run_id'])
            owned = [self.p.memory.workflow_run_get(r) for r in resources['child_run_ids']]
            resources['tokens_actual'] = sum((r or {}).get('budget', {}).get('tokens_actual', 0) for r in owned)
            resources['calls_actual'] = sum((r or {}).get('budget', {}).get('calls_attempted', 0) for r in owned)
            resources['usage_unknown'] = any((r or {}).get('budget', {}).get('usage_unknown', False) for r in owned)
        artifact = {'id': f'investigation:{uuid.uuid4().hex}', 'created_at': utc_now(),
            'task_id': task['id'], 'goal': task['goal'], 'patient_revision': self.p.revisions(),
            'material_revision': self.p.memory.scope_revision('materials'), 'scope_id': SCOPE,
            'format_version': 1, 'investigation': inv,
            'partial': result['termination_reason'] != 'checks_completed',
            'markdown': self._review_markdown(task, inv)}
        if publish:
            self.p.save('investigation_report', artifact)
        else:
            task['_artifact'] = artifact
        if result['termination_reason'] != 'checks_completed':
            task.setdefault('partial_report_refs', []).append(artifact['id'])
        task['subgoals'] = self._evidence_subgoals(inv, task)
        termination = result['termination_reason']
        # Surface (never hide) a provider outage: the caregiver must be able to
        # tell a model-checked review from one the rules completed because the
        # provider never answered, and must have a way to retry it.
        degraded = result.get('degraded_reason')
        task['degraded_reason'] = degraded
        task['retry_available'] = provider_outage(degraded)
        task['degraded_label'] = PROVIDER_OUTAGE_LABEL if task['retry_available'] else None
        if termination == 'waiting_input':
            task['status'] = 'waiting_input'
            task['waiting_reason'] = '需要补充记录后继续核查'
            task['missing_inputs'] = [{'gap_id': q['gap_id'], 'field': q['field'], 'question': q['question']}
                                      for q in inv.get('questions', [])]
        elif termination == 'waiting_review':
            task['status'] = 'waiting_review'
            task['waiting_reason'] = '证据存在分歧，需专业核实；未连接真实医生服务'
            if os.getenv('STAGE0_REVIEW_ENABLED', '0').lower() in {'1', 'true'}:
                case = self.p.memory.open_review_case(logic_key='care-review:' + result['run_id'],
                    run_id=result['run_id'], thread_id=task['id'], reason_codes=['evidence_conflict'],
                    summary={'care_task_id': task['id'], 'verification_status': 'incomplete',
                             'fact_revision': sum(inv['patient_version'].values()),
                             'fact_hash': self.p.memory.medication_set_hash(),
                             'investigation': inv, 'conflicts': [], 'simulated': True})
                task['review_refs'] = [case['id']]
                task['waiting_reason'] = '已进入模拟审核队列；未连接真实医生服务，到期不会自动批准'
                self.p.memory.workflow_run_update(result['run_id'], status='waiting_review')
        elif termination == 'checks_completed':
            # Completion is code-controlled and requires the output persisted.
            if publish and not self.p.get(artifact['id']):
                raise ProductError('任务产物尚未保存，不能完成', 409)
            task['result_refs'] = [artifact['id']]
            task['status'] = 'completed'
            task['waiting_reason'] = None
        elif termination == 'cancelled':
            task['status'] = 'cancelled'
            task['waiting_reason'] = '核查已取消；此前保存的记录与部分报告保留'
        else:
            # budget_insufficient / no_progress / unrecoverable_failure: an
            # honest bounded report was saved, but the contract is NOT complete.
            task['status'] = 'failed'
            task['waiting_reason'] = f'本轮核查未完成全部必查项（{termination}）；部分报告与已保存记录保留'
        task['input_versions'] = versions

    def apply_review_decision(self, record, run):
        """Reuse the existing review effect receipt; update the care-task projection.

        Evidence disputes cannot be approved into medical fact. This case only
        permits more information, rejection, or closing with safe guidance.
        """
        from .memory import MemoryPolicyError, ReviewFactsMovedError
        case = self.p.memory.review_case(record['case_id'])
        task = self.p.get(case['summary']['care_task_id'], 'care_task')
        if task['status'] == 'cancelled':
            raise MemoryPolicyError('cancelled care task cannot accept review')
        outcome = {'action': record['action'], 'decision_id': record['decision_id']}
        try:
            if record['outcome'] == 'review_stale':
                outcome['stale'] = True
            else:
                self.p.memory.apply_review_effect(record['decision_id'], session_id=task['id'], turn_id=run['run_id'])
        except ReviewFactsMovedError:
            self.p.memory.set_review_decision_outcome(record['decision_id'], outcome='review_stale')
            self.p.memory.close_review_case(case['id'], status='cancelled', reason='review_stale', actor='runner')
            outcome['stale'] = True
        def publish():
            current = self.p.get(task['id'], 'care_task')
            if current['status'] == 'cancelled':
                return current
            current['review_outcome'] = outcome
            if outcome.get('stale'):
                current.update(status='ready', waiting_reason='患者记录变化，旧审核失效；请继续核查以生成新依据')
            elif record['action'] == 'request_more_info':
                current.update(status='waiting_input', waiting_reason='模拟审核要求补充记录或问题，原证据分歧仍保留', missing_inputs=[])
            else:
                current.update(status='failed', waiting_reason='模拟审核已结束；证据分歧仍未解决，保留部分报告')
            current['revision'] += 1
            self.p.save('care_task', current)
            return current
        return self.p.command('care-review:' + record['decision_id'], {'type': 'care_task_review',
            'decision_id': record['decision_id']}, publish)

    def _evidence_subgoals(self, inv, task):
        """Derive subgoal projection from the investigation.  Deps only point
        at the authority read, so the graph is acyclic by construction; any
        future model-proposed subgoal must pass validate_subgoals."""
        subgoals = [{'subgoal_id': 'authority', 'kind': 'authority_read',
                     'statement': '读取完整权威药单与关键事实', 'depends_on': [],
                     'status': 'completed' if inv['checks'].get('authority') == 'checked' else 'blocked',
                     'result_refs': []}]
        for claim in inv.get('claims', []):
            refs = claim.get('supporting_evidence', []) + claim.get('opposing_evidence', [])
            subgoals.append({'subgoal_id': claim['claim_id'], 'kind': 'evidence_check',
                             'statement': claim['statement'], 'depends_on': ['authority'],
                             'status': 'completed' if claim['status'] != 'insufficient' else 'blocked',
                             'result_refs': refs})
        for index, question in enumerate(task.get('additional_questions', [])):
            subgoals.append({'subgoal_id': f'question:{index}', 'kind': 'user_question',
                             'statement': question, 'depends_on': ['authority'],
                             # Code cannot complete a free-text question; it stays
                             # recorded and is answered by the report's open-items
                             # section, never by a model-written completion.
                             'status': 'recorded', 'result_refs': []})
        return subgoals

    def _review_markdown(self, task, inv):
        from .investigation import InvestigationState
        # The ARTIFACT may quote the caregiver's own goal (it is not the
        # delivered response and is not subject to the response keyword check);
        # the report body itself must stay clear of arbitrary free text.
        lines = [f'<!-- 照护待办 {task["id"]} -->', f'> 调查目标：{task["goal"]}', '',
                 InvestigationState.restore(inv, SCOPE).report_text()]
        if task.get('additional_questions'):
            lines += ['', '## 待确认问题（代码记录，不自动判定）', '']
            lines += [f'- {q}' for q in task['additional_questions']]
        if task.get('invalidations'):
            lines += ['', '## 增量重查记录', '']
            for item in task['invalidations']:
                lines.append(f"- {item['at'][:19]} 变化范围：{'、'.join(item['changed'])}；{item['policy']}")
        return '\n'.join(lines)

    def record_input(self, task_id, key, revision, medications=None, additional_questions=None, semantic=None):
        """Supplement a waiting evidence_review task through the controlled
        write path, then the caller resumes the task.  Model/user input is
        data: it cannot mark the task complete or rewrite the contract."""
        from .investigation import MAX_CLAIMS
        from .memory import SemanticFact
        allowed_namespaces = {'age', 'allergy', 'renal_function', 'hepatic_function', 'pregnancy', 'chronic_condition'}
        if medications is not None and (not isinstance(medications, list) or len(medications) > MAX_CLAIMS):
            raise ProductError('补充用药必须为范围内的列表')
        if semantic is not None and not isinstance(semantic, dict):
            raise ProductError('补充事实必须为对象')
        if additional_questions is not None and not isinstance(additional_questions, list):
            raise ProductError('补充问题必须为列表')
        def execute():
            task = self.p.get(task_id, 'care_task')
            if task['revision'] != revision:
                raise ProductError('待办已被其他操作更新，请刷新', 409)
            if task['goal_type'] != 'evidence_review':
                raise ProductError('此待办不支持补充输入')
            if task['status'] not in ('ready', 'waiting_input'):
                raise ProductError('此待办当前不需要补充输入', 409)
            applied = []
            questions = task.get('additional_questions', [])
            # Validate the pure-text additions first: a failed question must
            # not leave already-committed medication writes behind.
            for question in additional_questions or []:
                if not isinstance(question, str) or not 2 <= len(question.strip()) <= 80:
                    raise ProductError('补充问题需要是 2-80 字的文本')
                cleaned = question.strip()
                if cleaned not in questions:
                    if len(questions) >= MAX_CLAIMS:
                        raise ProductError('补充问题数量超出本轮核查范围')
                    questions.append(cleaned)
            for item in medications or []:
                if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip():
                    raise ProductError('补充用药需要提供药名')
                name = item['name'].strip()
                if item.get('action') not in (None, 'add', 'dose_change'):
                    raise ProductError('补充输入不支持此用药操作')
                existing = next((m for m in self.p.memory.current_medications() if m['display_name'] == name), {})
                for field in ('dose', 'schedule', 'start_at'):
                    if item.get(field) is not None and (not isinstance(item[field], str) or not item[field].strip()):
                        raise ProductError('用药字段必须为非空文本')
                write = self.p.memory._apply_medication_change_tx(
                    action=item.get('action') if item.get('action') in ('add', 'dose_change') else 'add',
                    name=name, ingredients=existing.get('ingredients', []), session_id=task_id, turn_id=key,
                    source='caregiver-input', dose=item.get('dose') or existing.get('dose'), route=existing.get('route'),
                    schedule=item.get('schedule') or existing.get('schedule'), occurred_at=item.get('start_at'))
                applied.append(write.get('item') or write)
            facts = 0
            for namespace, value in (semantic or {}).items():
                # Reported facts stay "recorded_as_reported": user confirmation
                # is a record, never a clinical approval.
                if namespace not in allowed_namespaces:
                    raise ProductError('不支持的事实类型：' + str(namespace))
                if value is None or value == '' or value == {} or value == []:
                    raise ProductError('补充事实不能为空')
                fact_key = {'renal_function': 'renal_status', 'hepatic_function': 'hepatic_status',
                            'age': 'age', 'pregnancy': 'pregnancy_status'}.get(namespace, namespace)
                self.p.memory._write_semantic_fact_tx(
                    SemanticFact(namespace=namespace, key=fact_key, value=value, extraction_mode='caregiver_input'),
                    source='caregiver-input')
                facts += 1
            task['additional_questions'] = questions
            task['revision'] += 1
            self.p.save('care_task', task)
            return {'task_id': task_id, 'applied_medications': len(applied),
                    'applied_semantic_facts': facts, 'additional_questions': list(questions)}
        return self.p.command(key, {'type': 'care_task_input', 'task_id': task_id, 'revision': revision,
            'medications': medications, 'semantic': semantic, 'additional_questions': additional_questions}, execute)

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


def register_task_routes(app, product, access, invoke, agent_factory=None):
    from fastapi import Request
    from fastapi.responses import Response
    globals()['Request'] = Request
    tasks = CareTasks(product, agent_factory=agent_factory)
    app.state.care_tasks = tasks

    @app.get('/v1/care-tasks')
    def list_tasks(request: Request):
        access(request)
        return {'items': product.objects('care_task'), 'notifications': 'in_app_only', 'contracts': CONTRACTS}

    @app.post('/v1/care-tasks')
    def create(request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.create(body.get('key'), body.get('goal_type'), body.get('case_id'), body.get('due_at'), body.get('budget'), body.get('goal')))

    @app.post('/v1/care-tasks/{task_id}/input')
    def record_input(task_id: str, request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.record_input(task_id, body.get('key'), body.get('revision'),
                                                 body.get('medications'), body.get('additional_questions'),
                                                 body.get('semantic')))

    @app.post('/v1/care-tasks/{task_id}/resume')
    def resume(task_id: str, request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.resume(task_id, body.get('key'), body.get('revision'), body.get('action', 'continue'), enqueue=True))

    @app.get('/v1/investigation-reports/{report_id}')
    def report(report_id: str, request: Request):
        access(request)
        return invoke(lambda: product.get(report_id, 'investigation_report'))

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
