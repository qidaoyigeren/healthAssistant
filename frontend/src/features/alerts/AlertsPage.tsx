/**
 * 风险与证据(预警中心):按「当前有效 / 待复查 / 历史」组织,判断基于真实
 * conclusion status 与 superseded_by,不按用药是否在当前列表推断。
 * 严重度/置信度无结构化字段时如实显示「未记录」,不虚构百分比。
 */
import React, { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { CircleDashed, Link2, RefreshCw } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import type { ConclusionDto } from '../../api/types';
import {
  Badge, Card, ConfirmDialog, EmptyState, ErrorState, LoadingBlock,
  SectionTitle, SkeletonList, TimeText,
} from '../../components/ui';
import { Field, MemoryRefDrawer, SourceRefList } from '../../components/evidence';
import type { RecheckTasksDto } from '../../api/types';

type TabKind = 'current' | 'recheck' | 'stale';
const TAB_LABELS: Record<TabKind, string> = {
  current: '当前有效',
  recheck: '待复查',
  stale: '历史',
};

export function AlertsPage(): React.ReactElement {
  const { alertId } = useParams();
  const [tab, setTab] = useState<TabKind>('current');
  const [openRef, setOpenRef] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<number | null>(
    alertId ? Number(alertId) : null);

  const recordsQuery = useQuery({
    queryKey: qk.alertRecords(tab === 'stale' ? 'stale' : 'current'),
    queryFn: ({ signal }) => api.alertRecords(
      { status: tab === 'stale' ? 'stale' : 'current', limit: 50 }, signal),
  });
  const rechecksQuery = useQuery({
    queryKey: qk.recheckTasks,
    queryFn: ({ signal }) => api.recheckTasks(signal),
    enabled: tab === 'recheck',
  });

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold">风险与证据</h1>
        <p className="mt-1 text-sm text-ink-secondary">
          每条预警都保留结论状态、来源引用和替代链;失效结论不再当作当前风险。
        </p>
      </header>

      <div role="tablist" aria-label="预警分组" className="flex flex-wrap gap-2">
        {(Object.keys(TAB_LABELS) as TabKind[]).map((kind) => (
          <button key={kind} type="button" role="tab" aria-selected={tab === kind}
            onClick={() => setTab(kind)}
            className={`rounded-lg border px-3 py-1.5 text-sm ${
              tab === kind ? 'border-primary bg-primary-soft font-medium text-primary-strong' : 'border-border hover:bg-surface-alt'
            }`}>
            {TAB_LABELS[kind]}
          </button>
        ))}
      </div>

      {tab === 'recheck' ? (
        <RecheckPanel rechecksQuery={rechecksQuery} />
      ) : (
        <Card>
          <SectionTitle actions={
            <span className="text-xs text-ink-muted">
              {recordsQuery.data ? `共 ${recordsQuery.data.total} 条` : ''}
            </span>
          }>
            {TAB_LABELS[tab]}
          </SectionTitle>
          {recordsQuery.isPending && <SkeletonList rows={3} />}
          {recordsQuery.isError && <div className="p-4"><ErrorState error={recordsQuery.error} /></div>}
          {recordsQuery.data && recordsQuery.data.items.length === 0 && (
            <div className="p-4">
              {tab === 'current' ? (
                <EmptyState title="没有当前有效预警"
                  hint="这不代表「全部用药安全」——只表示当前没有处于有效状态的预警结论。" />
              ) : (
                <EmptyState title="没有历史(失效)结论" />
              )}
            </div>
          )}
          {recordsQuery.data && recordsQuery.data.items.length > 0 && (
            <ul className="divide-y divide-border px-4 pb-3">
              {recordsQuery.data.items.map((record) => (
                <AlertRecordRow key={record.ref} record={record}
                  onOpenDetail={() => setDetailId(record.id)}
                  onOpenRef={setOpenRef} />
              ))}
            </ul>
          )}
        </Card>
      )}

      <DetailPanel detailId={detailId} onClose={() => setDetailId(null)} onOpenRef={setOpenRef} />
      <MemoryRefDrawer memoryRef={openRef} onClose={() => setOpenRef(null)} />
    </div>
  );
}

export function AlertRecordRow({ record, onOpenDetail, onOpenRef }: {
  record: ConclusionDto; onOpenDetail: () => void; onOpenRef: (ref: string) => void;
}): React.ReactElement {
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        {record.status === 'current'
          ? <Badge tone="primary">当前有效</Badge>
          : <Badge tone="neutral">已失效</Badge>}
        <Badge tone="neutral">{record.kind}</Badge>
        <span className="ml-auto"><TimeText iso={record.created_at} prefix="结论生成于 " /></span>
      </div>
      <p className="mt-1.5 text-sm leading-relaxed">{record.text}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
        <button type="button" onClick={onOpenDetail}
          className="rounded-lg border border-border px-2 py-1 hover:bg-surface-alt">
          查看详情与替代链
        </button>
        {record.superseded_by && (
          <span className="text-ink-muted">已被结论 #{record.superseded_by} 替代</span>
        )}
        {record.memory_refs.slice(0, 3).map((ref) => (
          <button key={ref} type="button" onClick={() => onOpenRef(ref)}
            className="inline-flex items-center gap-1 rounded border border-border bg-surface-alt px-1.5 py-0.5 font-mono text-primary hover:bg-primary-soft">
            <Link2 size={10} aria-hidden />{ref}
          </button>
        ))}
        {!record.evidence_available && (
          <span className="text-caution">该结论没有结构化来源引用(原文缺失状态如实展示)</span>
        )}
      </div>
    </li>
  );
}

function DetailPanel({ detailId, onClose, onOpenRef }: {
  detailId: number | null; onClose: () => void; onOpenRef: (ref: string) => void;
}): React.ReactElement | null {
  const detailQuery = useQuery({
    queryKey: qk.alertRecord(detailId ?? ''),
    queryFn: ({ signal }) => api.alertRecord(detailId!, signal),
    enabled: detailId != null,
  });
  if (detailId == null) return null;
  return (
    <Card className="border-primary/40">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-sm font-medium">预警详情</h2>
        <button type="button" onClick={onClose}
          className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">关闭</button>
      </div>
      <div className="p-4">
        {detailQuery.isPending && <LoadingBlock />}
        {detailQuery.isError && <ErrorState error={detailQuery.error} />}
        {detailQuery.data && (
          <div className="space-y-3 text-sm">
            <p className="leading-relaxed">{detailQuery.data.text}</p>
            <dl>
              <Field label="结论状态">
                {detailQuery.data.status === 'current' ? '当前有效' : `已失效${detailQuery.data.stale_reason ? `(${detailQuery.data.stale_reason})` : ''}`}
              </Field>
              <Field label="生成时间"><TimeText iso={detailQuery.data.created_at} /></Field>
              <Field label="严重度">未记录(该结论没有结构化严重度字段,不从文字推断)</Field>
              <Field label="置信度">未记录</Field>
              <Field label="会话/轮次">
                {detailQuery.data.session_id ?? '未记录'} / {detailQuery.data.turn_id ?? '未记录'}
              </Field>
            </dl>
            <div>
              <h3 className="mb-1 font-medium">关联记录</h3>
              {detailQuery.data.memory_refs.length === 0
                ? <p className="text-ink-muted">没有关联 memory 引用。</p>
                : (
                  <div className="flex flex-wrap gap-1.5">
                    {detailQuery.data.memory_refs.map((ref) => (
                      <button key={ref} type="button" onClick={() => onOpenRef(ref)}
                        className="rounded border border-border bg-surface-alt px-2 py-1 font-mono text-xs text-primary hover:bg-primary-soft">
                        <Link2 size={10} className="mr-1 inline" aria-hidden />{ref}
                      </button>
                    ))}
                  </div>
                )}
            </div>
            <div>
              <h3 className="mb-1 font-medium">来源与原文</h3>
              <SourceRefList sources={detailQuery.data.source_refs}
                emptyHint="该结论没有结构化来源引用(原文缺失状态如实展示)。" />
            </div>
            {detailQuery.data.chain && (
              <div>
                <h3 className="mb-1 font-medium">结论替代链(旧 → 新)</h3>
                <ol className="space-y-1.5">
                  {detailQuery.data.chain.versions.map((version) => (
                    <li key={version.ref}
                      className={`rounded-lg border px-3 py-2 text-xs ${
                        version.id === detailQuery.data!.id ? 'border-primary bg-primary-soft' : 'border-border bg-surface-alt'
                      }`}>
                      <span className="font-mono">#{version.id}</span>
                      <span className="ml-2">{version.status === 'current' ? '当前有效' : '已失效'}</span>
                      <span className="ml-2"><TimeText iso={version.created_at} /></span>
                      <p className="mt-1">{version.text}</p>
                      {version.superseded_by && (
                        <span className="mt-0.5 block text-ink-muted">→ 被 #{version.superseded_by} 替代</span>
                      )}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

function RecheckPanel({ rechecksQuery }: {
  rechecksQuery: {
    isPending: boolean; isError: boolean; error: unknown;
    data: RecheckTasksDto | undefined;
  };
}): React.ReactElement {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [resultMessage, setResultMessage] = useState<string | null>(null);

  const runRechecks = () => {
    setRunning(true);
    api.runRechecks(10)
      .then((result) => {
        if (result.status === 'no_hook') {
          setResultMessage('复查未执行:没有可用的复查钩子配置(如未配置检测数据源)。');
        } else {
          setResultMessage(`复查执行完成:本次处理 ${result.completed.length} 项,剩余待复查 ${result.pending} 项。`);
        }
        void queryClient.invalidateQueries({ queryKey: qk.recheckTasks });
        void queryClient.invalidateQueries({ queryKey: qk.alertRecords('current') });
        void queryClient.invalidateQueries({ queryKey: qk.overview });
      })
      .catch((err) => setResultMessage(err instanceof Error ? err.message : '复查执行失败。'))
      .finally(() => setRunning(false));
  };

  return (
    <>
      <Card>
        <SectionTitle actions={
          <button type="button" onClick={() => setConfirmOpen(true)} disabled={running}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm text-white hover:bg-primary-strong disabled:opacity-50">
            <RefreshCw size={14} className={running ? 'animate-spin' : ''} aria-hidden />
            执行待复查任务
          </button>
        }>
          待复查任务
        </SectionTitle>
        <div className="p-4">
          {rechecksQuery.isPending && <SkeletonList rows={2} />}
          {rechecksQuery.isError && <ErrorState error={rechecksQuery.error} />}
          {rechecksQuery.data && rechecksQuery.data.pending_count === 0 && (
            <EmptyState title="没有待复查任务"
              hint="当用药或事实变化使结论失效时,系统会自动生成复查任务。" />
          )}
          {rechecksQuery.data && rechecksQuery.data.pending_count > 0 && (
            <ul className="space-y-2">
              {rechecksQuery.data.pending.map((task) => (
                <li key={task.id} className="rounded-lg border border-caution/30 bg-caution-soft/40 px-3 py-2 text-sm">
                  <p className="flex flex-wrap items-center gap-2">
                    <Badge tone="caution" icon={<CircleDashed size={13} aria-hidden />}>待复查</Badge>
                    <span className="text-xs text-ink-muted">原因:{task.reason ?? '未记录'}</span>
                    <TimeText iso={task.created_at} prefix="生成于 " />
                  </p>
                  <p className="mt-1">{task.target_conclusion?.text ?? '原结论内容读取失败(引用缺失如实展示)'}</p>
                </li>
              ))}
            </ul>
          )}
          {resultMessage && (
            <p aria-live="polite" className="mt-3 rounded-lg bg-surface-alt px-3 py-2 text-sm">{resultMessage}</p>
          )}
          <p className="mt-2 text-xs text-ink-muted">
            复查只处理已排队的失效结论,不是全量重检;执行结果以服务端返回为准。
          </p>
        </div>
      </Card>
      <ConfirmDialog
        open={confirmOpen} onOpenChange={setConfirmOpen}
        title="执行待复查任务?"
        description="将对当前排队的失效结论重新执行检测(最多 10 项);这不是全量重检。"
        confirmLabel="执行复查"
        busy={running}
        onConfirm={() => { setConfirmOpen(false); runRechecks(); }}
      />
    </>
  );
}
