/**
 * 提交任务展示:真实状态(queued/processing/committed/failed/unknown)、
 * 结构化 operation_outcomes、返回正文、预警、冲突与审计摘要。
 * 不使用伪成功 toast;重要结果留在可重新打开的卡片里。
 */
import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Ban, CheckCircle2, CircleDashed, Loader2, OctagonAlert, RefreshCw, Undo2 } from 'lucide-react';
import type { SubmissionTask } from '../../api/submissions';
import { submissions } from '../../api/submissions';
import { api } from '../../api/client';
import type { ChangeImpactDto, OperationOutcomeDto, RunProgressEventDto } from '../../api/types';
import { Badge, Card, ConfirmDialog, LiveAnnouncement, TimeText } from '../../components/ui';
import { ConflictCard, SourceRefList, WarningCard } from '../../components/evidence';
import { SafeMarkdown } from '../../components/safeMarkdown';

/** Harness P2:产品级进度词汇(服务端只推粗粒度状态,无未审核医学内容)。 */
const PROGRESS_LABELS: Record<string, string> = {
  accepted: '已受理',
  organizing: '整理记录…',
  retrieving: '检索依据…',
  checking_risks: '核对风险…',
  waiting_review: '等待人工审核',
  completed: '已完成',
  failed: '处理失败',
  cancel_requested: '取消请求已受理…',
  cancelled: '已取消',
};

function latestProgress(task: SubmissionTask): RunProgressEventDto | null {
  return task.progress.length > 0 ? (task.progress[task.progress.length - 1] ?? null) : null;
}

function progressLabel(event: RunProgressEventDto): string {
  return PROGRESS_LABELS[event.kind] ?? `阶段:${event.kind}`;
}

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
      if (task.result?.run_status === 'cancelled' || task.cancelState === 'cancelled') {
        return <Badge tone="neutral" icon={<Ban size={13} aria-hidden />}>已取消</Badge>;
      }
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
      if (task.cancelState === 'requested') {
        return <Badge tone="caution" icon={<Loader2 size={13} className="animate-spin" aria-hidden />}>取消中…</Badge>;
      }
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
  const cancelled = result.run_status === 'cancelled' || task.cancelState === 'cancelled';
  return (
    <Card className="mt-3">
      <div className="flex items-center gap-2 px-4 pt-3">
        <Badge tone={cancelled ? 'neutral' : 'primary'}
          icon={cancelled ? <Ban size={13} aria-hidden /> : <CheckCircle2 size={13} aria-hidden />}>
          {cancelled ? '任务已取消' : '记录完成'}
        </Badge>
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
        {task.status === 'committed' && task.event.event_type === 'medication_change' && (
          <ChangeImpactCard runId={task.runId} />
        )}
        <details className="mt-3 rounded-lg border border-border px-3 py-2">
          <summary className="cursor-pointer text-sm text-ink-secondary">审计与来源(诊断详情)</summary>
          <div className="mt-2 space-y-2 text-sm">
            {result.answer_bundle && (
              <div className="rounded-lg border border-border bg-surface-alt p-3">
                <h4 className="mb-1 text-xs font-medium">本轮回答依据({result.answer_bundle.bundle_version})</h4>
                <ul className="space-y-0.5 text-xs text-ink-secondary">
                  <li>结论/警告条目:{result.answer_bundle.claims.length}(与上方卡片同源)</li>
                  <li>关联记录引用:{result.answer_bundle.fact_refs.length} 项 · 证据引用:{result.answer_bundle.evidence_refs.length} 项</li>
                  <li>档案版本:用药 v{result.answer_bundle.patient_revision.medications} / 语义 v{result.answer_bundle.patient_revision.semantic}</li>
                  {result.answer_bundle.unresolved_questions.length > 0 && (
                    <li>未决事项:{result.answer_bundle.unresolved_questions.join('、')}</li>
                  )}
                  <li>保存状态:{result.answer_bundle.coverage.consolidated ? '本次事件已保存' : '本次事件未完成保存'}</li>
                </ul>
              </div>
            )}
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

/**
 * 变更影响卡片(Product P1):用药更正提交后,展示真实的失效与重查状态。
 * 数据来自 GET /v1/change-impact(依赖失效与重查任务的同一口径),`since` 锚定
 * 本次提交时间。stale = 依据变化待重查,不等于风险解除;重查失败/未运行时
 * 卡片如实例示,不给"已安全"的表述。
 */
export function ChangeImpactCard({ runId }: { runId: string | null }): React.ReactElement {
  const [expanded, setExpanded] = useState(false);
  const impactQuery = useQuery({
    queryKey: ['changeImpact', runId],
    queryFn: ({ signal }) => api.changeImpact(null, 20, signal, runId),
    enabled: expanded && !!runId,
    staleTime: 5_000,
    refetchInterval: expanded && runId ? 5_000 : false,
  });
  return (
    <div className="mt-3 rounded-card border border-border" data-testid="change-impact-card">
      <button type="button" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-alt">
        <RefreshCw size={13} aria-hidden />
        本次更正影响了哪些检查?
        <span className="ml-auto text-xs text-ink-muted">{expanded ? '收起' : '展开查看'}</span>
      </button>
      {expanded && (
        <div className="border-t border-border px-3 py-2 text-sm">
          {!runId && <p className="text-ink-muted">历史记录缺少操作标识，无法确定本次影响。</p>}
          {runId && impactQuery.isPending && <p className="text-ink-muted">正在按依赖关系计算影响…</p>}
          {impactQuery.isError && (
            <p className="text-danger">影响摘要读取失败:{impactQuery.error instanceof Error ? impactQuery.error.message : '未知错误'}</p>
          )}
          {impactQuery.data && <ChangeImpactBody impact={impactQuery.data} />}
        </div>
      )}
    </div>
  );
}

function recheckStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'open': return '待重查(不等于风险解除)';
    case 'running': return '重查进行中';
    case 'done': return '已重查,生成新结论';
    case 'failed': return '重查失败——保持待重查,不视为风险解除';
    case 'cancelled': return '重查已取消';
    default: return '未记录';
  }
}

function ChangeImpactBody({ impact }: { impact: ChangeImpactDto }): React.ReactElement {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge tone="neutral">变更事实 {impact.summary.changed_facts} 条</Badge>
        <Badge tone={impact.summary.affected_conclusions > 0 ? 'caution' : 'neutral'}>
          受影响结论 {impact.summary.affected_conclusions} 条
        </Badge>
        <Badge tone="neutral">待重查 {impact.summary.pending_rechecks} 条</Badge>
        {impact.summary.failed_rechecks > 0 && (
          <Badge tone="danger">重查失败 {impact.summary.failed_rechecks} 条</Badge>
        )}
      </div>
      {impact.affected_conclusions.length === 0 ? (
        <p className="text-ink-muted">本次更正没有使已有检查结论失效(按依赖关系计算)。</p>
      ) : (
        <ul className="space-y-1.5">
          {impact.affected_conclusions.map((item) => (
            <li key={item.conclusion_id} className="rounded-lg border border-border bg-surface-alt px-3 py-2">
              <p className="text-sm">{item.text}</p>
              <p className="mt-0.5 text-xs text-ink-muted">
                状态:{item.status === 'stale'
                  ? (item.recheck?.status === 'done' && item.successor ? '历史依据已变化，已有重查结果' : '依据变化待重查(不等于风险解除)')
                  : item.status}
                {item.recheck ? ` · ${recheckStatusLabel(item.recheck.status)}` : ''}
                {item.successor ? ' · 已有重查后继结论(见预警中心)' : ''}
              </p>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-ink-muted">{impact.note}</p>
      {impact.truncated && <p className="text-xs text-ink-muted">
        当前仅展示部分记录：事实共 {impact.changed_facts_total} 条，受影响结论共 {impact.affected_conclusions_total} 条。
      </p>}
    </div>
  );
}

/** 进行中/失败任务托盘:可重新打开;失败提供受控重试;进行中提供进度与取消。 */
export function TaskTray({ tasks, onRetryNew }: {
  tasks: SubmissionTask[];
  onRetryNew?: (task: SubmissionTask) => void;
}): React.ReactElement | null {
  const [retryTarget, setRetryTarget] = useState<SubmissionTask | null>(null);
  const [cancelTarget, setCancelTarget] = useState<SubmissionTask | null>(null);
  const active = tasks.filter((t) => ['submitting', 'queued', 'processing', 'unknown'].includes(t.status));
  const failed = tasks.filter((t) => t.status === 'failed' || t.status === 'rejected');
  const cancelled = tasks.filter((t) => t.status === 'committed'
    && (t.cancelState === 'cancelled' || t.result?.run_status === 'cancelled'));
  if (active.length === 0 && failed.length === 0 && cancelled.length === 0) return null;

  const announce = active.length > 0
    ? `${active.length} 个提交正在处理中`
    : failed.length > 0 ? `${failed.length} 个提交需要处理` : `${cancelled.length} 个任务已取消`;

  return (
    <>
      <LiveAnnouncement message={announce} />
      <Card className="mb-4 border-caution/40">
        <div className="border-b border-border px-4 pt-3">
          <h2 className="text-sm font-medium">提交任务</h2>
        </div>
        <ul>
          {cancelled.map((task) => (
            <li key={task.key} className="border-b border-border px-4 py-2.5 text-sm last:border-b-0">
              <div className="flex items-center gap-2"><TaskStatusChip task={task} />{describeTask(task)}</div>
              <p className="mt-1 text-xs text-ink-secondary">服务端已确认取消;已经保存的记录仍然保留。</p>
            </li>
          ))}
          {active.map((task) => {
            const progress = latestProgress(task);
            const cancelable = task.runId !== null
              && ['queued', 'processing'].includes(task.status)
              && task.cancelState === 'none';
            return (
              <li key={task.key} className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5 text-sm last:border-b-0">
                <TaskStatusChip task={task} />
                <span className="min-w-0 flex-1 truncate">
                  {describeTask(task)}
                </span>
                {progress && (
                  <span className="text-xs text-ink-secondary">{progressLabel(progress)}</span>
                )}
                {task.status === 'unknown' && task.networkError && (
                  <span className="w-full text-xs text-caution">{task.networkError}</span>
                )}
                {task.slow && task.status !== 'unknown' && (
                  <span className="w-full text-xs text-ink-muted">
                    处理时间较长,仍在确认处理状态;不会重复提交。
                  </span>
                )}
                {cancelable && (
                  <button type="button"
                    onClick={() => setCancelTarget(task)}
                    className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                    取消任务
                  </button>
                )}
                {task.status === 'unknown' && (
                  <button type="button"
                    onClick={() => submissions.stopWaiting(task.key)}
                    className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-surface-alt">
                    停止等待
                  </button>
                )}
              </li>
            );
          })}
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
      <ConfirmDialog
        open={!!cancelTarget}
        onOpenChange={(open) => { if (!open) setCancelTarget(null); }}
        title="取消这个正在处理的任务?"
        description="取消只停止尚未执行的工作;已经保存的记录不会删除或回滚。取消结果以服务端确认为准。"
        confirmLabel="取消任务"
        onConfirm={() => {
          if (cancelTarget) void submissions.cancelOnServer(cancelTarget.key);
          setCancelTarget(null);
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
