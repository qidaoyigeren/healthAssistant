import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { request, newIdempotencyKey } from '../../api/http';
import type { Candidate } from '../../api/product';
import { buttonClass } from './MaterialsPage';

interface Job { id: string; status: string; attempts: number; case_id: string | null; error?: string }
interface Page { page: number; width: number; height: number; png_base64: string }

export function DocumentImport({ onCase }: { onCase: (id: string) => void }): React.ReactElement {
  const client = useQueryClient();
  const jobs = useQuery({ queryKey: ['parse-jobs'], queryFn: () => request<{ items: Job[] }>('/v1/materials/parse-jobs'), refetchInterval: 5000 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [key, setKey] = useState(newIdempotencyKey);
  async function parse(id: string) {
    const job = await request<Job>(`/v1/materials/parse-jobs/${id}/parse`, { method: 'POST' });
    if (job.case_id) onCase(job.case_id);
  }
  async function run(fn: () => Promise<void>) { setBusy(true); setError(''); try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : '解析失败'); } finally { setBusy(false); await client.invalidateQueries(); } }
  async function upload() {
    if (!file) return;
    if (file.size > 6 * 1024 * 1024) throw new Error('材料不得超过 6 MB');
    const encoded = await new Promise<string>((resolve, reject) => { const r = new FileReader(); r.onerror = reject; r.onload = () => resolve(String(r.result).split(',')[1] ?? ''); r.readAsDataURL(file); });
    const job = await request<Job>('/v1/materials/document', { method: 'POST', body: { key, mime: file.type, base64: encoded } });
    await parse(job.id);
  }
  return <section className="space-y-3 rounded-card border border-border bg-surface p-4"><h3 className="font-medium">导入打印版出院用药表</h3><p className="text-sm text-ink-secondary">清晰 PNG、JPEG 或最多 3 页 PDF，6 MB 以内。需含药名、剂量、单位、频次表头。手写、药盒与复杂报告暂不支持。</p><input aria-label="选择用药表图片或 PDF" type="file" accept="image/png,image/jpeg,application/pdf" onChange={e => { setFile(e.target.files?.[0] ?? null); setKey(newIdempotencyKey()); }} /><button className={buttonClass} disabled={busy || !file} onClick={() => void run(upload)}>{busy ? '正在本地解析…' : '上传并识别用药表'}</button>{error && <p role="alert" className="text-sm text-red-800">{error}</p>}{jobs.data?.items.filter(j => j.status !== 'completed').map(j => <div className="flex gap-3 text-sm" key={j.id}><p>{j.status === 'failed' ? `解析失败：${j.error}` : j.status === 'running' ? '正在解析；中断后可稍后重试' : '材料已保存，等待解析'}</p><button className={buttonClass} disabled={busy || j.attempts >= 3} onClick={() => void run(() => parse(j.id))}>重试解析</button></div>)}</section>;
}

export function DocumentPreview({ documentId, selection }: { documentId: string; selection: Candidate['locations'][string] | null }): React.ReactElement | null {
  const doc = useQuery({ queryKey: ['document', documentId], queryFn: () => request<{ pages?: Page[] }>(`/v1/materials/documents/${documentId}`) });
  if (doc.error) return <p role="alert">原件不可用，请重新检查材料。</p>;
  if (!doc.data?.pages) return null;
  const page = doc.data.pages.find(p => p.page === selection?.page) ?? doc.data.pages[0];
  if (!page) return null;
  const box = selection?.bbox?.length === 4 ? selection.bbox as [number, number, number, number] : null;
  return <aside className="rounded-card border border-border bg-surface p-3" aria-label="材料原件"><h3 className="mb-2 text-sm font-medium">材料原件 · 第 {page.page} 页</h3><p className="mb-2 text-xs text-ink-muted">点击候选字段可定位原文。识别分数不代表医学正确概率。</p><div className="relative"><img className="w-full" src={`data:image/png;base64,${page.png_base64}`} alt={`用药表原件第 ${page.page} 页`} />{box && <div aria-label="选中字段原文位置" className="pointer-events-none absolute border-2 border-blue-600 bg-blue-300/25" style={{ left: `${box[0] / page.width * 100}%`, top: `${box[1] / page.height * 100}%`, width: `${(box[2] - box[0]) / page.width * 100}%`, height: `${(box[3] - box[1]) / page.height * 100}%` }} />}</div></aside>;
}
