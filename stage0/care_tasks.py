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


# 走"证据调查"这一类执行器的 goal_type。两个契约共享同一套任务生命周期与发布
# 机制，但各自有自己的调查引擎——旧契约不被原地改写。
REVIEW_GOAL_TYPES = ('evidence_review', 'material_review', 'safety_case')


def _stage_artifact(task, kind, artifact, product, stale_check, stale_reason):
    """排队执行期间先不落盘；发布事务里再按当时的事实版本判断它是否已经过时。

    这样"核查期间记录变了"不会把一份真实的报告丢掉，也不会让它冒充当前结果。
    """
    task.setdefault('_artifacts', []).append(
        {'kind': kind, 'artifact': artifact, 'stale_check': stale_check,
         'stale_reason': stale_reason})


def _clean_requested(requested):
    """校验并规整用户提出的**结构化**交付要求。

    它来自明确的入口与参数，因此这里做的是"合格与否"，不是"从一句话里猜出几件事"。
    猜出来的要求会让用户看到一堆自己没提过的义务——那正是本轮要避免的。
    """
    if requested in (None, []):
        return []
    if not isinstance(requested, list) or len(requested) > 8:
        raise ProductError('请用结构化参数描述本次要求（最多 8 项）')
    allowed = {'list_differences', 'confirm_field', 'answer_from_source'}
    from .review.contract import COMPARED_FIELD_LABELS
    cleaned = []
    for raw in requested:
        if not isinstance(raw, dict) or raw.get('kind') not in allowed:
            raise ProductError('本次只支持三种明确要求：列出差异与缺项、确认指定字段、依据资料回答问题')
        item = {'kind': raw['kind'],
                'subjects': [str(value).strip() for value in (raw.get('subjects') or [])
                             if str(value).strip()][:8]}
        if raw['kind'] == 'confirm_field':
            if raw.get('field') not in COMPARED_FIELD_LABELS:
                raise ProductError('请选择要确认的字段')
            item['field'] = raw['field']
            if raw.get('expect') in ('equal', 'different'):
                item['expect'] = raw['expect']
        if raw['kind'] == 'answer_from_source':
            question = str(raw.get('question') or '').strip()
            if not 4 <= len(question) <= 300:
                raise ProductError('请写清要依据资料回答的问题（4-300 字）')
            item['question'] = question
            item['source_refs'] = [str(value) for value in (raw.get('source_refs') or [])][:8]
            item['allows_external'] = bool(raw.get('allows_external', True))
        if raw['kind'] == 'list_differences':
            item['text'] = str(raw.get('text') or '').strip() or None
        cleaned.append({key: value for key, value in item.items() if value is not None})
    return cleaned


def safety_case_request_id(case_id: str, question: dict[str, Any]) -> str:
    """一条补问在**事项**上的稳定身份。

    优先用调查契约给出的 ``question_id``——它由（类型、对象、目标字段）派生，
    所以同一问题的改写、以及同一药物的不同字段问题，都能各自稳定。没有时退回
    ``gap_id`` / ``field``（旧调查状态里只有这些）。

    **不含**时间戳、序号或运行 id：恢复后把同一件事再问一遍在结构上就不可能，
    ``require_input`` 按 id 去重。
    """
    anchor = (question.get('question_id') or question.get('gap_id')
              or question.get('target_field') or question.get('field') or 'unknown')
    return f"case:{case_id}:{anchor}"


def _safety_case_goal(case: dict[str, Any]) -> str:
    """把事项写成一段调查目标。

    Agent 从一个**具体事项**开工，而不是从一份巨大的患者快照重新规划全部任务：
    目标里带上是"为什么产生这件事"，而不是"请全面检查这个患者"。
    """
    from .safety_cases import STATUS_LABELS
    trigger = (case.get('history') or [{}])[0].get('trigger') or {}
    label = STATUS_LABELS.get(case.get('current_status'), case.get('current_status'))
    kind = {'interaction_risk': '药物相互作用风险',
            'condition_risk': '患者个体风险', 'evidence_gap': '依据缺口',
            'discrepancy': '记录不一致', 'source_invalidated': '来源失效'}.get(
                case.get('case_type'), case.get('case_type'))
    parts = [f"跟进一件{kind}安全事项（当前状态：{label}）。",
             f"它由{trigger.get('kind') or '一次变化'}触发。"]
    if case.get('next_action_summary'):
        parts.append(f"当前待办：{case['next_action_summary']}。")
    parts.append("请核实该风险在当前记录下是否成立、依据是什么、还缺什么信息，并说明下一步。")
    return ''.join(parts)[:300]


def _failure_reason(degraded_reason):
    if not degraded_reason:
        return '本轮核对未能完成；已保存的记录与部分报告保留'
    if 'permission' in degraded_reason:
        return '当前权限不允许继续核对；已有结果保留，未受影响的部分仍然有效'
    if 'budget' in degraded_reason:
        return '本轮可用预算已用尽；已保存的记录与部分报告保留'
    return f'本轮核对未完成（{degraded_reason}）；已保存的记录与部分报告保留'

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
    # material-review@2: 用户提交核对目标与指定材料 → 代码准备可信上下文 →
    # 模型调查 → 带来源的核对与就诊准备报告 → 补充后只重算受影响部分。
    # ``version`` 是**这个 goal_type 的待办契约版本**；调查契约版本单独记录在
    # ``task['review_contract']`` 上（`material-review@2`），两者不互相冒充。
    'material_review': {'version': 1, 'outputs': ['material_review_report'],
                        'tools': ['read_material', 'research_evidence', 'request_information',
                                  'submit_question', 'submit_finding', 'submit_assertion',
                                  'request_delivery'],
                        'max_steps': 24},
    # safety-case@2: 从**一件具体的安全事项**出发的持续调查。与 evidence_review 的
    # 区别不是引擎，而是起点：后者从一段自由目标重新规划全部任务，这里带着事项的
    # 触发原因、已有结论、已失效依据、未决问题与用户补充开工，跨会话回到同一件事。
    #
    # @2 起，回访任务显式绑定 ``visit_id`` 并且目标由**本次回访的意图**给出
    # （见 `review_visits.visit_intent`），而不是套一段通用的"核实风险是否成立"。
    # 契约版本显式记录、恢复时校验：@1 的旧任务不会被复活成一次回访的执行者。
    'safety_case': {'version': 2, 'outputs': ['safety_case_report'],
                    'tools': ['memory_read', 'rag_search', 'read_evidence', 'ask_clarification',
                              'ddi_check', 'memory_write', 'answer_question',
                              # 回访用：把"用户那句话意味着记录该改了"记成**待确认**的
                              # 候选。它不写记录——改记录的唯一一步是用户确认。
                              'propose_medication_change'],
                    'max_steps': 16},
}

SAFETY_CASE_CONTRACT = 'safety-case@2'
REVIEW_CONTRACT = 'material-review@2'

#: 只有这些收尾方式算"这一轮把交给它的事件处理完了"。
#: 失败、取消、降级、`no_progress` 都不是——把没看过的事件标成看过了，
#: 下一轮回访就再也看不到它们。
CONSUMED_TERMINATIONS = ('checks_completed', 'waiting_input', 'waiting_review')


def consumed_cursor(current: int, consumed_at: int, termination: str | None,
                    history_length: int) -> int:
    """这一轮结束后消费游标停在哪。**规则只在这里**。

    * 没跑成（失败 / 取消 / 降级 / no_progress）→ **不动**：那些事件没被处理过，
      推进等于把它们从下一轮的新增里抹掉。
    * 跑成了 → 停在**建上下文时**那个位置，不越过运行期间新增的事件。
    """
    if termination not in CONSUMED_TERMINATIONS:
        return int(current or 0)
    return min(int(consumed_at), int(history_length))


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

    def create(self, key, goal_type, case_id=None, due_at=None, budget=None, goal=None,
               requested=None, visit_id=None, visit_intent=None):
        if goal_type not in CONTRACTS:
            raise ProductError('请选择材料核对、当前药单、就诊摘要或证据核查')
        contract = CONTRACTS[goal_type]
        if budget is not None and (type(budget) is not int or not 1 <= budget <= contract['max_steps']):
            raise ProductError('任务处理次数超出契约范围')
        if goal_type == 'evidence_review':
            if not isinstance(goal, str) or not 4 <= len(goal.strip()) <= 300:
                raise ProductError('请描述要核查的开放问题（4-300 字）')
            goal = goal.strip()
        if goal_type == 'material_review':
            if not isinstance(case_id, str) or not case_id.strip():
                raise ProductError('请选择要核对的材料')
            goal = (goal or '').strip() or '核对材料与当前记录，整理一致项、差异与缺项'
            if not 4 <= len(goal) <= 300:
                raise ProductError('请描述本次核对目标（4-300 字）')
        if goal_type == 'safety_case':
            if not isinstance(case_id, str) or not case_id.strip():
                raise ProductError('请选择要跟进的安全事项')
            from .safety_cases import SafetyCaseStore
            case = SafetyCaseStore(self.p).get(case_id)
            # 目标由**事项本身**给出，不由调用方自由发挥：这样跨会话回来时调查的是
            # 同一件事，而不是一段被重新描述过的目标。
            goal = (goal or '').strip() or _safety_case_goal(case)
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
            if goal_type == 'safety_case':
                # 一件事项同时只应有一次在跑的调查。否由服务端强制，而不是靠界面
                # 记得先查一遍：两次点击会产生两个并发的调查，各自花预算，最后在
                # 同一件事上互相覆盖状态。
                #
                # 判据带**回访维度**：一次新回访不是"同一件调查的重复创建"。
                # 少了这一维，第二次回访会拿回第一件任务——它的目标、它的
                # investigation 状态、它记着的结论，全都属于上一次回访。
                running = [t for t in self.p.objects('care_task')
                           if t.get('goal_type') == 'safety_case'
                           and t.get('safety_case_id') == case_id
                           and t.get('visit_id') == visit_id
                           and t['status'] not in ('completed', 'cancelled', 'failed')]
                if running:
                    return running[0]
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
            if goal_type == 'material_review':
                # 新契约的持久状态是**版本化**的：契约版本显式记录，恢复时校验；
                # 旧 goal_type 的载荷不受影响。
                task['goal'] = goal
                task['review_contract'] = REVIEW_CONTRACT
                task['review_state'] = None
                task['selected_case_ids'] = [case_id]
                task['report_refs'] = []
                # 用户**明确**要求完成的内容。它来自结构化参数，不是从一句话里
                # 猜出来的：本轮只支持三种明确要求，各自有不同的满足条件。
                task['requested'] = _clean_requested(requested)
            if goal_type == 'safety_case':
                # 调查从**一件已存在的事项**开始：契约版本显式记录，恢复时校验；
                # investigation 状态随任务持久化，所以下次会话接着上次的进度走，
                # 而不是把已经问过的问题再问一遍。
                task['goal'] = goal
                task['safety_case_contract'] = SAFETY_CASE_CONTRACT
                task['safety_case_id'] = case_id
                # 这次执行**属于哪一次回访**。绑定是双向的（回访也记着 care_task_id），
                # 恢复时据此判断"这个任务还是不是那次回访的"——没有它，一件旧任务的
                # 结果会写进另一回访。
                task['visit_id'] = visit_id
                # 本次执行意图随任务落盘：恢复后不必重算，也不会因为记录又变了
                # 而把目标悄悄换掉。
                task['visit_intent'] = ({'visit_id': visit_id, **dict(visit_intent)}
                                        if visit_intent else None)
                task['input_versions'] = self.p.revisions()
                task['investigation'] = None
                task['subgoals'] = []
                task['invalidations'] = []
            self.p.save('care_task', task)
            return task
        return self.p.command(key, {'type': 'care_task_create', 'goal_type': goal_type,
                                    'case_id': case_id, 'due_at': due_at, 'budget': budget,
                                    'goal': goal, 'requested': _clean_requested(requested),
                                    # 回访绑定进幂等载荷：同一个 key 换一次回访是
                                    # **另一件请求**，不能命中上一条回执。
                                    'visit_id': visit_id}, execute)

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
                if task['goal_type'] in REVIEW_GOAL_TYPES and task['status'] == 'running':
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
                    if task['goal_type'] in REVIEW_GOAL_TYPES:
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
        elif result['goal_type'] in REVIEW_GOAL_TYPES and not enqueue:
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
        operation = {'material_review': 'care-task-material-review@2',
                     'safety_case': 'care-task-safety-case@1'}.get(
                         task['goal_type'], 'care-task-evidence-review@1')
        task['runs'][-1].update(workflow_run_id=run_id, runner=operation)
        # Reservation and queue acceptance share the product command transaction.
        payload = {'operation': operation, 'care_task_id': task['id'], 'run_id': run_id, 'limits': limits}
        now = utc_now()
        self.p.db.execute("""INSERT INTO outbox_tasks(task_type,payload_json,dedup_key,status,attempts,created_at,updated_at)
            VALUES('process_event',?,?,'open',0,?,?)""", (packed(payload), run_id, now, now))

    def publish(self, task):
        """发布事务的**全部落盘**。调用方负责持有事务；这里不做任何长操作。

        执行期间先只暂存产物（``_stage_artifact``），发布时才按**当时**的事实版本
        判断它是否已经过时——这样"核对期间记录变了"既不会丢掉一份真实的报告，
        也不会让它冒充当前结果。
        """
        for entry in task.pop('_artifacts', None) or []:
            artifact = entry['artifact']
            if entry['stale_check']():
                artifact['partial'] = True
                task.update(status='ready', waiting_reason=entry['stale_reason'],
                            result_refs=[ref for ref in task['result_refs']
                                         if ref != artifact['id']])
                task.setdefault('partial_report_refs', []).append(artifact['id'])
            self.p.save(entry['kind'], artifact)
        self.p.save('care_task', task)
        return task

    def execute_queued(self, queue):
        """Lease-fenced execution; short transactions only at acceptance/publication."""
        from .turn_budget import check_lease
        payload = queue['payload']
        task = self.p.get(payload['care_task_id'], 'care_task')
        run_id = payload['run_id']
        if task.get('active_run_id') == run_id and task['status'] == 'running':
            task['_queued_limits'] = payload['limits']
            self._execute_review(task, run_id=run_id)
        with self.p.transaction():
            check_lease()
            current = self.p.get(task['id'], 'care_task')
            if current.get('active_run_id') == run_id and current['status'] == 'running':
                task.pop('_queued_limits', None)
                task['revision'] = current['revision'] + 1
                task['runs'][-1].update(status=task['status'], finished_at=utc_now())
                self.publish(task)
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
    def _execute_evidence_review(self, task, *, run_id=None):
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
            # 旧契约的累加口径**原样保留**：它在自己的契约版本下已经有历史数据，
            # 换算法会让同一个字段在两版之间不可比。新契约另有 `usage`，两者并存。
            owned = [self.p.memory.workflow_run_get(r) for r in resources['child_run_ids']]
            resources['tokens_actual'] = sum((r or {}).get('budget', {}).get('tokens_actual', 0) for r in owned)
            resources['calls_actual'] = sum((r or {}).get('budget', {}).get('calls_attempted', 0) for r in owned)
            resources['usage_unknown'] = any((r or {}).get('budget', {}).get('usage_unknown', False) for r in owned)
        task['usage'] = self.usage(task)
        artifact = {'id': f'investigation:{uuid.uuid4().hex}', 'created_at': utc_now(),
            'task_id': task['id'], 'goal': task['goal'], 'patient_revision': self.p.revisions(),
            'material_revision': self.p.memory.scope_revision('materials'), 'scope_id': SCOPE,
            'format_version': 1, 'investigation': inv,
            'partial': result['termination_reason'] != 'checks_completed',
            'markdown': self._review_markdown(task, inv)}
        # 产物暂存，由 publish() 在短事务里落盘：执行期间先不写，发布时才按当时
        # 的事实版本判断它是否已经过时。
        _stage_artifact(task, 'investigation_report', artifact, self.p,
                        lambda inv=inv: inv['patient_version'] != self.p.revisions(),
                        '记录在核查期间发生变化，请继续以重查相关依据')
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
            # 完成由代码控制，并要求产物确实落盘——落盘发生在发布事务里。
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

    # ---- material-review@2 --------------------------------------------------

    def _execute_review(self, task, *, run_id=None):
        """按 goal_type 分派到各自的执行器。**新路径只有一个核心**（advance）；旧
        路径保留自己的执行器，两者共享同一套预算、租约、取消与产物发布。"""
        if task['goal_type'] == 'material_review':
            return self._execute_material_review(task, run_id=run_id)
        if task['goal_type'] == 'safety_case':
            return self._execute_safety_case(task, run_id=run_id)
        return self._execute_evidence_review(task, run_id=run_id)

    # ---- safety-case@1 ------------------------------------------------------

    def _execute_safety_case(self, task, *, run_id=None):
        """一次针对**具体安全事项**的持续调查。

        三个不变式：

        * 被问到的问题写回**事项本身**（``required_inputs``），问句的身份由 query
          稳定派生——所以跨会话恢复时不会把同一件事再问一遍；
        * 事项状态由代码按既有契约推导（检查是否完成、依据是否还有效），模型的
          输出**不能**把它推成"已处置"；
        * 执行失败只把事项推进到 ``execution_failed``，未决问题原样保留。
        """
        from .investigation import MAX_CLAIMS
        from .safety_cases import (SafetyCaseStore, STATUS_AWAITING_USER,
                                   STATUS_AWAITING_PROFESSIONAL, STATUS_EXECUTION_FAILED,
                                   STATUS_INVESTIGATING)
        contract = CONTRACTS['safety_case']
        store = SafetyCaseStore(self.p)
        case = store.get(task['safety_case_id'])
        # 消费游标在**建上下文那一刻**取：这一轮真正交给模型的就是此前的事件。
        # 运行期间新增的那些（重开请求写的 answer_retired 等）不在快照里，
        # 因此也不会被误标成"已消费"。
        consumed_at = len(case.get('history') or [])
        versions = self.p.revisions()
        previous = task.get('input_versions') or versions
        if versions != previous:
            changed = [k for k in versions if versions[k] != previous.get(k)]
            task.setdefault('invalidations', []).append({
                'at': utc_now(), 'changed': changed,
                'policy': '用药或事实变化 → 受影响依据重核，未受影响的证据保留复用'})
        task['missing_inputs'] = []
        agent = self.agent()
        run_id = run_id or f"care-task:{task['id']}:{len(task['runs'])}"
        result = agent.run_open_review(
            task['goal'], run_id=run_id, scope_id=SCOPE,
            initial_state=task.get('investigation'), max_cycles=contract['max_steps'],
            saved_budget=task.get('_queued_limits'),
            case_context=self._safety_case_context(task, case, contract),
            policy='safety_case')
        inv = result['investigation']
        task['investigation'] = inv
        resources = task.get('resource_budget')
        if resources and result['run_id'] not in resources['child_run_ids']:
            resources['child_run_ids'].append(result['run_id'])
        task['usage'] = self.usage(task)

        artifact = {'id': f'safety-case-report:{uuid.uuid4().hex}', 'created_at': utc_now(),
                    'task_id': task['id'], 'safety_case_id': case['id'],
                    'contract_version': SAFETY_CASE_CONTRACT, 'format_version': 1,
                    'goal': task['goal'], 'scope_id': SCOPE,
                    'patient_revision': versions, 'investigation': inv,
                    'markdown': self._review_markdown(task, inv),
                    'partial': result['termination_reason'] != 'checks_completed'}
        _stage_artifact(task, 'safety_case_report', artifact, self.p,
                        lambda: artifact['patient_revision'] != self.p.revisions(),
                        '记录在调查期间发生变化，相关依据需要重新核对')

        # 问句写回事项：身份由 question_id 派生，恢复后是**同一条**请求。
        questions = [q for q in (inv.get('questions') or [])
                     if q.get('status', 'open') == 'open']
        for question in questions[:MAX_CLAIMS]:
            request_id = safety_case_request_id(case['id'], question)
            store.require_input(
                case['id'], request_id=request_id,
                question=str(question.get('statement') or question.get('question')
                             or question.get('target_field') or '需要补充信息'),
                fields=[question['target_field']] if question.get('target_field') else [],
                for_professional=question.get('strategy') == 'professional_review',
                why_needed=question.get('why'),
                # 信息目标与取证来源随请求一起给到界面：页面据此说明"要弄清什么、
                # 从哪里拿、换过没有"，而不是让用户对着一句没有来源的话猜。
                question_kind=question.get('information_target'),
                question_strategy=question.get('strategy'),
                strategy_history=list(question.get('strategy_history') or []),
                subject_refs=list(question.get('subject_refs') or []),
                command_key=f"{task['id']}:ask:{request_id}:{len(task['runs'])}")

        # **已经答上的问题也要落到事项上。** `answered_inputs` 是消费方读取答案的
        # 唯一入口（前端「已经查清的部分」），而它来自 required_inputs 里
        # status='answered' 的那些。只写"还开着的问题"，模型在调查里**自己核对
        # 通过**的答案（连同它的 assessment）就永远到不了界面——用户报告的答案能
        # 上屏、模型核实出来的反而不能，`verified`/`stale`/`unsupported` 三个状态
        # 无人可见。走专用的投影方法，**不**记 input_requested：那条历史的意思是
        # "请求您补充信息"，对一条从没问过用户的问题是假的。
        answered = [q for q in (inv.get('questions') or [])
                    if q.get('answers') and q.get('status', 'open') != 'open']
        if answered:
            store.project_answered_questions(case['id'], [
                {'request_id': safety_case_request_id(case['id'], q),
                 'question': str(q.get('statement') or q.get('question')
                                 or q.get('target_field') or '已查清的问题'),
                 'fields': [q['target_field']] if q.get('target_field') else [],
                 'for_professional': q.get('strategy') == 'professional_review',
                 'why_needed': q.get('why'),
                 'question_kind': q.get('information_target'),
                 'question_strategy': q.get('strategy'),
                 'strategy_history': list(q.get('strategy_history') or []),
                 'subject_refs': list(q.get('subject_refs') or []),
                 # 立刻就是"已答"：紧接着的 `_sync_questions_to_case` 会把
                 # `answered_parts`（含 assessment）与剩余不确定填上。初始值就写成
                 # 已答，是为了在两次同步之间不留一个"假装还在等回答"的窗口。
                 'status': 'answered',
                 'answered_parts': list(q.get('answers') or []),
                 # 必须写 `answered_against`：`retire_stale_answers` 按它判断
                 # "这条回答是不是针对旧记录版本给的"。不写就会被读成"版本对不上"
                 # 而**立刻重开**——一条刚核对通过的答案会退回"等您补充"。
                 'answered_against': dict(q.get('dependency_version') or versions),
                 'asked_at': None}
                for q in answered[:MAX_CLAIMS]],
                command_key=f"{task['id']}:projected:{len(task['runs'])}")

        task['missing_inputs'] = [
            {'request_id': safety_case_request_id(case['id'], q),
             'field': q.get('target_field'), 'question': q.get('statement'),
             'information_target': q.get('information_target'),
             'strategy': q.get('strategy')}
            for q in questions[:MAX_CLAIMS]]

        # 问题状态回写到事项上（**派生**，不是第二份真相）：问题由调查状态
        # 权威维护，事项上的请求只是它的投影。已经拿到适用依据的问题不再挂在
        # "等您补充"里；只答上一部分的把已知部分与剩余缺口一起显示出来。
        inv = self._sync_questions_to_case(store, case['id'], inv)
        task['investigation'] = inv

        # 这一次如果是一次**回访**，把它的结果落下来。
        # 回访记录只存引用与渲染后的叙述（见 `review_visits`），患者事实、药单、
        # 证据一律现取——所以这里做的是"把这一刻的样子记进这次回访"，不是
        # 另存一份档案。
        self._record_visit_outcome(task, store, case, inv)

        # 运行登记在事项上——"这件事查过几次、哪次失败"要能追溯。
        with self.p.transaction():
            current = store.get(case['id'])
            if run_id not in current['linked_run_ids']:
                current['linked_run_ids'].append(run_id)
                current['revision'] += 1
                self.p.save('safety_case', current)

        termination = result['termination_reason']
        degraded = result.get('degraded_reason')
        task['degraded_reason'] = degraded
        task['retry_available'] = provider_outage(degraded)
        task['degraded_label'] = PROVIDER_OUTAGE_LABEL if task['retry_available'] else None
        task['subgoals'] = self._evidence_subgoals(inv, task)
        if termination == 'cancelled':
            task['status'] = 'cancelled'
            task['waiting_reason'] = '调查已取消；此前保存的记录与部分报告保留'
        elif termination == 'checks_completed':
            task['result_refs'] = [artifact['id']]
            task['status'] = 'completed'
            task['waiting_reason'] = None
        elif termination in ('waiting_input', 'waiting_review'):
            task['status'] = 'waiting_input'
            # 等待补充 ≠ 没有结果：已经跑完的检查与它们的依据仍然有效，报告是部分
            # 结果而不是失败品。
            task['waiting_reason'] = '需要补充信息后继续跟进；已保存的检查结果仍然有效'
        else:
            task['status'] = 'failed'
            task['waiting_reason'] = f'本轮调查未完成（{termination}）；部分报告与已保存记录保留'
            with self.p.transaction():
                from .safety_cases import STATUS_RESOLVED as _RESOLVED
                current = store.get(case['id'])
                # 已完成有依据处置的事项不因一次运行失败被改写；其余照实标成
                # "本次执行未完成、仍未解决"。走**同一个**状态迁移函数，这样这次
                # 迁移也会带 `why` 进历史——直接赋值会让它从记录里消失。
                if current['current_status'] != _RESOLVED:
                    current['current_status'] = STATUS_EXECUTION_FAILED
                    current['next_action_summary'] = '本次调查未完成，事项仍未解决；可以重试或补充信息'
                    current['revision'] += 1
                    current['history'].append({
                        'at': utc_now(), 'event': 'status_changed',
                        'from': case['current_status'], 'to': STATUS_EXECUTION_FAILED,
                        'why': f'本轮调查未完成（{termination}）'})
                    current['updated_at'] = utc_now()
                    self.p.save('safety_case', current)
        task['input_versions'] = versions
        # 推进游标：本轮已经把这些事件交给过 Agent，下一轮不该再把它们当"新增"。
        #
        # **只在成功消费时推进**，而且只推到建上下文时那个位置：失败、取消、降级、
        # no_progress 都没把事件处理完，推进等于把没看过的变化标成看过了——下一轮
        # 回访就再也看不到它们。取 min 是为了让本轮运行期间新增的事件留在游标之后。
        task['case_history_cursor'] = consumed_cursor(
            task.get('case_history_cursor') or 0, consumed_at, termination,
            len(store.get(case['id']).get('history') or []))

    def _sync_answers_to_investigation(self, task, request_ids, by_request, answers, key) -> None:
        """把用户刚给的回答写回 investigation 里对应的那条问题。

        只更新**被回答到的那几条**；回答的性质是 ``user_reported``——它是记录，
        不是临床确认，所以信息状态停在"收到但未确认"，问题继续保持未决直到
        有适用依据。事实写入仍然走既有的受控确认路径，不经过这里。
        """
        inv_state = task.get('investigation')
        if not inv_state:
            return
        from .investigation import (InvestigationState, QUESTION_STATUS_OPEN,
                                    INFO_RECEIVED_UNCONFIRMED, is_question_answered)
        from .product import SCOPE as _SCOPE
        investigation = InvestigationState.restore(inv_state, _SCOPE)
        changed = False
        prefix = f'case:{task.get("safety_case_id")}:'
        for request_id in request_ids or []:
            # request_id 是 `case:<case_id>:<question_id>`，而 question_id 自己
            # 也含冒号（`q:...`）——按最后一个冒号切会切错。
            text = str(request_id)
            question_id = text[len(prefix):] if text.startswith(prefix) else text
            question = investigation.question(question_id)
            if question is None or is_question_answered(question):
                continue
            entry = by_request.get(str(request_id))
            if entry is None and len(request_ids) == 1 and len(answers or []) == 1:
                entry = answers[0]
            value = (entry or {}).get('value')
            if not value:
                continue
            investigation.record_question_attempt(question_id, {
                'tool': 'user_answer', 'ok': True, 'found_information': True,
                'information_state': INFO_RECEIVED_UNCONFIRMED})
            question.setdefault('answers', []).append({
                'value': str(value), 'field': question.get('target_field'),
                'source': 'user_answer', 'provenance': 'user_reported',
                'answer_ref': f'care-task-input:{key}', 'origin': 'user',
                'still_uncertain': ['尚未与权威记录或材料核对'],
                # 缺 assessment 就是"未核实"（CONTRACT §3.4）。对一条**有真实提交
                # 记录**的用户报告来说，那会丢掉一个事实：它确实有一份来源（这次
                # 提交本身），只是还没与权威记录或材料核对过。§3.5 规定 user_reported
                # 记 `candidate`，且**不得**仅因用户陈述就 verified——所以这里既不
                # 留空、也不升级。dependency_refs 为空是如实的：这条答案不派生自任何
                # 版本化记录，它派生的对象是这次提交。
                'assessment': {
                    'status': 'candidate',
                    'reason': '用户报告：来源是这次提交本身，尚未与权威记录或材料核对',
                    'source_ref': None, 'locator': None, 'dependency_refs': []}})
            changed = True
        if changed:
            task['investigation'] = investigation.to_dict()

    def _sync_questions_to_case(self, store, case_id, inv):
        """把调查里每条问题的状态投到事项的对应请求上，**并把重开反向同步回去**。

        `required_inputs` 只是**视图**：它跟随问题走，不自己保存一份 answered。

        投影是双向的。只做"问题 → 请求"这一半时，记录变化重开了一条请求，
        而 investigation 里那条答案的 assessment 仍是 `verified`——界面会同时
        看到"这条要重新补充"和"这条的答案仍然可靠"。返回（可能被改写的）``inv``，
        调用方负责存回任务：investigation 状态在任务上，不在事项上。
        """
        from .investigation import INFO_AVAILABLE, is_question_answered, InvestigationState
        from .product import SCOPE as _SCOPE
        questions = (inv or {}).get('questions') or []
        if not questions:
            return inv
        by_id = {safety_case_request_id(case_id, q): q for q in questions
                 if q.get('question_id')}
        state = InvestigationState.restore(inv, _SCOPE)
        with self.p.transaction():
            case = store.get(case_id)
            changed = False
            for request in case.get('required_inputs') or []:
                question = by_id.get(request['request_id'])
                if question is None:
                    continue
                request['information_state'] = question.get('information_state')
                request['strategy'] = question.get('strategy')
                request['strategy_history'] = list(question.get('strategy_history') or [])
                if question.get('answers'):
                    # 已知的那部分照实带上，包含它的来源属性与仍不确定的地方。
                    request['answered_parts'] = list(question['answers'])
                    request['still_uncertain'] = list(
                        question['answers'][-1].get('still_uncertain') or [])
                if is_question_answered(question):
                    request['status'] = 'answered'
                    request['answered_at'] = question.get('answered_at') or utc_now()
                changed = True
            if changed:
                # 先按被引用的真相重算状态——**重开过期回答正是这一步做的**。
                store.derive_status(case)
                # 再反向同步。顺序反了的话，重开发生在反向同步之后，
                # 两边要等到下一轮运行时才一致。
                if self._invalidate_reopened_answers(state, case):
                    changed = True
                case['revision'] += 1
                self.p.save('safety_case', case)
        return state.to_dict() if changed else inv

    def _invalidate_reopened_answers(self, state, case) -> bool:
        """事项上被重开的请求 → 调查里那条答案也不再可靠。"""
        changed = False
        for request in case.get('required_inputs') or []:
            if request.get('status') != 'open' or not request.get('answer_invalidated'):
                continue
            request_id = str(request.get('request_id') or '')
            question_id = request_id[len(f'case:{case["id"]}:'):] \
                if request_id.startswith(f'case:{case["id"]}:') else request_id
            if state.invalidate_answer(
                    question_id,
                    str(request.get('reopened_reason') or '记录变化使这条回答不再适用')):
                changed = True
            request.pop('answer_invalidated', None)
        return changed

    def _record_visit_outcome(self, task, store, case, inv) -> None:
        """把这一轮的结果记进**这次回访**（如果有的话）。

        只是记账：结果的每一条内容都从既有真相源现取，`render_result` 是纯读的。
        没有正在进行的回访就什么也不做——绝大多数调查不是回访，不该凭空产出一条。
        """
        from . import review_visits as visits_module
        visits = visits_module.ReviewVisitStore(self.p)
        visit = visits.open_for_case(case['id'])
        if visit is None:
            return
        if task.get('visit_id') and task['visit_id'] != visit['id']:
            # 这个任务服务的不是当前未结束的这次回访——它的结果不属于这里。
            # 没有这道闸，另一个回访（或一件旧任务）的结果会覆盖当前回访。
            return
        if visit.get('care_task_id') not in (None, task['id']):
            # 回访已经绑在别的任务上，同样不覆盖。
            return
        if visit.get('care_task_id') != task['id']:
            visit = visits.bind_task(visit['id'], task['id'])
        # 调查里识别出的变更候选落到这次回访的候选队列上——**仍然是候选**，
        # 来源标成模型提议。登记之后权威记录一个字节都没变，改不改由用户确认。
        for candidate in (inv or {}).get('pending_change_candidates') or []:
            current = _current_field(self.p, candidate['name'], candidate['field'])
            visits.add_candidate(
                visit['id'], name=candidate['name'], field=candidate['field'],
                before=current, after=candidate['value'],
                source=visits_module.SOURCE_MODEL_PROPOSED,
                basis={'kind': 'model_explanation',
                       'refs': [candidate.get('question_id')] if candidate.get('question_id') else [],
                       'note': candidate.get('quote')})
        fresh = store.get(case['id'])
        result = visits_module.render_result(self.p, fresh, visit, task=task)
        # 焦点 = 这次回访真正在等用户回答的那几条。引用 request_id，不复制问题正文。
        focus = [{'request_id': item['request_id'], 'question': item.get('question')}
                 for item in (fresh.get('required_inputs') or [])
                 if item.get('status') in ('open', 'unknown')]
        visits.save_result(visit['id'], result, focus=focus,
                           cursor_after=len(fresh.get('history') or []))
        status = visits_module.status_from_task(task)
        if status is not None:
            visits.set_status(visit['id'], status)

    def _safety_case_context(self, task, case, contract) -> dict[str, Any]:
        """Agent 这一轮看到的**事项上下文**。

        事项那一半由 ``SafetyCaseStore.investigation_context`` 从既有真相源现取；
        这里补上只有任务知道的那一半：上次调查做到哪、这次相对上次新增了什么、
        本轮能做什么、还剩多少预算。两边合起来是同一个字典，随 investigation
        状态一起持久化——所以恢复后不用重读整个患者。
        """
        from .safety_cases import SafetyCaseStore
        store = SafetyCaseStore(self.p)
        # 一律取**原始**事项对象：视图（case_view）与原始记录的字段名不同，
        # 在这里统一取一次，避免下游要同时认两种形状。
        case = store.get(case.get('id') or case.get('case_id'))
        context = store.investigation_context(case)
        previous = task.get('input_versions') or {}
        current = self.p.revisions()
        previous_inv = task.get('investigation') or {}
        context.update({
            'previous_investigation': {
                'termination_reason': previous_inv.get('termination_reason'),
                'mode': previous_inv.get('mode'),
                'queries': list(previous_inv.get('queries') or [])[-8:],
                'evidence_read': len(previous_inv.get('read_refs') or []),
                'evidence_captured': len(previous_inv.get('evidence_refs') or []),
                'gaps_open': len([g for g in (previous_inv.get('gaps') or [])
                                  if g.get('status') == 'open']),
                'claims': [{'statement': c.get('statement'), 'status': c.get('status')}
                           for c in (previous_inv.get('claims') or [])[:8]],
            } if previous_inv else None,
            # "相对上次新增了什么"：版本变化 + **上次消费位置之后**的事件。
            #
            # 用持久化的游标，不是"最后 8 条"：后者每轮都把同样的几条历史重新
            # 标成"新增"，于是模型永远分不清哪些是真的新到的。游标随任务持久化，
            # 重启和重试读到的是同一个位置。
            'new_since_last_run': {
                'changed_scopes': [key for key in current if current[key] != previous.get(key)],
                'since_cursor': task.get('case_history_cursor'),
                'events': [
                    {'event': entry.get('event'), 'at': entry.get('at'),
                     'request_id': entry.get('request_id'),
                     'answer_kind': entry.get('answer_kind'),
                     'answered': entry.get('answered'),
                     'from': entry.get('from'), 'to': entry.get('to'),
                     'why': entry.get('why')}
                    for entry in (case.get('history') or ())[
                        int(task.get('case_history_cursor') or 0):]],
            },
            'budget': {
                'max_cycles': contract['max_steps'],
                'remaining_task_steps': max(0, task['budget']['limit'] - task['budget']['spent']),
                'model_runs_so_far': len(task.get('runs') or []),
            },
            # 这次执行如果是一次**回访**，它另外带来一份引用式摘要。它不是第二个
            # 真相源：每一样都从事项与回访记录现取，只是把"本次该看什么"收拢成
            # 一处，省得模型自己从整段历史里翻。
            'visit': self._visit_context(task, case),
        })
        return context

    def _visit_context(self, task, case) -> dict[str, Any] | None:
        """本次回访的摘要。没有回访就是 `None`，不凭空造一个。

        **只放引用与状态**：答案给 request_id、值与它的可信性判定，候选给字段与
        前后值，事件给一句可读的叙述。不复制患者事实、药单、证据正文或结论——
        那些一律现取，摘要才不会和权威记录分叉（`review_visits` 的同一原则）。
        """
        from . import review_visits as visits_module
        from .safety_cases import (ANSWER_UNKNOWN, answer_assessment, answer_source,
                                   answer_value)
        visit_id = task.get('visit_id')
        if not visit_id:
            return None
        visits = visits_module.ReviewVisitStore(self.p)
        try:
            visit = visits.get(visit_id)
        except ProductError:
            return None
        inputs = case.get('required_inputs') or []
        entries = list(case.get('history') or [])
        start = int((visit.get('cursor') or {}).get('before') or 0)
        previous = None
        if visit.get('previous_visit_id'):
            try:
                previous = visits.get(visit['previous_visit_id'])
            except ProductError:
                previous = None
        previous_result = (previous or {}).get('result') or {}
        fresh = [visits_module.history_line(entry)
                 for entry in visits_module.news_entries(case, start)]
        return {
            'visit_id': visit['id'],
            'sequence': visits_module.sequence_of(self.p, case['id'], visit),
            'reason': dict(visit['reason']),
            'started_from': start,
            'previous_result': ({
                'unresolved': previous_result.get('unresolved') or [],
                'focus': list(previous.get('focus') or []),
                'next_step': previous_result.get('next_step'),
                'closed_at': previous.get('closed_at'),
            } if previous else None),
            'new_since_last_visit': {
                'changed_scopes': visits_module.changed_scopes(self.p, case),
                # 三种状态**分开表达**，不合流：范围变了是记录真的变了，
                # 候选是用户说了但还没确认，两者都没有才是"系统尚未收到新记录"。
                # 最后那句用 NO_NEW_RECORDS 原话，绝不定性成"情况稳定"。
                'statement': (visits_module.NO_NEW_RECORDS if not fresh
                              and not visits_module.changed_scopes(self.p, case) else None),
                'events': [line['text'] for line in fresh],
            },
            'reusable_answers': [
                {'request_id': item['request_id'], 'question': item.get('question'),
                 'value': answer_value(item), 'source': answer_source(item),
                 'assessment': answer_assessment(item),
                 'answered_against': item.get('answered_against')}
                for item in inputs if item.get('status') == 'answered'],
            'retired_answers': [
                {'request_id': item['request_id'], 'question': item.get('question'),
                 'reason': item.get('reopened_reason'),
                 'scopes': list(item.get('reopened_scopes') or [])}
                for item in inputs if item.get('answer_invalidated')],
            'pending_candidates': [
                {'candidate_id': candidate['id'], 'name': candidate['name'],
                 'field': candidate['field'], 'before': candidate.get('before'),
                 'after': candidate.get('after'), 'source': candidate['source']}
                for candidate in (visit.get('change_candidates') or [])
                if candidate['status'] == visits_module.CANDIDATE_PENDING],
            'confirmed_follow_up': visits_module.arrangement_view(case),
            'open_questions': [
                {'request_id': item['request_id'], 'question': item.get('question'),
                 'why_needed': item.get('why_needed'),
                 'for_professional': bool(item.get('for_professional'))}
                for item in inputs if item.get('status') in ('open', ANSWER_UNKNOWN)],
            'allowed_actions': list(visits_module.VISIT_NEXT_STEPS),
            'budget': {
                'remaining_task_steps': max(0, task['budget']['limit'] - task['budget']['spent']),
                'runs_so_far': len(task.get('runs') or []),
            },
        }

    @staticmethod
    def _answerable_by_user(question: dict[str, Any]) -> bool:
        """这条问题是不是用户自己能答的。

        方向为"专业复核"的问题不能由用户代答——那会把"用户说医生觉得没事"变成
        一条看起来已核实过的记录。
        """
        direction = str(question.get('direction') or '').lower()
        if direction:
            return direction in ('user_input', 'authoritative_record', 'selected_material')
        return '医生' not in str(question.get('question') or '')

    def _execute_material_review(self, task, *, run_id=None):
        """一次 material-review@2 推进。任务拥有状态、产物与累计预算；调查引擎
        拥有覆盖与交付语义。本执行器自己不产生任何领域副作用——所有领域操作都
        经过共享的 ToolExecutor 与既有证据库。"""
        from .review.advance import ReviewRunner
        contract = CONTRACTS['material_review']
        versions = {**self.p.revisions(), 'materials': self.p.memory.scope_revision('materials')}
        task['missing_inputs'] = []
        run_id = run_id or f"care-task:{task['id']}:{len(task['runs'])}"
        runner = ReviewRunner.for_product(self.p, agent=self.agent(),
                                          planner_transport=self.review_transport())
        result = runner.advance(
            task_id=task['id'], run_id=run_id, goal=task['goal'], scope_id=SCOPE,
            selected_case_ids=task.get('selected_case_ids') or [task['case_id']],
            initial_state=task.get('review_state'), max_cycles=contract['max_steps'],
            saved_budget=task.get('_queued_limits'), requested=task.get('requested'))
        review = result['review']
        task['review_state'] = review
        resources = task.get('resource_budget')
        if resources and result['run_id'] not in resources['child_run_ids']:
            resources['child_run_ids'].append(result['run_id'])
        # 用量统一从**任务保存的 run 引用**里读，不在别处再算一份。
        task['usage'] = self.usage(task)
        report = result['report']
        artifact = {'id': f'material-review-report:{uuid.uuid4().hex}', 'created_at': utc_now(),
                    'task_id': task['id'], 'goal': task['goal'], 'scope_id': SCOPE,
                    'patient_revision': self.p.revisions(),
                    'material_revision': self.p.memory.scope_revision('materials'),
                    'contract_version': REVIEW_CONTRACT, 'format_version': 1,
                    'review_revision': len(review.get('reports') or []),
                    'delivery_status': result['delivery_status'],
                    'evidence_status': result['evidence_status'],
                    'run_status': result['run_status'],
                    'axes': result['axes'], 'markdown': report['markdown'],
                    'sections': report['sections'], 'revision_diff': report['revision_diff'],
                    'gaps': report['gaps'], 'all_gaps': report.get('all_gaps') or [],
                    'input_requests': report['input_requests'],
                    'answered_requests': report.get('answered_requests') or [],
                    'safety_checks': report['safety_checks'], 'cycle_note': report['cycle_note'],
                    'evidence_refs': report.get('evidence_refs') or [],
                    'material_refs': report.get('material_refs') or [],
                    # "已读回原文"与"谁读的"是两件事，随报告一起交付，页面才分得清
                    # 哪些结论来自代码的确定性比较、哪些来自模型的解释。
                    'system_material_refs': report.get('system_material_refs') or [],
                    'model_material_refs': report.get('model_material_refs') or [],
                    'unread_material_refs': report.get('unread_material_refs') or [],
                    'invalid_material_refs': report.get('invalid_material_refs') or [],
                    'coverage_progress': report.get('coverage_progress') or {},
                    'attribution': report.get('attribution') or {},
                    # 本次承诺解决什么、逐字段比到了什么程度、用量多少。
                    'requirements': report.get('requirements') or [],
                    'field_comparisons': report.get('field_comparisons') or {},
                    'usage': task.get('usage') or {},
                    'research_decisions': report.get('research_decisions') or [],
                    'findings': report.get('findings') or [],
                    'answers': report.get('answers') or [],
                    'change_summary': report.get('change_summary') or {},
                    'partial': result['delivery_status'] != 'complete'}
        _stage_artifact(task, 'material_review_report', artifact, self.p,
                        lambda: artifact['patient_revision'] != self.p.revisions()
                        or artifact['material_revision'] != self.p.memory.scope_revision('materials'),
                        '记录或材料在核对期间发生变化，相关依据需要重新核对')
        task.setdefault('report_refs', []).append(artifact['id'])
        if artifact['partial']:
            task.setdefault('partial_report_refs', []).append(artifact['id'])
        task['report_axes'] = result['axes']
        task['report_revision'] = artifact['review_revision']
        task['subgoals'] = self._review_subgoals(review, artifact)
        degraded = result.get('degraded_reason')
        task['degraded_reason'] = degraded
        task['retry_available'] = provider_outage(degraded)
        task['degraded_label'] = PROVIDER_OUTAGE_LABEL if task['retry_available'] else None
        task['missing_inputs'] = [
            {'request_id': item['request_id'], 'field': (item.get('required_fields') or [None])[0],
             'fields': item.get('required_fields') or [],
             'question': item['question_text'], 'why_needed': item.get('why_needed'),
             'subjects': item.get('subjects') or [],
             # 用途与目标对象随请求一起给到界面：页面据此决定"提交回答"要做什么，
             # 而不是靠解析问句猜药名、猜要不要写回记录。
             'purpose': item.get('purpose'), 'target': item.get('target') or {},
             # **这条请求挡不挡必需要求**：页面靠它把"必须回答"与"可选建议"分开。
             # 不给这个字段，界面就只能把两者当成同一件事。
             'blocks_requirement_ids': item.get('blocks_requirement_ids') or [],
             'blocking': bool(item.get('blocks_requirement_ids')),
             'missing_fact': item.get('missing_fact'),
             'related_question_ids': item.get('related_question_ids') or []}
            for item in report['input_requests']]
        # 覆盖进展直接来自 review 状态里的实际处理记录，不另建一份进度。
        task['coverage_progress'] = review.get('coverage_progress') or {}
        task['read_attribution'] = review.get('read_attribution') or {}
        run_status = result['run_status']
        if run_status == 'waiting_input':
            task['status'] = 'waiting_input'
            task['waiting_reason'] = '需要您补充记录后继续核对；已交付的部分结果仍然有效'
        elif run_status == 'cancelled':
            task['status'] = 'cancelled'
            task['waiting_reason'] = '核对已取消；此前保存的记录与部分报告保留'
        elif run_status == 'failed':
            task['status'] = 'failed'
            task['waiting_reason'] = _failure_reason(degraded)
        elif result['delivery_status'] == 'complete':
            # 完成由代码控制，并要求产物**确实落盘**——落盘发生在发布事务里，
            # 所以这里只声明意图，由 publish() 兑现。
            task['result_refs'] = [artifact['id']]
            task['status'] = 'completed'
            task['waiting_reason'] = None
        else:
            # 运行已结束但交付不完整：报告是**部分结果**，内容仍然准确；
            # 任务回到"可以继续"，而不是假装完成或直接失败。
            task['status'] = 'ready'
            gaps = len(report['gaps'])
            task['waiting_reason'] = (f'已交付部分报告；本次承诺的交付要求还有 {gaps} 项未满足，'
                                      f'可以继续核对补齐')
        task['input_versions'] = versions

    def usage(self, task) -> dict:
        """任务到现在的模型用量。

        **只从任务自己保存的 run 引用读**（``resource_budget.child_run_ids``，由
        ``_enqueue_review_tx`` 在受理时就写下来）。按 ``care-task:<id>:<n>`` 这种
        字符串格式去拼 run_id 曾经让统计全部读成 0——那不是"没有调用"，是读错了
        地方。

        读不到的键一律 ``None``（对外显示 unknown），**不显示 0**：0 是一个测量
        结果，"没测到"不是同一个意思。
        """
        resources = task.get('resource_budget') or {}
        run_ids = list(resources.get('child_run_ids') or [])
        empty = {'runs': 0, 'calls': None, 'tokens': None, 'refused_calls': None,
                 'measured': False, 'reason': 'no_child_runs'}
        if not run_ids:
            return empty
        runs = [self.p.memory.workflow_run_get(run_id) for run_id in run_ids]
        missing = [run_id for run_id, run in zip(run_ids, runs) if run is None]
        budgets = [(run or {}).get('budget') or {} for run in runs if run is not None]
        if missing or not budgets:
            return {**empty, 'runs': len(run_ids), 'reason': 'run_reference_not_found',
                    'missing_run_ids': missing}
        if any(budget.get('usage_unknown') for budget in budgets):
            return {**empty, 'runs': len(run_ids), 'reason': 'provider_usage_unknown'}

        def total(key):
            values = [budget.get(key) for budget in budgets]
            if any(value is None for value in values):
                return None
            return sum(values)

        return {'runs': len(run_ids), 'calls': total('calls_attempted'),
                'tokens': total('tokens_actual'), 'refused_calls': total('refused_calls'),
                'measured': True, 'reason': None}

    def _review_subgoals(self, review, artifact):
        """子目标只做**投影**：来源是 review 状态本身，不是另建一份进度记录。"""
        subgoals = [
            {'subgoal_id': 'coverage', 'kind': 'coverage',
             'statement': '核对本次选中的材料与相关当前记录',
             'depends_on': [],
             'status': 'completed' if not review.get('coverage_pending') else 'blocked',
             'result_refs': []},
            {'subgoal_id': 'delivery', 'kind': 'delivery',
             'statement': '按本次交付要求生成报告',
             'depends_on': ['coverage'],
             'status': 'completed' if artifact['delivery_status'] == 'complete' else 'recorded',
             'result_refs': [artifact['id']]},
        ]
        for question in review.get('questions') or []:
            subgoals.append({'subgoal_id': question['question_id'], 'kind': 'question',
                             'statement': question['text'], 'depends_on': ['coverage'],
                             'status': 'completed' if question['status'] != 'open' else 'blocked',
                             'result_refs': list(question.get('related_evidence_refs') or [])})
        return subgoals

    def review_transport(self):
        """模型接入点。没有可用 planner 时返回 None：推进照常进行，只是标注
        "未经模型核查"，而不是把一次规则整理冒充成模型调查。"""
        agent = self.agent()
        planner = getattr(agent, 'planner', None)
        transport = getattr(planner, 'llm_planner', None)
        if transport is None:
            return None
        from .review.advance import _transport_available
        return transport if _transport_available(transport) else None

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

    def _record_review_answers(self, task, answers, request_ids, authoritative):
        """把用户的补充记进 review 状态，并关闭**确实被回答到**的请求。

        两类补充走两条完全不同的路：

        * ``answers`` 是**回答或候选事实**——材料上写的是什么、用户对当前情况的
          说明。它们进 ``state.answers``，**不**写权威记录，也不因为被回答过就变成
          已核实。它们只关闭对应的那几条请求。
        * ``authoritative`` 是**权威记录更新**，已经走完既有的受控写入路径（参数
          校验、预算、事务、回执都在那一条路上）；这里只记录它的来源与前后值，好让
          报告的依据里分得清"记录被改了"和"用户说了一句话"。

        只关闭被实际回答的那些请求：部分回答只关对应的那一条，其余仍然开放。
        """
        from .review.state import INPUT_AUTHORITATIVE_UPDATE, MaterialReviewState
        review = MaterialReviewState.restore(task['review_state'], SCOPE)
        open_ids = {item['request_id'] for item in review.input_requests
                    if item['status'] == 'open'}
        recorded = []
        for entry in answers or []:
            request_id = str(entry.get('request_id') or '')
            if request_id not in open_ids:
                # 对不上未决请求的补充不能顺手把什么关掉；它仍然被如实记下来，
                # 只是不改变任何请求的状态。
                review.record_answer(request_id=request_id, value=entry.get('value'),
                                     field=entry.get('field'), kind=entry.get('kind'),
                                     subjects=entry.get('subjects'))
                continue
            answer = review.record_answer(request_id=request_id, value=entry.get('value'),
                                          field=entry.get('field'), kind=entry.get('kind'),
                                          subjects=entry.get('subjects'))
            recorded.append((request_id, answer))
        for entry in authoritative or []:
            name = str(entry.get('name') or '').strip()
            if not name or not str(entry.get('value') or '').strip():
                continue
            answer = review.record_answer(
                request_id='', field=entry.get('field') or 'dose',
                value=entry['value'], kind=INPUT_AUTHORITATIVE_UPDATE, subjects=[name])
            answer['applied_authoritative'] = True
            answer['previous_value'] = entry.get('before')
        wanted = [ref for ref in (request_ids or []) if ref in open_ids]
        if not wanted:
            # 只有**确实被回答到**的请求才关闭。没有指名道姓地说答了哪一条，
            # 就不关闭任何一条——收到任意一条补充就关掉全部未决项，会让报告
            # 再也想不起那件事。
            wanted = [request_id for request_id, _ in recorded]
        for request_id, answer in recorded:
            if request_id in wanted:
                answer['applied_authoritative'] = bool(authoritative)
        answered, recorded_only = review.answer_input_requests(
            request_ids=wanted,
            answer_refs=[entry['answer_id'] for _, entry in recorded])
        task['review_state'] = review.to_dict()
        return answered, recorded_only

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

    def record_input(self, task_id, key, revision, medications=None, additional_questions=None,
                     semantic=None, review_request_ids=None, answers=None):
        """Supplement a waiting evidence_review task through the controlled
        write path, then the caller resumes the task.  Model/user input is
        data: it cannot mark the task complete or rewrite the contract.

        ``answers`` 是 material-review@2 的**普通提交回答**：它们被记成对应请求的
        回答或候选事实，**不**写权威记录。``medications`` / ``semantic`` 是**显式
        的权威写入**通道（材料核对的确认流程与既有的照护输入都走它）。两者在这里
        分开，前端的一句"提交回答"不会隐式变成一次 dose_change。
        """
        from .investigation import MAX_CLAIMS
        from .memory import SemanticFact
        allowed_namespaces = {'age', 'allergy', 'renal_function', 'hepatic_function', 'pregnancy', 'chronic_condition'}
        if answers is not None and (not isinstance(answers, list) or len(answers) > MAX_CLAIMS):
            raise ProductError('补充回答必须为范围内的列表')
        for entry in answers or []:
            if not isinstance(entry, dict) or not str(entry.get('value') or '').strip():
                raise ProductError('每条补充回答都需要内容')
            if entry.get('kind') not in (None, 'material_note', 'user_report'):
                raise ProductError('补充回答的用途只能是材料说明或情况说明')
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
            if task['goal_type'] not in REVIEW_GOAL_TYPES:
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
            # 权威写入的**原值**先取下来：报告与回执要能说出"改了什么"，
            # 而不是只说"改过了"。
            before_values = {m['display_name']: {'dose': m.get('dose'), 'schedule': m.get('schedule')}
                             for m in self.p.memory.current_medications()}
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
            answered, recorded_only = [], []
            if task['goal_type'] == 'material_review' and task.get('review_state'):
                # 只关闭**本次补充真正回答到**的请求，不是全部：未回答的请求仍然
                # 未决，报告里也必须继续写着它。
                answered, recorded_only = self._record_review_answers(
                    task, answers, review_request_ids,
                    # 已经走完受控写入路径的那几条：只留来源与前后值，供报告的依据
                    # 里说明"这一项改了当前记录、从什么改成什么"。写入本身不经过这里。
                    authoritative=[{
                        'name': item.get('name'),
                        'field': 'dose' if item.get('dose') else 'schedule',
                        'value': item.get('dose') or item.get('schedule'),
                        'before': (before_values.get(item.get('name')) or {}).get(
                            'dose' if item.get('dose') else 'schedule'),
                    } for item in medications or []])
            if task['goal_type'] == 'safety_case' and task.get('safety_case_id'):
                # 补充到达 → 写回**同一件事项**并只关闭它真正回答到的请求。
                # 用户提供的信息是"用户报告"，不是"已核实的专业记录"。
                from .safety_cases import SafetyCaseStore
                case_store = SafetyCaseStore(self.p)
                # 每条回答按它自己的 request_id 归档。一次提交里带多条回答时，
                # 把第一条的值安到每一个请求上会**张冠李戴**——用户答了 A，
                # 事项却记成他答了 B。只有"一条回答对一条请求"时才允许省略 id。
                by_request = {str(entry.get('request_id')): entry
                              for entry in (answers or []) if entry.get('request_id')}
                wanted = list(review_request_ids or [])
                # 直接操作已取出的事项对象：这里是照护待办提交的**同一个事务**，
                # 事务里再开事务会被拒绝，而且补充输入与事项更新本来就该一起提交。
                case = self.p.get(task['safety_case_id'], 'safety_case')
                for index, request_id in enumerate(wanted):
                    entry = by_request.get(str(request_id))
                    if entry is None and len(wanted) == 1 and len(answers or []) == 1:
                        entry = answers[0]
                    case_store.apply_input(
                        case, request_id=request_id,
                        answer_ref=f'care-task-input:{key}:{index}',
                        value=(entry or {}).get('value'))
                case_store.derive_status(case)
                self.p.save('safety_case', case)
                # 用户补充同时落到**调查里的那条问题**上。只写 SafetyCase 的话，
                # investigation 的问题永远停在未决，下一轮又去问一遍同一件事——
                # 这是"同一份 answered 存在两处、彼此不同步"的典型后果。
                self._sync_answers_to_investigation(task, wanted, by_request, answers, key)
            task['revision'] += 1
            self.p.save('care_task', task)
            return {'task_id': task_id, 'applied_medications': len(applied),
                    # 记下来但**没有**关闭请求的那些：用户答了，但这条请求的答案
                    # 不该由用户提供（例如"说明书怎么说"）。
                    'recorded_only_requests': recorded_only,
                    'applied_semantic_facts': facts, 'additional_questions': list(questions),
                    'answered_requests': answered,
                    # 归因：哪些补充**改了当前记录**，哪些只是被如实记下来。界面与
                    # 报告都据此区分，用户不必猜"我填的这一格到底改了什么"。
                    'authoritative_writes': len(applied),
                    'recorded_answers': len(answers or [])}
        return self.p.command(key, {'type': 'care_task_input', 'task_id': task_id, 'revision': revision,
            'medications': medications, 'semantic': semantic, 'additional_questions': additional_questions,
            'answers': answers}, execute)

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
        return invoke(lambda: tasks.create(body.get('key'), body.get('goal_type'),
                                           body.get('case_id'), body.get('due_at'),
                                           body.get('budget'), body.get('goal'),
                                           body.get('requested')))

    @app.post('/v1/material-reviews')
    def start_material_review(request: Request, body: dict):
        """助手入口：把一次对话直接变成一次材料核对，并**当场推进**。

        它和 worker 走的是同一个核心（``ReviewRunner.advance``）：入口只负责把
        用户的选择变成一次任务，业务执行循环没有第二份实现。
        """
        access(request, True)
        def run():
            created = tasks.create(body.get('key'), 'material_review', body.get('case_id'),
                                   None, body.get('budget'), body.get('goal'),
                                   body.get('requested'))
            current = product.get(created['id'], 'care_task')
            current['budget']['spent'] += 1
            current['status'] = 'running'
            current['runs'].append({'run_id': body.get('key'), 'runner': 'material-review@2@sync',
                                    'started_at': utc_now(), 'input_revision': product.revisions()})
            # 受理是一个**短事务**；调查本身不放进数据库事务（网络请求不能占着锁）。
            with product.transaction():
                product.save('care_task', current)
            tasks._execute_material_review(current)
            with product.transaction():
                current['runs'][-1].update(status=current['status'], finished_at=utc_now())
                current['revision'] += 1
                tasks.publish(current)
            return product.get(current['id'], 'care_task')
        return invoke(run)

    @app.post('/v1/safety-cases/{case_id}/investigate')
    def investigate(case_id: str, request: Request, body: dict):
        """让 Agent 围绕**这一件**安全事项展开一次有界调查。

        与材料核对的"助手入口"不同，这里**排队**而不当场执行：调查要读资料、可能要
        检索外部证据，长度不可预期；同步等待会把用户卡在一个长请求上。任务落地后由
        worker 推进，页面轮询进展。
        """
        access(request, True)
        def run():
            key = body.get('key')
            created = tasks.create(key, 'safety_case', case_id, None,
                                   body.get('budget'), body.get('goal'))
            # 受理与推进是两次不同的操作，必须用**不同的**幂等键：共用一个键会让
            # 第二次提交落进"同一提交标识不能用于不同内容"——用户看到的是 409，
            # 而不是"调查已开始"。
            return tasks.resume(created['id'], f'{key}:run', created['revision'],
                                'continue', enqueue=True)
        return invoke(run)

    @app.post('/v1/care-tasks/{task_id}/input')
    def record_input(task_id: str, request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.record_input(task_id, body.get('key'), body.get('revision'),
                                                 body.get('medications'), body.get('additional_questions'),
                                                 body.get('semantic'), body.get('review_request_ids'),
                                                 body.get('answers')))

    @app.post('/v1/care-tasks/{task_id}/resume')
    def resume(task_id: str, request: Request, body: dict):
        access(request, True)
        return invoke(lambda: tasks.resume(task_id, body.get('key'), body.get('revision'), body.get('action', 'continue'), enqueue=True))

    @app.get('/v1/investigation-reports/{report_id}')
    def report(report_id: str, request: Request):
        access(request)
        return invoke(lambda: product.get(report_id, 'investigation_report'))

    @app.get('/v1/material-review-reports/{report_id}')
    def material_review_report(report_id: str, request: Request):
        access(request)
        return invoke(lambda: product.get(report_id, 'material_review_report'))

    @app.get('/v1/material-reviews')
    def material_reviews(request: Request):
        access(request)
        return invoke(lambda: {
            'items': [t for t in product.objects('care_task')
                      if t.get('goal_type') == 'material_review'],
            'contract': REVIEW_CONTRACT,
            'axes': {'run': ['running', 'waiting_input', 'ended', 'cancelled', 'failed'],
                     'delivery': ['none', 'partial', 'complete'],
                     'evidence': ['verified', 'conflicting', 'insufficient']}})

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
