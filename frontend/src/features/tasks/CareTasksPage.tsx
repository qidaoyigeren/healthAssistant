import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { request, newIdempotencyKey } from '../../api/http';
import { productApi } from '../../api/product';
import { SafeMarkdown } from '../../components/safeMarkdown';
import { EvidenceDrawer } from '../../components/evidence';
import { inputClass, buttonClass } from '../materials/MaterialsPage';
import { RunProgressLine } from '../shared/runProgress';
import { MaterialReviewTaskCard, type MaterialReviewTask } from './MaterialReviewCard';

interface CareTask { id: string; goal_type: string; revision: number; status: string; case_id: string | null; waiting_reason: string | null; due_at: string | null; budget: { spent: number; limit: number }; result_refs: string[]; goal?: string; missing_inputs?: Array<{ gap_id: string; field?: string; question: string }>; subgoals?: Array<{ subgoal_id: string; kind: string; statement: string; status: string }>; partial_report_refs?: string[]; active_run_id?: string | null; degraded_label?: string | null; retry_available?: boolean; runs?: Array<{ run_id: string; status?: string; workflow_run_id?: string }> }
interface InvestigationReport { id: string; markdown: string; partial: boolean; goal: string; investigation?: { evidence_refs: string[] } }
interface Summary { id: string; created_at: string; stale: boolean; markdown: string }
const names: Record<string, string> = { reconcile_material: '材料核对', visit_summary: '准备就诊摘要', current_medications: '查看当前药单', evidence_review: '开放证据核查', material_review: '材料核对与就诊准备', safety_case: '用药安全事项调查', ready: '可以继续', running: '正在处理', waiting_input: '等待补充', waiting_review: '等待专业审核', completed: '已完成', cancelled: '已取消', failed: '处理未完成' };
const subgoalStatus: Record<string, string> = { completed: '已完成', blocked: '未完成', recorded: '已记录待确认' };
const fetchTasks = () => request<{ items: CareTask[] }>('/v1/care-tasks');

const semanticLabels: Record<string, string> = { renal_function: '肾功能', hepatic_function: '肝功能', allergy: '过敏', age: '年龄', pregnancy: '孕产情况', chronic_condition: '既往疾病' };

function EvidenceReviewTask({ task, busy, onRun, resume }: {
  task: CareTask; busy: boolean;
  onRun: (fn: () => Promise<unknown>) => Promise<void>;
  resume: (task: { id: string; revision: number }, action?: string) => Promise<unknown>;
}): React.ReactElement {
  const questions = task.missing_inputs ?? [];
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState('');
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const reportId = task.result_refs[0] ?? task.partial_report_refs?.at(-1);
  const report = useQuery({ queryKey: ['investigation-report', reportId], enabled: !!reportId, queryFn: () => request<InvestigationReport>(`/v1/investigation-reports/${reportId}`) });
  async function supplement() {
    const semantic: Record<string, string> = {}; const medications: Array<Record<string, string>> = [];
    for (const q of questions) {
      const value = (answers[q.gap_id] ?? '').trim();
      if (!value) continue;
      if (q.field === 'medication_name') medications.push({ name: value });
      else if (q.field && q.field.includes(':')) { /* dose_unit/start_date questions are answered by re-adding the drug with dose/date */ medications.push({ name: q.field.split(':')[1] ?? '', dose: q.field.startsWith('dose_unit') ? value : undefined, start_at: q.field.startsWith('start_date') ? value : undefined } as Record<string, string>); }
      else semantic[q.field ?? ''] = value;
    }
    const additional = extra.trim() ? [extra.trim()] : [];
    await request(`/v1/care-tasks/${task.id}/input`, { method: 'POST', body: { key: newIdempotencyKey(), revision: task.revision, medications, semantic, additional_questions: additional } });
    // record_input bumps the task revision exactly once; resume against that.
    const updated = { ...task, revision: task.revision + 1 };
    await onRun(() => resume(updated));
  }
  return <article className="rounded-card border border-border bg-surface p-4">
    <div className="flex justify-between gap-2"><h3 className="font-medium">{names[task.goal_type]}</h3><span className="text-sm text-primary-strong">{names[task.status]}</span></div>
    {task.goal && <p className="mt-2 text-sm">核查目标:{task.goal}</p>}
    <p className="mt-2 text-sm">{task.waiting_reason}</p>
    {(task.subgoals?.length ?? 0) > 0 && <ul className="mt-3 space-y-1 text-sm" aria-label="子目标">
      {task.subgoals!.map(s => <li key={s.subgoal_id}>· {s.statement} — {subgoalStatus[s.status] ?? s.status}</li>)}
    </ul>}
    {questions.length > 0 && task.status === 'waiting_input' && <div className="mt-3 space-y-2 border-t border-border pt-3">
      <p className="text-sm font-medium">请补充以下记录(按报告记录保存，不构成临床审批):</p>
      {questions.map(q => <label key={q.gap_id} className="block text-sm">{q.question}
        <input className={inputClass} value={answers[q.gap_id] ?? ''} onChange={e => setAnswers(a => ({ ...a, [q.gap_id]: e.target.value }))} placeholder={q.field && semanticLabels[q.field] ? `例如:${semanticLabels[q.field]}` : '请输入'} /></label>)}
      <label className="block text-sm">补充一个待确认问题(可选)
        <input className={inputClass} value={extra} onChange={e => setExtra(e.target.value)} placeholder="例如:晚上服药需要注意什么" /></label>
      <button disabled={busy} className={buttonClass} onClick={() => void onRun(supplement)}>保存补充并继续核查</button>
    </div>}
    {reportId && <details className="mt-3 border-t border-border pt-3"><summary className="cursor-pointer text-sm">{task.status === 'completed' ? '查看核查报告' : '查看当前部分报告'}</summary>
      <div className="my-3 min-w-0 [overflow-wrap:anywhere]">{report.data ? <SafeMarkdown text={report.data.markdown} /> : <p className="text-sm text-ink-secondary">报告读取失败或尚未生成。</p>}</div>
      <div className="flex flex-wrap gap-2">{report.data?.investigation?.evidence_refs.map((ref, index) => <button key={ref} className={buttonClass} onClick={() => setEvidenceId(ref)}>回读证据 {index + 1}</button>)}</div>
    </details>}
    {evidenceId && <EvidenceDrawer evidenceId={evidenceId} onClose={() => setEvidenceId(null)} />}
    {task.degraded_label && <p className="mt-2 text-sm text-caution" role="status">{task.degraded_label}</p>}
    <div className="mt-3 flex gap-3">
      {task.status === 'waiting_review' && <p className="text-sm text-caution">请查看任务说明与风险依据；本地审核为模拟流程，未连接真实医生服务。</p>}
      {task.status === 'running' && <RunProgressLine runId={task.active_run_id} active />}
      {task.status !== 'running' && (task.runs?.length ?? 0) > 0 && <RunProgressLine runId={task.runs?.at(-1)?.workflow_run_id ?? task.runs?.at(-1)?.run_id} active={false} />}
      {!['completed', 'cancelled', 'failed', 'waiting_input', 'running'].includes(task.status) && <button disabled={busy} className={buttonClass} onClick={() => void onRun(() => resume(task))}>继续核查</button>}
      {task.retry_available && ['completed', 'failed'].includes(task.status) && <button disabled={busy} className={buttonClass} onClick={() => void onRun(() => resume(task))}>重新核查</button>}
      {!['completed', 'cancelled', 'failed'].includes(task.status) && <button disabled={busy} className={buttonClass} onClick={() => void onRun(() => resume(task, 'cancel'))}>取消后续核查</button>}
    </div>
    <p className="mt-2 text-xs text-ink-muted">已保存的记录与部分报告在取消或失败后仍然保留;多次核查累计消耗任务预算(第 {task.budget.spent}/{task.budget.limit} 次)。</p>
  </article>;
}

export function CareTaskLink(): React.ReactElement {
  const tasks = useQuery({ queryKey: ['care-tasks'], queryFn: fetchTasks });
  const active = tasks.data?.items.filter(t => !['completed', 'cancelled'].includes(t.status)).length;
  return <Link className="my-3 block rounded-lg border border-border bg-surface p-3 text-sm text-primary-strong" to="/tasks">继续照护待办{active !== undefined ? ` · ${active} 项未完成` : ''} →</Link>;
}

export function CareTasksPage(): React.ReactElement {
  const client = useQueryClient();
  const tasks = useQuery({ queryKey: ['care-tasks'], queryFn: fetchTasks, refetchInterval: 5000 });
  const cases = useQuery({ queryKey: ['reconciliations'], queryFn: productApi.cases });
  const summaries = useQuery({ queryKey: ['visit-summaries'], queryFn: () => request<{ items: Summary[] }>('/v1/visit-summaries') });
  const [caseId, setCaseId] = useState('');
  const [due, setDue] = useState('');
  const [openGoal, setOpenGoal] = useState('');
  // 本次要求来自**明确的选择**,不是从一句话里猜出来的:只支持三种明确要求,
  // 各自有不同的满足条件。没勾的不会被当成义务。
  const [wantDifferences, setWantDifferences] = useState(true);
  const [wantField, setWantField] = useState(false);
  const [confirmField, setConfirmField] = useState('strength');
  const [wantSource, setWantSource] = useState(false);
  const [sourceQuestion, setSourceQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(fn: () => Promise<unknown>) { setBusy(true); setError(''); try { await fn(); await client.invalidateQueries(); } catch(e) { setError(e instanceof Error ? e.message : '处理失败'); } finally { setBusy(false); } }
  function requestedNow(): Array<Record<string, unknown>> {
    const items: Array<Record<string, unknown>> = [];
    if (wantDifferences) items.push({ kind: 'list_differences' });
    if (wantField) items.push({ kind: 'confirm_field', field: confirmField });
    if (wantSource && sourceQuestion.trim().length >= 4) {
      items.push({ kind: 'answer_from_source', question: sourceQuestion.trim() });
    }
    return items;
  }
  async function create(goal: string, openGoal?: string) {
    const task = await request<CareTask>('/v1/care-tasks', { method: 'POST', body: { key: newIdempotencyKey(), goal_type: goal, case_id: caseId || null, due_at: due ? new Date(due).toISOString() : null, goal: openGoal, requested: goal === 'material_review' ? requestedNow() : undefined } });
    await resume(task);
  }
  const resume = (task: { id: string; revision: number }, action = 'continue') => request<CareTask>(`/v1/care-tasks/${task.id}/resume`, { method: 'POST', body: { key: newIdempotencyKey(), revision: task.revision, action } });
  return <div className="space-y-5"><header><h2 className="font-serif text-2xl">照护待办与就诊准备</h2><p className="mt-2 text-sm text-ink-secondary">离开后可在这里继续。到期仅显示应用内待办，不会在关闭应用后发送通知。</p></header>
    {(error || tasks.error || summaries.error) && <p role="alert" className="bg-red-50 p-3 text-red-800">{error || '读取失败，请刷新重试'}</p>}
    <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">留下一个材料核对待办</h3><label className="block text-sm">选择材料<select className={inputClass} value={caseId} onChange={e => setCaseId(e.target.value)}><option value="">请选择</option>{cases.data?.items.map(c => <option value={c.case_id} key={c.case_id}>{c.created_at} · {c.items.length} 项</option>)}</select></label><label className="block text-sm">计划处理时间（可选）<input className={inputClass} type="datetime-local" value={due} onChange={e => setDue(e.target.value)} /></label><button disabled={busy || !caseId} className={buttonClass} onClick={() => void run(() => create('reconcile_material'))}>保存待办</button></section>
    <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">核对一份材料,准备就诊</h3><p className="text-sm text-ink-secondary">选一份已上传的材料,系统整理它与当前记录的一致项、差异和缺项,必要时向您补充确认,然后给出带来源的报告。补充信息或换一版材料后,只会重新核对受影响的部分。</p><label className="block text-sm">选择材料<select className={inputClass} value={caseId} onChange={e => setCaseId(e.target.value)}><option value="">请选择</option>{cases.data?.items.map(c => <option value={c.case_id} key={c.case_id}>{c.created_at} · {c.items.length} 项</option>)}</select></label><label className="block text-sm">本次核对目标<textarea className={inputClass} rows={2} value={openGoal} onChange={e => setOpenGoal(e.target.value)} placeholder="例如:核对这份材料与当前药单是否一致,整理就诊时要确认的问题" /></label><fieldset className="space-y-2 rounded-lg border border-border px-3 py-2"><legend className="text-sm">本次要完成什么(可多选,决定这次怎样才算做完)</legend>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={wantDifferences} onChange={e => setWantDifferences(e.target.checked)} />列出这份材料与当前记录的差异及缺项</label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={wantField} onChange={e => setWantField(e.target.checked)} />确认指定字段是否一致
        <select className={inputClass} value={confirmField} disabled={!wantField} onChange={e => setConfirmField(e.target.value)} aria-label="要确认的字段">{(['dose', 'unit', 'schedule', 'date', 'route', 'form', 'strength', 'name'] as const).map(f => <option key={f} value={f}>{{dose:'剂量',unit:'单位',schedule:'服用频次',date:'日期',route:'给药途径',form:'剂型',strength:'规格',name:'药名'}[f]}</option>)}</select>
      </label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={wantSource} onChange={e => setWantSource(e.target.checked)} />依据资料回答一个问题</label>
      {wantSource && <input className={inputClass} value={sourceQuestion} onChange={e => setSourceQuestion(e.target.value)} placeholder="例如:这份材料里的用法与说明书是否一致?" aria-label="要依据资料回答的问题" />}
      <p className="text-xs text-ink-muted">没有勾选的要求不会被系统当成义务;核对过程中发现的其它问题会作为**可选建议**列出,不阻塞本次任务。</p>
    </fieldset>
    <button disabled={busy || !caseId || openGoal.trim().length < 4 || (wantSource && sourceQuestion.trim().length < 4)} className={buttonClass} onClick={() => void run(() => create('material_review', openGoal.trim()).then(() => setOpenGoal('')))}>开始核对</button></section>
    <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">发起一个开放证据核查</h3><p className="text-sm text-ink-secondary">围绕已有患者记录与材料核查证据、整理待确认问题；核查会分步进行，缺少记录时等待补充，已保存的进度可以随时回来继续。</p><label className="block text-sm">核查目标<textarea className={inputClass} rows={2} value={openGoal} onChange={e => setOpenGoal(e.target.value)} placeholder="例如:核查当前用药相互作用证据与适用条件" /></label><button disabled={busy || openGoal.trim().length < 4} className={buttonClass} onClick={() => void run(() => create('evidence_review', openGoal.trim()).then(() => setOpenGoal('')))}>开始核查</button></section>
    <section className="space-y-3" aria-label="照护待办列表">{tasks.data?.items.map(t => t.goal_type === 'evidence_review'
      ? <EvidenceReviewTask key={t.id} task={t} busy={busy} onRun={run} resume={resume} />
      : t.goal_type === 'material_review'
      ? <MaterialReviewTaskCard key={t.id} task={t as unknown as MaterialReviewTask} busy={busy} onRun={run} resume={resume} />
      : <article key={t.id} className="rounded-card border border-border bg-surface p-4"><div className="flex justify-between gap-2"><h3 className="font-medium">{names[t.goal_type]}</h3><span className="text-sm text-primary-strong">{names[t.status]}</span></div><p className="mt-2 text-sm">{t.waiting_reason}</p>{t.due_at && <p className="mt-2 text-xs text-ink-muted">计划：{new Date(t.due_at).toLocaleString()}{new Date(t.due_at).getTime() < Date.now() && t.status !== 'completed' ? ' · 已到计划时间' : ''}</p>}<div className="mt-3 flex gap-3">{t.case_id && (t.goal_type === 'safety_case'
        ? <Link className={buttonClass} to={`/safety/${encodeURIComponent(t.case_id)}`}>回到这件事项</Link>
        : <Link className={buttonClass} to={`/materials?case=${encodeURIComponent(t.case_id)}`}>打开材料补充信息</Link>)}{!['completed', 'cancelled', 'failed'].includes(t.status) && <><button disabled={busy} className={buttonClass} onClick={() => void run(() => resume(t))}>继续处理</button><button disabled={busy} className={buttonClass} onClick={() => void run(() => resume(t, 'cancel'))}>取消后续处理</button></>}</div></article>)}</section>
    <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">就诊准备摘要</h3><p className="text-sm text-ink-secondary">保存当前报告药单、近期变化、未决问题与来源。每次生成独立版本。</p><button disabled={busy} className={`${buttonClass} bg-primary-soft`} onClick={() => void run(() => create('visit_summary'))}>生成就诊摘要</button>{summaries.data?.items.map(s => <details className="border-t border-border pt-3" key={s.id}><summary className="cursor-pointer text-sm">{s.created_at} · {s.stale ? '记录已变化，建议重新生成' : '与当前记录一致'}</summary><div className="my-3 min-w-0 [overflow-wrap:anywhere]"><SafeMarkdown text={s.markdown} /></div><div className="flex gap-4"><a className="text-sm text-primary-strong underline" href={`${import.meta.env.VITE_API_BASE ?? ''}/v1/visit-summaries/${s.id}/download?format=html`}>下载 HTML</a><a className="text-sm text-primary-strong underline" href={`${import.meta.env.VITE_API_BASE ?? ''}/v1/visit-summaries/${s.id}/download`}>下载 Markdown</a></div></details>)}</section>
  </div>;
}
