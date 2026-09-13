/**
 * 长期用药安全事项 —— 产品主线页(路由 `/`,别名 `/safety`)。
 *
 * 这一页只回答一个问题:「这件事查清到什么程度、现在需要谁做什么」。
 * 所以六块内容按**接力棒在谁手上**排:需要关注(系统) → 等您补充(您) →
 * 等专业复核(专业人员) → 已处理(无人) → 需重新核对(系统);
 * 每一块的开头都写明这一棒现在归谁。
 *
 * 两条不能违反的诚实规则:
 *  1. 必要检查队列的真实状态常驻可见。检查没跑 ≠ 检查通过,「没有提示」
 *     不能被读成「没问题」——队列读不到时也要说出来。
 *  2. 不出现没有依据的"没问题":没有结论就写没有结论。
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { CheckCircle2, CircleDashed, Loader2, ShieldCheck } from 'lucide-react';
import { api } from '../../api/client';
import { qk } from '../../api/queryKeys';
import type { SafetyCaseDto, SafetyMainlineDto } from '../../api/types';
import {
  Badge, Card, ErrorState, SkeletonList, TimeText,
} from '../../components/ui';
import { CaseCard, AnswerPanel, SeenButton } from './CaseCard';
import {
  MONITORING_NOTICE, NO_CLINICIAN_NOTICE, basisText, dispositionLabel,
  medicationChangeLabel, partyLabel, recheckReason,
} from './labels';

export function SafetyPage(): React.ReactElement {
  const mainline = useQuery({
    queryKey: qk.safetyMainline,
    queryFn: ({ signal }) => api.safetyMainline(signal),
    refetchInterval: 15_000,
  });
  /**
   * 「持续跟进」不在主线的六个分组里（主线只分"需要关注/等您补充/等专业复核/
   * 待重新核对/已有依据的处置"）。它是一条**已有安排、风险仍在**的状态，
   * 不能因为主线没这一格就从界面上消失，所以这里从全部事项列表里读回来，
   * 并明确标注它的来源。
   */
  const allCases = useQuery({
    queryKey: qk.safetyCases,
    queryFn: ({ signal }) => api.safetyCases(signal),
    refetchInterval: 15_000,
  });

  const data = mainline.data;
  const shownIds = new Set((data ? [
    ...data.attention, ...data.awaiting_user, ...data.awaiting_professional,
    ...data.needs_recheck, ...data.recently_settled,
  ] : []).map((caseView) => caseView.case_id));
  const monitoring = (allCases.data?.items ?? []).filter(
    (caseView) => caseView.status === 'monitoring' && !shownIds.has(caseView.case_id));
  const isEmpty = !!data
    && data.current_medications.length === 0
    && data.counts.attention === 0
    && data.counts.awaiting_user === 0
    && data.counts.awaiting_professional === 0
    && data.counts.needs_recheck === 0
    && data.counts.settled === 0
    && monitoring.length === 0;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 font-serif text-2xl font-semibold">
            <ShieldCheck size={22} className="text-primary" aria-hidden />
            长期用药安全事项
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-ink-secondary">
            每一条用药安全事项查清到什么程度、现在需要谁做什么，都记在这里。
            判断与依据分开写：程序查到的、系统调查过的、需要您或专业人员确认的，不混在一起。
          </p>
        </div>
        <div className="flex gap-2">
          <Link to="/medications"
            className="rounded-lg bg-primary px-3.5 py-2 text-sm font-medium text-white hover:bg-primary-strong">
            记录用药变化
          </Link>
          <Link to="/tasks"
            className="rounded-lg border border-border px-3.5 py-2 text-sm hover:bg-surface-alt">
            照护待办
          </Link>
        </div>
      </header>

      {mainline.isPending && <SkeletonList rows={4} />}
      {mainline.isError && (
        <ErrorState error={mainline.error} title="安全事项主线读取失败（不当作「没有问题」处理）"
          onRetry={() => void mainline.refetch()} />
      )}

      {data && (
        <>
          <CheckQueueStrip checks={data.necessary_checks} />
          <HandoffBar counts={data.counts} monitoringCount={monitoring.length} />
        </>
      )}

      {isEmpty && data && (
        <Card className="p-6">
          <h2 className="text-lg font-medium">还没有可跟进的用药安全事项</h2>
          <p className="mt-2 max-w-xl text-sm leading-relaxed text-ink-secondary">
            这里还没有用药记录，也没有由记录变化产生的必要检查结果。
            先登记患者情况、再逐条记录实际用药，药单一变就会按当前记录重新做一次必要检查，
            查到的结论会成为可以持续跟进的事项。
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

      {data && !isEmpty && (
        <>
          {/* 1 当前用药与最近变化 */}
          <Card>
            <BlockHeader id="block-medications" title="当前用药与最近变化"
              party="照护记录" hint={`在用 ${data.current_medications.length} 种`} />
            <div className="grid gap-5 px-4 pb-4 md:grid-cols-2">
              <div>
                <h3 className="text-sm font-medium">当前在用</h3>
                {data.current_medications.length === 0 ? (
                  <p className="mt-1 text-sm text-ink-muted">当前没有在用药记录。</p>
                ) : (
                  <ul className="mt-1.5 space-y-1.5 text-sm">
                    {data.current_medications.map((medication) => (
                      <li key={medication.ref}>
                        <span className="font-medium">{medication.display_name}</span>
                        <span className="ml-2 text-ink-secondary">
                          {[medication.dose, medication.schedule, medication.route]
                            .filter(Boolean).join(' · ') || '剂量与用法未记录'}
                        </span>
                        <span className="ml-2 text-xs text-ink-muted">
                          开始 <TimeText iso={medication.start_at} />
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <h3 className="text-sm font-medium">最近用药变化</h3>
                {data.recent_medication_changes.length === 0 ? (
                  <p className="mt-1 text-sm text-ink-muted">还没有用药变化记录。</p>
                ) : (
                  <ul className="mt-1.5 space-y-1.5 text-sm">
                    {data.recent_medication_changes.slice(0, 8).map((event) => (
                      <li key={event.ref} className="flex flex-wrap items-baseline gap-2">
                        <Badge tone="neutral">{medicationChangeLabel(event.event_type)}</Badge>
                        <span>{String(event.payload.name ?? event.payload.reported_text ?? event.subject_key ?? '未记录药名')}</span>
                        {event.payload.dose ? (
                          <span className="text-ink-secondary">{String(event.payload.dose)}</span>
                        ) : null}
                        <TimeText iso={event.occurred_at} />
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </Card>

          <CaseBlock id="block-attention" title="需要关注的安全事项" party={partyLabel('system')}
            count={data.counts.attention} description="程序或系统正在继续，还没有结论。"
            cases={data.attention} emptyText="当前没有正在跟进的安全事项。" />

          {/* 3 等待您提供的信息 */}
          <CaseBlock id="block-awaiting-user" title="等待您提供的信息" party={partyLabel('caregiver')}
            count={data.counts.awaiting_user}
            description="这些事项缺一条只有您知道的记录，补上才能继续核查。"
            cases={data.awaiting_user} emptyText="当前没有等待您补充的事项。">
            {(caseView) => <AnswerPanel view={caseView} />}
          </CaseBlock>

          {/* 4 等待专业复核的事项 */}
          <CaseBlock id="block-awaiting-professional" title="等待专业复核的事项" party={partyLabel('professional')}
            count={data.counts.awaiting_professional}
            description="这些事项需要医生或药师的判断，系统给不出结论。"
            cases={data.awaiting_professional} emptyText="当前没有等待专业复核的事项。">
            {() => (
              <p className="rounded-lg bg-caution-soft px-3 py-2 text-sm text-caution">
                {NO_CLINICIAN_NOTICE}
              </p>
            )}
          </CaseBlock>

          {/* 5 持续跟进中的事项(风险仍在,已有安排) */}
          <CaseBlock id="block-monitoring" title="持续跟进中的事项（风险仍在）"
            party="由您按安排继续跟进"
            count={monitoring.length}
            description={`${MONITORING_NOTICE}这一块的数量来自全部事项列表（主线分组不含这个状态）。`}
            cases={monitoring} emptyText={allCases.isError || allCases.isPending
              // 读不到 ≠ 没有:读取失败时不能显示成"一件都没有"。
              ? '这一块现在读不到（不是「没有」）：事项列表读取失败或仍在加载，请刷新页面重试。'
              : '当前没有登记为持续跟进的事项。'} />

          {/* 6 最近已处理事项及依据 */}
          <Card>
            <BlockHeader id="block-settled" title="最近已处理事项及依据"
              party={partyLabel('none')} hint={`共 ${data.counts.settled} 件`} />
            <div className="px-4 pb-4">
              {data.recently_settled.length === 0 ? (
                <p className="text-sm text-ink-muted">还没有已处理的处置。</p>
              ) : (
                <ul className="space-y-3">
                  {data.recently_settled.map((caseView) => (
                    <li key={caseView.case_id} className="rounded-lg border border-border bg-surface-alt px-3 py-2.5 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone="primary">{caseView.status_label}</Badge>
                        <Link to={`/safety/${encodeURIComponent(caseView.case_id)}`}
                          className="font-medium text-primary underline">
                          查看这件事项
                        </Link>
                        <span className="ml-auto text-xs text-ink-muted">
                          处置动作：{dispositionLabel(caseView.disposition)}
                        </span>
                        <span className="text-xs text-ink-muted">
                          <TimeText iso={caseView.updated_at} prefix="处置于 " />
                        </span>
                      </div>
                      <p className="mt-1 text-ink-secondary">{basisText(caseView.resolution_basis, caseView)}</p>
                      <p className="mt-1 text-xs text-ink-muted">
                        这类处置不是"以后都不会有事"：记录再变化时，这件事会自动重新打开。
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Card>

          {/* 6 新信息导致的重新复核 */}
          <Card className="border-caution/40">
            <BlockHeader id="block-recheck" title="新信息导致的重新复核" party={partyLabel('system')}
              hint={`${data.counts.needs_recheck} 件`}
              description="这些事项此前有依据，但记录变了；重新核对完成前，不能当成已排除。" />
            <div className="px-4 pb-4">
              {data.needs_recheck.length === 0 ? (
                <p className="text-sm text-ink-muted">当前没有因新信息被重新打开的事项。</p>
              ) : (
                <ul className="space-y-3">
                  {data.needs_recheck.map((caseView) => (
                    <li key={caseView.case_id} className="rounded-lg border border-caution/30 bg-caution-soft/40 px-3 py-2.5 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone="caution" icon={<CircleDashed size={13} aria-hidden />}>
                          {caseView.status_label}
                        </Badge>
                        <span className="font-medium">为什么重新打开：</span>
                        <span className="min-w-0 flex-1">{recheckReason(caseView)}</span>
                        <Link to={`/safety/${encodeURIComponent(caseView.case_id)}`}
                          className="text-xs text-primary underline">
                          详情
                        </Link>
                      </div>
                      <p className="mt-1 text-xs text-ink-muted">
                        现在需要{partyLabel(caseView.responsible_party)}：{caseView.next_action_summary ?? '服务端未记录下一步。'}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Card>
        </>
      )}

    </div>
  );
}

// ---- 必要检查队列 ------------------------------------------------------------

/**
 * 「检查跑没跑」必须自己说话。队列读不到、有排队或失败时都要显出来,
 * 并把服务端的 note 原文放在这里——它是"没有提示 ≠ 检查通过"的依据。
 */
function CheckQueueStrip({ checks }: {
  checks: SafetyMainlineDto['necessary_checks'];
}): React.ReactElement {
  if (!checks.available) {
    return (
      <div role="status" className="rounded-card border border-caution/40 bg-caution-soft px-4 py-3 text-sm">
        <p className="font-medium text-caution">必要检查队列状态读不到</p>
        <p className="mt-1 text-ink-secondary">
          现在无法判断检查有没有在推进。没有看到提示<strong>不代表</strong>检查已经通过或没有风险。
        </p>
      </div>
    );
  }
  const open = checks.open ?? 0;
  const running = checks.running ?? 0;
  const failed = checks.failed ?? 0;
  const blocked = open + running + failed > 0;
  return (
    <div className={`rounded-card border px-4 py-3 text-sm ${
      blocked ? 'border-caution/40 bg-caution-soft' : 'border-border bg-surface'
    }`}>
      <div className="flex flex-wrap items-center gap-2">
        {blocked
          ? <Loader2 size={14} className="text-caution animate-spin" aria-hidden />
          : <CheckCircle2 size={14} className="text-primary" aria-hidden />}
        <span className="font-medium">必要检查队列</span>
        <span className="text-ink-secondary">
          累计 {checks.total ?? 0} 项 · 待运行 {open} · 进行中 {running}
          {failed > 0 ? ` · 失败 ${failed}` : ''}
        </span>
        {checks.last_at && (
          <span className="text-xs text-ink-muted">
            最近更新 <TimeText iso={checks.last_at} />
          </span>
        )}
      </div>
      {checks.note && <p className="mt-1 text-xs text-ink-secondary">{checks.note}</p>}
    </div>
  );
}

// ---- 接力棒概览 --------------------------------------------------------------

/** 六块内容的快速入口:让"现在谁在挡路"一眼可见。 */
function HandoffBar({ counts, monitoringCount }: {
  counts: SafetyMainlineDto['counts']; monitoringCount: number;
}): React.ReactElement {
  const items: { id: string; label: string; partyText: string; count: number }[] = [
    { id: 'block-attention', label: '需要关注', partyText: partyLabel('system'), count: counts.attention },
    { id: 'block-awaiting-user', label: '等您补充', partyText: partyLabel('caregiver'), count: counts.awaiting_user },
    { id: 'block-awaiting-professional', label: '等专业复核', partyText: partyLabel('professional'), count: counts.awaiting_professional },
    { id: 'block-monitoring', label: '持续跟进中', partyText: '由您按安排继续跟进', count: monitoringCount },
    { id: 'block-recheck', label: '待重新核对', partyText: partyLabel('system'), count: counts.needs_recheck },
    { id: 'block-settled', label: '已有依据的处置', partyText: partyLabel('none'), count: counts.settled },
  ];
  return (
    <section aria-label="现在需要谁做什么" className="rounded-card border border-border bg-surface px-4 py-3">
      <p className="text-xs text-ink-muted">现在需要谁做什么</p>
      <ul className="mt-2 flex flex-wrap gap-2">
        {items.map((item) => (
          <li key={item.id}>
            <a href={`#${item.id}`}
              className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm ${
                item.count > 0 && item.id !== 'block-settled'
                  ? 'border-caution/40 bg-caution-soft text-ink hover:border-caution'
                  : 'border-border bg-surface-alt text-ink-secondary hover:border-border-strong'
              }`}>
              {item.label}
              <span className="font-semibold">{item.count}</span>
              <span className="text-xs text-ink-muted">{item.partyText}</span>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---- 块 ---------------------------------------------------------------------

function BlockHeader({ id, title, party, hint, description }: {
  id: string; title: string; party: string; hint?: string; description?: string;
}): React.ReactElement {
  return (
    <div className="px-4 pt-4">
      <h2 id={id} className="scroll-mt-4 font-serif text-lg font-semibold">{title}</h2>
      <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        <Badge tone="neutral">{party}</Badge>
        {hint && <span>{hint}</span>}
      </p>
      {description && <p className="mt-1 text-sm text-ink-secondary">{description}</p>}
    </div>
  );
}

function CaseBlock({ id, title, party, count, description, cases, emptyText, children }: {
  id: string; title: string; party: string; count: number; description?: string;
  cases: SafetyCaseDto[]; emptyText: string;
  children?: (caseView: SafetyCaseDto) => React.ReactNode;
}): React.ReactElement {
  return (
    <section className="rounded-card border border-border bg-surface" aria-labelledby={id}>
      <BlockHeader id={id} title={title} party={party} hint={`${count} 件`} description={description} />
      <div className="space-y-3 px-4 pb-4 pt-3">
        {cases.length === 0 ? (
          <p className="text-sm text-ink-muted">{emptyText}</p>
        ) : (
          cases.map((caseView) => (
            <CaseCard key={caseView.case_id} view={caseView}
              footer={children
                ? children(caseView)
                : <SeenButton view={caseView} />} />
          ))
        )}
        {count !== cases.length && (
          // 列表与计数不一致时如实说明,不让用户以为看到了全部。
          <p className="text-xs text-caution">
            这一块显示了 {cases.length} 件，但服务端计数是 {count} 件；请刷新页面确认。
          </p>
        )}
      </div>
    </section>
  );
}
