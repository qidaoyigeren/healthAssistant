/**
 * 单件安全事项详情(路由 `/safety/:caseId`)。
 *
 * 这一页按**用户真正要问的顺序**排,五步各回答一个问题:
 *   1 为什么出现这件事 → 2 已经查到了什么 → 3 仍有什么不确定 →
 *   4 现在需要我做什么 → 5 补充之后发生了什么变化。
 *
 * 两条贯穿全页的诚实规则:
 *  1. 「程序查到的」与「模型调查过的」分开写。模型判断不是依据,依据是必要检查的
 *     结论;工具调用与预算收在折叠区里,不占主流程。
 *  2. 关闭条件由服务端强制(`closure-evidence` 只读接口现场核对)。界面用它决定
 *     关闭按钮能不能按——不让用户去点一个一定会被拒绝的按钮;被拒绝时把服务端
 *     说的原话照抄出来。
 */
import React, { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, FileSearch, HelpCircle, Info } from 'lucide-react';
import { api } from '../../api/client';
import { request, newIdempotencyKey } from '../../api/http';
import { qk } from '../../api/queryKeys';
import type {
  CareTaskDto, SafetyCaseDto, SafetyClosureEvidenceDto,
} from '../../api/types';
import {
  Badge, Card, ErrorState, LiveAnnouncement, SkeletonList, TimeText,
} from '../../components/ui';
import { inputClass, buttonClass } from '../materials/MaterialsPage';
import { RUN_PROGRESS_LABELS, RunProgressLine, type RunProgress } from '../shared/runProgress';
import {
  AnswerPanel, InvestigateButton, SeenButton, triggerStateTone, type AnswerReceipt,
} from './CaseCard';
import { AnswerPartList } from './assessment';
import { FollowUpCard } from './FollowUpPanel';
import {
  fixtureModeOn, loadCareTasks, loadCase, loadClosureEvidence,
} from './fixtureBridge';
import {
  ANSWER_KIND_LABELS, MONITORING_NOTICE, NO_CLINICIAN_NOTICE, TRIGGER_STATE_MEANING,
  UNKNOWN_ANSWER_NOTICE, answerKindLabel, basisText, careTaskStatusLabel,
  caseTypeLabel, dispositionLabel, dispositionOutcome, followUpOf, followUpText,
  historyText, informationStateText, informationTargetText, partyActionLabel,
  questionStrategyText, serverMessage, stateLabel, statusTone,
  traceId, triggerStateLabel, triggerText,
} from './labels';

export function SafetyCaseDetailPage(): React.ReactElement {
  const params = useParams<{ caseId: string }>();
  const caseId = params.caseId ?? '';
  // 三个读查询都经 fixtureBridge:开发期 fixture 打开时它替换请求,
  // 生产构建里那条分支根本不存在(见 fixtureBridge.ts 的说明)。
  const detail = useQuery({
    queryKey: qk.safetyCase(caseId),
    queryFn: ({ signal }) => loadCase(caseId, signal),
    enabled: caseId.length > 0,
    refetchInterval: 15_000,
  });
  const tasks = useQuery({
    queryKey: qk.careTasks,
    queryFn: ({ signal }) => loadCareTasks(signal),
    refetchInterval: 15_000,
  });
  /**
   * 「能不能关、为什么」由服务端现场核对。第 3 步要用它说明"什么在挡着",
   * 处置面板要用它决定关闭按钮是否可用,所以读一次两边共用(同一个查询键)。
   */
  const evidence = useQuery({
    queryKey: qk.safetyClosureEvidence(caseId),
    queryFn: ({ signal }) => loadClosureEvidence(caseId, signal),
    enabled: caseId.length > 0,
    // 关闭条件会随记录变化:过期的"可以关闭"会是危险的假话。
    refetchInterval: 15_000,
  });
  const view = detail.data;
  const task = tasks.data?.items.find((item) => item.goal_type === 'safety_case'
    && item.safety_case_id === caseId) ?? null;
  const [receipt, setReceipt] = useState<AnswerReceipt | null>(null);
  const statusLabel = (status: string): string => stateLabel(status, view);

  return (
    <div className="space-y-5">
      <Link to="/" className="inline-flex items-center gap-1 text-sm text-primary underline">
        <ArrowLeft size={13} aria-hidden /> 回到用药安全主线
      </Link>

      {fixtureModeOn() && <FixtureBanner />}
      {!caseId && <ErrorState error="链接里没有事项编号。" title="链接不完整" />}
      {detail.isPending && caseId.length > 0 && <SkeletonList rows={4} />}
      {detail.isError && (
        <ErrorState error={detail.error} title="这件事项读取失败（可能是编号不对或已被清理）"
          onRetry={() => void detail.refetch()} />
      )}

      {view && (
        <>
          <header>
            <p className="text-sm text-ink-muted">
              安全事项 · 建立于 <TimeText iso={view.created_at} />
            </p>
            <h1 className="mt-1 font-serif text-2xl font-semibold">{caseTypeLabel(view.case_type)}</h1>
            <p className="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink-secondary">
              <Badge tone={statusTone(view.status)}>{view.status_label}</Badge>
              <span>
                现在需要{partyActionLabel(view.responsible_party, view.status)}：
                {view.next_action_summary ?? '服务端未记录下一步。'}
              </span>
            </p>
            <div className="mt-3">
              <SeenButton view={view} />
            </div>
          </header>

          <WhyStep view={view} />

          <WhatWeKnowStep view={view} task={task} />

          <StillUncertainStep view={view} evidence={evidence.data} />

          <WhatToDoStep view={view} task={task} onAnswered={setReceipt} />

          <FollowUpCard view={view} task={task} />

          <DeltaStep view={view} receipt={receipt} task={task} />

          <EvidenceRefsCard view={view} />

          <DispositionPanel view={view} evidence={evidence} />

          <HistoryCard view={view} statusLabel={statusLabel} />

          <details className="rounded-card border border-border bg-surface px-4 py-3 text-sm">
            <summary className="flex cursor-pointer items-center gap-1 text-ink-secondary">
              <Info size={13} aria-hidden /> 诊断详情（内部编号与版本，供核对用）
            </summary>
            <dl className="mt-2 space-y-1 text-xs text-ink-muted">
              <div>事项编号：<span className="break-all font-mono">{view.case_id}</span></div>
              <div>对象键：<span className="break-all font-mono">{view.subject_keys.join('、') || '未记录'}</span></div>
              <div>版本：v{view.revision}；已补充 {view.answered_inputs_count} 条</div>
              <div>
                记录版本（处置依据即按它校验）：
                <span className="font-mono">
                  {Object.entries(view.input_versions).map(([scope, revision]) => `${scope} v${revision}`).join('、') || '未记录'}
                </span>
              </div>
              <div>
                关联复核决定：
                <span className="font-mono">
                  {(view.linked_review_case_ids ?? []).join('、') || '未记录'}
                </span>
              </div>
              <div>更新于 <TimeText iso={view.updated_at} /></div>
            </dl>
          </details>
        </>
      )}
    </div>
  );
}

/**
 * 开发期 fixture 的显式提示。
 *
 * 加了这一个条幅,是为了让"这一页显示的是合成数据"永远是**看得见的事实** ——
 * 没有它,一张画得很像的 fixture 页面会被当成真实事项读。
 */
function FixtureBanner(): React.ReactElement {
  return (
    <div role="status"
      className="rounded-card border border-caution/40 bg-caution-soft px-4 py-3 text-sm">
      <p className="font-medium text-caution">开发期 fixture 已打开：本页显示的是合成数据</p>
      <p className="mt-1 text-ink-secondary">
        这一页没有连接真实后端，也没有真实患者数据、没有调用模型。
        场景可用 <span className="font-mono">?fx=legacy</span>（旧记录）、
        <span className="font-mono"> ?fx=empty</span>（空数据）、
        <span className="font-mono"> ?fx=conflict</span>（版本冲突）、
        <span className="font-mono"> ?fx=failure</span>（提交失败）切换。
      </p>
    </div>
  );
}

// ---- 步骤外壳 ---------------------------------------------------------------

function Step({ index, title, hint, children }: {
  index: number; title: string; hint?: string; children: React.ReactNode;
}): React.ReactElement {
  return (
    <Card>
      <div className="px-4 pt-4">
        <h2 className="flex flex-wrap items-center gap-2 font-serif text-lg font-semibold">
          <span aria-hidden
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-semibold text-primary-strong">
            {index}
          </span>
          {title}
        </h2>
        {hint && <p className="mt-1 text-sm text-ink-secondary">{hint}</p>}
      </div>
      <div className="space-y-3 px-4 pb-4 pt-3 text-sm">{children}</div>
    </Card>
  );
}

// ---- 1 为什么出现这件事 ------------------------------------------------------

function WhyStep({ view }: { view: SafetyCaseDto }): React.ReactElement {
  return (
    <Step index={1} title="为什么出现这件事"
      hint="这一次触发从哪里来。触发的是「记录发生了变化」，不是对您的判断。">
      <p>{triggerText(view.trigger)}</p>
      <p className="text-xs text-ink-muted">
        触发的具体来源指针在页面下方「证据引用」里逐条列出（内部编号，供核对用）。
      </p>
      {(view.medications.length > 0 || view.facts.length > 0) && (
        <div>
          <p className="text-xs text-ink-muted">涉及的记录</p>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {view.medications.map((item) => (
              <RefChip key={item.ref} label={item.label} available={item.available} />
            ))}
            {view.facts.map((item) => (
              <RefChip key={item.ref} label={item.label} available={item.available} />
            ))}
          </ul>
          {[...view.medications, ...view.facts].some((item) => !item.available) && (
            <p className="mt-1 text-xs text-caution">
              有引用读不出内容（可能已被新版本替代或超出可读范围）——这里如实标注，不补造内容。
            </p>
          )}
        </div>
      )}
    </Step>
  );
}

function RefChip({ label, available }: { label: string; available: boolean }): React.ReactElement {
  return (
    <li className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${
      available ? 'border-border bg-surface-alt text-ink-secondary'
        : 'border-caution/30 bg-caution-soft text-caution'
    }`}>
      {available ? label : `${label}（读取不到）`}
    </li>
  );
}

// ---- 2 已经查到了什么 --------------------------------------------------------

function WhatWeKnowStep({ view, task }: {
  view: SafetyCaseDto; task: CareTaskDto | null;
}): React.ReactElement {
  const available = view.conclusions.filter((item) => item.available);
  return (
    <Step index={2} title="已经查到了什么"
      hint="程序必要检查跑出来的结论，以及系统在这件事上做过的调查——两者不是一回事。">
      <section aria-label="程序判定" className="rounded-r-lg border-l-2 border-l-primary bg-primary-soft/40 py-2 pl-3 pr-2">
        <p className="flex flex-wrap items-center gap-2">
          <Badge tone="primary">程序判定</Badge>
          <span className="text-xs text-ink-muted">来自已记录的必要检查结论与它们的来源</span>
        </p>
        <div className="mt-1.5">
          {available.length === 0 ? (
            <p className="text-ink-muted">
              这件事还没有可展示的检查结论。没有结论不等于没有风险，只表示程序还没查到这里。
            </p>
          ) : (
            <ul className="space-y-3">
              {view.conclusions.map((item) => (
                <ConclusionBlock key={item.ref} conclusion={item} />
              ))}
            </ul>
          )}
        </div>
      </section>
      <p className="rounded-lg bg-surface-alt px-3 py-2 text-xs text-ink-secondary">
        {TRIGGER_STATE_MEANING}
      </p>
      <ModelLayer view={view} />
      <InvestigationTrace runIds={view.linked_run_ids} task={task} />
    </Step>
  );
}

function ConclusionBlock({ conclusion }: {
  conclusion: SafetyCaseDto['conclusions'][number];
}): React.ReactElement {
  if (!conclusion.available) {
    return (
      <li className="text-ink-muted">
        一条检查结论读取不到（引用 {conclusion.ref}）——不补造内容。
      </li>
    );
  }
  const stale = conclusion.status !== 'current';
  return (
    <li>
      <p>{conclusion.text}</p>
      <p className="mt-1 flex flex-wrap items-center gap-2 text-xs">
        {stale
          ? <Badge tone="caution">已失效{conclusion.stale_reason ? `：${conclusion.stale_reason}` : ''}</Badge>
          : <Badge tone="neutral">当前有效</Badge>}
        <Badge tone={triggerStateTone(conclusion.trigger_state)}>
          触发条件：{triggerStateLabel(conclusion.trigger_state)}
        </Badge>
      </p>
      {conclusion.trigger_reasons && conclusion.trigger_reasons.length > 0 && (
        <p className="mt-0.5 text-xs text-ink-muted">
          判定依据：{conclusion.trigger_reasons.join('；')}
        </p>
      )}
      <SourcesList sources={conclusion.sources ?? []} />
    </li>
  );
}

/** 来源:http(s) 可点开,其余如实说明应用内不读取。 */
function SourcesList({ sources }: { sources: string[] }): React.ReactElement {
  if (sources.length === 0) {
    return <p className="mt-1 text-xs text-ink-muted">这条结论没有记录来源。</p>;
  }
  return (
    <ul className="mt-1 space-y-0.5">
      {sources.map((source, index) => (
        <li key={`${source}#${index}`} className="text-xs">
          {/^https?:\/\//i.test(source) ? (
            <a href={source} target="_blank" rel="noopener noreferrer"
              className="break-all text-primary underline">{source}</a>
          ) : (
            <span className="break-all font-mono text-ink-muted">{source}（应用内不直接读取）</span>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * 模型调查那一层。只写它**贡献了什么**(提出的问题、读到的来源、运行状态),
 * 不把模型判断写成依据;工具调用与预算收在折叠明细里。
 */
function ModelLayer({ view }: { view: SafetyCaseDto }): React.ReactElement {
  return (
    <section aria-label="模型调查" className="rounded-r-lg border-l-2 border-l-border-strong bg-surface-alt py-2 pl-3 pr-2">
      <p className="flex flex-wrap items-center gap-2">
        <Badge tone="neutral">模型调查</Badge>
        <span className="text-xs text-ink-muted">系统做过什么调查；模型判断不是依据</span>
      </p>
      <div className="mt-1.5 space-y-2">
        <ul className="space-y-1">
          <li>
            {view.linked_run_ids.length === 0
              ? '还没有围绕这件事展开过调查。'
              : `已经围绕这件事展开过 ${view.linked_run_ids.length} 次调查（状态见下）。`}
          </li>
          <li>它提出了 {view.open_questions.length} 条待确认问题（列在第 3 步里）。</li>
          <li>它记下了 {view.evidence_refs.length} 条证据引用（列在下方「证据引用」里）。</li>
        </ul>
        {view.linked_run_ids.length > 0 && (
          <RunProgressLine runId={view.linked_run_ids[view.linked_run_ids.length - 1]}
            active={false}
            emptyHint="这次调查没有可展示的进度事件（服务端未推送或未开启进度记录）。" />
        )}
        <p className="text-xs text-ink-muted">
          这些都不是风险结论：模型读过什么、问过什么，不等于这件事已经查清或已经没有问题。
          工具调用与预算这类过程记录收在下面的折叠明细里。
        </p>
      </div>
    </section>
  );
}

// ---- 3 仍有什么不确定 --------------------------------------------------------

function StillUncertainStep({ view, evidence }: {
  view: SafetyCaseDto; evidence: SafetyClosureEvidenceDto | undefined;
}): React.ReactElement {
  const mine = view.required_inputs.filter(
    (item) => item.status === 'open' && !item.for_professional);
  const theirs = view.required_inputs.filter(
    (item) => item.status === 'open' && item.for_professional);
  const unknown = view.required_inputs.filter(
    (item) => item.needs_alternative_evidence || item.status === 'unknown');
  const reopened = view.required_inputs.filter((item) => item.reopened_reason);
  // 服务端只把 status=open 的请求放进视图。用户明说"不知道"的那条会因此消失,
  // 只留下历史里的一笔——这里把它读回来,不然界面会假装"没有这条问题"。
  const unknownIds = new Set(unknown.map((item) => item.request_id));
  for (const entry of view.history) {
    if (entry.event === 'input_recorded' && entry.answer_kind === 'unknown' && entry.request_id
      && !view.required_inputs.some((item) => item.request_id === entry.request_id)) {
      unknownIds.add(entry.request_id);
    }
  }
  const blocking = evidence?.blocking_inputs ?? [];
  const blockingNotShown: string[] = blocking.filter(
    (id) => !view.required_inputs.some((item) => item.request_id === id) && !unknownIds.has(id));
  const stillPresent = evidence?.still_present ?? [];
  const nothingRecorded = mine.length === 0 && theirs.length === 0 && unknownIds.size === 0
    && view.open_questions.length === 0 && blockingNotShown.length === 0;

  return (
    <Step index={3} title="仍有什么不确定"
      hint="这些是「还没有答案」的部分。没有列出来，不等于已经查清；关不掉的原因也在这里。">
      {nothingRecorded && (
        <p className="text-ink-secondary">
          这件事当前没有记录下来的未决问题。
          「没有未决问题」不等于「风险已经排除」——风险是否成立，看第 2 步里检查结论的触发条件状态。
        </p>
      )}

      {mine.length > 0 && (
        <div>
          <h3 className="font-medium">还在等您回答（{mine.length} 条）</h3>
          <ul className="mt-1 space-y-1.5">
            {mine.map((item) => (
              <li key={item.request_id}>
                <span className="font-medium">问题：</span>{item.question}
                {(informationTargetText(item.question_kind)
                  || questionStrategyText(item.question_strategy)) && (
                  <p className="text-xs text-ink-muted">
                    {informationTargetText(item.question_kind)
                      && <>要弄清的是：{informationTargetText(item.question_kind)}</>}
                    {questionStrategyText(item.question_strategy)
                      && <>；这一次从哪里取：{questionStrategyText(item.question_strategy)}</>}
                  </p>
                )}
                {(item.strategy_history ?? []).map((entry, index) => (
                  <p key={index} className="text-xs text-caution">
                    取证来源调整过：{questionStrategyText(entry.from) ?? '（未记录）'}
                    {' → '}{questionStrategyText(entry.to) ?? '（未记录）'}
                    {entry.reason ? `；原因：${entry.reason}` : ''}
                  </p>
                ))}
                {item.why_needed && (
                  <p className="text-xs text-ink-muted">
                    为什么需要这个信息，它会影响哪一步：{item.why_needed}
                  </p>
                )}
                {informationStateText(item.information_state) && (
                  <p className="text-xs text-ink-muted">
                    查到了什么程度：{informationStateText(item.information_state)}
                  </p>
                )}
                <AnswerPartList parts={item.answered_parts}
                  emptyHint="这条问题还没有拿到任何一部分内容。" />
                {(item.still_uncertain ?? []).length > 0 && (
                  <p className="text-xs text-caution">
                    仍不能判断：{(item.still_uncertain ?? []).join('、')}
                  </p>
                )}
                {item.reopened_reason && (
                  <p className="text-xs text-caution">
                    这条问题此前回答过，但回答已不再适用于当前记录：{item.reopened_reason}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(view.answered_inputs ?? []).length > 0 && (
        <div>
          <h3 className="font-medium">已经查清的部分（{(view.answered_inputs ?? []).length} 条）</h3>
          <ul className="mt-1 space-y-1.5">
            {(view.answered_inputs ?? []).map((item) => (
              <li key={item.request_id}>
                <span className="font-medium">问题：</span>{item.question}
                {informationStateText(item.information_state) && (
                  <p className="text-xs text-ink-muted">
                    查到了什么程度：{informationStateText(item.information_state)}
                  </p>
                )}
                <AnswerPartList parts={item.answered_parts}
                  emptyHint="这一条已经收到补充，但没有记录下具体内容；具体依据见下方「经过」。" />
                {(item.still_uncertain ?? []).length > 0 && (
                  <p className="text-xs text-caution">
                    仍不能判断：{(item.still_uncertain ?? []).join('、')}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {reopened.length > 0 && (
        <p className="rounded-lg bg-caution-soft px-3 py-2 text-caution">
          有 {reopened.length} 条问题因为记录变化被重新打开：此前的回答不再适用于当前状态，
          需要重新确认后才能继续。
        </p>
      )}

      {unknownIds.size > 0 && (
        <div>
          <h3 className="font-medium">
            <Badge tone="caution" icon={<HelpCircle size={12} aria-hidden />}>{UNKNOWN_ANSWER_NOTICE}</Badge>
          </h3>
          <ul className="mt-1 space-y-1.5">
            {[...unknownIds].map((id) => {
              const item = unknown.find((candidate) => candidate.request_id === id);
              return (
                <li key={id}>
                  {item ? item.question : questionTextFor(view, id)}
                  <p className="text-xs text-ink-muted">
                    这条问题不再等您回答，但它仍然没有解决，也仍然阻止关闭事项，
                    改由系统从其他来源核实。
                  </p>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {theirs.length > 0 && (
        <div>
          <h3 className="font-medium">需要专业人员回答（{theirs.length} 条）</h3>
          <ul className="mt-1 space-y-1.5">
            {theirs.map((item) => (
              <li key={item.request_id}>{item.question}</li>
            ))}
          </ul>
          <p className="mt-2 rounded-lg bg-caution-soft px-3 py-2 text-caution">{NO_CLINICIAN_NOTICE}</p>
        </div>
      )}

      {view.open_questions.length > 0 && (
        <div>
          <h3 className="font-medium">调查留下的待确认问题（{view.open_questions.length} 条）</h3>
          <ul className="mt-1 space-y-1.5">
            {view.open_questions.map((question) => (
              <li key={question.question_id}>
                {question.question ?? question.field ?? '问题内容未记录。'}
                {question.why && <p className="text-xs text-ink-muted">{question.why}</p>}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs text-ink-muted">
            这些是调查过程中记下的问题，不是检查结论；它们没有答案时不会关闭事项。
          </p>
        </div>
      )}

      {evidence && !evidence.ok && (
        <div className="rounded-lg border border-caution/30 bg-caution-soft/40 px-3 py-2">
          <h3 className="font-medium text-caution">此刻关不掉这件事，因为：</h3>
          {evidence?.reason && (
            <p className="mt-1 whitespace-pre-wrap text-ink">
              服务端原话：{evidence.reason}
            </p>
          )}
          {stillPresent.length > 0 && (
            <div className="mt-1">
              <p className="text-ink-secondary">仍显示风险成立的检查结论：</p>
              <ul className="mt-0.5 list-inside list-disc text-ink-secondary">
                {stillPresent.map((ref) => (
                  <li key={ref}>{conclusionTextFor(view, ref)}</li>
                ))}
              </ul>
            </div>
          )}
          {blockingNotShown.length > 0 && (
            <div className="mt-1">
              <p className="text-ink-secondary">仍未解决的补充问题：</p>
              <ul className="mt-0.5 list-inside list-disc text-ink-secondary">
                {blockingNotShown.map((id) => <li key={id}>{questionTextFor(view, id)}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </Step>
  );
}

/** 把一条结论引用翻成人看得懂的那句话；找不到就如实说找不到。 */
function conclusionTextFor(view: SafetyCaseDto, ref: string): string {
  const item = view.conclusions.find((conclusion) => conclusion.ref === ref);
  if (!item) return `${ref}（这条结论的内容没有出现在当前视图里）`;
  if (!item.available) return `${ref}（这条结论读取不到内容）`;
  return item.text ?? ref;
}

/** 把一条补充问题的编号翻成它的问题原文;视图里没有这条就只说编号,不编问题。 */
function questionTextFor(view: SafetyCaseDto, requestId: string): string {
  const item = view.required_inputs.find((candidate) => candidate.request_id === requestId);
  return item ? item.question : `问题编号 ${requestId}（这条问题的内容没有出现在当前视图里）`;
}

// ---- 4 现在需要我做什么 ------------------------------------------------------

function WhatToDoStep({ view, task, onAnswered }: {
  view: SafetyCaseDto; task: CareTaskDto | null;
  onAnswered: (receipt: AnswerReceipt) => void;
}): React.ReactElement {
  const followUp = view.status === 'monitoring' ? followUpOf(view) : null;
  return (
    <Step index={4} title="现在需要我做什么"
      hint="这件事此刻的下一步，以及需要您回答的问题（一次只显示一条）。">
      <p className="rounded-lg bg-surface-alt px-3 py-2">
        <span className="font-medium">
          现在需要{partyActionLabel(view.responsible_party, view.status)}：
        </span>
        {view.next_action_summary ?? '服务端未记录下一步。'}
      </p>
      {view.status === 'monitoring' && (
        <div className="rounded-lg border border-caution/30 bg-caution-soft/40 px-3 py-2">
          <p className="text-caution">{MONITORING_NOTICE}</p>
          <p className="mt-1">
            跟进安排：{followUpText(followUp)}
            {' '}要查看它现在算不算数、并做确认/改期/取消，见下方「下一次跟进」。
          </p>
        </div>
      )}
      <AnswerPanel view={view} onAnswered={onAnswered} />
      <div className="border-t border-border pt-3">
        <h3 className="font-medium">不需要您回答时，也可以让系统去查</h3>
        <div className="mt-2">
          <InvestigateButton view={view} activeTask={task} />
        </div>
      </div>
      {view.responsible_party === 'professional' && (
        <p className="rounded-lg bg-caution-soft px-3 py-2 text-caution">{NO_CLINICIAN_NOTICE}</p>
      )}
    </Step>
  );
}

// ---- 5 补充之后发生了什么变化 -------------------------------------------------

interface AnswerDelta {
  requestId: string;
  value: string;
  answerKind: string | null;
  /** closed = 服务端关掉了这条问题；unknown = 用户说不知道；open = 仍然待答。 */
  outcome: 'closed' | 'unknown' | 'open' | 'empty' | 'unreadable';
  statusChangedTo: string | null;
}

/** 只从服务端返回的**新**视图里读这次补充的结果,读不到就说读不到。 */
function summariseAnswer(view: SafetyCaseDto, requestId: string, value: string): AnswerDelta {
  const history = view.history;
  let index = -1;
  for (let cursor = history.length - 1; cursor >= 0; cursor -= 1) {
    const entry = history[cursor];
    if (entry && entry.event === 'input_recorded' && entry.request_id === requestId) {
      index = cursor;
      break;
    }
  }
  const recorded = index >= 0 ? history[index] : undefined;
  const answerKind = recorded?.answer_kind ?? null;
  const answered = typeof recorded?.answered === 'boolean' ? recorded.answered : null;
  const item = view.required_inputs.find((candidate) => candidate.request_id === requestId);
  let outcome: AnswerDelta['outcome'];
  if (item) {
    if (item.status === 'unknown' || item.needs_alternative_evidence) outcome = 'unknown';
    else if (item.status === 'open') outcome = 'open';
    else outcome = 'closed';
  } else if (answered === true) outcome = 'closed';
  else if (answerKind === 'unknown') outcome = 'unknown';
  else if (answerKind === 'empty') outcome = 'empty';
  else outcome = 'unreadable';
  let statusChangedTo: string | null = null;
  if (index >= 0) {
    for (let cursor = index + 1; cursor < history.length; cursor += 1) {
      const entry = history[cursor];
      if (entry && entry.event === 'status_changed' && entry.to) statusChangedTo = entry.to;
    }
  }
  return { requestId, value, answerKind, outcome, statusChangedTo };
}

function DeltaStep({ view, receipt, task }: {
  view: SafetyCaseDto; receipt: AnswerReceipt | null; task: CareTaskDto | null;
}): React.ReactElement {
  // 提交成功后这件事会重新拉取。拉取落地之前,先按服务端**当时返回**的那份视图算,
  // 免得把"本地还没刷新到"显示成"读不到这次提交的结果"。
  const hasRecord = receipt !== null && view.history.some(
    (entry) => entry.event === 'input_recorded' && entry.request_id === receipt.requestId);
  const source = receipt && !hasRecord ? receipt.updated : view;
  const delta = receipt ? summariseAnswer(source, receipt.requestId, receipt.value) : null;
  const processing = task && !['completed', 'cancelled', 'failed'].includes(task.status)
    ? `这件事仍有一轮调查在排队或进行中（任务状态：${careTaskStatusLabel(task.status)}）。`
    : view.status === 'investigating'
      ? '事项状态仍是「调查中」：服务端仍标记这件事在处理。'
      : '当前没有正在进行的调查任务。这不表示风险已经排除，只表示本轮没有在跑。';
  return (
    <Step index={5} title="补充之后发生了什么变化"
      hint="只写服务端返回的内容：记了什么、那条问题现在算什么、这件事现在是什么状态。">
      {!delta ? (
        <p className="text-ink-muted">
          回答上面任意一条问题后，这里会显示这次补充实际改变了什么。
          （这一页不会替服务端预告结果，也不会把"提交成功"说成"问题解决了"。）
        </p>
      ) : (
        <dl className="space-y-2">
          <div>
            <dt className="text-xs text-ink-muted">这次保存的内容</dt>
            <dd>{delta.value}</dd>
          </div>
          <div>
            <dt className="text-xs text-ink-muted">服务端把它记成了</dt>
            <dd>
              {answerKindLabel(delta.answerKind)}
              {delta.answerKind && ANSWER_KIND_LABELS[delta.answerKind]
                ? '（这个分类由服务端给出，本页不自行改写）' : ''}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-ink-muted">这条问题现在</dt>
            <dd>
              {delta.outcome === 'closed' && '已被服务端关闭（对应的那条问题不再计入未决）。'}
              {delta.outcome === 'unknown' && '转入「需要替代证据」：不再等您回答，但仍未解决，'
                + '也仍然阻止关闭事项，改由系统从其他来源核实。'}
              {delta.outcome === 'open' && '仍然是待回答——这次提交没有关闭它。'}
              {delta.outcome === 'empty' && '仍然待回答：空回答什么都不关。'}
              {delta.outcome === 'unreadable'
                && '返回的视图里没有读到这条问题的结果（历史里也没有对应的记录）。'
                + '请刷新后确认，不要按"已经解决"理解。'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-ink-muted">这件事现在</dt>
            <dd>
              <Badge tone={statusTone(view.status)}>{view.status_label}</Badge>
              {delta.statusChangedTo && (
                <span className="ml-2 text-ink-secondary">
                  （状态随之变为「{stateLabel(delta.statusChangedTo, view)}」）
                </span>
              )}
              <span className="ml-2 text-ink-secondary">
                下一步：{view.next_action_summary ?? '服务端未记录下一步。'}
              </span>
            </dd>
          </div>
          <div>
            <dt className="text-xs text-ink-muted">是否还在处理</dt>
            <dd>{processing}</dd>
          </div>
          <div>
            <dt className="text-xs text-ink-muted">未决问题</dt>
            <dd>
              现在还有 {view.required_inputs.filter((item) => item.status === 'open').length} 条待回答、
              {' '}{view.answered_inputs_count} 条已回答。
            </dd>
          </div>
        </dl>
      )}
    </Step>
  );
}

// ---- 证据引用 ---------------------------------------------------------------

function EvidenceRefsCard({ view }: { view: SafetyCaseDto }): React.ReactElement {
  return (
    <Card>
      <div className="px-4 pt-4">
        <h2 className="font-serif text-lg font-semibold">证据引用</h2>
        <p className="mt-1 text-sm text-ink-secondary">
          这件事项记下来的来源指针与证据编号。http(s) 来源可以打开；其余为服务端内部指针，
          应用内不直接读取，也不会替它们编一个可点的地址。
        </p>
      </div>
      <div className="px-4 pb-4 pt-3">
        {view.evidence_refs.length === 0 ? (
          <p className="text-sm text-ink-muted">这件事项没有记录证据引用。</p>
        ) : (
          <ul className="space-y-1">
            {view.evidence_refs.map((ref, index) => (
              <li key={`${ref}#${index}`} className="text-sm">
                {/^https?:\/\//i.test(ref) ? (
                  <a href={ref} target="_blank" rel="noopener noreferrer"
                    className="break-all text-primary underline">{ref}</a>
                ) : (
                  <span className="break-all font-mono text-xs text-ink-secondary">{ref}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

// ---- 调查过程明细(折叠) ------------------------------------------------------

/**
 * 工具调用与预算的明细。**故意收在折叠区里**:它们是核对用的过程记录,不是结论,
 * 也不该被读成"做了多少步 = 这件事查得多清楚"。
 */
function InvestigationTrace({ runIds, task }: {
  runIds: string[]; task: CareTaskDto | null;
}): React.ReactElement {
  return (
    <details className="rounded-lg border border-border bg-surface px-3 py-2">
      <summary className="cursor-pointer text-xs text-ink-secondary">
        调查过程明细（工具调用与预算，供核对用——这些不是结论）
      </summary>
      <div className="mt-2 space-y-3">
        {task ? (
          <p className="text-xs text-ink-secondary">
            当前调查任务：{careTaskStatusLabel(task.status)}
            {task.budget ? ` · 已用 ${task.budget.spent}/${task.budget.limit} 步` : ' · 未记录预算'}
            {task.waiting_reason ? ` · ${task.waiting_reason}` : ''}
          </p>
        ) : (
          <p className="text-xs text-ink-muted">这件事目前没有调查任务。</p>
        )}
        {runIds.length === 0 ? (
          <p className="text-xs text-ink-muted">没有可展示的调查运行。</p>
        ) : (
          runIds.map((runId, index) => (
            <div key={runId}>
              <p className="text-xs text-ink-muted">第 {index + 1} 次调查</p>
              <RunEventList runId={runId} />
            </div>
          ))
        )}
      </div>
    </details>
  );
}

function RunEventList({ runId }: { runId: string }): React.ReactElement {
  const progress = useQuery({
    queryKey: qk.runProgress(runId, false),
    queryFn: ({ signal }) => request<RunProgress>(
      `/v1/runs/${encodeURIComponent(runId)}/progress`, { signal }),
  });
  if (progress.isPending) return <p className="text-xs text-ink-secondary">读取进度…</p>;
  if (progress.isError) {
    return <p className="text-xs text-caution">进度读取失败，无法判断这一步走到哪里。</p>;
  }
  const events = progress.data?.events ?? [];
  if (events.length === 0) {
    return <p className="text-xs text-ink-muted">
      这次调查没有可展示的进度事件（服务端未推送或未开启进度记录）。
    </p>;
  }
  return (
    <ul className="space-y-0.5 text-xs text-ink-secondary">
      {events.slice(-12).map((event) => (
        <li key={event.event_id} className="flex flex-wrap items-baseline gap-2">
          <TimeText iso={event.created_at} />
          <span>{RUN_PROGRESS_LABELS[event.kind] ?? `阶段：${event.kind}`}</span>
          {event.tool && <span className="font-mono text-ink-muted">工具：{event.tool}</span>}
        </li>
      ))}
      {events.length > 12 && <li className="text-ink-muted">（只显示最近 12 条）</li>}
    </ul>
  );
}

// ---- 历史 -------------------------------------------------------------------

function HistoryCard({ view, statusLabel }: {
  view: SafetyCaseDto; statusLabel: (status: string) => string;
}): React.ReactElement {
  const newestFirst = [...view.history].reverse();
  return (
    <Card>
      <div className="px-4 pt-4">
        <h2 className="font-serif text-lg font-semibold">经过（按时间倒序）</h2>
        <p className="mt-1 text-sm text-ink-secondary">
          谁在什么时候做了什么，都在这里；处置记录也在其中。已经失效的处置依据也留在历史里
          ——因为它确实发生过，但它不再是现行依据。
        </p>
      </div>
      <div className="px-4 pb-4 pt-3">
        {newestFirst.length === 0 ? (
          <p className="text-sm text-ink-muted">没有历史记录。</p>
        ) : (
          <ol className="space-y-1.5 text-sm">
            {newestFirst.map((entry, index) => (
              <li key={`${entry.at}:${entry.event}:${index}`} className="flex flex-wrap items-baseline gap-2">
                <TimeText iso={entry.at} />
                <span>{historyText(entry, statusLabel)}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </Card>
  );
}

// ---- 处置 -------------------------------------------------------------------

const DISPOSITION_MODES = ['resolved_with_basis', 'accepted_monitoring', 'escalated_to_professional'] as const;
type DispositionMode = typeof DISPOSITION_MODES[number];

function DispositionPanel({ view, evidence }: {
  view: SafetyCaseDto;
  evidence: {
    data?: SafetyClosureEvidenceDto;
    isPending: boolean;
    isError: boolean;
    error: unknown;
    refetch: () => void;
  };
}): React.ReactElement {
  const client = useQueryClient();
  const [mode, setMode] = useState<DispositionMode>('accepted_monitoring');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [trace, setTrace] = useState<string | null>(null);
  const [done, setDone] = useState('');

  const check = evidence.data;
  const canClose = check?.ok === true;
  const closeReason = check?.reason
    ?? (evidence.isError ? '关闭条件读取失败，不能按"可以关闭"处理。' : null);

  async function submit() {
    setBusy(true);
    setError('');
    setTrace(null);
    setDone('');
    try {
      // **不在这里安排跟进**。时间与条件改由下方「下一次跟进」统一安排:
      // 那里用的是白名单结构(§4.3),而且不会把"排了期"说成"已确认"(§4.5)。
      const updated = await api.safetyCaseDisposition(view.case_id, {
        key: newIdempotencyKey(),
        expected_revision: view.revision,
        disposition: mode,
        basis_kind: mode === 'escalated_to_professional'
          ? 'user_reported' : 'deterministic_check_completed',
        note: note.trim() || undefined,
      });
      setNote('');
      setDone(`处置已记录，事项现在是「${updated.status_label}」。`
        + (updated.next_action_summary ? `下一步：${updated.next_action_summary}` : '')
        + (mode === 'accepted_monitoring'
          ? '这件事还没有跟进安排——请在下方「下一次跟进」里排一次。' : ''));
      await client.invalidateQueries({ queryKey: qk.safetyMainline });
      await client.invalidateQueries({ queryKey: qk.safetyCases });
      await client.invalidateQueries({ queryKey: qk.safetyClosureEvidence(view.case_id) });
    } catch (caught) {
      // 关闭依据被拒绝时,服务端说明了原因——原样显示,不改写成别的结论。
      setError(serverMessage(caught));
      setTrace(traceId(caught));
      await client.invalidateQueries({ queryKey: qk.safetyCase(view.case_id) });
      await client.invalidateQueries({ queryKey: qk.safetyClosureEvidence(view.case_id) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <LiveAnnouncement message={done || null} />
      <div className="px-4 pt-4">
        <h2 className="font-serif text-lg font-semibold">处置这件事</h2>
        <p className="mt-1 text-sm text-ink-secondary">
          当前：{dispositionLabel(view.disposition)}。
          {view.resolution_basis
            ? basisText(view.resolution_basis, view)
            : '还没有处置依据。'}
        </p>
      </div>
      <div className="space-y-3 px-4 pb-4 pt-3 text-sm">
        {/* 关闭条件现场核对结果:先给结论,再让人决定按不按。 */}
        <div className={`rounded-lg border px-3 py-2 ${
          canClose ? 'border-primary/30 bg-primary-soft/40'
            : 'border-caution/30 bg-caution-soft/40'}`}>
          <p className="font-medium">
            服务端现场核对：{evidence.isPending ? '正在核对…'
              : evidence.isError ? '关闭条件读取失败'
                : canClose ? '现在可以关闭' : '现在不能关闭'}
          </p>
          {!evidence.isPending && !evidence.isError && (
            <p className="mt-1 text-ink-secondary">
              触发条件已消除的结论 {check?.eliminated.length ?? 0} 条；
              仍显示风险成立的结论 {check?.still_present.length ?? 0} 条；
              仍未解决的补充问题 {check?.blocking_inputs.length ?? 0} 条。
            </p>
          )}
          {closeReason && !canClose && (
            <p className="mt-1 whitespace-pre-wrap text-ink">服务端原话：{closeReason}</p>
          )}
          {/* 挡着关闭的具体条目:逐条列出来,而不是只给一句"不能关"。 */}
          {!canClose && check && (
            (check.still_present.length > 0 || check.blocking_inputs.length > 0) && (
              <ul className="mt-1 list-inside list-disc text-ink-secondary">
                {check.still_present.map((ref) => (
                  <li key={ref}>结论仍显示风险成立：{conclusionTextFor(view, ref)}</li>
                ))}
                {check.blocking_inputs.map((id) => (
                  <li key={id}>仍有未解决的补充问题：{questionTextFor(view, id)}</li>
                ))}
              </ul>
            )
          )}
          {evidence.isError && (
            <button type="button" className="mt-1 text-xs text-primary underline"
              onClick={() => evidence.refetch()}>
              重新核对关闭条件
            </button>
          )}
          <p className="mt-1 text-xs text-ink-muted">
            这个结果是只读的现场核对，不是写入。它由服务端按当前记录算出来，
            因此按之前请以这一次的核对为准。
          </p>
        </div>

        <fieldset className="space-y-2">
          <legend className="text-xs text-ink-muted">选择这次要做什么</legend>

          <label className={`flex gap-2 rounded-lg border px-3 py-2 ${
            canClose ? 'border-border' : 'border-border bg-surface-alt opacity-70'}`}>
            <input type="radio" name="disposition-mode" value="resolved_with_basis"
              checked={mode === 'resolved_with_basis'} disabled={!canClose}
              onChange={() => setMode('resolved_with_basis')} className="mt-1" />
            <span>
              <span className="font-medium">关闭事项</span>
              <span className="ml-2 text-xs text-ink-muted">依据：程序检查已完成</span>
              <span className="mt-0.5 block text-ink-secondary">
                {canClose
                  ? dispositionOutcome('resolved_with_basis', 'deterministic_check_completed')
                  : `现在不可用：${closeReason ?? '关闭条件尚未核对完成。'}`}
              </span>
            </span>
          </label>

          <label className="flex gap-2 rounded-lg border border-border px-3 py-2">
            <input type="radio" name="disposition-mode" value="accepted_monitoring"
              checked={mode === 'accepted_monitoring'}
              onChange={() => setMode('accepted_monitoring')} className="mt-1" />
            <span>
              <span className="font-medium">持续跟进</span>
              <span className="ml-2 text-xs text-ink-muted">风险仍在，已有安排</span>
              <span className="mt-0.5 block text-ink-secondary">
                {MONITORING_NOTICE}{dispositionOutcome('accepted_monitoring', 'deterministic_check_completed')}
              </span>
            </span>
          </label>

          <label className="flex gap-2 rounded-lg border border-border px-3 py-2">
            <input type="radio" name="disposition-mode" value="escalated_to_professional"
              checked={mode === 'escalated_to_professional'}
              onChange={() => setMode('escalated_to_professional')} className="mt-1" />
            <span>
              <span className="font-medium">提交专业复核</span>
              <span className="ml-2 text-xs text-ink-muted">依据：用户转述，未经核实</span>
              <span className="mt-0.5 block text-ink-secondary">
                {dispositionOutcome('escalated_to_professional', 'user_reported')}
                {' '}这条不关闭事项，也不会把您转述的意见写成已验证的专业记录。
                {' '}{NO_CLINICIAN_NOTICE}
              </span>
            </span>
          </label>

          {/* 服务端会拒绝的专业依据:显示为不可用并说明原因,而不是让用户去撞一次 409。 */}
          <div className="rounded-lg border border-border bg-surface-alt px-3 py-2 opacity-70">
            <p className="font-medium">已生效的专业复核决定（当前不可用）</p>
            <p className="mt-0.5 text-ink-secondary">
              这条依据要求先有一条属于本事项、且已生效的真实专业复核决定。
              {NO_CLINICIAN_NOTICE}
            </p>
          </div>
        </fieldset>

        {mode === 'accepted_monitoring' && (
          <div className="space-y-2 rounded-lg bg-surface-alt px-3 py-2">
            <p className="text-ink-secondary">
              这一步只记下「持续跟进」这个处置，不会同时排一个复核时间：
              服务端会把安排如实记成「尚无可信依据的复核时间或触发条件；这是一项待确认的安排」，
              不会替您编一个复查周期。
            </p>
            <p className="text-xs text-ink-muted">
              要排时间或触发条件，用下方「下一次跟进」——那里记录的是白名单触发条件，
              而且排了期不等于已确认：确认要在那里单独做一次。
            </p>
          </div>
        )}

        <label className="block">
          <span className="text-xs text-ink-muted">备注（可选，会写进历史）</span>
          <textarea className={inputClass} rows={2} value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="例如：已与药师电话确认，按记录继续观察" />
        </label>

        <p className="rounded-lg bg-surface-alt px-3 py-2 text-ink-secondary">
          {dispositionOutcome(mode, mode === 'escalated_to_professional'
            ? 'user_reported' : 'deterministic_check_completed')}
        </p>

        <button type="button" className={`${buttonClass} inline-flex items-center gap-1`}
          disabled={busy || (mode === 'resolved_with_basis' && !canClose)}
          onClick={() => void submit()}>
          <FileSearch size={13} aria-hidden />
          {busy ? '提交中…' : '记录这次处置'}
        </button>

        {error && (
          <div role="alert" className="rounded-lg border border-danger/30 bg-danger-soft/40 p-3">
            <p className="font-medium text-danger">服务端没有接受这次处置，下面是它的原话：</p>
            <p className="mt-1 whitespace-pre-wrap text-ink">{error}</p>
            {trace && <p className="mt-1 font-mono text-xs text-ink-muted">追踪号 {trace}</p>}
          </div>
        )}
        {done && <p role="status" className="text-primary-strong">{done}</p>}
        <p className="text-xs text-ink-muted">
          处置的操作者由服务端从认证上下文取，请求体里的身份不会被读取。
          无论选哪一种，服务端都会拒绝拿旧版本的检查结果批准当前状态；被拒绝时上面的原话会说明原因。
        </p>
      </div>
    </Card>
  );
}
