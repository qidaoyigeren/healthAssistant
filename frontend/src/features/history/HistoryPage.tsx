/**
 * 照护时间线:真实事件分页(倒序)、筛选、历史回溯(valid_at/known_at 独立)、
 * 搜索(历史候选,不等于当前事实)。同时展示实际发生时间与系统记录时间。
 */
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSubmissions } from '../../hooks/useSubmissions';
import { History as HistoryIcon, Link2, Search, Undo2 } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import type { EpisodicEventDto } from '../../api/types';
import {
  Badge, Card, EmptyState, ErrorState, LoadingBlock, SectionTitle, SkeletonList,
} from '../../components/ui';
import { MemoryRefDrawer } from '../../components/evidence';
import { TaskTray } from '../shared/submission';

const EVENT_TYPE_LABELS: Record<string, string> = {
  medication_change: '用药变化',
  medication_change_unresolved: '用药变化(未匹配)',
  procedure_exposure: '造影剂暴露',
  allergy_report: '过敏报告(待核实)',
  disease_report: '疾病报告(待核实)',
  renal_function_report: '肾功能报告(待核实)',
  register_profile: '登记患者情况',
  profile_update: '补充患者情况',
};

export function HistoryPage(): React.ReactElement {
  const [eventType, setEventType] = useState('');
  const [keyword, setKeyword] = useState('');
  const [query, setQuery] = useState('');
  const [openRef, setOpenRef] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);

  // 历史回溯
  const [historyMode, setHistoryMode] = useState(false);
  const [validAt, setValidAt] = useState('');
  const [knownAt, setKnownAt] = useState('');

  const typesQuery = useQuery({
    queryKey: qk.eventTypes,
    queryFn: ({ signal }) => api.eventTypes(signal),
  });

  const filters: Record<string, string> = {};
  if (eventType) filters.event_type = eventType;
  if (query) filters.q = query;

  const eventsQuery = useQuery({
    queryKey: qk.historyEvents(filters),
    queryFn: ({ signal }) => api.historyEvents({ ...filters, limit: 20 }, signal),
    enabled: !historyMode,
  });

  const historyStateQuery = useQuery({
    queryKey: qk.memoryState(validAt || undefined, knownAt || undefined),
    queryFn: ({ signal }) => api.memoryState(
      { valid_at: validAt || undefined, known_at: knownAt || undefined }, signal),
    enabled: historyMode,
  });

  const searchQuery = useQuery({
    queryKey: qk.historySearch(query),
    queryFn: ({ signal }) => api.historySearch(query, 10, signal),
    enabled: !!query,
  });

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">照护时间线</h1>
          <p className="mt-1 text-sm text-ink-secondary">
            每条记录都区分「实际发生时间」和「系统记录时间」。
          </p>
        </div>
        <Link to="/assistant"
          className="rounded-lg border border-border px-3.5 py-2 text-sm hover:bg-surface-alt">
          照护助手
        </Link>
      </header>

      {historyMode && (
        <Card className="border-caution/50 bg-caution-soft/30">
          <div className="flex flex-wrap items-center gap-2 p-4">
            <Badge tone="caution" icon={<HistoryIcon size={13} aria-hidden />}>历史查看模式(只读)</Badge>
            <span className="text-sm">
              情况在 <strong>{validAt || '(未指定)'}</strong> 有效;只使用截至 <strong>{knownAt || '(未指定)'}</strong> 已记录的信息。
            </span>
            <button type="button" onClick={() => setHistoryMode(false)}
              className="ml-auto inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-alt">
              <Undo2 size={13} aria-hidden /> 回到当前
            </button>
          </div>
        </Card>
      )}

      <Card>
        <SectionTitle>历史回溯(两个时间独立)</SectionTitle>
        <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-3">
          <label className="block text-sm">
            <span className="mb-1 block font-medium">valid_at:查看什么时间的情况</span>
            <input type="datetime-local" value={validAt} disabled={historyMode}
              onChange={(e) => setValidAt(e.target.value)} className={inputClass} />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block font-medium">known_at:只看截至何时已记录的信息</span>
            <input type="datetime-local" value={knownAt} disabled={historyMode}
              onChange={(e) => setKnownAt(e.target.value)} className={inputClass} />
          </label>
          <div className="flex items-end">
            <button type="button" disabled={!validAt && !knownAt}
              onClick={() => setHistoryMode(true)}
              className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-strong disabled:opacity-50">
              查看历史状态
            </button>
          </div>
        </div>
        <p className="px-4 pb-3 text-xs text-ink-muted">
          历史查询由服务端按双时间模型重新计算;后补录/更正的信息不会泄漏到较早的知悉时点。
          历史模式为只读,防止在旧状态上误操作。
        </p>
      </Card>

      {historyMode && (
        <HistoryStateView stateQuery={historyStateQuery} />
      )}

      {!historyMode && (
        <>
          <Card>
            <SectionTitle>搜索历史记录</SectionTitle>
            <div className="flex flex-wrap gap-2 p-4">
              <input type="search" value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') setQuery(keyword); }}
                placeholder="如:克拉霉素 / 造影剂"
                className={`${inputClass} md:max-w-md`} aria-label="搜索历史记录" />
              <button type="button" onClick={() => setQuery(keyword)}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-sm text-white hover:bg-primary-strong">
                <Search size={14} aria-hidden /> 搜索
              </button>
            </div>
            {searchQuery.data && (
              <div className="border-t border-border p-4 text-sm">
                <p className="mb-2 text-xs text-ink-muted">
                  覆围说明:搜索只覆盖已落库的情景记录(原始报告),不包含患者档案全部
                  事实和外部医学知识;返回的是历史候选,不代表患者当前事实。
                  匹配方式:{searchQuery.data.mode === 'fts5_trigram' ? '全文索引' : searchQuery.data.mode === 'like_fallback' ? '模糊匹配(索引未命中)' : '无搜索词'}
                </p>
                {searchQuery.data.results.length === 0 ? (
                  <EmptyState title="没有匹配的历史记录" />
                ) : (
                  <EventList events={searchQuery.data.results}
                    expanded={expanded} setExpanded={setExpanded} onOpenRef={setOpenRef} />
                )}
              </div>
            )}
          </Card>

          <Card>
            <SectionTitle>时间线</SectionTitle>
            <div className="flex flex-wrap items-center gap-2 px-4 pt-3">
              <label className="text-sm">
                <span className="sr-only">按事件类型筛选</span>
                <select value={eventType} onChange={(e) => setEventType(e.target.value)}
                  className={inputClass} aria-label="按事件类型筛选">
                  <option value="">全部类型</option>
                  {(typesQuery.data ?? []).map((type) => (
                    <option key={type.event_type} value={type.event_type}>
                      {EVENT_TYPE_LABELS[type.event_type] ?? type.event_type}({type.count})
                    </option>
                  ))}
                </select>
              </label>
              <span className="text-xs text-ink-muted">
                {eventsQuery.data ? `共 ${eventsQuery.data.total} 条,最新在前` : ''}
              </span>
            </div>
            <div className="p-4">
              {eventsQuery.isPending && <SkeletonList rows={4} />}
              {eventsQuery.isError && <ErrorState error={eventsQuery.error} />}
              {eventsQuery.data && eventsQuery.data.items.length === 0 && (
                <EmptyState title="还没有照护记录" hint="提交用药或档案记录后,时间线会出现在这里。" />
              )}
              {eventsQuery.data && eventsQuery.data.items.length > 0 && (
                <>
                  <EventList events={eventsQuery.data.items}
                    expanded={expanded} setExpanded={setExpanded} onOpenRef={setOpenRef} />
                  {eventsQuery.data.next_cursor && (
                    <LoadMoreButton cursor={eventsQuery.data.next_cursor} filters={filters} />
                  )}
                </>
              )}
            </div>
          </Card>
        </>
      )}

      <TaskTrayWrapper />
      <MemoryRefDrawer memoryRef={openRef} onClose={() => setOpenRef(null)} />
    </div>
  );
}

function LoadMoreButton({ cursor, filters }: {
  cursor: string; filters: Record<string, string>;
}): React.ReactElement {
  const queryClient = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const loadMore = () => {
    setLoading(true);
    api.historyEvents({ ...filters, limit: 20, cursor })
      .then((page) => {
        queryClient.setQueryData<Awaited<ReturnType<typeof api.historyEvents>>>(
          qk.historyEvents(filters),
          (previous) => previous
            ? { ...previous, items: [...previous.items, ...page.items], next_cursor: page.next_cursor }
            : page,
        );
      })
      .catch((err) => setError(err))
      .finally(() => setLoading(false));
  };
  if (error) return <ErrorState error={error} title="加载更多失败" />;
  return (
    <button type="button" onClick={loadMore} disabled={loading}
      className="mt-3 w-full rounded-lg border border-border px-4 py-2 text-sm hover:bg-surface-alt disabled:opacity-50">
      {loading ? '加载中…' : '加载更早的记录'}
    </button>
  );
}

function EventList({ events, expanded, setExpanded, onOpenRef }: {
  events: EpisodicEventDto[];
  expanded: number | null;
  setExpanded: (id: number | null) => void;
  onOpenRef: (ref: string) => void;
}): React.ReactElement {
  return (
    <ul className="divide-y divide-border">
      {events.map((event) => (
        <li key={event.ref} className="py-3">
          <button type="button" onClick={() => setExpanded(expanded === event.id ? null : event.id)}
            aria-expanded={expanded === event.id}
            className="flex w-full flex-wrap items-center gap-2 text-left">
            <Badge tone="neutral">{EVENT_TYPE_LABELS[event.event_type] ?? event.event_type}</Badge>
            <span className="min-w-0 flex-1 truncate text-sm">
              {summarize(event)}
            </span>
            <span className="font-mono text-xs text-ink-muted">
              发生 {event.occurred_at.slice(0, 16).replace('T', ' ')}
            </span>
            <span className="font-mono text-xs text-ink-muted">
              记录 {event.recorded_at.slice(0, 16).replace('T', ' ')}
            </span>
          </button>
          {expanded === event.id && (
            <div className="mt-2 rounded-lg border border-border bg-surface-alt p-3 text-sm">
              <dl className="space-y-1">
                <Row label="实际发生时间">{event.occurred_at}(ISO:{event.occurred_at})</Row>
                <Row label="系统记录时间">{event.recorded_at}</Row>
                <Row label="来源">{event.source}{event.source_uri ? ` · ${event.source_uri}` : ''}</Row>
                <Row label="会话/轮次">{event.session_id} / {event.turn_id}</Row>
                {event.severity && <Row label="标记严重度">{event.severity}</Row>}
                {event.needs_verification === 1 && (
                  <Row label="待核实">是(报告内容尚未成为确认事实)</Row>
                )}
              </dl>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-code-bg p-2 font-mono text-xs">
                {JSON.stringify(event.payload, null, 2)}
              </pre>
              <button type="button" onClick={() => onOpenRef(event.ref)}
                className="mt-2 inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-1 font-mono text-xs text-primary hover:bg-primary-soft">
                <Link2 size={11} aria-hidden /> 查看记录与审计:{event.ref}
              </button>
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function summarize(event: EpisodicEventDto): string {
  const payload = event.payload as Record<string, unknown>;
  if (typeof payload.reported_text === 'string') return payload.reported_text;
  if (typeof payload.name === 'string') {
    return `${payload.action === 'remove' ? '停用' : payload.action === 'dose_change' ? '变更' : '新增'}:${payload.name}`;
  }
  return event.subject_key ?? event.event_type;
}

function HistoryStateView({ stateQuery }: {
  stateQuery: {
    isPending: boolean; isError: boolean; error: unknown;
    data: Awaited<ReturnType<typeof api.memoryState>> | undefined;
  };
}): React.ReactElement | null {
  if (stateQuery.isPending) return <LoadingBlock />;
  if (stateQuery.isError) return <ErrorState error={stateQuery.error} title="历史状态读取失败" />;
  if (!stateQuery.data) return null;
  const state = stateQuery.data;
  return (
    <Card>
      <SectionTitle>历史时刻的状态(只读)</SectionTitle>
      <div className="space-y-2 p-4 text-sm">
        <p className="text-xs text-ink-muted">
          当前作用域修订号(药单 {state.meta.medications_revision} / 事实 {state.meta.semantic_revision})
          是服务端当前计数器读数,不代表该历史时点的修订号。
        </p>
        <div>
          <h3 className="font-medium">当时的在用药({state.medications.length})</h3>
          {state.medications.length === 0
            ? <p className="text-ink-muted">没有记录。</p>
            : (
              <ul className="mt-1 list-disc pl-5">
                {state.medications.map((medication) => (
                  <li key={medication.ref}>
                    {medication.display_name}
                    {medication.dose ? ` · ${medication.dose}` : ''}
                    {medication.schedule ? ` · ${medication.schedule}` : ''}
                  </li>
                ))}
              </ul>
            )}
        </div>
        <div>
          <h3 className="font-medium">当时已记录的事实({state.facts.length})</h3>
          <ul className="mt-1 list-disc pl-5">
            {state.facts.map((fact) => (
              <li key={fact.ref}>
                {fact.namespace}:{typeof fact.value === 'object' ? JSON.stringify(fact.value) : String(fact.value)}
                (v{fact.version})
              </li>
            ))}
          </ul>
        </div>
        {state.uncertainties.length > 0 && (
          <p className="text-caution">
            另有 {state.uncertainties.length} 条生效时间不明的记录,单独列出:不隐去。
          </p>
        )}
        <div>
          <h3 className="font-medium">当时未决的待核实事项({state.open_conflicts.length})</h3>
          {state.open_conflicts.length === 0
            ? <p className="text-ink-muted">没有记录。</p>
            : (
              <ul className="mt-1 list-disc pl-5">
                {state.open_conflicts.map((conflict) => (
                  <li key={conflict.ref}>{conflict.description}</li>
                ))}
              </ul>
            )}
        </div>
      </div>
    </Card>
  );
}

function Row({ label, children }: {
  label: string; children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function TaskTrayWrapper(): React.ReactElement {
  const tasks = useSubmissions();
  return <TaskTray tasks={tasks} />;
}

const inputClass = 'w-full rounded-lg border border-border bg-surface px-3 py-2 text-base focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30';
