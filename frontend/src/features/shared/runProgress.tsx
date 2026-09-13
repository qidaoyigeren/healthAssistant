/**
 * 运行进度的公开措辞:与服务端 `harness/progress.py` 的事件词汇一一对应。
 * 只渲染服务端真实推送的执行事件,不做合成进度条——没有事件就说没有事件。
 */
import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/http';
import { qk } from '../../api/queryKeys';
import { retrievalProgress } from './retrievalProgress';

export interface RunProgressEvent {
  event_id: string;
  seq: number;
  kind: string;
  tool?: string | null;
  detail?: Record<string, unknown>;
  created_at: string;
}

export interface RunProgress {
  run_id: string;
  events: RunProgressEvent[];
  latest_seq: number;
  snapshot: boolean;
  run_status?: string;
}

/** 粗粒度阶段词(未审核的医学内容不出现在这里)。 */
export const RUN_PROGRESS_LABELS: Record<string, string> = {
  accepted: '已受理，正在准备核查…',
  organizing: '整理记录…',
  retrieving: '检索依据…',
  checking_risks: '核对风险…',
  waiting_input: '本轮已暂停，等待补充记录',
  recheck_required: '记录已变化，需要继续核查',
  waiting_review: '等待本地模拟审核',
  completed: '核查完成',
  failed: '处理失败',
  cancel_requested: '正在取消…',
  cancelled: '已取消',
};

/**
 * 一行运行进度。没有可展示的事件时:
 * `emptyHint` 给了就如实说明"没有事件",没给就整行不渲染。
 */
export function RunProgressLine({ runId, active, emptyHint }: {
  runId?: string | null; active: boolean; emptyHint?: string;
}): React.ReactElement | null {
  const progress = useQuery({
    queryKey: qk.runProgress(runId ?? '', active),
    queryFn: ({ signal }) => request<RunProgress>(
      `/v1/runs/${encodeURIComponent(runId ?? '')}/progress`, { signal }),
    enabled: !!runId,
    refetchInterval: active ? 2000 : false,
  });
  const latest = progress.data?.events.at(-1);
  const runStatus = progress.data?.run_status;
  if (!latest && !runStatus) {
    if (!emptyHint) return null;
    if (progress.isPending) return <p className="text-sm text-ink-secondary" role="status">读取进度…</p>;
    if (progress.isError) {
      return <p className="text-sm text-caution">进度读取失败，无法判断这一步走到哪里。</p>;
    }
    return <p className="text-sm text-ink-muted">{emptyHint}</p>;
  }
  const label = latest
    ? (retrievalProgress[String(latest.detail?.retrieval_status ?? '')]
      ?? RUN_PROGRESS_LABELS[latest.kind] ?? `阶段:${latest.kind}`)
    : runStatus === 'queued' ? '已受理，等待后台核查…' : '';
  const degradedNote = runStatus === 'degraded'
    ? '（本轮存在降级或未完成步骤，请查看报告说明）' : '';
  return <p className="text-sm text-ink-secondary" role="status">{label}{degradedNote}</p>;
}
