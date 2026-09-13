"""material-review@2 —— 统一任务推进。

**一个核心，三个入口。** 聊天入口、持久任务 worker 和评测入口各自处理自己的请求
与展示，但都调用这里：入口不得复制业务执行循环——分别维护预算、无进展、取消、
异常交付和工具执行的语义，正是"同一件事三个答案"的来源。

一次推进：

    读取任务与有效上下文
    → 获取模型决策
    → 校验
    → 执行领域操作
    → 记录观察和结果
    → 保存任务状态
    → 判断继续、等待或交付

网络请求不放进长数据库事务：状态在每一步之后通过既有 ``workflow_run_update``
落盘，远端结果未知的情况靠**预留与恢复**处理，而不是靠一个跨网络的锁。
"""
from __future__ import annotations

import json
import os
import time
import uuid

from ..harness.progress import NoProgressTracker, cancel_event_for, read_signature
from ..harness.runtime import RunContext, principal_from
from ..memory import utc_now
from . import capabilities as caps
from .contract import build_task_spec
from .delivery import (BLOCKING_GAPS, check_delivery, evidence_axis, hard_stop_reason,
                       refresh_requirements)
from .incremental import (apply_change, build_report_record, compute_change_summary,
                          invalidate_sources, refresh_from_materials)
from .report import diff_reports, report_markdown, section_content
from .contract import REQ_PENDING
from .state import (DELIVERY_COMPLETE, DELIVERY_PARTIAL, ISSUE_BUDGET, ISSUE_CONNECTION,
                    ISSUE_INVALID_ARGUMENTS, ISSUE_NO_MATCH, ISSUE_PERMISSION_DENIED,
                    ISSUE_SOURCE_INVALID, MaterialReviewState, RUN_CANCELLED, RUN_ENDED,
                    RUN_FAILED, RUN_RUNNING, RUN_WAITING_INPUT)

GRAPH_VERSION = 'material-review@2'
MAX_REJECTIONS = 3
# 一次推进里最多跑几趟基础覆盖。每趟有界，趟数有限：份数多于一批是材料数量问题，
# 不该让覆盖停在半路。到顶之后剩下的条目会被明确记为"本次尚未处理"。
BASE_COVERAGE_PASSES = 12
ERROR_KIND_TO_ISSUE = {
    'invalid_arguments': ISSUE_INVALID_ARGUMENTS, 'unknown_tool': ISSUE_INVALID_ARGUMENTS,
    'permission_denied': ISSUE_PERMISSION_DENIED, 'evidence_unavailable': ISSUE_SOURCE_INVALID,
    'retrieval_empty': ISSUE_NO_MATCH, 'budget_exhausted': ISSUE_BUDGET,
    'policy_violation': ISSUE_INVALID_ARGUMENTS, 'internal_error': ISSUE_CONNECTION,
}


class ReviewRunner:
    """持有依赖并执行推进。**不拥有**任务状态：状态由调用方持久化。"""

    def __init__(self, *, memory, evidence_store, agent=None, product=None,
                 planner_transport=None, material_index=None, emit_progress=None):
        self.memory = memory
        self.evidence_store = evidence_store
        self.agent = agent
        self.product = product
        self.planner_transport = planner_transport
        self.material_index = material_index
        self.emit_progress = emit_progress

    @classmethod
    def for_product(cls, product, *, agent=None, planner_transport=None, emit_progress=None):
        """三个入口共用的构造：材料索引与证据库都由产品装配，入口只提供模型。"""
        from ..harness.evidence import EvidenceStore
        from ..product import MaterialIndex
        memory = product.memory
        return cls(memory=memory, evidence_store=EvidenceStore(memory.connection, memory._lock),
                   agent=agent, product=product, planner_transport=planner_transport,
                   material_index=MaterialIndex(product), emit_progress=emit_progress)

    # ---- 对外唯一入口 --------------------------------------------------------

    def advance(self, *, task_id, run_id, goal, scope_id, selected_case_ids,
                user_questions=None, initial_state=None, max_cycles=None,
                saved_budget=None, session_id='local-demo', turn_id=None,
                principal=None, review_index=None, requested=None) -> dict:
        index = review_index or self.material_index
        state = self._load_state(task_id=task_id, goal=goal, scope_id=scope_id,
                                 selected_case_ids=selected_case_ids,
                                 user_questions=user_questions, initial_state=initial_state,
                                 index=index, max_cycles=max_cycles, requested=requested)
        ctx = RunContext(run_id, turn_id or run_id, session_id=session_id,
                         principal=principal_from(principal))
        ctx.cancel_event = cancel_event_for(run_id)
        trace: list = []
        tracker = NoProgressTracker(self.memory.connection, self.memory._lock)
        transport = self.planner_transport
        turn = _turn_state(state, index)
        cycles = 0
        degraded_reason = None
        # 连续被拒的提案次数与上一次被拒的**签名**：用来区分"模型在改"和
        # "模型在原地重复同一个错误"。上限与旧路径同源。
        consecutive_rejections = 0
        last_rejection = None
        try:
            rejection_limit = max(1, int(os.getenv('PLANNER_SAFETY_REJECTION_LIMIT', '2')))
        except ValueError:
            rejection_limit = 2
        try:
            from ..turn_budget import budget_scope
            # 预算会话要求 run 已落盘（预留、租约与恢复都挂在它上面）。已用的额度
            # 随 run 一起持久化，不在恢复时被清空——所以这里不重新声明 ``saved``。
            self._begin_run(run_id, saved_budget)
            limit = max_cycles or state.spec.resource_limits['max_cycles']
            with budget_scope(self.memory, run_id, limit):
                while state.run_status == RUN_RUNNING:
                    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                        # 取消是运行状态，不是失败：已获得的证据与已落盘的发现
                        # 全部保留，报告照常交付，只是标注它是被取消的。
                        degraded_reason = 'cancelled'
                        state.run_status = RUN_CANCELLED
                        break
                    if self._cycle_budget_exhausted(turn):
                        degraded_reason = 'budget_exhausted'
                        break
                    self._refresh_world(state, index)
                    if hard_stop_reason(state):
                        degraded_reason = 'permission_denied'
                        state.run_status = RUN_FAILED
                        state.termination_reason = 'permission_denied'
                        break
                    if _wrap_up_due(turn, limit):
                        # 收尾额度是**划分**出来的：到了就交付手上已有的准确结果，
                        # 而不是把最后一轮也花在调查上、然后一条报告都没有。
                        outcome = self._attempt_delivery(state, index)
                        trace.append({'phase': 'deliver', 'cycle': turn.cycle,
                                      'wrap_up': True, **outcome['trace']})
                        break
                    state.review_context = self._prepare_context(state, index, ctx)
                    proposal, plan_note = self._plan(state, turn, transport, trace)
                    if proposal is None:
                        if plan_note == 'proposal_rejected':
                            # 一次不合格的提案**不是**一次失败的调查：把结构化反馈
                            # 交回去，让模型改一个参数再来。只有同一个错误反复出现，
                            # 或连续被拒到上限，才停下来——否则一个参数笔误就会让
                            # 整轮调查什么都没查到就结束。
                            signature = json.dumps(
                                (state.pending_correction or {}).get('rejection_reasons'),
                                sort_keys=True, ensure_ascii=False)
                            consecutive_rejections += 1
                            _budget_cycle(turn)
                            self._persist(run_id, state, trace)
                            if signature == last_rejection or consecutive_rejections >= rejection_limit:
                                degraded_reason = 'planner_circuit_break:repeated_rejection'
                                break
                            last_rejection = signature
                            continue
                        degraded_reason = degraded_reason or plan_note
                        break
                    consecutive_rejections = 0
                    last_rejection = None
                    # 归因：模型**确实参与过**这一轮的判断。计在提案被接受的地方，
                    # 而不是计在某条执行路径上——"决定交付"同样是模型做的判断，
                    # 漏掉它会让一次有模型参与的运行被记成"模型没参与"。
                    state.model_cycles += 1
                    if proposal.get('tool') == 'request_delivery':
                        outcome = self._attempt_delivery(state, index)
                        trace.append({'phase': 'deliver', 'cycle': turn.cycle, **outcome['trace']})
                        _budget_cycle(turn)
                        if outcome['delivered']:
                            break
                        state.pending_correction = {'previous_proposal_was_rejected': True,
                                                    'rejection_reasons': outcome['gaps'],
                                                    'instruction': outcome['summary']}
                        verdict = tracker.record(
                            ctx.run_id, 'deliver:' + _op_key({'gaps': len(outcome['gaps'])}),
                            limit=self._no_progress_limit(), new_information=False)
                        if verdict['verdict'] == 'stop':
                            degraded_reason = 'no_progress:repeated_delivery_attempts'
                            break
                        self._persist(run_id, state, trace)
                        continue
                    adopted, note = self._execute(state, turn, ctx, proposal, index, tracker)
                    trace.append(note)
                    _budget_cycle(turn)
                    if adopted == 'stop':
                        degraded_reason = degraded_reason or note.get('degraded_reason')
                        break
                    self._persist(run_id, state, trace)
                    if state.run_status != RUN_RUNNING:
                        break
                if state.run_status == RUN_RUNNING:
                    state.run_status = RUN_ENDED
        except Exception as exc:  # 交付失败不等于丢掉已有结果
            degraded_reason = f'{type(exc).__name__}: {exc}'
            state.run_status = RUN_FAILED
            trace.append({'phase': 'error', 'error': type(exc).__name__,
                          'detail': str(exc)[:300]})
        report = self._finish(state, run_id, index, degraded_reason)
        result = {'task_id': task_id, 'run_id': run_id, 'review': state.to_dict(),
                  'report': report, 'termination_reason': state.termination_reason,
                  'run_status': state.run_status, 'delivery_status': state.delivery_status,
                  'evidence_status': state.evidence_status, 'cycles': cycles,
                  'degraded_reason': degraded_reason, 'trace': trace,
                  'axes': {'run': state.run_status, 'delivery': state.delivery_status,
                           'evidence': state.evidence_status}}
        self._persist(run_id, state, trace, status=self._run_status_word(state, degraded_reason),
                      result=result)
        return result

    # ---- 状态装配 ------------------------------------------------------------

    def _load_state(self, *, task_id, goal, scope_id, selected_case_ids, user_questions,
                    initial_state, index, max_cycles, requested=None) -> MaterialReviewState:
        if initial_state:
            state = MaterialReviewState.restore(initial_state, scope_id)
            # 恢复就是**继续**：上一轮停在哪个终态（等待补充、已结束、已取消、失败）
            # 都是上一轮的结论，不是这一次的起点。不重置它，"继续核对"会一次都不
            # 执行就原样返回——看起来像"没有新进展"，实际是循环根本没进去。
            state.run_status = RUN_RUNNING
            state.termination_reason = None
            return state
        facts = self.memory.snapshot()
        versions = {'medications': self.memory.scope_revision('medications'),
                    'semantic': self.memory.scope_revision('semantic'),
                    'materials': self.memory.scope_revision('materials')}
        material_items = self._material_items(index, selected_case_ids)
        spec = build_task_spec(task_id=task_id, user_goal=goal, scope_id=scope_id,
                               selected_case_ids=selected_case_ids,
                               material_items=material_items,
                               medications=facts.get('medications') or [],
                               input_versions=versions,
                               requested=requested,
                               resource_limits={'max_cycles': max_cycles} if max_cycles else None)
        state = MaterialReviewState(spec)
        state.facts = facts
        # 基础覆盖是**运行器**的第一件事，不是模型的第一个决定：用户选中的材料
        # 会被实际读取、校验并完成确定性比较，不取决于模型愿不愿意逐条调用工具。
        self._run_base_coverage(state, index)
        # 用户的问题：身份取自**用户说的那件事本身**。同一个问题问两次是同一条，
        # 换一个说法问就是另一件要查的事——这两条都符合直觉，也不与"模型问题的
        # 身份不得由措辞决定"冲突（那条规则针对的是模型的调查拆分）。
        #
        # 任务目标**不**在这里变成一条待核实的问题：它是本次要交付的东西，已经
        # 写在报告第 1 节；把它当成未决问题会让每份报告都"自带一条查不完的问题"。
        for text in (user_questions or []):
            state.submit_question(question_key='user_intent', text=text, subjects=[text],
                                  origin='user')
        self._run_safety_checks(state, facts)
        state.sync_coverage()
        return state

    def _material_items(self, index, selected_case_ids):
        if index is None:
            return []
        wanted = set(selected_case_ids or [])
        return [(material['case_id'], material.get('items') or [])
                for material in (index.index().get('materials') or [])
                if material['case_id'] in wanted]

    def _run_safety_checks(self, state, facts):
        """既有的**强制安全检查**照常执行，结果与本次调查成果**分别记录**。

        它是确定性的、有界的一次检查，不是"全部药物组合调查"：结果进入报告的独立
        小节，不写成本任务的 findings/questions。
        """
        names = [str(medication.get('display_name')) for medication in facts.get('medications') or []
                 if medication.get('display_name')]
        if len(names) < 2:
            return
        try:
            from .. import ddi_engine
            warnings = ddi_engine.detect(names)
        except Exception as exc:
            state.safety_checks.append({'summary': f'强制安全检查未能执行（{type(exc).__name__}）；'
                                                  f'这不代表没有风险，请与医生或药师核对。',
                                        'status': 'not_executed'})
            return
        if not warnings:
            state.safety_checks.append({'summary': '强制安全检查：当前药单未检出已知相互作用提示。',
                                        'status': 'clear'})
            return
        for warning in warnings:
            state.safety_checks.append({
                'summary': f"强制安全检查检出：{warning.get('drug_a')} / {warning.get('drug_b')} — "
                           f"{warning.get('effect') or warning.get('source_text') or '需要核对'}",
                'status': 'warning', 'drug_a': warning.get('drug_a'), 'drug_b': warning.get('drug_b')})

    # ---- 每轮 ----------------------------------------------------------------

    def _refresh_world(self, state, index) -> None:
        self._refresh_stale_cases(state)
        state.facts = self.memory.snapshot()
        versions = {'medications': self.memory.scope_revision('medications'),
                    'semantic': self.memory.scope_revision('semantic'),
                    'materials': self.memory.scope_revision('materials')}
        if versions != state.spec.input_versions and state.findings:
            # 增量更新：只重新处理受影响的部分，并把"变化了什么"作为摘要交给模型。
            change = compute_change_summary(state, index, versions)
            if change['material_changed'] or change['material_added'] or change['material_removed']:
                apply_change(state, change)
        state.spec.input_versions = versions
        if index is not None:
            refresh_from_materials(state, index)
        # 基础覆盖每轮都跑一遍：已经读过、版本没变的条目直接跳过，所以它是增量的；
        # 材料改版或新增条目会在这一趟被读进来，而不是等模型想起来去读。
        self._run_base_coverage(state, index)
        invalidate_sources(state, self.evidence_store)
        state.sync_coverage()

    def _run_base_coverage(self, state, index, *, max_passes=BASE_COVERAGE_PASSES) -> dict:
        """把选中的材料核对完。它**不**消耗模型规划调用。

        每一趟都有界（批次上限 + 每条的翻页上限），但**趟数会继续**直到没有新的
        条目被处理完——否则"份数多于一批"这件事本身就会让覆盖停在半路，而那是
        材料数量问题，不是模型能力问题。读不到的条目留在 ``unprocessed`` 里，
        下一趟不会空转（没有新进展就停）。

        失败在这里不是异常：读不到的材料会被记成执行问题与 ``unreadable``，
        其余条目照常核对完毕——暂时的读不到永远不会变成"没有差异"。
        """
        from .coverage import run_coverage_pass
        refresh_requirements(state)
        runs: list = []
        previous_offsets = None
        for _ in range(max(1, max_passes)):
            try:
                run = run_coverage_pass(state, index, at=utc_now())
            except Exception as exc:
                state.notes.append({'event': 'base_coverage_failed',
                                    'error': type(exc).__name__, 'detail': str(exc)[:200]})
                return {'error': type(exc).__name__, 'runs': len(runs)}
            runs.append(run)
            # 覆盖变了，交付要求的状态就得跟着重算——暂停判断读的是它，读晚了
            # 就会拿着上一轮的结论做决定。
            refresh_requirements(state)
            if not run['unprocessed']:
                break
            # 还有没读完的条目时继续下一趟——但**只在读取确实在前进时**。读不到的
            # 条目会一直留在 unprocessed 里，光看"还有剩下的"会让这里空转到上限。
            offsets = dict(state.coverage_cursor.get('offsets') or {})
            if offsets == previous_offsets:
                break
            previous_offsets = offsets
        return {'runs': len(runs), 'processed': sum(len(run['processed']) for run in runs),
                'remaining': list(runs[-1]['unprocessed']) if runs else []}

    def _refresh_stale_cases(self, state) -> list:
        """权威记录变了以后，材料与记录的**确定性差异**必须重算。

        这用的是产品自己的 ``refresh``：它不是模型决策，而是"比较对象变了，比较
        结果要跟着变"这一机械事实。重算后的 kind/issues 会改变条目的版本指纹，
        于是受影响的判断被正常地标记失效——不需要另一套机制。
        """
        if self.product is None:
            return []
        refreshed, failures = [], []
        for case_id in state.spec.selected_material_refs:
            try:
                case = self.product.get(case_id, 'case')
            except Exception as exc:
                failures.append({'case_id': case_id, 'error': type(exc).__name__})
                continue
            if case.get('base_revision') == self.product.revisions():
                continue
            try:
                self.product.refresh(case_id)
                refreshed.append(case_id)
            except Exception as exc:
                # 重算失败必须可见：差异停留在旧版本会直接反映到报告里，静默吞掉
                # 就等于让报告"看起来没变"，而真正的原因是它没能重算。
                failures.append({'case_id': case_id, 'error': type(exc).__name__,
                                 'detail': str(exc)[:200]})
        if refreshed or failures:
            state.notes.append({'event': 'case_refresh', 'case_ids': refreshed,
                                'failures': failures})
        return refreshed

    def _prepare_context(self, state, index, ctx) -> dict:
        from .context import prepare_context
        from ..turn_budget import CURRENT
        session = CURRENT.get()
        budget = {}
        if session is not None:
            data = session.sync()
            budget = {'cycles_used': data.get('cycles_consumed'),
                      'max_cycles': data.get('max_cycles'),
                      'seconds_left': round(data['wall_clock_seconds'] - data['consumed_seconds'], 1),
                      'searches_used': len(state.queries),
                      'searches_left': max(0, state.spec.resource_limits['max_searches'] - len(state.queries))}
        return prepare_context(state=state, material_index=index, facts=state.facts,
                               versions=state.spec.input_versions, budget=budget,
                               evidence_store=self.evidence_store)

    def _plan(self, state, turn, transport, trace):
        if not _transport_available(transport):
            return None, 'no_model_available'
        from ..agent import PlanningRejected
        turn.pending_correction = state.pending_correction
        try:
            proposal = caps.ReviewPlanner(transport).propose(turn)
        except PlanningRejected:
            return None, 'planner_rejected'
        except Exception as exc:
            trace.append({'phase': 'plan', 'error': type(exc).__name__, 'detail': str(exc)[:200]})
            # 供应商不可用是一个**暂时**状态，不是本次调查的结论。它的名字必须让
            # 既有的降级识别（``provider_outage``）认出来，照护者才有"稍后重试"
            # 这个选项，而不是看到一份看不出为什么没核查的报告。
            code = getattr(exc, 'code', None) or type(exc).__name__
            if code in ('provider_error', 'usage_unknown'):
                return None, f'provider_error:{code}'
            return None, f'planner_error:{code}'
        state.pending_correction = None
        proposal.setdefault('decision', 'tool')
        errors = caps.proposal_errors(state, proposal)
        if errors:
            state.pending_correction = {'previous_proposal_was_rejected': True,
                                        'rejection_reasons': errors,
                                        'allowed_tools_now': list(caps.allowed_review_tools(state)),
                                        'instruction': _correction_text(errors)}
            trace.append({'phase': 'plan', 'cycle': turn.cycle, 'rejected': errors,
                          'tool': proposal.get('tool')})
            return None, 'proposal_rejected'
        turn.pending_correction = None
        return proposal, None

    def _execute(self, state, turn, ctx, proposal, index, tracker):
        tool = proposal['tool']
        arguments = dict(proposal.get('arguments') or {})
        executor = self._executor()
        started = time.perf_counter()
        before = _progress_fingerprint(state)
        result = executor.execute(ctx, tool, arguments, state=turn)
        added = _progress_fingerprint(state) != before
        note = {'phase': 'act', 'cycle': turn.cycle, 'tool': tool,
                'arguments': {key: value for key, value in arguments.items() if key != 'excerpt'},
                'ok': result.ok, 'seconds': round(time.perf_counter() - started, 3)}
        if not result.ok:
            error = result.error or {}
            kind = str(error.get('error_kind') or 'internal_error')
            category = ERROR_KIND_TO_ISSUE.get(kind, ISSUE_CONNECTION)
            issue = state.add_issue(operation_ref=f'{tool}:{_op_key(arguments)}', category=category,
                                    affected_question_ids=[arguments.get('question_id')]
                                    if arguments.get('question_id') else [],
                                    remote_outcome='not_executed' if kind in
                                    ('invalid_arguments', 'unknown_tool', 'permission_denied') else 'unknown',
                                    # 业务语言：一句话说清"这一步没做成、它影响什么"。
                                    # 工具名与错误枚举留在 detail 里，不进用户读的正文。
                                    user_visible_summary=_issue_summary(tool, kind),
                                    detail={'tool': tool, 'error_kind': kind})
            note.update(error_kind=kind, issue_id=issue['issue_id'])
            if category == ISSUE_PERMISSION_DENIED:
                state.run_status = RUN_FAILED
                state.termination_reason = 'permission_denied'
                return 'stop', note
        else:
            state.sync_coverage()
            state.refresh_axes()
            note['evidence_refs'] = list(result.evidence_refs or [])
            # 同一个动作后来成功了，之前那条自纠的失败就不再是"执行限制"：它已经被
            # 处理掉了。留在正文里会让一条自动改正过的参数笔误看起来像故障。
            for issue in state.issues:
                if issue['status'] == 'open' and issue['operation_ref'].startswith(tool + ':'):
                    state.resolve_issue(issue['issue_id'])
        # 重复检测对**成功但无新增**的步骤同样生效：同一次检索换个说法再跑一遍、
        # 同一条材料读第二遍，都是"这一步没有带回任何新的东西"。只按失败计数会让
        # 一个原地打转的循环看起来每一步都在推进。
        state.revision += 1
        verdict = tracker.record(
            ctx.run_id,
            read_signature(tool, arguments, scope_id=state.spec.scope_id,
                           patient_revision=state.spec.input_versions.get('medications'),
                           corpus_version=state.spec.input_versions.get('materials')),
            limit=self._no_progress_limit(), new_information=added)
        state.no_progress = verdict['repeats']
        note['added_information'] = added
        note['no_progress'] = verdict['verdict']
        paused = self._pause_for_input(state, result, index, note)
        if paused:
            return 'stop', note
        if verdict['verdict'] == 'stop':
            note['degraded_reason'] = ('no_progress:repeated_tool_errors' if not result.ok
                                       else 'no_progress:repeated_steps')
            state.notes.append({'event': 'no_progress', 'tool': tool,
                                'detail': '连续多步没有带回任何新内容，停止重复规划。',
                                'unfinished': _unfinished(state)})
            return 'stop', note
        if self.emit_progress:
            self.emit_progress(state, tool)
        return 'continue', note

    def _pause_for_input(self, state, result, index, note) -> bool:
        """``request_information`` 成功后，把"等用户补充"变成一个**状态转换**。

        以前它只是一个工具调用：模型问完，循环继续，于是它要么重复问同一个问题，
        要么在没有新信息的情况下反复读旧结果。现在：

        * 先把与补问无关的确定性工作做完（基础覆盖不消耗模型调用）；
        * 如果**没有剩下任何需要模型判断的事**，立即进入 waiting_input 并结束本轮；
        * 还有可独立推进的工作时照常继续，但记录下这次暂停。

        "没有剩下需要判断的事"是具体条件，不是感觉：覆盖要求全部有处置，并且
        每一条还开着的问题都被某条未回答的补充请求挡着——那时候再调用模型，它
        除了把已经问过的话换个说法再问一遍，没有别的事可做。
        """
        if not result.ok or getattr(result, 'tool', None) != 'request_information':
            return False
        value = result.value or {}
        if not value.get('request_id'):
            # 请求没有被建立（例如没指明对象）：这不是"在等用户"，模型需要改。
            note['input_request_rejected'] = value.get('status')
            return False
        note['input_request_id'] = value['request_id']
        note['input_request_reused'] = bool(value.get('reused_existing_request'))
        note['input_request_blocking'] = bool(value.get('blocking'))
        self._run_base_coverage(state, index)
        remaining = self._independent_work(state)
        note['independent_work_remaining'] = remaining
        if remaining:
            return False
        if not value.get('blocking'):
            # 这条请求**没有挡住任何必需要求**。它不是"在等用户"，只是模型顺手
            # 提的一条建议：结束本轮，让任务照常收尾——它可以就此完整交付。
            note['paused_for_input'] = False
            note['optional_only'] = True
            return True
        state.run_status = RUN_WAITING_INPUT
        state.termination_reason = 'waiting_input'
        note['paused_for_input'] = True
        if self.emit_progress:
            self.emit_progress(state, 'request_information')
        return True

    @staticmethod
    def _independent_work(state) -> list:
        """不依赖这次补问、且还需要做的事。空列表 = 这一轮没有可推进的了。

        **可选问题不算可推进的工作**：它不阻塞原任务，也就没有理由让循环继续
        为它转下去（更不该把任务变成等待状态）。它留在报告与页面上，用户想继续
        就从那里继续。
        """
        blocked = {item['question_id'] for item in state.blocked_questions()}
        remaining = [question['question_id'] for question in state.open_questions()
                     if question['question_id'] not in blocked and not question.get('optional')]
        remaining += [requirement['requirement_id'] for requirement in state.pending_coverage()]
        # 必需要求里"还没轮到处理"的那些同样要做。
        remaining += [requirement['requirement_id']
                      for requirement in state.required_requirements()
                      if requirement['status'] == REQ_PENDING]
        return remaining

    def _executor(self):
        if getattr(self, '_review_executor', None) is None:
            executor = self.agent.executor if self.agent is not None else _standalone_executor(self.memory)
            handlers = caps.build_review_handlers(
                rag_tool=self._rag_tool(), evidence_store=self.evidence_store,
                run_id=None, patient_revision_fn=self._patient_revision)
            for name, spec in caps.REVIEW_TOOL_SPECS.items():
                executor.register(spec, handlers[name], override=True)
            self._review_executor = executor
        return self._review_executor

    def _rag_tool(self):
        if self.agent is not None and getattr(self.agent, 'tools', None):
            return self.agent.tools.get('rag_search')
        from ..agent import RAGTool
        return RAGTool()

    def _patient_revision(self):
        try:
            return self.memory.scope_revision('medications') + self.memory.scope_revision('semantic')
        except Exception:
            return None

    # ---- 交付 ----------------------------------------------------------------

    def _attempt_delivery(self, state, index) -> dict:
        from .verify import verify_all
        verify_all(state, evidence_store=self.evidence_store)
        state.evidence_status = evidence_axis(state)
        sections = section_content(state)
        check = check_delivery(state, sections=sections, evidence_store=self.evidence_store,
                               markdown=report_markdown(state))
        if not check['ok']:
            state.delivery_status = DELIVERY_PARTIAL
            return {'delivered': False, 'gaps': check['gaps'], 'summary': check['summary'],
                    'trace': {'ok': False, 'gaps': [item['detail'] for item in check['gaps']
                                                    if item['code'] in BLOCKING_GAPS]}}
        return {'delivered': True, 'gaps': [], 'summary': check['summary'],
                'trace': {'ok': True, 'delivery_status': check['delivery_status']}}

    def _finish(self, state, run_id, index, degraded_reason):
        """不管怎么结束，只要已经有可说的内容，就交付**已有的准确结果**。"""
        from .verify import verify_all
        try:
            verify_all(state, evidence_store=self.evidence_store)
            invalidate_sources(state, self.evidence_store)
        except Exception:
            pass
        state.evidence_status = evidence_axis(state)
        if state.run_status == RUN_RUNNING:
            state.run_status = RUN_ENDED
        if any(item['status'] == 'open' for item in state.input_requests) and \
                state.run_status == RUN_ENDED:
            state.run_status = RUN_WAITING_INPUT
        state.termination_reason = state.termination_reason or _termination(state, degraded_reason)
        sections = section_content(state)
        check = check_delivery(state, sections=sections, evidence_store=self.evidence_store,
                               markdown=report_markdown(state))
        state.delivery_status = check['delivery_status']
        if state.run_status == RUN_CANCELLED:
            state.termination_reason = 'cancelled'
        # 交付轴刚刚定下来，第 1 节里那句"本次状态"必须说的是**这一版**的状态。
        # 用上面那份 sections 会让报告正文写着"尚无报告"，而它本身已经是一份报告。
        sections = section_content(state)
        report = self._publish(state, sections, check, run_id)
        return report

    def _publish(self, state, sections, check, run_id):
        previous = None
        if state.reports:
            previous = self._previous_report(state)
        reported = set(state.reports[-1].get('answer_ids') or []) if state.reports else set()
        # 权威写入**不**列在"您的补充"里：它已经在"依据（当前记录）"那一条讲过了，
        # 两处都说一遍会让"用户说了一句话"和"记录被改了"看起来像同一件事。
        fresh_answers = [item for item in state.answers
                         if item['answer_id'] not in reported
                         and not item.get('applied_authoritative')]
        change = {**(state.change_summary or {}),
                  'answers': [f"{'、'.join(item['subjects']) or '（未指明对象）'}"
                              f"{item['field'] or ''}：{item['value']}" for item in fresh_answers]}
        diff = diff_reports(previous, {'sections': sections,
                                       'versions': state.spec.input_versions,
                                       'requirements': [dict(item) for item in
                                                        state.spec.delivery_requirements]},
                            change=change) if previous else []
        report = build_report_record(
            state, report_id=f'material-review:{uuid.uuid4().hex}', created_at=utc_now(),
            sections=sections, delivery_status=state.delivery_status,
            evidence_status=state.evidence_status, diff=diff,
            markdown='')
        report['markdown'] = report_markdown(state, revision_diff=diff)
        report['gaps'] = [item for item in check['gaps'] if item['code'] in BLOCKING_GAPS]
        report['all_gaps'] = list(check['gaps'])
        report['input_requests'] = [item for item in state.input_requests if item['status'] == 'open']
        report['answered_requests'] = [item for item in state.input_requests
                                       if item['status'] == 'answered']
        report['safety_checks'] = list(state.safety_checks)
        # 来源清单随报告一起交付：页面的"查看来源"读的是这一份，而不是从正文里
        # 正则抠出来的 id——抠出来的东西无法保证就是这条结论的来源。
        report['evidence_refs'] = sorted({ref for finding in state.findings
                                          if not finding.get('stale')
                                          for ref in (finding.get('evidence_refs') or [])}
                                         | {ref for assertion in state.assertions
                                            if assertion.get('verification_status') == 'supported'
                                            for ref in (assertion.get('evidence_refs') or [])})
        # "已读回原文"与"谁读的"是两件事。页面按这两份清单分别展示：系统读取支撑的是
        # **确定性字段比较**，模型读取才代表模型看过原文。
        reads = state.read_attribution()
        report['material_refs'] = sorted(reads['system'] + reads['model'])
        report['system_material_refs'] = reads['system']
        report['model_material_refs'] = reads['model']
        report['unread_material_refs'] = reads['unread']
        report['invalid_material_refs'] = reads['invalid']
        report['coverage_progress'] = state.coverage_progress()
        # 本次承诺解决什么、逐字段比到了什么程度——页面与报告读的是同一份。
        report['requirements'] = [dict(item) for item in state.spec.delivery_requirements]
        report['field_comparisons'] = {ref: list(rows)
                                       for ref, rows in (state.field_comparisons or {}).items()}
        report['attribution'] = _attribution(state)
        report['research_decisions'] = list(state.research_decisions)
        report['answers'] = list(state.answers)
        report['answer_ids'] = [item['answer_id'] for item in state.answers]
        report['change_summary'] = dict(state.change_summary or {})
        report['findings'] = [{'finding_id': item['finding_id'],
                               'finding_type': item['finding_type'],
                               'statement': item['statement'], 'origin': item['origin'],
                               'material_refs': item['material_refs'],
                               'evidence_refs': item['evidence_refs'],
                               'stale': bool(item.get('stale'))}
                              for item in state.findings]
        report['cycle_note'] = _degraded_note(state, check)
        state.record_report(report)
        if self.emit_progress:
            self.emit_progress(state, None)
        return report

    def _previous_report(self, state):
        """上一版报告的**渲染输入**，用于算差异；取不到就不编造变化。"""
        return None if not state.reports else {
            'sections': state.reports[-1].get('sections') or {},
            'versions': state.reports[-1].get('versions') or {},
            'requirements': state.reports[-1].get('requirements') or []}

    def _persist(self, run_id, state, trace, *, status=None, result=None):
        payload = {'review': state.to_dict(), 'trace': (trace or [])[-60:]}
        if result is not None:
            # 归因需要的信息必须一起落盘：跑完之后再想知道"模型到底有没有被调用、
            # 停在哪一步、为什么"，只能靠恢复出来的这一份。
            payload.update({key: result.get(key) for key in
                            ('termination_reason', 'run_status', 'delivery_status',
                             'evidence_status', 'cycles', 'degraded_reason')})
        try:
            self.memory.workflow_run_update(run_id, status=status, result=payload)
        except Exception:
            pass

    def _begin_run(self, run_id, saved_budget):
        prior = self.memory.workflow_run_get(run_id)
        if prior and prior.get('graph_version') not in (None, GRAPH_VERSION):
            raise ValueError('material review runner version requires migration')
        self.memory.workflow_run_start(run_id=run_id, graph_version=GRAPH_VERSION,
                                       budget={**(saved_budget or {}), 'accounting_version': 2})

    @staticmethod
    def _cycle_budget_exhausted(turn):
        from ..turn_budget import CURRENT
        session = CURRENT.get()
        if session is None:
            return False
        return bool(session.exhausted(cycle=turn.cycle))

    @staticmethod
    def _no_progress_limit() -> int:
        try:
            return max(1, int(os.getenv('AGENT_NO_PROGRESS_LIMIT', '3')))
        except ValueError:
            return 3

    @staticmethod
    def _run_status_word(state, degraded_reason):
        if state.run_status == RUN_CANCELLED:
            return 'cancelled'
        if state.run_status == RUN_FAILED:
            return 'failed'
        return 'degraded' if degraded_reason else 'succeeded'


def _turn_state(review, index):
    """一次推进的**回合对象**：承载 trace、预算周期计数与 review 状态。

    与旧路径共用 ``AgentState`` 的形状，所以 ``budget.cycle`` 与 tool_history
    看到的仍是同一个东西——新路径没有自己的计数口径。
    """
    from ..agent import AgentState, CareEvent
    turn = AgentState(review.spec.task_id, review.spec.task_id,
                      CareEvent('user_message', review.spec.user_goal))
    turn.review = review
    turn.review_index = index
    return turn


def _progress_fingerprint(state) -> tuple:
    """这一步有没有带回**新的东西**。

    它数的是状态里可数的产出，不数调用次数：新证据、新读回的材料、新发现、
    新问题、新断言、新补充请求、新执行问题。任何一个变了，这一步就确实推进了
    调查；全都没变，那么不论工具返回得多成功，本次调查没有前进。
    """
    return (len(state.evidence_refs), len(state.read_evidence_refs),
            len(state.material_read_refs), len(state.findings), len(state.questions),
            len(state.assertions), len(state.input_requests), len(state.issues),
            len(state.queries))


def _unfinished(state) -> list:
    unfinished = [item['description'] for item in state.pending_coverage()][:8]
    unfinished += [item['text'] for item in state.open_questions()][:8]
    unfinished += [item['user_visible_summary'] for item in state.open_issues()][:8]
    return unfinished or ['没有未完成项']


def _budget_cycle(turn) -> None:
    from ..turn_budget import CURRENT
    session = CURRENT.get()
    if session is not None:
        session.cycle(turn)


def _wrap_up_due(turn, limit: int) -> bool:
    """收尾额度：约 1/6 的周期留给"交付"，与旧路径同一个划分口径。"""
    from ..turn_budget import CURRENT
    session = CURRENT.get()
    consumed = session.data['cycles_consumed'] if session is not None else turn.cycle
    return max(limit - consumed, 0) <= max(1, (limit + 5) // 6)


def _transport_available(transport) -> bool:
    """模型是否真的可用。三种接入方式（离线替身 / 注入客户端 / 自建客户端）等价。"""
    if transport is None:
        return False
    return bool(getattr(transport, 'proposal_provider', None)
                or getattr(transport, 'client', None)
                or getattr(transport, 'model', None))


def _standalone_executor(memory):
    from ..harness.tools import ToolExecutor
    return ToolExecutor(memory=memory)


def _termination(state, degraded_reason):
    if degraded_reason == 'cancelled':
        return 'cancelled'
    if degraded_reason and degraded_reason.startswith('permission'):
        return 'permission_denied'
    if state.run_status == RUN_WAITING_INPUT:
        return 'waiting_input'
    if state.delivery_status == DELIVERY_COMPLETE:
        return 'delivery_complete'
    if degraded_reason:
        return 'ended_incomplete'
    return 'ended_partial'


def _issue_summary(tool: str, kind: str) -> str:
    """用户读到的执行问题：说清"哪一步没做成、它意味着什么"。

    业务语言，不带工具名与错误枚举——那些是诊断信息。照护者要判断的是"这条结论
    有没有依据"，不是"哪个函数返回了 invalid_arguments"。
    """
    what = {'read_material': '读取一份材料', 'research_evidence': '查一次依据',
            'request_information': '提出补充请求', 'submit_question': '记下一条待确认的问题',
            'submit_finding': '记下一条发现', 'submit_assertion': '核查一条结论'}.get(tool, '一步核对动作')
    return {'invalid_arguments': f'有一次{what}的参数不完整，系统没有执行它。',
            'unknown_tool': f'有一次{what}的请求系统不认识，没有执行。',
            'permission_denied': f'当前权限不允许{what}。',
            'evidence_unavailable': f'{what}时取到的来源没能通过校验，这一条没有核对到。',
            'retrieval_empty': f'{what}时没有找到匹配内容；这不代表没有风险。',
            'budget_exhausted': f'本次可用额度用完，{what}没有继续。',
            }.get(kind, f'{what}没有成功执行；它不影响已经核对过的部分。')


def _attribution(state) -> dict:
    """一眼看清**谁做了什么**。它是报告的一部分，不是内部调试信息。"""
    reads = state.read_attribution()
    return {
        'system': {
            'label': '代码完成的确定性工作',
            'coverage_pass_rounds': len(state.coverage_runs),
            'materials_read': reads['system'],
            'field_comparisons': _comparison_count(state),
            'safety_checks': len(state.safety_checks),
        },
        'model': {
            'label': '模型做出的调查判断',
            # 这一版里的参与轮数，不是累计值——否则"上一版有模型、这一版挂了"
            # 会被显示成仍然有模型参与。
            'cycles': state.model_cycles_this_delivery(),
            'materials_read': reads['model'],
            'research_actions': [{'question_id': item['question_id'], 'query': item['query'],
                                  'purpose': item.get('purpose'), 'outcome': item.get('outcome')}
                                 for item in state.research_decisions],
            'assertions': len(state.assertions),
            'findings': len([f for f in state.findings if f['origin'] != 'system']),
        },
        'unattributed': {
            'label': '尚未由任何人处理',
            'materials_not_read': reads['unread'],
            'materials_failed_integrity': reads['invalid'],
            'coverage_requirements_pending': [item['requirement_id']
                                              for item in state.pending_coverage()],
        },
    }


def _comparison_count(state) -> int:
    """代码一共做了多少次**字段级**比较。数字来自留档的比较记录本身。"""
    return sum(len(rows) for rows in (state.field_comparisons or {}).values())


def _degraded_note(state, check):
    if state.delivery_status == DELIVERY_COMPLETE:
        return '本次交付完整：完成检查里的每一项交付要求都已满足。'
    lines = []
    if check.get('awaiting_input'):
        lines.append('本次交付的是**部分结果**：还有补充请求没有回答，'
                     '回答之后可以从这里继续，不需要重做已经核对过的部分。')
    elif check.get('open_questions'):
        lines.append('本次交付的是**部分结果**：还有调查问题没有结论。报告把它们逐条'
                     '写在了"仍待确认的问题"一节，但这件事还没有查完。')
    else:
        lines.append('本次交付的是**部分结果**，下面这些交付要求还没有满足：')
    for item in (check.get('gaps') or [])[:8]:
        if item['code'] in ('awaiting_user_input', 'question_still_open'):
            continue
        lines.append('· ' + item['detail'])
    lines.append('部分结果里的内容仍然是准确的：它们只包含已经实际读过、并且通过检查的条目。')
    return '\n'.join(lines)


def _correction_text(errors):
    hints = {
        'unknown_tool': '这个工具当前不可用；改用 allowed_tools_now 里列出的工具。',
        'research_not_allowed_or_budget_exhausted': '检索次数已用完或本次不允许外部取证；改用已有材料与证据。',
        'unknown_question': 'question_id 不存在；先用 submit_question 声明它。',
        'question_not_open': '这个问题已经关闭；要重新调查请提交新问题或重新打开它。',
        'material_not_in_scope': '这个材料条目不在本次选中的范围内。',
        'material_not_read_back': '引用材料前必须先用 read_material 读回原文。',
        'evidence_not_observed_in_scope': '这个证据 id 不是本轮观察到的。',
        'closing_requires_resolution_summary': '关闭一个问题必须说明它凭什么被关闭。',
        'structured_assertion_needs_expect': '结构化断言请用 {"expect": "same|different|missing"}。',
        'assertion_needs_evidence_excerpt': '这条断言需要给出证据原文片段（qualifiers.excerpt）。',
        'semantic_assertion_needs_explanation': '语义解释需要给出 qualifiers.explanation。',
        'invalid_question_key': 'question_key 是问题的"方面"，请给一个稳定的短标识。',
    }
    lines = ['上一步提案未通过校验，请只修正这几处：']
    for error in errors:
        base = str(error).split(':', 1)[0]
        lines.append(f'· {error} — {hints.get(base, hints.get(error, "请对照函数表修正参数。"))}')
    return '\n'.join(lines)


def _op_key(arguments):
    from .contract import digest
    return digest({key: value for key, value in arguments.items()
                   if key not in ('excerpt', 'explanation')})[:12]
