import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { productApi, newIdempotencyKey, type Reconciliation, type ReconciliationItem } from '../../api/product';
import { DocumentImport, DocumentPreview } from './DocumentImport';
import type { Candidate } from '../../api/product';
import { request } from '../../api/http';

const labels: Record<string, string> = { new: '新增候选', changed: '记录有差异', same: '与当前一致', possible_duplicate: '可能重复', unresolved: '需要补充', not_listed: '材料未列出', accepted: '已确认记录', kept: '已保留当前', pending: '待核对' };
const fieldLabels: Record<string, string> = { name: '药名', dose: '剂量数字', unit: '单位', schedule: '频次', date: '材料日期', subject: '患者标识', route: '给药途径', form: '剂型', strength: '规格' };
export const inputClass = 'w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm focus:outline-primary';
export const buttonClass = 'rounded-lg border border-border px-3 py-2 text-sm hover:bg-surface-alt disabled:opacity-50';

export function MaterialsPage(): React.ReactElement {
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const id = params.get('case');
  const [text, setText] = useState('');
  const [key, setKey] = useState(newIdempotencyKey);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selection, setSelection] = useState<Candidate['locations'][string] | null>(null);
  const list = useQuery({ queryKey: ['reconciliations'], queryFn: productApi.cases });
  const detail = useQuery({ queryKey: ['reconciliation', id], queryFn: () => productApi.case(id!), enabled: !!id, refetchInterval: 5000 });
  const careTasks = useQuery({ queryKey: ['care-tasks'], queryFn: () => request<{items: {id: string; case_id: string; revision: number; status: string}[]}>('/v1/care-tasks') });
  const activeTask = careTasks.data?.items.find(t => t.case_id === id && !['completed', 'cancelled', 'failed'].includes(t.status));
  async function run(fn: () => Promise<Reconciliation>) {
    setBusy(true); setError('');
    try {
      const result = await fn();
      setParams({ case: result.case_id });
      client.setQueryData(['reconciliation', result.case_id], result);
      await client.invalidateQueries();
    } catch (e) { setError(e instanceof Error ? e.message : '操作失败，请重试'); }
    finally { setBusy(false); }
  }
  return <div className="space-y-5">
    <header><p className="text-sm text-primary-strong">材料核对</p><h2 className="mt-1 font-serif text-2xl">把材料中的记录，一项项核清楚</h2><p className="mt-2 text-sm text-ink-secondary">先核对来源与患者，再确认记录内容。材料未列出的药物仍保留在当前药单中。</p></header>
    {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-red-800">{error}</p>}
    {(list.error || detail.error) && <p role="alert">读取失败，请刷新页面重试。</p>}
    <section className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-2"><h3 className="font-medium">导入结构化药单</h3><button className={buttonClass} onClick={() => void productApi.template().then(r => { setText(r.csv); setKey(newIdempotencyKey()); }).catch(e => setError(String(e)))}>填入示例模板</button></div>
      <p className="my-2 text-xs text-ink-muted">UTF-8 CSV，最多 200 行。患者标识 local-demo 代表当前患者；请替换示例内容。空白字段会要求补充。</p>
      <label className="block text-sm">选择 CSV 文件<input aria-label="选择 CSV 文件" type="file" accept=".csv,text/csv" className="my-2 block text-sm" onChange={e => { const f = e.target.files?.[0]; if (f) void f.text().then(t => { setText(t); setKey(newIdempotencyKey()); }); }} /></label>
      <textarea aria-label="药单 CSV 内容" className={`${inputClass} min-h-36 font-mono`} value={text} onChange={e => { setText(e.target.value); setKey(newIdempotencyKey()); }} />
      <button disabled={busy || !text.trim()} className={`${buttonClass} mt-3 bg-primary-soft text-primary-strong`} onClick={() => void run(() => productApi.import(text, key))}>导入并预览差异</button>
    </section>
    <label className="block text-sm">继续核对已有材料<select className={`${inputClass} mt-1`} value={id ?? ''} onChange={e => setParams(e.target.value ? { case: e.target.value } : {})}><option value="">选择已导入材料</option>{list.data?.items.map(c => <option key={c.case_id} value={c.case_id}>{c.created_at} · {c.status === 'completed' ? '已完成' : '待核对'}</option>)}</select></label>
    <DocumentImport onCase={caseId => setParams({ case: caseId })} />
    <div className="grid items-start gap-4 xl:grid-cols-2">
    {detail.data && <DocumentPreview documentId={detail.data.document_id} selection={selection} />}
    {detail.isLoading && <p>正在读取核对单…</p>}
    {detail.data && <section className="space-y-3" aria-label="药单差异表">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-lg font-medium">逐项核对 · {detail.data.items.filter(i => i.status !== 'pending').length}/{detail.data.items.length}</h3><button className={buttonClass} disabled={busy} onClick={() => void run(() => productApi.refresh(detail.data!.case_id))}>刷新当前药单并重新核对</button></div>
      {detail.data.stale && <p role="alert" className="rounded-lg bg-amber-50 p-3 text-amber-900">患者记录已变化。请刷新差异，重新核对尚未处理的项目。</p>}
      {detail.data.items.map(item => <Item key={`${item.item_id}:${JSON.stringify(detail.data!.base_revision)}:${item.candidate?.corrections.length}`} item={item} disabled={busy || !!detail.data!.stale} onLocate={setSelection} onAction={(action, corrections, k) => run(() => productApi.decide(detail.data!, item, action, corrections, k, activeTask ? {task_id: activeTask.id, revision: activeTask.revision} : undefined))} />)}
      {detail.data.status === 'completed' && <p role="status" className="rounded-card bg-primary-soft p-4">本次核对已完成。已保存记录内容；相关提醒按实际依赖进入重查。可在“风险与证据”查看当前状态。</p>}
    </section>}
    </div>
  </div>;
}

function Item({ item, disabled, onAction, onLocate }: { item: ReconciliationItem; disabled: boolean; onAction: (action: string, corrections: Record<string, unknown>, key: string) => Promise<void>; onLocate: (location: Candidate['locations'][string]) => void }) {
  const [editing, setEditing] = useState(false);
  const [fields, setFields] = useState(item.candidate?.fields ?? {});
  const [nameConfirmed, setNameConfirmed] = useState(false);
  const [presentationConfirmed, setPresentationConfirmed] = useState(false);
  const [target, setTarget] = useState('');
  const [ocrReviewed, setOcrReviewed] = useState(false);
  const [keys] = useState({ accept: newIdempotencyKey(), keep: newIdempotencyKey() });
  const candidate = item.candidate;
  return <article className="overflow-hidden rounded-card border border-border bg-surface">
    <div className="flex items-center justify-between border-b border-border px-4 py-3"><strong>{candidate?.fields.name || item.current[0]?.display_name || '药名待补充'}</strong><span className="text-sm text-ink-secondary">{labels[item.status === 'pending' ? item.kind : item.status]}</span></div>
    <div className="grid sm:grid-cols-2"><div className="border-b border-border bg-surface-alt p-4 sm:border-b-0 sm:border-r"><p className="mb-2 text-xs text-ink-muted">材料中的记录 {candidate?.locations.name?.line ? `· 第 ${candidate.locations.name.line} 行` : ''}</p><p>{candidate ? `${candidate.fields.dose ?? '剂量待补'} ${candidate.fields.unit ?? '单位待补'} · ${candidate.fields.schedule ?? '频次待补'}` : '这份材料没有列出此药'}</p><p className="mt-2 text-xs text-ink-secondary">{candidate?.fields.date} {candidate?.fields.subject}</p></div><div className="p-4"><p className="mb-2 text-xs text-ink-muted">当前记录</p>{item.current.length ? item.current.map(m => <p key={m.ref}>{m.display_name} · {m.dose || '剂量未记录'} · {m.schedule || '频次未记录'}</p>) : <p>尚无匹配的当前记录</p>}</div></div>
    {item.status === 'pending' && <div className="space-y-3 border-t border-border p-4">
      {candidate && <div className="flex flex-wrap gap-2">{Object.entries(candidate.locations).filter(([,loc]) => loc.bbox).map(([field, loc]) => <button key={field} className="text-xs text-primary-strong underline" onClick={() => onLocate(loc)}>定位{fieldLabels[field]}</button>)}</div>}
      {!!item.issues.length && <p className="text-sm text-amber-800">{item.issues.join('；')}</p>}
      {editing && candidate && <div className="space-y-3"><div className="grid grid-cols-2 gap-3">{Object.entries(fieldLabels).map(([field, label]) => <label key={field} className="text-sm">{label}<input className={`${inputClass} mt-1`} value={fields[field] ?? ''} onChange={e => setFields({ ...fields, [field]: e.target.value })} /></label>)}</div>
        <label className="block text-sm"><input type="checkbox" checked={nameConfirmed} onChange={e => setNameConfirmed(e.target.checked)} /> 已按原文核实药名</label>
        <label className="block text-sm"><input type="checkbox" checked={presentationConfirmed} onChange={e => setPresentationConfirmed(e.target.checked)} /> 已核实剂型、规格与所选记录一致</label>
        {candidate.locations.name?.bbox && <label className="block text-sm"><input type="checkbox" checked={ocrReviewed} onChange={e => setOcrReviewed(e.target.checked)} /> 已对照原件核实所有识别字段与患者归属</label>}
        {!!item.current.length && <label className="block text-sm">对应当前哪条记录<select className={inputClass} value={target} onChange={e => setTarget(e.target.value)}><option value="">请选择</option>{item.current.map(m => <option key={m.ref} value={m.ref}>{m.display_name} {m.dose}</option>)}</select></label>}
        <button className={buttonClass} disabled={disabled} onClick={() => void onAction('correct', { ...fields, name_confirmed: nameConfirmed, presentation_confirmed: presentationConfirmed, ...(candidate.locations.name?.bbox ? { ocr_reviewed: ocrReviewed } : {}), ...(target ? { target_ref: target } : {}) }, newIdempotencyKey())}>保存补充信息</button>
      </div>}
      <div className="flex flex-wrap gap-2">{candidate && <><button className={`${buttonClass} bg-primary-soft text-primary-strong`} disabled={disabled || item.issues.length > 0 || item.kind === 'possible_duplicate'} onClick={() => void onAction('accept', {}, keys.accept)}>确认记录内容</button><button className={buttonClass} disabled={disabled} onClick={() => setEditing(!editing)}>补充或更正信息</button></>}<button className={buttonClass} disabled={disabled} onClick={() => void onAction('keep', {}, keys.keep)}>保留现有记录</button></div>
    </div>}
    {item.receipt_id && <p className="border-t border-border px-4 py-2 text-xs text-ink-muted">处理结果已保存，刷新或稍后回来可继续。{item.safety_check && `风险检查：${({succeeded:'已完成，请查看风险与证据', failed:'未完成，请查看失败原因', cancelled:'已取消', waiting_review:'等待专业审核'} as Record<string,string>)[item.safety_check.status ?? ''] ?? '已排入后台检查'}`}</p>}
  </article>;
}
