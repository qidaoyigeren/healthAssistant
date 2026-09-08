import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { request, newIdempotencyKey } from '../../api/http';
import { productApi } from '../../api/product';
import { SafeMarkdown } from '../../components/safeMarkdown';
import { inputClass, buttonClass } from '../materials/MaterialsPage';

interface CareTask { id: string; goal_type: string; revision: number; status: string; case_id: string | null; waiting_reason: string | null; due_at: string | null; budget: { spent: number; limit: number }; result_refs: string[] }
interface Summary { id: string; created_at: string; stale: boolean; markdown: string }
const names: Record<string, string> = { reconcile_material: '材料核对', visit_summary: '准备就诊摘要', current_medications: '查看当前药单', ready: '可以继续', running: '正在处理', waiting_input: '等待补充', waiting_review: '等待专业审核', completed: '已完成', cancelled: '已取消', failed: '处理未完成' };
const fetchTasks = () => request<{ items: CareTask[] }>('/v1/care-tasks');

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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(fn: () => Promise<unknown>) { setBusy(true); setError(''); try { await fn(); await client.invalidateQueries(); } catch(e) { setError(e instanceof Error ? e.message : '处理失败'); } finally { setBusy(false); } }
  async function create(goal: string) {
    const task = await request<CareTask>('/v1/care-tasks', { method: 'POST', body: { key: newIdempotencyKey(), goal_type: goal, case_id: caseId || null, due_at: due ? new Date(due).toISOString() : null } });
    await resume(task);
  }
  const resume = (task: CareTask, action = 'continue') => request<CareTask>(`/v1/care-tasks/${task.id}/resume`, { method: 'POST', body: { key: newIdempotencyKey(), revision: task.revision, action } });
  return <div className="space-y-5"><header><h2 className="font-serif text-2xl">照护待办与就诊准备</h2><p className="mt-2 text-sm text-ink-secondary">离开后可在这里继续。到期仅显示应用内待办，不会在关闭应用后发送通知。</p></header>
    {(error || tasks.error || summaries.error) && <p role="alert" className="bg-red-50 p-3 text-red-800">{error || '读取失败，请刷新重试'}</p>}
    <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">留下一个材料核对待办</h3><label className="block text-sm">选择材料<select className={inputClass} value={caseId} onChange={e => setCaseId(e.target.value)}><option value="">请选择</option>{cases.data?.items.map(c => <option value={c.case_id} key={c.case_id}>{c.created_at} · {c.items.length} 项</option>)}</select></label><label className="block text-sm">计划处理时间（可选）<input className={inputClass} type="datetime-local" value={due} onChange={e => setDue(e.target.value)} /></label><button disabled={busy || !caseId} className={buttonClass} onClick={() => void run(() => create('reconcile_material'))}>保存待办</button></section>
    <section className="space-y-3" aria-label="照护待办列表">{tasks.data?.items.map(t => <article key={t.id} className="rounded-card border border-border bg-surface p-4"><div className="flex justify-between gap-2"><h3 className="font-medium">{names[t.goal_type]}</h3><span className="text-sm text-primary-strong">{names[t.status]}</span></div><p className="mt-2 text-sm">{t.waiting_reason}</p>{t.due_at && <p className="mt-2 text-xs text-ink-muted">计划：{new Date(t.due_at).toLocaleString()}{new Date(t.due_at).getTime() < Date.now() && t.status !== 'completed' ? ' · 已到计划时间' : ''}</p>}<div className="mt-3 flex gap-3">{t.case_id && <Link className={buttonClass} to={`/materials?case=${encodeURIComponent(t.case_id)}`}>打开材料补充信息</Link>}{!['completed', 'cancelled', 'failed'].includes(t.status) && <><button disabled={busy} className={buttonClass} onClick={() => void run(() => resume(t))}>继续处理</button><button disabled={busy} className={buttonClass} onClick={() => void run(() => resume(t, 'cancel'))}>取消后续处理</button></>}</div></article>)}</section>
    <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">就诊准备摘要</h3><p className="text-sm text-ink-secondary">保存当前报告药单、近期变化、未决问题与来源。每次生成独立版本。</p><button disabled={busy} className={`${buttonClass} bg-primary-soft`} onClick={() => void run(() => create('visit_summary'))}>生成就诊摘要</button>{summaries.data?.items.map(s => <details className="border-t border-border pt-3" key={s.id}><summary className="cursor-pointer text-sm">{s.created_at} · {s.stale ? '记录已变化，建议重新生成' : '与当前记录一致'}</summary><div className="my-3 min-w-0 [overflow-wrap:anywhere]"><SafeMarkdown text={s.markdown} /></div><div className="flex gap-4"><a className="text-sm text-primary-strong underline" href={`${import.meta.env.VITE_API_BASE ?? ''}/v1/visit-summaries/${s.id}/download?format=html`}>下载 HTML</a><a className="text-sm text-primary-strong underline" href={`${import.meta.env.VITE_API_BASE ?? ''}/v1/visit-summaries/${s.id}/download`}>下载 Markdown</a></div></details>)}</section>
  </div>;
}
