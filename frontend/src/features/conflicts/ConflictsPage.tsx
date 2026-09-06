/**
 * 待核实事项:冲突双方并排展示、动作历史、真实动作枚举
 * (resolved/dismissed/reopened/undo)。动作只记录照护者的核实行为 ——
 * 冲突处理状态与事实投影状态分别展示,不显示「系统已认定哪方正确」。
 */
import React, { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSubmissions } from '../../hooks/useSubmissions';
import { ArrowLeftRight, History as HistoryIcon } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import {
  Badge, Card, ConfirmDialog, EmptyState, ErrorState, LoadingBlock,
  SectionTitle, SkeletonList, TimeText,
} from '../../components/ui';
import { ConflictSides, MemoryRefDrawer } from '../../components/evidence';
import { TaskTray } from '../shared/submission';

type TabKind = 'open' | 'resolved' | 'dismissed' | 'all';
const TAB_LABELS: Record<TabKind, string> = {
  open: '待处理',
  resolved: '已记录核实结果',
  dismissed: '已标记无需处理',
  all: '全部',
};

export function ConflictsPage(): React.ReactElement {
  const { conflictId } = useParams();
  const [tab, setTab] = useState<TabKind>('open');
  const [openRef, setOpenRef] = useState<string | null>(null);

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">待核实事项</h1>
          <p className="mt-1 text-sm text-ink-secondary">
            冲突双方都会完整展示;系统不静默选择任何一方。
          </p>
        </div>
        <Link to="/exposure"
          className="rounded-lg border border-primary px-3.5 py-2 text-sm font-medium text-primary-strong hover:bg-primary-soft">
          记录造影剂暴露
        </Link>
      </header>

      {conflictId ? (
        <ConflictDetail conflictId={Number(conflictId)} onOpenRef={setOpenRef} />
      ) : (
        <>
          <div role="tablist" aria-label="冲突状态分组" className="flex flex-wrap gap-2">
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
          <ConflictList status={tab} onOpenRef={setOpenRef} />
          <TaskTrayWrapper />
        </>
      )}
      <MemoryRefDrawer memoryRef={openRef} onClose={() => setOpenRef(null)} />
    </div>
  );
}

function ConflictList({ status, onOpenRef }: {
  status: TabKind; onOpenRef: (ref: string) => void;
}): React.ReactElement {
  const query = useQuery({
    queryKey: qk.conflictRecords(status),
    queryFn: ({ signal }) => api.conflictRecords(
      { status: status === 'all' ? 'all' : status, limit: 50 }, signal),
  });
  if (query.isPending) return <SkeletonList rows={3} />;
  if (query.isError) return <ErrorState error={query.error} />;
  if (query.data.items.length === 0) {
    return (
      <EmptyState
        title={status === 'open' ? '没有待处理的核实事项' : '这个分组下没有冲突记录'}
        hint={status === 'open' ? '当事实记录之间出现矛盾时,系统会在这里创建待核实事项。' : undefined} />
    );
  }
  return (
    <ul className="space-y-3">
      {query.data.items.map((conflict) => (
        <li key={conflict.ref}>
          <Card className="p-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={conflict.status === 'open' ? 'caution' : 'neutral'}
                icon={<ArrowLeftRight size={13} aria-hidden />}>
                {conflict.status === 'open' ? '待处理' : conflict.status === 'resolved' ? '已记录核实结果' : '已标记无需处理'}
              </Badge>
              <span className="text-xs text-ink-muted">{conflict.conflict_type}</span>
              <span className="ml-auto"><TimeText iso={conflict.created_at} prefix="创建于 " /></span>
              <Link to={`/conflicts/${conflict.id}`}
                className="rounded-lg border border-border px-2.5 py-1 text-xs hover:bg-surface-alt">
                处理 / 查看双方
              </Link>
            </div>
            <p className="mt-2 text-sm">{conflict.description}</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {[conflict.left_ref, conflict.right_ref].map((ref) => (
                <button key={ref} type="button" onClick={() => onOpenRef(ref)}
                  className="rounded border border-border bg-surface-alt px-2 py-0.5 font-mono text-xs text-primary hover:bg-primary-soft">
                  {ref}
                </button>
              ))}
            </div>
          </Card>
        </li>
      ))}
    </ul>
  );
}

function ConflictDetail({ conflictId, onOpenRef }: {
  conflictId: number; onOpenRef: (ref: string) => void;
}): React.ReactElement {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: qk.conflictRecord(conflictId),
    queryFn: ({ signal }) => api.conflictRecord(conflictId, signal),
    retry: false,
  });
  const [action, setAction] = useState<'resolved' | 'dismissed' | 'reopened' | 'undo'>('resolved');
  const [basis, setBasis] = useState('');
  const [chosenRef, setChosenRef] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  if (query.isPending) return <LoadingBlock />;
  if (query.isError) return <ErrorState error={query.error} title="读取冲突详情失败" />;
  const conflict = query.data;

  const actionLabels: Record<string, string> = {
    resolved: '记录核实结果(我已核实双方信息)',
    dismissed: '标记本次无需继续处理',
    reopened: '重新打开',
    undo: '撤销上次处理',
  };

  const submitAction = () => {
    setBusy(true);
    api.conflictAction(conflictId, {
      action, basis, actor: 'caregiver',
      ...(chosenRef ? { chosen_ref: chosenRef } : {}),
    })
      .then((result) => {
        setMessage(`动作已记录:${actionLabels[action] ?? action}。冲突当前状态:${result.status}。`);
        setBasis('');
        setChosenRef('');
        void queryClient.invalidateQueries({ queryKey: qk.conflictRecord(conflictId) });
        void queryClient.invalidateQueries({ queryKey: qk.conflictRecords('open') });
        void queryClient.invalidateQueries({ queryKey: qk.conflictRecords('all') });
        void queryClient.invalidateQueries({ queryKey: qk.conflicts });
        void queryClient.invalidateQueries({ queryKey: qk.memoryState() });
      })
      .catch((err) => setMessage(err instanceof Error ? `动作失败:${err.message}` : '动作失败。'))
      .finally(() => setBusy(false));
  };

  return (
    <div className="space-y-4">
      <Link to="/conflicts" className="text-sm text-primary underline">← 返回待核实列表</Link>

      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={conflict.status === 'open' ? 'caution' : 'neutral'}
            icon={<ArrowLeftRight size={13} aria-hidden />}>
            冲突状态:{conflict.status === 'open' ? '待处理' : conflict.status}
          </Badge>
          <span className="text-xs text-ink-muted">{conflict.conflict_type}</span>
        </div>
        <p className="mt-2 text-sm">{conflict.description}</p>
        <p className="mt-1 text-xs text-ink-muted">
          上面的状态是「核实流程」的状态;各条事实本身是否被采信,以患者档案中
          各自版本的事实状态为准 —— 记录核实结果不会自动把事实投影改成某一方。
        </p>
      </Card>

      <SectionTitle>冲突双方记录</SectionTitle>
      <ConflictSides conflict={conflict} />
      <div className="flex flex-wrap gap-1.5">
        {[conflict.left_ref, conflict.right_ref].map((ref) => (
          <button key={ref} type="button" onClick={() => onOpenRef(ref)}
            className="rounded border border-border bg-surface-alt px-2 py-1 font-mono text-xs text-primary hover:bg-primary-soft">
            查看完整记录:{ref}
          </button>
        ))}
      </div>

      <Card>
        <SectionTitle>处理动作</SectionTitle>
        <div className="space-y-3 p-4">
          <label className="block text-sm">
            <span className="mb-1 block font-medium">动作</span>
            <select value={action} onChange={(e) => setAction(e.target.value as typeof action)}
              className={inputClass}>
              {(Object.entries(actionLabels) as [string, string][]).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">依据(必填)</span>
            <input type="text" value={basis} onChange={(e) => setBasis(e.target.value)}
              className={inputClass}
              placeholder="如:对照两份出院小结 / 与医生当面确认 / 患者再次口头确认" />
          </label>
          {(action === 'resolved' || action === 'dismissed') && (
            <label className="block text-sm">
              <span className="mb-1 block font-medium">采信引用(可选)</span>
              <select value={chosenRef} onChange={(e) => setChosenRef(e.target.value)}
                className={inputClass}>
                <option value="">不指定</option>
                <option value={conflict.left_ref}>甲方:{conflict.left_ref}</option>
                <option value={conflict.right_ref}>乙方:{conflict.right_ref}</option>
              </select>
            </label>
          )}
          <button type="button" disabled={busy || !basis.trim()}
            onClick={() => setConfirmOpen(true)}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-strong disabled:opacity-50">
            提交动作
          </button>
          {message && <p aria-live="polite" className="rounded-lg bg-surface-alt px-3 py-2 text-sm">{message}</p>}
        </div>
      </Card>

      <Card>
        <SectionTitle actions={<HistoryIcon size={14} className="text-ink-muted" aria-hidden />}>
          动作历史
        </SectionTitle>
        <ul className="divide-y divide-border px-4 pb-3">
          {conflict.actions && conflict.actions.length > 0 ? (
            conflict.actions.map((entry) => (
              <li key={entry.id} className="py-2.5 text-sm">
                <span className="font-medium">{actionLabels[entry.action] ?? entry.action}</span>
                <span className="ml-2 text-xs text-ink-muted">
                  <TimeText iso={entry.created_at} /> · {entry.actor}
                  {entry.previous_status ? ` · 前状态 ${entry.previous_status}` : ''}
                </span>
                <p className="mt-0.5 text-xs text-ink-secondary">依据:{entry.basis}</p>
                {entry.chosen_ref && (
                  <p className="mt-0.5 font-mono text-xs text-ink-muted">采信:{entry.chosen_ref}</p>
                )}
              </li>
            ))
          ) : (
            <li className="py-3 text-sm text-ink-muted">还没有处理动作。</li>
          )}
        </ul>
      </Card>

      <TaskTrayWrapper />

      <ConfirmDialog
        open={confirmOpen} onOpenChange={setConfirmOpen}
        title={actionLabels[action] ?? action}
        description={<>将记录一次「{actionLabels[action] ?? action}」动作,依据:{basis || '(未填写)'}。动作会落库并可撤销/重开。</>}
        confirmLabel="确认提交"
        busy={busy}
        onConfirm={() => { setConfirmOpen(false); submitAction(); }}
      />
    </div>
  );
}

function TaskTrayWrapper(): React.ReactElement {
  const tasks = useSubmissions();
  return <TaskTray tasks={tasks} />;
}

const inputClass = 'w-full rounded-lg border border-border bg-surface px-3 py-2 text-base focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30';
