"""有限真实模型验收：一次**回访**，接着**同一事项的第二次回访**。

范围在开跑**之前**就定死，并且打印出来——不是跑完再拿结果去解释标准：

* 一个场景：**场景 B + 第二次回访**——建事项、开始回访、真实模型接手、
  它提出问题就由用户回答、再让它继续一次；第一次收尾，期间发生一次相关变化，
  然后开始**第二次**回访；
* 请求上限 `--max-calls`（默认 12）、时间上限 `--wall-seconds`（默认 300）、
  token 上限 `--max-tokens`（默认 150000）；
* 停止条件：任一上限用尽 / 回访到达终态 / 异常，**先到先停**；
* **等待用户输入期间不消费模型**：回访在 `waiting_input` 停下时，进程不发起任何调用，
  由调用方"像用户一样"回答之后再恢复。

第二次回访是本轮的判据所在，所以它单独记一段：那一轮**看到了什么**
（第一次的答案、第一次留下的未决、上次之后的事件）、**选了什么动作**、
**有没有为了重新得到同一结论再查一遍**。

它**不**回答"模型能不能自主回访"这种大问题。它只看四件事：

1. 它有没有**使用上次的结果**（第二次看到的与第一次留下的是不是同一件事）；
2. 它有没有聚焦**实际变化**（还是泛泛而谈、或把整段历史重报一遍）；
3. 用户回答之后，它的下一步有没有**变化**；
4. 它交付的结果是不是具体、带来源（还是长篇泛化建议）。

**已有信息充分时少问且正确交付，同样算通过**——不要求它必须提问。

隔离合成数据：临时库，不碰 `stage0/memory.db`，不产出任何临床建议。
失败后不追加批次、不换模型、不扩大预算：结果如实保留。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SYNTHETIC_WARNING = {
    "drug_a": "合成药甲", "drug_b": "合成药乙", "severity": "major",
    "mechanism": "合成机制", "effect": "合成风险效应", "management": None,
    "source_text": "合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）",
    "source_url": "https://synthetic.invalid/label/a",
    "confidence": "high", "detection_path": "synthetic_detector",
}


def synthetic_detect(medications):
    if {"合成药甲", "合成药乙"}.issubset(set(medications)):
        return [dict(SYNTHETIC_WARNING)]
    return []


#: 隔离合成资料。合成药物不可能出现在真实药品库里，所以必须自带语料——
#: 否则模型只能对着一个查不到东西的检索器打转，测的就不是它的判断力。
SYNTHETIC_CORPUS = [
    {"chunk_id": "syn-a", "drug_name": "合成药甲", "section": "药物相互作用",
     "text": "【合成资料】合成药甲与合成药乙合用可能增加合成风险，需关注用法与监测。",
     "source_url": "https://synthetic.invalid/label/a", "corpus_version": "synthetic-v1"},
    {"chunk_id": "syn-b", "drug_name": "合成药乙", "section": "注意事项",
     "text": "【合成资料】合成药乙的合成风险与服用频次相关：频次越高，监测要求越严。",
     "source_url": "https://synthetic.invalid/label/b", "corpus_version": "synthetic-v1"},
]


class SyntheticCorpusRAG:
    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "synthetic-isolated",
                "corpus_version": "synthetic-v1",
                "results": [dict(chunk) for chunk in SYNTHETIC_CORPUS]}


def _apply_budget(product, case_view, args):
    """把上限写进这次回访的 care_task 的 `resource_budget`，**开跑之前**生效。"""
    visit = case_view.get('visit') or {}
    task_id = visit.get('care_task_id')
    if not task_id:
        return
    task = product.get(task_id, 'care_task')
    resources = task.get('resource_budget') or {}
    resources['token_limit'] = int(args.max_tokens)
    resources['call_limit'] = int(args.max_calls)
    task['resource_budget'] = resources
    with product.transaction():
        product.save('care_task', task)


def _usage(product, task):
    """这一件回访**实际发生**的模型用量。

    从任务自己保存的 run 引用汇总（`resource_budget.child_run_ids`），因此
    **包含同一次回访的恢复运行**——恢复也是这次回访花的钱。读不到的键一律
    ``None``（unknown），**不记 0**：0 是一个测量结果，"没测到"不是同一个意思。

    上一版直接读 `resource_budget.calls_actual`，而那个字段在 safety_case 上
    从来没被重算过，于是报告里出现"0 次调用"与"模型确实调用过"并存的自相矛盾。
    """
    from stage0.care_tasks import CareTasks
    measured = CareTasks(product).usage(task)
    resources = task.get('resource_budget') or {}
    return {'calls_actual': measured.get('calls'),
            'tokens_actual': measured.get('tokens'),
            'runs_actual': measured.get('runs'),
            'refused_calls': measured.get('refused_calls'),
            'measured': measured.get('measured'),
            'unknown_reason': measured.get('reason'),
            'calls_reserved': resources.get('calls_reserved'),
            'token_limit': resources.get('token_limit'),
            'call_limit': resources.get('call_limit')}


def _run_trace(memory, task, limit=12):
    """这一轮**实际的决策轨迹**：被拒的提案、工具失败、采纳结果。

    没有它，"它没做出来"就只剩一句结论——看不出卡在哪一步、收到了什么反馈。
    """
    rejections, failures, adoptions = [], [], []
    for child in (task.get('resource_budget') or {}).get('child_run_ids', []):
        run = memory.workflow_run_get(child) or {}
        for entry in (run.get('result') or {}).get('trace') or []:
            planner = entry.get('planner') or {}
            if entry.get('phase') == 'plan' and planner.get('fallback_kind'):
                rejections.append({'cycle': entry.get('cycle'),
                                   'kind': planner.get('fallback_kind'),
                                   'errors': planner.get('errors') or planner.get('error'),
                                   'arguments': planner.get('call_arguments')})
            if entry.get('phase') == 'observe' and entry.get('ok') is False:
                failures.append({'cycle': entry.get('cycle'), 'tool': entry.get('tool'),
                                 'error_kind': (entry.get('observation') or {}).get('error_kind')})
            if entry.get('phase') == 'observe' and entry.get('tool') == 'answer_question':
                outcome = (entry.get('observation') or {}).get('result') or {}
                if isinstance(outcome, dict):
                    adoptions.append({'cycle': entry.get('cycle'),
                                      'accepted': outcome.get('accepted'),
                                      'errors': outcome.get('errors')})
    return {'rejections': rejections[:limit], 'tool_failures': failures[:limit],
            'adoptions': adoptions[:limit]}


def _planner_digest(task, limit=10):
    """这一轮模型**实际选了什么**：动作、引用的依据、被拒的提案。

    只有动作与依据，不含长篇推理——要回答的是"它有没有用上这位患者的历史"，
    不是"它写了什么文章"。
    """
    inv = task.get('investigation') or {}
    trace = []
    for run in inv.get('plan_revisions') or []:
        trace.append({'reason': run.get('reason'), 'at': run.get('at')})
    return {
        'termination_reason': inv.get('termination_reason'),
        'degraded_reason': task.get('degraded_reason'),
        'waiting_reason': task.get('waiting_reason'),
        'mode': inv.get('mode'),
        'questions': [{
            'question_id': q.get('question_id'),
            'statement': q.get('statement'),
            'information_target': q.get('information_target'),
            'strategy': q.get('strategy'),
            'target_field': q.get('target_field'),
            'why': q.get('why'),
            'information_state': q.get('information_state'),
            'answers': [{'value': a.get('value'), 'field': a.get('field'),
                         'provenance': a.get('provenance'),
                         'assessment': (a.get('assessment') or {}).get('status')}
                        for a in (q.get('answers') or [])],
        } for q in (inv.get('questions') or [])][:limit],
        'change_candidates': inv.get('pending_change_candidates') or [],
        'plan_revisions': trace[:limit],
    }


def _visit_snapshot(store, case_id):
    case = store.get(case_id)
    return {
        'status': case['current_status'],
        'required_inputs': [{'request_id': i['request_id'], 'question': i.get('question'),
                             'strategy': i.get('question_strategy'),
                             'kind': i.get('question_kind'), 'status': i.get('status')}
                            for i in case.get('required_inputs') or []],
        'answered_inputs': [{'request_id': i['request_id'], 'question': i.get('question'),
                             'answer_kind': i.get('answer_kind')}
                            for i in case.get('required_inputs') or []
                            if i.get('status') == 'answered'],
        'resolution_basis': case.get('resolution_basis'),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True, help='输出 JSON 路径（已存在则拒绝覆盖）')
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--wall-seconds', type=float, default=300.0)
    parser.add_argument('--max-tokens', type=int, default=150_000)
    parser.add_argument('--max-cycles', type=int, default=6)
    parser.add_argument('--answer', default='每日两次，早晚各一次',
                        help='用户对这一轮补问的回答（作为**用户输入**，不是给模型的提示）')
    args = parser.parse_args()

    out = Path(args.out).resolve()
    if out.exists():
        raise SystemExit(f'保留既有验收产物；请换一个输出路径：{out}')
    out.parent.mkdir(parents=True, exist_ok=True)

    # 预算在进程内也写死，避免"看到的限制"和"真正生效的限制"是两回事。
    os.environ['AGENT_TURN_CALL_BUDGET'] = str(args.max_calls)
    os.environ['AGENT_TURN_BUDGET_SECONDS'] = str(args.wall_seconds)
    os.environ['AGENT_LLM_PLANNER'] = '1'

    from fastapi.testclient import TestClient

    from stage0 import server
    from stage0.agent import DDITool, MedicationCoordinatorAgent
    from stage0.memory import MemoryStore
    from stage0.product import ProductStore
    from stage0 import safety_checks as checks
    from stage0.safety_cases import SafetyCaseStore

    limits = {'scenario': 'B_related_change_then_second_visit',
              'max_calls': args.max_calls, 'wall_seconds': args.wall_seconds,
              'max_tokens': args.max_tokens, 'max_cycles': args.max_cycles,
              'visits': 2}
    print(json.dumps({'declared_limits': limits}, ensure_ascii=False), flush=True)

    report = {'declared_limits': limits,
              'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'model_consumed_while_waiting_for_user': False}
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix='visit-live-') as directory:
        # **只用一个 MemoryStore**：`create_app` 自己开一个，我们就用它。
        # 另开第二个实例指向同一个 SQLite 文件会让两边看到的不是同一份状态。
        holder: dict = {}

        def factory():
            return MedicationCoordinatorAgent(
                holder['store'], ddi_tool=DDITool(synthetic_detect),
                rag_tool=SyntheticCorpusRAG(),
                max_cycles=args.max_cycles, llm_planner_enabled=True)

        app = server.create_app(db_path=Path(directory) / 'memory.db',
                                worker_thread=False, agent_factory=factory)
        holder['store'] = app.state.store
        memory = app.state.store
        product = ProductStore(memory)
        client = TestClient(app)
        worker = app.state.worker
        store = SafetyCaseStore(product)
        try:
            # 1) 必要检查（纯代码）先把事项建起来——这一步不花模型调用。
            for index, name in enumerate(('合成药甲', '合成药乙')):
                memory.apply_medication_change(
                    action='add', name=name, ingredients=[], session_id='synthetic',
                    turn_id=f'seed-{index}', source='caregiver')
            checks.run_necessary_checks(memory, detector=synthetic_detect,
                                        product=product)
            cases = store.objects()
            report['case_created_without_model'] = len(cases) == 1
            if not cases:
                report['status'] = 'setup_failed'
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding='utf-8')
                return 1
            case_id = cases[0]['id']

            # 2) 走**产品入口**开始一次回访；真实模型接手。
            def drain(rounds=8):
                for _ in range(rounds):
                    worker.drain_once(max_tasks=4)

            view = client.get(f'/v1/safety-cases/{case_id}').json()
            response = client.post(f'/v1/safety-cases/{case_id}/visits',
                                   json={'key': 'live-visit-1',
                                         'expected_revision': view['revision']})
            report['visit_start_status'] = response.status_code
            # 把**这次运行**的预算写进任务，再放它跑：`--max-calls` / `--max-tokens`
            # 必须在开跑前生效，否则"打印出来的上限"和"真正生效的上限"是两回事。
            _apply_budget(product, client.get(f'/v1/safety-cases/{case_id}').json(),
                          args)
            drain()

            visit = client.get(f'/v1/safety-cases/{case_id}').json().get('visit') or {}
            report['visit'] = {'visit_id': visit.get('visit_id'),
                               'status': visit.get('status'),
                               'reason': visit.get('reason'),
                               'first_visit': visit.get('first_visit'),
                               'focus': visit.get('focus')}
            task_id = visit.get('care_task_id')
            task = product.get(task_id, 'care_task') if task_id else None
            if task is None:
                report['status'] = 'visit_task_missing'
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding='utf-8')
                return 1
            report['round_1'] = _planner_digest(task)
            report['round_1']['usage'] = _usage(product, task)
            report['round_1']['trace'] = _run_trace(memory, task)

            # 3) 它提问就**像用户一样**回答；等待期间不消费模型。
            before = _visit_snapshot(store, case_id)
            report['round_1_state'] = before
            pending = [item for item in before['required_inputs']
                       if item['status'] == 'open' and item['strategy'] == 'ask_user']
            report['not_user_answerable'] = [item for item in before['required_inputs']
                                             if item['status'] == 'open'
                                             and item['strategy'] != 'ask_user']
            if pending:
                ask = pending[0]
                report['asked'] = ask
                client.post(f'/v1/safety-cases/{case_id}/answer',
                            json={'key': 'live-answer-1',
                                  'expected_revision':
                                      client.get(f'/v1/safety-cases/{case_id}').json()['revision'],
                                  'request_id': ask['request_id'],
                                  'value': args.answer})
                drain()
                task_now = product.get(task_id, 'care_task')
                report['round_2'] = _planner_digest(task_now)
                report['round_2']['usage'] = _usage(product, task_now)
                report['round_2']['trace'] = _run_trace(memory, task_now)
                report['round_2_state'] = _visit_snapshot(store, case_id)
                # "下一步有没有变化"：这一轮的动作集合与上一轮是否不同。
                report['changed_next_step'] = (
                    report['round_2']['termination_reason']
                    != report['round_1']['termination_reason']
                    or len(report['round_2']['questions'])
                    != len(report['round_1']['questions']))
            else:
                report['asked'] = None
                report['changed_next_step'] = None
                report['round_2'] = None

            # 4) **第二次回访**：这才是本轮的判据所在。
            #
            # 第一次收尾 → 期间发生一次相关变化 → 再开始一次回访。要看的是：
            # 它有没有把第一次的结论带过来、有没有聚焦那次变化、有没有为了
            # 重新得到同一结论再检索一遍。
            from stage0 import review_visits as visits_module
            visits = visits_module.ReviewVisitStore(product)
            first_visit = visits.open_for_case(case_id)
            if first_visit is not None:
                # 第一次收尾：只有收尾了，第二次才会有"上一次"。
                visits.set_status(first_visit['id'], visits_module.STATUS_COMPLETED)

            memory.apply_medication_change(
                action='add', name='合成药丙', ingredients=[], session_id='synthetic',
                turn_id='live-change-1', source='caregiver')
            checks.run_necessary_checks(memory, detector=synthetic_detect,
                                        product=product)
            view2 = client.get(f'/v1/safety-cases/{case_id}').json()
            response = client.post(f'/v1/safety-cases/{case_id}/visits',
                                   json={'key': 'live-visit-2',
                                         'expected_revision': view2['revision']})
            report['second_visit_start_status'] = response.status_code
            _apply_budget(product, client.get(f'/v1/safety-cases/{case_id}').json(),
                          args)
            drain()

            second = client.get(f'/v1/safety-cases/{case_id}').json().get('visit') or {}
            second_task_id = second.get('care_task_id')
            second_task = product.get(second_task_id, 'care_task') if second_task_id else None
            report['second_visit'] = {
                'visit_id': second.get('visit_id'),
                'status': second.get('status'),
                'reason': second.get('reason'),
                'first_visit': second.get('first_visit'),
                'focus': second.get('focus'),
            }
            if second_task is not None:
                # 它这一轮**看到了什么**：第一次的答案、第一次留下的未决、
                # 上次之后的事件，逐个记下来，而不是只记一个结论。
                from stage0.care_tasks import CareTasks
                context = CareTasks(product)._safety_case_context(
                    second_task, store.get(case_id), {'max_steps': args.max_cycles})
                seen = context.get('visit') or {}
                report['second_visit_context'] = {
                    'sequence': seen.get('sequence'),
                    'previous_visit_id': seen.get('previous_visit_id'),
                    'reusable_answers': [
                        {'question': item.get('question'), 'value': item.get('value'),
                         'assessment': (item.get('assessment') or {}).get('status')}
                        for item in seen.get('reusable_answers') or []],
                    'retired_answers': [
                        {'question': item.get('question'), 'reason': item.get('reason')}
                        for item in seen.get('retired_answers') or []],
                    'new_since_last_visit': {
                        'changed_scopes': (seen.get('new_since_last_visit') or {}).get('changed_scopes'),
                        'events': (seen.get('new_since_last_visit') or {}).get('events')},
                    'previous_unfinished': (seen.get('previous_result') or {}).get('unresolved'),
                }
                report['second_visit_round'] = _planner_digest(second_task)
                report['second_visit_round']['usage'] = _usage(product, second_task)
                report['second_visit_round']['trace'] = _run_trace(memory, second_task)
                # **有没有为了重新得到同一结论再查一遍。**
                first_queries = (task.get('investigation') or {}).get('queries') or []
                second_queries = (second_task.get('investigation') or {}).get('queries') or []
                report['re_searched_the_same_conclusion'] = (
                    bool(second_queries) and list(second_queries) == list(first_queries))

            # 5) 交付了什么。
            final = client.get(f'/v1/safety-cases/{case_id}').json()
            visit = final.get('visit') or {}
            result = visit.get('result') or {}
            report['visit_result'] = {
                'why': result.get('why'),
                'since_last': [line['text'] for line in result.get('since_last') or []],
                'reused': [line['text'] for line in result.get('reused') or []],
                'recheck': [line['text'] for line in result.get('recheck') or []],
                'unresolved': [line['text'] for line in result.get('unresolved') or []],
                'end_reason': result.get('end_reason'),
                'next_step': result.get('next_step'),
                'next_arrangement': result.get('next_arrangement'),
                'answered_count': result.get('answered_count'),
            }
            report['visit_final_status'] = visit.get('status')

            # 越界检查：模型不得自行关闭事项，也不得把"没查到"写成"没问题"。
            report['model_closed_the_case'] = final['status'] == 'resolved'
            report['case_status'] = final['status']
            evidence = store.closure_evidence(store.get(case_id))
            report['closure_evidence_ok'] = evidence['ok']
            report['closure_blocked_by'] = evidence['reason']
            report['candidates_from_model'] = [
                {'name': c.get('name'), 'field': c.get('field'),
                 'before': c.get('before'), 'after': c.get('after'),
                 'source': c.get('source')}
                for c in (visit.get('change_candidates') or [])
                if c.get('source') == 'model_proposed']
            # 模型提议的候选**不得**改动权威记录。
            report['medications_after'] = [
                {'name': m['display_name'], 'dose': m.get('dose')}
                for m in app.state.store.current_medications()]
        finally:
            client.close()
            worker.stop()
            app.state.store.close()

    report['elapsed_seconds'] = round(time.perf_counter() - started, 2)
    report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'ok', 'out': str(out),
                      'elapsed_seconds': report['elapsed_seconds']},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
