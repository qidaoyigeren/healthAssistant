/**
 * 照护总览:当前档案摘要、有效预警/待复查/未决冲突、最近变化。
 * 首次打开空数据库:简短说明 + 登记入口,不自动创建任何数据。
 * 读取失败保留旧缓存并标明,不把失败当成 0。
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, CircleDashed, FileClock } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import { useSubmissions } from '../../hooks/useSubmissions';
import { deriveProfile } from '../profile/ProfilePage';
import {
  Badge, Card, EmptyState, ErrorState, LoadingBlock, SectionTitle, SkeletonList,
} from '../../components/ui';
import { TaskTray } from '../shared/submission';

export function OverviewPage(): React.ReactElement {
  const overviewQuery = useQuery({
    queryKey: qk.overview,
    queryFn: ({ signal }) => api.overview(signal),
  });
  const stateQuery = useQuery({
    queryKey: qk.memoryState(),
    queryFn: ({ signal }) => api.memoryState({}, signal),
  });
  const alertsQuery = useQuery({
    queryKey: qk.alertRecords('current'),
    queryFn: ({ signal }) => api.alertRecords({ status: 'current', limit: 20 }, signal),
  });
  const rechecksQuery = useQuery({
    queryKey: qk.recheckTasks,
    queryFn: ({ signal }) => api.recheckTasks(signal),
  });

  const isEmpty = overviewQuery.data && overviewQuery.data.counts.medications_records === 0
    && overviewQuery.data.counts.facts_active === 0;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">照护总览</h1>
          <p className="mt-1 text-sm text-ink-secondary">当前记录了什么、哪些需要核实、依据来自哪里。</p>
        </div>
        <div className="flex gap-2">
          <Link to="/medications"
            className="rounded-lg bg-primary px-3.5 py-2 text-sm font-medium text-white hover:bg-primary-strong">
            记录用药变化
          </Link>
          <Link to="/profile"
            className="rounded-lg border border-primary px-3.5 py-2 text-sm font-medium text-primary-strong hover:bg-primary-soft">
            补充患者情况
          </Link>
        </div>
      </header>

      {overviewQuery.isPending && <SkeletonList rows={4} />}
      {overviewQuery.isError && <ErrorState error={overviewQuery.error} title="总览统计读取失败" />}

      {isEmpty && (
        <Card className="p-6">
          <h2 className="text-lg font-medium">欢迎使用用药协管员</h2>
          <p className="mt-2 max-w-xl text-sm leading-relaxed text-ink-secondary">
            这里还没有任何记录。您可以先「登记患者情况」(年龄、过敏、慢病等),
            再逐条记录实际用药变化。所有记录都会保存来源和版本,可以随时核实与回溯。
          </p>
          <div className="mt-4 flex gap-2">
            <Link to="/profile" className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-strong">
              登记患者情况
            </Link>
            <Link to="/medications" className="rounded-lg border border-border px-4 py-2 text-sm hover:bg-surface-alt">
              记录用药变化
            </Link>
          </div>
        </Card>
      )}

      {overviewQuery.data && !isEmpty && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <CountCard label="当前在用药" value={overviewQuery.data.counts.medications_active} to="/medications" />
            <CountCard label="当前有效预警" value={overviewQuery.data.counts.conclusions_current} to="/alerts" />
            <CountCard label="未决待核实" value={overviewQuery.data.counts.conflicts_open} to="/conflicts" caution={overviewQuery.data.counts.conflicts_open > 0} />
            <CountCard label="待复查结论" value={overviewQuery.data.counts.rechecks_pending} to="/alerts" caution={overviewQuery.data.counts.rechecks_pending > 0} />
          </div>

          <Card>
            <SectionTitle>患者档案摘要</SectionTitle>
            {stateQuery.isPending && <LoadingBlock />}
            {stateQuery.isError && (
              <div className="p-4">
                <ErrorState error={stateQuery.error} title="档案读取失败" />
              </div>
            )}
            {stateQuery.data && <ProfileSummary state={stateQuery.data} />}
          </Card>

          {rechecksQuery.data && rechecksQuery.data.pending_count > 0 && (
            <Card className="border-caution/40">
              <SectionTitle>待复查</SectionTitle>
              <ul className="px-4 pb-3 pt-2 text-sm">
                {rechecksQuery.data.pending.slice(0, 5).map((task) => (
                  <li key={task.id} className="flex flex-wrap items-center gap-2 py-1.5">
                    <Badge tone="caution" icon={<CircleDashed size={13} aria-hidden />}>待复查</Badge>
                    <span className="min-w-0 flex-1 truncate">
                      {task.target_conclusion?.text ?? '结论内容读取失败,请到风险页查看'}
                    </span>
                    <Link to="/alerts" className="text-xs text-primary underline">继续查看</Link>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card>
            <SectionTitle actions={<Link to="/alerts" className="text-xs text-primary underline">全部风险</Link>}>
              当前有效预警
            </SectionTitle>
            <div className="p-4">
              {alertsQuery.isPending && <SkeletonList rows={2} />}
              {alertsQuery.isError && <ErrorState error={alertsQuery.error} title="预警读取失败(不当作 0 处理)" />}
              {alertsQuery.data && alertsQuery.data.items.length === 0 && (
                <EmptyState title="没有当前有效预警"
                  hint="这不代表「全部用药安全」;仅表示当前没有处于有效状态的预警结论。" />
              )}
              {alertsQuery.data && alertsQuery.data.items.length > 0 && (
                <ul className="space-y-2">
                  {alertsQuery.data.items.slice(0, 5).map((alert) => (
                    <li key={alert.ref} className="rounded-lg border border-border bg-surface-alt px-3 py-2">
                      <p className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                        <Badge tone="primary">当前有效</Badge>
                        <span>{alert.created_at.slice(0, 16).replace('T', ' ')}(UTC)</span>
                        <Link to="/alerts" className="text-primary underline">详情与来源</Link>
                      </p>
                      <p className="mt-1 text-sm">{alert.text}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Card>

          <Card>
            <SectionTitle actions={
              <Link to="/history" className="inline-flex items-center gap-1 text-xs text-primary underline">
                <FileClock size={12} aria-hidden /> 照护时间线
              </Link>
            }>
              最近变化
            </SectionTitle>
            <RecentChanges />
          </Card>
        </>
      )}

      <TaskTrayWrapper />
    </div>
  );
}

/**
 * 总览的预警只展示结论原文与状态入口;完整风险卡(严重度/置信度/证据)在
 * 预警中心处理 —— 不把结论文字映射成虚构的完整风险卡。
 */
function RecentChanges(): React.ReactElement {
  const stateQuery = useQuery({
    queryKey: qk.memoryState(),
    queryFn: ({ signal }) => api.memoryState({}, signal),
    select: (state) => ({
      revision: state.meta.medications_revision,
      validAt: state.valid_at,
    }),
  });
  const historyQuery = useQuery({
    queryKey: qk.historyEvents({}),
    queryFn: ({ signal }) => api.historyEvents({ limit: 5 }, signal),
  });
  if (historyQuery.isPending) return <SkeletonList rows={2} />;
  if (historyQuery.isError) return <div className="p-4"><ErrorState error={historyQuery.error} /></div>;
  if (historyQuery.data.items.length === 0) {
    return <div className="p-4"><EmptyState title="还没有照护记录" /></div>;
  }
  return (
    <ul className="divide-y divide-border px-4 pb-3 text-sm">
      {historyQuery.data.items.map((event) => (
        <li key={event.ref} className="flex flex-wrap items-center gap-2 py-2">
          <Badge tone="neutral">{event.event_type}</Badge>
          <span className="min-w-0 flex-1 truncate">
            {typeof event.payload === 'object' && event.payload !== null
              ? String((event.payload as Record<string, unknown>).reported_text ?? event.subject_key ?? '')
              : event.subject_key ?? ''}
          </span>
          <span className="font-mono text-xs text-ink-muted">{event.occurred_at.slice(0, 16).replace('T', ' ')}</span>
        </li>
      ))}
      {stateQuery.data && (
        <li className="py-2 text-xs text-ink-muted">药单当前修订号:{stateQuery.data.revision}</li>
      )}
    </ul>
  );
}

function ProfileSummary({ state }: { state: Awaited<ReturnType<typeof api.memoryState>> }): React.ReactElement {
  const profile = deriveProfile(state.facts);
  const entries: [string, string][] = [
    ['年龄', profile.age != null ? `${profile.age} 岁` : '未记录'],
    ['性别', profile.sex ?? '未记录'],
    ['体重', profile.weight_kg != null ? `${profile.weight_kg} kg` : '未记录'],
    ['肾功能', profile.renal_function ?? '未记录'],
    ['肝功能', profile.hepatic_function ?? '未记录'],
    ['过敏', profile.allergies.length > 0 ? profile.allergies.join('、') : '未记录'],
    ['慢性病', profile.chronic_diseases.length > 0 ? profile.chronic_diseases.join('、') : '未记录'],
  ];
  return (
    <div className="px-4 pb-4">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm md:grid-cols-4">
        {entries.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-ink-muted">{label}</dt>
            <dd className={value === '未记录' ? 'text-ink-muted' : ''}>{value}</dd>
          </div>
        ))}
      </dl>
      {state.uncertainties.length > 0 && (
        <p className="mt-2 text-xs text-caution">
          有 {state.uncertainties.length} 条记录生效时间不明,详情见患者档案页。
        </p>
      )}
      <Link to="/profile" className="mt-3 inline-flex items-center gap-1 text-xs text-primary underline">
        查看完整档案与核实操作 <ArrowRight size={11} aria-hidden />
      </Link>
    </div>
  );
}

function CountCard({ label, value, to, caution }: {
  label: string; value: number; to: string; caution?: boolean;
}): React.ReactElement {
  return (
    <Link to={to} className="rounded-card border border-border bg-surface p-4 transition hover:border-primary">
      <p className="text-xs text-ink-muted">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${caution ? 'text-caution' : ''}`}>{value}</p>
    </Link>
  );
}

function TaskTrayWrapper(): React.ReactElement {
  const tasks = useSubmissions();
  return <TaskTray tasks={tasks} />;
}
