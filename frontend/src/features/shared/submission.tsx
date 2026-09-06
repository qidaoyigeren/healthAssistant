/**
 * 提交任务展示:真实状态(queued/processing/committed/failed/unknown)、
 * 结构化 operation_outcomes、返回正文、预警、冲突与审计摘要。
 * 不使用伪成功 toast;重要结果留在可重新打开的卡片里。
 */
import React, { useState } from 'react';
import { CheckCircle2, CircleDashed, Loader2, OctagonAlert, Undo2 } from 'lucide-react';
import type { SubmissionTask } from '../../api/submissions';
import { submissions } from '../../api/submissions';
import type { OperationOutcomeDto } from '../../api/types';
import { Badge, Card, ConfirmDialog, LiveAnnouncement, TimeText } from '../../components/ui';
import { ConflictCard, SourceRefList, WarningCard } from '../../components/evidence';
import { SafeMarkdown } from '../../components/safeMarkdown';

export function outcomeLabel(outcome: OperationOutcomeDto): string {
  if (outcome.kind === 'medication_change') {
    switch (outcome.outcome) {
      case 'add': return '已记录新增用药';
      case 'dose_change': return '已记录剂量/用法变更';
      case 'remove': return '已记录停用';
      case 'deduplicated': return '与现有在用记录相同,后端去重(未重复新增)';
      case 'unresolved': return '没有找到匹配的在用药记录,本次停用/变更未绑定到药单';
      default: return `操作结果:${outcome.outcome}`;
    }
  }
  switch (outcome.outcome) {
    case 'inserted': return '已记录新事实';
    case 'update': return '已按新记录更新';
    case 'conflict': return '与现有记录不一致,已生成待核实事项';
    case 'deduplicated': return '与已有记录一致,未重复写入';
    default: return `操作结果:${outcome.outcome}`;
  }
}

function OutcomeRow({ outcome }: { outcome: OperationOutcomeDto }): React.ReactElement {
  const tone = ['add', 'dose_change', 'remove', 'inserted', 'update'].includes(outcome.outcome)
    ? 'primary' as const
    : ['deduplicated'].includes(outcome.outcome)
      ? 'neutral' as const
      : 'caution' as const;
  return (
    <li className="flex flex-wrap items-center gap-2">
      <Badge tone={tone}>{outcomeLabel(outcome)}</Badge>
      {outcome.display_name && <span>{outcome.display_name}</span>}
      {outcome.ref && <span className="font-mono text-xs text-ink-muted">{outcome.ref}</span>}
    </li>
  );
}

export function TaskStatusChip({ task }: { task: SubmissionTask }): React.ReactElement {
  switch (task.status) {
    case 'committed':
      return <Badge tone="primary" icon={<CheckCircle2 size={13} aria-hidden />}>已完成</Badge>;
    case 'failed':
      return <Badge tone="danger" icon={<OctagonAlert size={13} aria-hidden />}>处理失败</Badge>;
    case 'rejected':
      return <Badge tone="danger" icon={<OctagonAlert size={13} aria-hidden />}>提交被拒绝</Badge>;
    case 'unknown':
      return <Badge tone="caution" icon={<CircleDashed size={13} aria-hidden />}>状态确认中</Badge>;
    case 'submitting':
    case 'queued':
    case 'processing':
      return <Badge tone="caution" icon={<Loader2 size={13} className="animate-spin" aria-hidden />}>处理中</Badge>;
    default:
      return <Badge tone="neutral">{task.status}</Badge>;
  }
}

export function SubmissionResultView({ task, onClose }: {
  task: SubmissionTask;
  onClose?: () => void;
}): React.ReactElement | null {
  const result = task.result;
  if (!result) return null;
  const outcomes = result.operation_outcomes ?? [];
  return (
    <Card className="mt-3">
      <div className="flex items-center gap-2 px-4 pt-3">
        <Badge tone="primary" icon={<CheckCircle2 size={13} aria-hidden />}>记录完成</Badge>
        <span className="text-xs text-ink-muted">
          提交于 <TimeText iso={task.startedAt} />
        </span>
        {onClose && (
          <button type="button" onClick={onClose}
            className="ml-auto rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
            收起
          </button>
        )}
      </div>
      <div className="px-4 pb-4 pt-2">
        {outcomes.length > 0 && (
          <ul className="mb-2 space-y-1.5">
            {outcomes.map((outcome, index) => <OutcomeRow key={index} outcome={outcome} />)}
          </ul>
        )}
        <SafeMarkdown text={result.text} />
        {result.warnings.length > 0 && (
          <div className="mt-3 space-y-2">
            <h3 className="text-sm font-medium">本次检查结果</h3>
            {result.warnings.map((warning, index) => (
              <WarningCard key={index} warning={warning} />
            ))}
          </div>
        )}
        {result.warnings.length === 0 && ['medication_change', 'procedure_exposure'].includes(task.event.event_type) && (
          <p className="mt-2 rounded-lg bg-surface-alt px-3 py-2 text-sm text-ink-secondary">
            本次检查未形成带证据的新增警告(这不代表「全部用药安全」)。
          </p>
        )}
        {result.conflicts.length > 0 && (
          <div className="mt-3 space-y-2">
            <h3 className="text-sm font-medium">本次产生的待核实事项</h3>
            {result.conflicts.map((conflict) => (
              <ConflictCard key={conflict.ref} conflict={conflict} />
            ))}
          </div>
        )}
        <details className="mt-3 rounded-lg border border-border px-3 py-2">
          <summary className="cursor-pointer text-sm text-ink-secondary">审计与来源(诊断详情)</summary>
          <div className="mt-2 space-y-2 text-sm">
            <p className="text-xs text-ink-muted">
              回答来源:{result.audit_trail.response_source ?? '未记录'}
              {result.audit_trail.response_fallback_reason
                ? ` · 回退原因:${result.audit_trail.response_fallback_reason}` : ''}
              {result.audit_trail.turn_id ? ` · 轮次 ${result.audit_trail.turn_id}` : ''}
            </p>
            <SourceRefList sources={result.audit_trail.source_refs}
              emptyHint="本轮回答没有引用外部来源。" />
            {result.audit_trail.memory_refs && result.audit_trail.memory_refs.length > 0 && (
              <p className="break-all font-mono text-xs text-ink-muted">
                关联记录:{result.audit_trail.memory_refs.join('、')}
              </p>
            )}
          </div>
        </details>
      </div>
    </Card>
  );
}

/** 进行中/失败任务托盘:可重新打开;失败提供受控重试。 */
export function TaskTray({ tasks, onRetryNew }: {
  tasks: SubmissionTask[];
  onRetryNew?: (task: SubmissionTask) => void;
}): React.ReactElement | null {
  const [retryTarget, setRetryTarget] = useState<SubmissionTask | null>(null);
  const active = tasks.filter((t) => ['submitting', 'queued', 'processing', 'unknown'].includes(t.status));
  const failed = tasks.filter((t) => t.status === 'failed' || t.status === 'rejected');
  if (active.length === 0 && failed.length === 0) return null;

  const announce = active.length > 0
    ? `${active.length} 个提交正在处理中`
    : `${failed.length} 个提交需要处理`;

  return (
    <>
      <LiveAnnouncement message={announce} />
      <Card className="mb-4 border-caution/40">
        <div className="border-b border-border px-4 pt-3">
          <h2 className="text-sm font-medium">提交任务</h2>
        </div>
        <ul>
          {active.map((task) => (
            <li key={task.key} className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5 text-sm last:border-b-0">
              <TaskStatusChip task={task} />
              <span className="min-w-0 flex-1 truncate">
                {describeTask(task)}
              </span>
              {task.status === 'unknown' && task.networkError && (
                <span className="w-full text-xs text-caution">{task.networkError}</span>
              )}
              {task.slow && task.status !== 'unknown' && (
                <span className="w-full text-xs text-ink-muted">
                  处理时间较长,仍在确认处理状态;不会重复提交。
                </span>
              )}
              {task.status === 'unknown' && (
                <button type="button"
                  onClick={() => submissions.stopWaiting(task.key)}
                  className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                  停止等待
                </button>
              )}
            </li>
          ))}
          {failed.map((task) => (
            <li key={task.key} className="border-b border-border px-4 py-2.5 text-sm last:border-b-0">
              <div className="flex flex-wrap items-center gap-2">
                <TaskStatusChip task={task} />
                <span className="min-w-0 flex-1 truncate">{describeTask(task)}</span>
                {task.status === 'failed' && (
                  <button type="button" onClick={() => setRetryTarget(task)}
                    className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                    <Undo2 size={12} aria-hidden /> 服务端重试(保留原记录)
                  </button>
                )}
                {onRetryNew && (
                  <button type="button" onClick={() => onRetryNew(task)}
                    className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                    重新填写提交
                  </button>
                )}
              </div>
              <p className="mt-1 break-words text-xs text-danger">
                {task.failure?.error ?? task.rejection?.displayMessage() ?? '未知错误'}
              </p>
              {task.status === 'rejected' && task.rejection?.body?.code === 'idempotency_key_reused' && (
                <p className="mt-1 text-xs text-ink-secondary">
                  您填写的内容已保留;这是一次新的提交操作,请检查后重新提交。
                </p>
              )}
            </li>
          ))}
        </ul>
      </Card>
      <ConfirmDialog
        open={!!retryTarget}
        onOpenChange={(open) => { if (!open) setRetryTarget(null); }}
        title="在服务端重试这次提交?"
        description="将使用原始提交记录重试(不产生第二条记录)。重试结果仍以服务端确认为准。"
        confirmLabel="重试"
        onConfirm={() => {
          if (retryTarget) void submissions.retryOnServer(retryTarget.key);
          setRetryTarget(null);
        }}
      />
    </>
  );
}

export function describeTask(task: SubmissionTask): string {
  const typeLabels: Record<string, string> = {
    register_profile: '登记患者情况',
    profile_update: '更新患者情况',
    medication_change: `用药记录:${task.event.payload['medication'] ?? ''}`,
    procedure_exposure: `造影剂暴露:${task.event.payload['agent'] ?? ''}`,
    query_current_medications: '查询当前用药',
    user_message: '照护助手提问',
  };
  return typeLabels[task.event.event_type] ?? task.event.event_type;
}
