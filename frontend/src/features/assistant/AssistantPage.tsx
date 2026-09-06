/**
 * 照护助手:基于照护记录的事件式查询。所有发送走 POST /v1/events;
 * 展示真实完成响应(text/warnings/conflicts/audit_trail/safety_status)。
 * 刷新后从服务端恢复历史会话;不补造未保存的助手正文;不伪造流式进度。
 */
import React, { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { MessageSquarePlus, ScrollText } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import { submissions } from '../../api/submissions';
import { useSessionId, useSubmissions } from '../../hooks/useSubmissions';
import type { SubmissionTask } from '../../api/submissions';
import type { SessionEventDto } from '../../api/types';
import {
  Badge, Card, DetailDrawer, EmptyState, ErrorState, LoadingBlock, SkeletonList,
} from '../../components/ui';
import { ConflictCard, WarningCard } from '../../components/evidence';
import { SafeMarkdown } from '../../components/safeMarkdown';
import { TaskStatusChip, TaskTray } from '../shared/submission';

const QUICK_PROMPTS = [
  { label: '现在吃什么药?', eventType: 'query_current_medications' as const },
  { label: '当前用药清单', eventType: 'query_current_medications' as const },
];

export function AssistantPage(): React.ReactElement {
  const [sessionId, newSession] = useSessionId();
  const tasks = useSubmissions();
  const [input, setInput] = useState('');
  const [traceTurn, setTraceTurn] = useState<string | null>(null);
  const [localMessages, setLocalMessages] = useState<number>(0);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // 刷新后从服务端恢复本会话的历史提交(真实持久化结果;未保存正文就是空)
  const historyQuery = useQuery({
    queryKey: qk.sessionEvents(sessionId),
    queryFn: ({ signal }) => api.sessionEvents(sessionId, { limit: 50 }, signal),
    placeholderData: (previous) => previous,
  });

  const sessionTasks = tasks.filter((t) => t.sessionId === sessionId);
  const activeCount = sessionTasks.filter((t) =>
    ['submitting', 'queued', 'processing', 'unknown'].includes(t.status)).length;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [historyQuery.data, localMessages]);

  const send = (text: string, eventType: 'user_message' | 'query_current_medications') => {
    const trimmed = text.trim();
    if (!trimmed || activeCount > 0) return;
    submissions.submit({
      event_type: eventType,
      text: trimmed,
      payload: {},
      source: 'caregiver',
      occurred_at: null,
      session_id: sessionId,
    });
    setInput('');
    setLocalMessages((count) => count + 1);
  };

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">照护助手</h1>
          <p className="mt-1 text-sm text-ink-secondary">
            基于已记录的照护事实回答;会话:{sessionId.slice(0, 12)}…
          </p>
        </div>
        <button type="button"
          onClick={() => { newSession(); setLocalMessages(0); }}
          className="inline-flex items-center gap-1.5 rounded-lg border border-primary px-3.5 py-2 text-sm font-medium text-primary-strong hover:bg-primary-soft">
          <MessageSquarePlus size={14} aria-hidden /> 开启新会话
        </button>
      </header>

      <Card className="flex min-h-[50vh] flex-col">
        <div className="flex-1 space-y-3 p-4">
          {historyQuery.isPending && <SkeletonList rows={2} />}
          {historyQuery.isError && <ErrorState error={historyQuery.error} title="历史会话读取失败" />}
          {historyQuery.data && historyQuery.data.items.length === 0 && sessionTasks.length === 0 && (
            <EmptyState title="这个会话还没有提问"
              hint="可以先在总览/用药页完成记录,再回来查询;或直接在下方输入。" />
          )}

          {/* 服务端恢复的历史(旧 → 新) */}
          {historyQuery.data && [...historyQuery.data.items].reverse().map((event) => (
            <RestoredExchange key={event.id} event={event} onOpenTrace={setTraceTurn} />
          ))}

          {/* 本标签页的任务(处理中 / 已完成) */}
          {sessionTasks.map((task) => (
            <LiveExchange key={task.key} task={task} onOpenTrace={setTraceTurn} />
          ))}
          <div ref={bottomRef} />
        </div>

        <div className="border-t border-border p-3">
          <div className="mb-2 flex flex-wrap gap-2">
            {QUICK_PROMPTS.map((prompt) => (
              <button key={prompt.label} type="button"
                disabled={activeCount > 0}
                onClick={() => send(prompt.label, prompt.eventType)}
                className="rounded-full border border-border px-3 py-1 text-xs text-ink-secondary hover:bg-surface-alt disabled:opacity-50">
                {prompt.label}
              </button>
            ))}
          </div>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); send(input, 'user_message'); }}>
            <input type="text" value={input} onChange={(e) => setInput(e.target.value)}
              placeholder={activeCount > 0 ? '有正在处理的提问,请稍候…' : '输入关于照护记录的问题'}
              aria-label="输入问题"
              className="flex-1 rounded-lg border border-border px-3 py-2 text-base focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30" />
            <button type="submit" disabled={!input.trim() || activeCount > 0}
              className="rounded-lg bg-primary px-4 py-2 font-medium text-white hover:bg-primary-strong disabled:opacity-50">
              发送
            </button>
          </form>
          <p className="mt-1.5 text-xs text-ink-muted">
            同一会话同时只处理一个提问;助手不能诊断或开药,安全提示会原样展示。
          </p>
        </div>
      </Card>

      <TaskTrayWrapper />

      <TraceDrawer sessionId={sessionId} turnId={traceTurn} onClose={() => setTraceTurn(null)} />
    </div>
  );
}

function RestoredExchange({ event, onOpenTrace }: {
  event: SessionEventDto; onOpenTrace: (turnId: string) => void;
}): React.ReactElement | null {
  // 没有保存的助手正文:如实显示「未保存正文」,不补造。
  const result = event.response;
  const question = result ? event.request?.text : event.user_text;
  if (!question && !result) {
    return (
      <div className="rounded-lg bg-surface-alt px-3 py-2 text-xs text-ink-muted">
        <Badge tone="neutral">{event.process_status}</Badge>
        <span className="ml-2">这是一条历史提交,助手正文未保存,无法恢复显示。</span>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <p className="ml-auto w-fit max-w-[85%] rounded-2xl bg-primary-soft px-3.5 py-2 text-sm">
        {question ?? '(提问内容未保存)'}
      </p>
      <div className="w-fit max-w-[95%] rounded-2xl border border-border bg-surface px-3.5 py-2.5">
        {result ? (
          <>
            <SafeMarkdown text={result.text} />
            {result.warnings.length > 0 && (
              <div className="mt-2 space-y-2">
                {result.warnings.map((warning, index) => <WarningCard key={index} warning={warning} />)}
              </div>
            )}
            {result.conflicts.length > 0 && (
              <div className="mt-2 space-y-2">
                {result.conflicts.map((conflict) => <ConflictCard key={conflict.ref} conflict={conflict} />)}
              </div>
            )}
            <TraceButton turnId={event.turn_id} onOpen={onOpenTrace} />
          </>
        ) : (
          <p className="text-sm text-ink-muted">
            这条提交的状态:{event.process_status}
            {event.outbox_status === 'open' || event.outbox_status === 'running'
              ? '(服务器仍在处理/排队;刷新后可再查看)' : ''}
            {event.outbox_error ? ` · 失败信息:${event.outbox_error}` : ''}
          </p>
        )}
      </div>
    </div>
  );
}

function LiveExchange({ task, onOpenTrace }: {
  task: SubmissionTask; onOpenTrace: (turnId: string) => void;
}): React.ReactElement {
  return (
    <div className="space-y-2">
      <p className="ml-auto w-fit max-w-[85%] rounded-2xl bg-primary-soft px-3.5 py-2 text-sm">
        {task.event.text}
      </p>
      <div className="w-fit max-w-[95%] rounded-2xl border border-border bg-surface px-3.5 py-2.5">
        {task.status === 'committed' && task.result ? (
          <>
            <SafeMarkdown text={task.result.text} />
            {task.result.safety_status !== 'enforced' && (
              <p className="mt-1 text-xs text-caution">安全状态:{task.result.safety_status}</p>
            )}
            {task.result.warnings.length > 0 && (
              <div className="mt-2 space-y-2">
                {task.result.warnings.map((warning, index) => <WarningCard key={index} warning={warning} />)}
              </div>
            )}
            {task.result.conflicts.length > 0 && (
              <div className="mt-2 space-y-2">
                {task.result.conflicts.map((conflict) => <ConflictCard key={conflict.ref} conflict={conflict} />)}
              </div>
            )}
            {task.result.audit_trail.response_source && (
              <p className="mt-1 text-xs text-ink-muted">
                回答来源:{task.result.audit_trail.response_source}
              </p>
            )}
            <TraceButton turnId={task.result.audit_trail.turn_id ?? ''} onOpen={onOpenTrace} />
          </>
        ) : (
          <p className="flex items-center gap-2 text-sm text-ink-secondary">
            <TaskStatusChip task={task} />
            {['submitting', 'queued', 'processing'].includes(task.status) && '已受理,处理中…'}
            {task.status === 'unknown' && (task.networkError ?? '状态确认中…')}
            {(task.status === 'failed' || task.status === 'rejected') && '处理失败,见下方任务列表。'}
          </p>
        )}
      </div>
    </div>
  );
}

function TraceButton({ turnId, onOpen }: {
  turnId: string; onOpen: (turnId: string) => void;
}): React.ReactElement | null {
  if (!turnId) return null;
  return (
    <button type="button" onClick={() => onOpen(turnId)}
      className="mt-1.5 inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs text-ink-secondary hover:bg-surface-alt">
      <ScrollText size={12} aria-hidden /> 查看执行审计
    </button>
  );
}

function TraceDrawer({ sessionId, turnId, onClose }: {
  sessionId: string; turnId: string | null; onClose: () => void;
}): React.ReactElement {
  const query = useQuery({
    queryKey: qk.turnTrace(sessionId, turnId ?? ''),
    queryFn: ({ signal }) => api.turnTrace(sessionId, turnId!, signal),
    enabled: !!turnId,
  });
  return (
    <DetailDrawer open={!!turnId} onOpenChange={(open) => { if (!open) onClose(); }}
      title="执行审计(已落库记录)">
      {query.isPending && <LoadingBlock />}
      {query.isError && <ErrorState error={query.error} />}
      {query.data && query.data.traces.length === 0 && (
        <EmptyState title="没有已落库的执行记录"
          hint="不能声称「未执行工具」——只是没有可审计的持久化记录。" />
      )}
      {query.data && query.data.traces.length > 0 && (
        <ol className="space-y-2 text-sm">
          {query.data.traces.map((trace) => (
            <li key={trace.id} className="rounded-lg border border-border bg-surface-alt p-3">
              <p className="flex flex-wrap items-center gap-2">
                <Badge tone="primary">{trace.phase}</Badge>
                {trace.cycle != null && <span className="text-xs text-ink-muted">周期 {trace.cycle}</span>}
                <span className="font-mono text-xs text-ink-muted">{trace.created_at.slice(0, 19).replace('T', ' ')}</span>
              </p>
              <pre className="mt-1.5 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded bg-code-bg p-2 font-mono text-xs">
                {JSON.stringify(trace.payload, null, 2)}
              </pre>
            </li>
          ))}
        </ol>
      )}
    </DetailDrawer>
  );
}

function TaskTrayWrapper(): React.ReactElement {
  const tasks = useSubmissions();
  return <TaskTray tasks={tasks} />;
}
