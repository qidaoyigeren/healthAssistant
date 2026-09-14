/**
 * 一次**回访**(路由 `/safety/:caseId/visit`)。
 *
 * 按用户真正要问的顺序推进,五步各回答一个问题:
 *   1 这次为什么需要跟进 → 2 上次之后已记录的变化 → 3 当前最需要回答的问题 →
 *   4 回答之后的实际进展 → 5 本次结果及下一次安排。
 *
 * 做成**子路由**是有意的:回访是一个可寻址的东西,"中途离开回来继续同一次回访"
 * 就是回到同一个 URL。服务端上一访没结束就接着走(不新开),所以刷新/后退都不会
 * 把用户已经答过的问题变回第一题。
 *
 * 三条贯穿全页的诚实规则:
 *  1. 每条结论都带**依据类型**(程序核对 / 用户报告 / 模型解释 / 权威记录),不混成
 *     一句"系统认为"。
 *  2. **没有新记录 ≠ 情况稳定**。服务端送回的原话照抄,界面不替它下判断。
 *  3. 变更候选在**确认之前**一个字节都不写。确认的人要能看出这条是"你说的"还是
 *     "模型猜的"——所以来源永远显示。
 */
import React, { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Info } from 'lucide-react';
import { api } from '../../api/client';
import { newIdempotencyKey } from '../../api/http';
import { qk } from '../../api/queryKeys';
import type {
  SafetyCaseDto, SafetyChangeCandidateDto, SafetyChangeNoteDto, SafetyVisitStatementDto,
} from '../../api/types';
import { Badge, Card, ErrorState, SkeletonList, TimeText } from '../../components/ui';
import { inputClass, buttonClass } from '../materials/MaterialsPage';
import { AnswerPanel } from './CaseCard';
import { loadCase } from './fixtureBridge';
import {
  CANDIDATE_SOURCE_LABELS, CHANGE_FIELD_LABELS, basisKindLabel, candidateOperationLabel,
  candidateSourceLabel, changeFieldLabel, noteStatusLabel,
  serverMessage, timeBasisLabel, visitReasonLabel, visitStatusLabel,
} from './labels';

export function ReviewVisitPage(): React.ReactElement {
  const params = useParams<{ caseId: string }>();
  const caseId = params.caseId ?? '';
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [started, setStarted] = useState(false);

  const detail = useQuery({
    queryKey: qk.safetyCase(caseId),
    queryFn: ({ signal }) => loadCase(caseId, signal),
    enabled: caseId.length > 0,
    // 回访常常在等一次调查跑完：轮询到它停下来为止,而不是让用户手动刷新。
    refetchInterval: (query) => {
      const visit = (query.state.data as SafetyCaseDto | undefined)?.visit;
      return visit && visit.status === 'open' ? 3_000 : 15_000;
    },
  });

  const view = detail.data;
  const visit = view?.visit ?? null;

  async function withKey(action: (key: string) => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await action(newIdempotencyKey());
      await client.invalidateQueries({ queryKey: qk.safetyCase(caseId) });
      await client.invalidateQueries({ queryKey: qk.safetyCases });
      await client.invalidateQueries({ queryKey: qk.careTasks });
    } catch (caught) {
      setError(serverMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!caseId) return <ErrorState error="链接里没有事项编号。" title="链接不完整" />;
  if (detail.isLoading) return <SkeletonList rows={4} />;
  if (detail.isError || !view) {
    return <ErrorState error={detail.error} title="这件事项读取失败"
      onRetry={() => void detail.refetch()} />;
  }

  const startOrContinue = () => withKey(async (key) => {
    setStarted(true);
    await api.safetyCaseStartVisit(caseId, {
      key, expected_revision: view.revision,
    });
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to={`/safety/${encodeURIComponent(caseId)}`}
          className="inline-flex items-center gap-1 text-sm text-ink-secondary hover:underline">
          <ArrowLeft className="h-4 w-4" aria-hidden /> 回到这件事项
        </Link>
      </div>

      <header className="space-y-1">
        <h1 className="text-xl font-semibold">继续本次跟进</h1>
        <p className="text-sm text-ink-muted">
          {visit
            ? <>第 {visit.first_visit ? '一' : '若干'} 次回访 · 开始于 <TimeText iso={visit.opened_at} /> ·
              {' '}{visitStatusLabel(visit.status)}</>
            : '这件事项还没有开始过回访。'}
        </p>
      </header>

      {!visit && (
        <Card>
          {view.next_visit_reason?.detail && (
            <p className="text-sm text-ink-secondary" data-visit-reason="pending">
              <span className="font-medium">这次为什么需要跟进：</span>
              {view.next_visit_reason.detail}
              <span className="ml-1 text-xs text-ink-muted">
                （起因类型：{visitReasonLabel(view.next_visit_reason.kind)}，由服务端按事实判定）
              </span>
            </p>
          )}
          <p className="mt-2 text-sm text-ink-secondary">
            开始一次回访：系统会先说明上次之后记录里多了什么，再把当前最需要确认的
            一两件事摆出来。信息已经足够时，它不会硬造问题。
          </p>
          <button type="button" className={`${buttonClass} mt-3`}
            disabled={busy} onClick={() => void startOrContinue()}>
            {busy ? '正在准备…' : '开始回访'}
          </button>
        </Card>
      )}

      {visit && (
        <>
          {visit.status === 'open' && (
            <Card>
              <p className="text-sm text-ink-secondary">
                {started || visit.task_status === 'queued' || visit.task_status === 'running'
                  ? '正在整理这次回访的重点…'
                  : '这次回访还没有开始推进。'}
              </p>
              <button type="button" className={`${buttonClass} mt-3`}
                disabled={busy} onClick={() => void startOrContinue()}>
                {busy ? '正在推进…' : '继续本次跟进'}
              </button>
            </Card>
          )}
          {visit.status === 'blocked' && (
            <Card>
              <p role="alert" className="text-sm text-danger">
                这次回访没有跑成，所以**没有**产生任何结论。已保存的记录仍然有效。
              </p>
              <button type="button" className={`${buttonClass} mt-3`}
                disabled={busy} onClick={() => void startOrContinue()}>
                再试一次
              </button>
            </Card>
          )}

          <StepOne view={view} />
          <StepTwo view={view} />
          <StepThree view={view} />
          <StepFour view={view} />
          <StepFive view={view} busy={busy} caseId={caseId} onAction={withKey} />
        </>
      )}

      {error && (
        <div role="alert" className="space-y-1 rounded-lg border border-danger/30 bg-danger-soft/40 p-3 text-sm">
          <p className="font-medium text-danger">服务端没有接受这次操作，下面是它的原话：</p>
          <p className="whitespace-pre-wrap text-ink">{error}</p>
        </div>
      )}
    </div>
  );
}

function StatementList({ title, lines, empty }: {
  title: string; lines: SafetyVisitStatementDto[]; empty: string;
}): React.ReactElement {
  return (
    <div>
      <h3 className="text-sm font-medium">{title}</h3>
      {lines.length === 0
        ? <p className="mt-1 text-sm text-ink-muted">{empty}</p>
        : (
          <ul className="mt-1 space-y-1">
            {lines.map((line, index) => (
              <li key={`${index}-${line.text}`} className="text-sm text-ink-secondary"
                  data-basis={line.basis?.kind ?? 'unknown'}>
                {line.text}
                <span className="ml-1 text-xs text-ink-muted">
                  （{basisKindLabel(line.basis?.kind)}）
                </span>
              </li>
            ))}
          </ul>
        )}
    </div>
  );
}

function StepOne({ view }: { view: SafetyCaseDto }): React.ReactElement {
  const visit = view.visit!;
  return (
    <Card>
      <h2 className="text-base font-medium">1 · 这次为什么需要跟进</h2>
      <p className="mt-1 text-sm text-ink-secondary">{visit.reason?.detail}</p>
      <p className="mt-1 text-xs text-ink-muted">
        起因类型：{visitReasonLabel(visit.reason?.kind)}
        {visit.reason?.kind === 'record_change' || visit.reason?.kind === 'due'
          ? '（这类起因由服务端按事实判定，不是界面猜的）' : ''}
      </p>
      {visit.first_visit && (
        <p className="mt-2 text-xs text-ink-muted">
          这是这件事项的第一次回访，所以下面没有"相对上次"的比较基准。
        </p>
      )}
    </Card>
  );
}

function StepTwo({ view }: { view: SafetyCaseDto }): React.ReactElement {
  const result = view.visit?.result;
  return (
    <Card>
      <h2 className="text-base font-medium">2 · 上次之后已记录的变化</h2>
      {result
        ? <div className="mt-2"><StatementList title="" lines={result.since_last} empty="—" /></div>
        : <p className="mt-1 text-sm text-ink-muted">这次回访还没有跑出结果。</p>}
      <p className="mt-2 flex items-start gap-1 text-xs text-ink-muted">
        <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
        「系统尚未收到新记录」说的是**系统的信息状态**，不等于情况没有变化，
        也不表示风险已经解除。
      </p>
    </Card>
  );
}

function StepThree({ view }: { view: SafetyCaseDto }): React.ReactElement {
  const visit = view.visit!;
  const focusIds = new Set(visit.focus.map((item) => item.request_id));
  const focused = view.required_inputs.filter((item) => focusIds.has(item.request_id));
  const open = focused.length > 0 ? focused : view.required_inputs;
  return (
    <Card>
      <h2 className="text-base font-medium">3 · 当前最需要回答的问题</h2>
      {open.length === 0
        ? (
          <p className="mt-1 text-sm text-ink-secondary">
            这次没有需要您补充的问题——已有信息足够形成本次结果。
            这**不是**"没有问题"，只是不需要再占用您的时间。
          </p>
        )
        : (
          <>
            <p className="mt-1 text-xs text-ink-muted">
              默认只问最关键的 {Math.min(3, open.length)} 条，不要求您复述整个用药情况。
            </p>
            <div className="mt-2">
              <AnswerPanel view={{ ...view, required_inputs: open.slice(0, 3) }}
                followUpActions />
            </div>
          </>
        )}
      {open.length > 3 && (
        <p className="mt-2 text-xs text-ink-muted">另有 {open.length - 3} 条稍后再问。</p>
      )}
    </Card>
  );
}

function StepFour({ view }: { view: SafetyCaseDto }): React.ReactElement {
  const result = view.visit?.result;
  return (
    <Card>
      <h2 className="text-base font-medium">4 · 回答之后的实际进展</h2>
      {result
        ? (
          <div className="mt-1 space-y-2">
            <StatementList title="" lines={result.actions}
              empty="这次回访还没有产生已确认的变更。" />
            <p className="text-sm text-ink-secondary">
              已记录您 {result.answered_count} 条回答。
            </p>
          </div>
        )
        : <p className="mt-1 text-sm text-ink-muted">还没有可显示的进展。</p>}
    </Card>
  );
}

function StepFive({ view, busy, caseId, onAction }: {
  view: SafetyCaseDto; busy: boolean; caseId: string;
  onAction: (action: (key: string) => Promise<unknown>) => Promise<void>;
}): React.ReactElement {
  const visit = view.visit!;
  const result = visit.result;
  const arrangement = result?.next_arrangement ?? null;
  return (
    <Card>
      <h2 className="text-base font-medium">5 · 本次结果及下一次安排</h2>

      {result
        ? (
          <div className="mt-2 space-y-3">
            <section data-visit-section="reused">
              <StatementList title="复用的已有信息" lines={result.reused ?? []}
                empty="本次没有可复用的已有结论。" />
            </section>
            <section data-visit-section="recheck">
              <StatementList title="需要重新核对" lines={result.recheck ?? []}
                empty="没有依据需要重新核对。" />
            </section>
            <GroupStatements view={view} />
            <StatementList title="仍未解决" lines={result.unresolved}
              empty="这次没有留下未解决的问题。" />
            <section data-visit-section="why-ended">
              <h3 className="text-sm font-medium">本次为什么结束或等待</h3>
              <p className="mt-1 text-sm text-ink-secondary" data-end-reason>
                {result.end_reason ?? '—'}
              </p>
            </section>
            {result.next_step && (
              <p className="text-sm text-ink-secondary">下一步：{result.next_step}</p>
            )}
            <div>
              <h3 className="text-sm font-medium">下一次跟进安排</h3>
              {arrangement?.present
                ? (
                  <p className="mt-1 text-sm text-ink-secondary">
                    {arrangement.at ? <>时间 <TimeText iso={arrangement.at} />；</> : null}
                    调度状态：{arrangement.schedule_state ?? '未登记'}；
                    {arrangement.confirmed
                      ? ' 已确认（有确认记录）。'
                      : ' **尚未确认**——已安排不等于有人确认过。'}
                    {arrangement.blocked_reason
                      ? <> 阻塞原因：{arrangement.blocked_reason}</> : null}
                  </p>
                )
                : <p className="mt-1 text-sm text-ink-muted">{arrangement?.note
                  ?? '这件事项目前没有登记跟进安排。'}</p>}
            </div>
          </div>
        )
        : <p className="mt-1 text-sm text-ink-muted">这次回访还没有跑出结果。</p>}

      <SupplementSection view={view} busy={busy} caseId={caseId} onAction={onAction} />
    </Card>
  );
}

type NoteAction = (action: (key: string) => Promise<unknown>) => Promise<void>;

/** 一组换药的**逐条**陈述。刻意没有"换药已完成"这样的总结论。 */
function GroupStatements({ view }: { view: SafetyCaseDto }): React.ReactElement | null {
  const groups = view.visit?.result?.groups ?? [];
  if (groups.length === 0) return null;
  return (
    <section data-visit-section="groups" className="space-y-2">
      <h3 className="text-sm font-medium">换药：一组有关联的变更</h3>
      {groups.map((group) => (
        <ul key={group.group_id} className="space-y-1">
          {group.statements.map((line, index) => (
            <li key={`${group.group_id}-${index}`} className="text-sm text-ink-secondary"
              data-group-statement={line.status}>
              · {line.text}
            </li>
          ))}
        </ul>
      ))}
      <p className="text-xs text-ink-muted">
        这里逐条说明每一项各自到哪一步了——「旧药已经停用」和「新药还没开始」是两件事，
        各自如实说，不合并成一句总结论。
      </p>
    </section>
  );
}

/** 用户说了、但**还没发生**的事。它们不在可确认列表里。 */
function Plans({ note }: { note: SafetyChangeNoteDto }): React.ReactElement | null {
  const plans = note.plans ?? [];
  if (plans.length === 0) return null;
  return (
    <div className="mt-2 rounded-md border border-line bg-surface-alt/40 p-2"
      data-note-plans>
      <p className="text-xs font-medium text-ink-secondary">您提到但还没发生的事</p>
      <ul className="mt-1 space-y-1">
        {plans.map((plan, index) => (
          <li key={index} className="text-xs text-ink-secondary">
            {plan.target?.name} · {candidateOperationLabel(plan.operation)}
            {plan.time?.text ? `（${plan.time.text}）` : ''}
            {plan.time?.basis === 'reported_vague'
              ? '——只说了个大概，没有记成具体日期' : ''}
            <span className="ml-1 text-ink-muted">· {plan.quote}</span>
          </li>
        ))}
      </ul>
      <p className="mt-1 text-xs text-ink-muted">
        计划**不会**被写成已经发生。等您回来说"已经开始了"，系统会关联到这条计划，
        再按当时的记录重新核对一次。
      </p>
    </div>
  );
}

function Questions({ note }: { note: SafetyChangeNoteDto }): React.ReactElement | null {
  const questions = note.questions ?? [];
  if (questions.length === 0) return null;
  return (
    <div className="mt-2 rounded-md border border-caution/40 bg-caution-soft/30 p-2"
      data-note-questions>
      <p className="text-xs font-medium">为了不猜，这里需要您补一句：</p>
      <ul className="mt-1 space-y-1">
        {questions.map((item, index) => (
          <li key={index} className="text-sm text-ink-secondary" data-note-question>
            {item.text}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-xs text-ink-muted">
        这一条**没有**变成待确认的变更——对象或信息还不确定时，系统宁可问，不猜。
      </p>
    </div>
  );
}

function Unsupported({ note }: { note: SafetyChangeNoteDto }): React.ReactElement | null {
  const items = note.unsupported ?? [];
  if (items.length === 0) return null;
  return (
    <div className="mt-2 rounded-md border border-line p-2" data-note-unsupported>
      <p className="text-xs font-medium">这几件事系统还表达不了，没有静默丢掉：</p>
      <ul className="mt-1 space-y-1">
        {items.map((item, index) => (
          <li key={index} className="text-xs text-ink-secondary">
            {item.what}<span className="ml-1 text-ink-muted">（原话：{item.quote}）</span>
            <span className="ml-1">{item.guidance}</span>
          </li>
        ))}
      </ul>
      <Link to="/medications" className="mt-1 inline-block text-xs text-primary underline">
        去「用药记录」页登记
      </Link>
    </div>
  );
}

/** 一条「补充情况」：原话 + 对它的理解。 */
function NoteCard({ note, busy, onRetry }: {
  note: SafetyChangeNoteDto; busy: boolean; onRetry: () => void;
}): React.ReactElement {
  const reading = note.reading ?? null;
  return (
    <li className="rounded-lg border border-line p-3" data-change-note={note.status}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={note.status === 'failed' || note.status === 'unavailable' ? 'caution' : 'neutral'}>
          {noteStatusLabel(note.status)}
        </Badge>
        <TimeText iso={note.received_at} />
      </div>
      <p className="mt-1 text-sm" data-note-text>「{note.text || '（只点了状态按钮）'}」</p>

      {reading?.summary && note.status === 'interpreted' && (
        <p className="mt-1 text-sm text-ink-secondary" data-note-summary>
          系统理解到：{reading.summary}
        </p>
      )}
      {note.error && (
        <p className="mt-1 text-xs text-danger" data-note-error>{note.error}</p>
      )}
      {note.status === 'unavailable' && (
        <p className="mt-1 text-xs text-ink-muted">
          原文已经保存下来了。您可以在下面直接登记结构化变更，或者稍后再重试。
        </p>
      )}
      {note.status === 'interpreted' && (
        <p className="mt-1 text-xs text-ink-muted">
          下面这些是**待确认的解释**，不是已经写进记录的事实。
        </p>
      )}
      <Plans note={note} />
      <Questions note={note} />
      <Unsupported note={note} />
      {(note.status === 'failed' || note.status === 'unavailable') && (
        <button type="button" className="mt-2 text-xs text-ink-secondary underline"
          disabled={busy} onClick={onRetry}>
          {note.status === 'failed' ? '重试这次理解' : '重新尝试自动理解'}
        </button>
      )}
      {note.usage && (
        <p className="mt-1 text-xs text-ink-muted" data-note-usage>
          本次理解调用了 {note.usage.calls ?? '未知'} 次模型
          {note.usage.tokens === null || note.usage.tokens === undefined
            ? '；用量未测到（不是 0）'
            : `，用量 ${note.usage.tokens} tokens`}
          {note.model?.model ? `（${note.model.model}）` : ''}
        </p>
      )}
    </li>
  );
}

/** 「补充情况」：一句自然语言 → 待确认候选。 */
function SupplementSection({ view, busy, caseId, onAction }: {
  view: SafetyCaseDto; busy: boolean; caseId: string; onAction: NoteAction;
}): React.ReactElement {
  const visit = view.visit!;
  const notes = visit.change_notes ?? [];
  const [text, setText] = useState('');
  const [open, setOpen] = useState(false);

  return (
    <div className="mt-4 border-t border-line pt-3">
      <h3 className="text-sm font-medium">补充情况</h3>
      <p className="mt-1 text-xs text-ink-muted">
        直接用您自己的话说就行，不必先想清楚该填哪一格。系统的理解会先摆出来给您核对，
        **确认之前当前药单一个字节都不会变**。
      </p>

      {notes.length > 0 && (
        <ul className="mt-2 space-y-2" data-change-notes>
          {notes.map((note) => (
            <NoteCard key={note.id} note={note} busy={busy}
              onRetry={() => void onAction((key) => api.safetyCaseRetryNote(
                caseId, visit.visit_id, note.id, { key }))} />
          ))}
        </ul>
      )}

      <div className="mt-2 space-y-2">
        <textarea className={`${inputClass} w-full`} rows={3} value={text}
          data-note-input
          placeholder="例如：这两天药乙改成每天两次了；药甲上周就停了"
          onChange={(event) => { setText(event.target.value); setOpen(true); }} />
        <div className="flex flex-wrap gap-2">
          {['还没做', '暂不回答', '情况有变化'].map((label) => (
            <button key={label} type="button"
              className="rounded-md border border-line px-2 py-1 text-xs"
              data-note-hint={label}
              disabled={busy}
              onClick={() => void onAction((key) => api.safetyCaseSubmitNote(
                caseId, visit.visit_id, { key, text: '', speech_act: label }))}>
              {label}
            </button>
          ))}
        </div>
        <div className="flex gap-2">
          <button type="button" className={buttonClass} data-note-submit
            disabled={busy || !text.trim()}
            onClick={() => void onAction((key) => api.safetyCaseSubmitNote(
              caseId, visit.visit_id, { key, text }))
              .then(() => { setText(''); setOpen(false); })}>
            提交，让系统先理解一下
          </button>
          {open && (
            <button type="button" className="text-sm text-ink-secondary underline"
              onClick={() => { setText(''); setOpen(false); }}>清空</button>
          )}
        </div>
      </div>

      <ChangeCandidates view={view} busy={busy} caseId={caseId} onAction={onAction} />
    </div>
  );
}

function ChangeCandidates({ view, busy, caseId, onAction }: {
  view: SafetyCaseDto; busy: boolean; caseId: string; onAction: NoteAction;
}): React.ReactElement {
  const visit = view.visit!;
  const pending = visit.pending_candidates ?? [];
  const conflicting = pending.filter((item) => item.conflict).length;
  const [declaring, setDeclaring] = useState(false);
  const [form, setForm] = useState({ name: '', field: 'dose', value: '' });
  const meds = useMemo(() => view.medications
    .map((item) => (item as { display_name?: string }).display_name)
    .filter((name): name is string => Boolean(name)), [view.medications]);

  return (
    <div className="mt-4 border-t border-line pt-3">
      <h3 className="text-sm font-medium">待确认的用药变更</h3>

      {pending.length === 0
        ? <p className="mt-1 text-sm text-ink-muted">现在没有待确认的变更。</p>
        : (
          <ul className="mt-2 space-y-2" data-pending-candidates>
            {pending.map((candidate) => (
              <CandidateRow key={candidate.id} candidate={candidate} busy={busy}
                onConfirm={() => void onAction((key) => api.safetyCaseConfirmChange(
                  caseId, visit.visit_id, candidate.id, { key }))}
                onConfirmGroup={() => void onAction((key) => api.safetyCaseConfirmGroup(
                  caseId, visit.visit_id, candidate.id, { key }))}
                onDismiss={() => void onAction((key) => api.safetyCaseDismissChange(
                  caseId, visit.visit_id, candidate.id, { key }))}
                onEdit={() => {
                  const changes = candidate.changes ?? {};
                  const field = Object.keys(changes)[0] ?? 'dose';
                  setForm({
                    name: candidate.target?.name ?? candidate.name ?? '',
                    field,
                    value: String(changes[field] ?? candidate.after ?? ''),
                  });
                  setDeclaring(true);
                }} />
            ))}
          </ul>
        )}
      {conflicting > 0 && (
        <p className="mt-2 text-xs text-danger" data-conflict-note>
          有 {conflicting} 条候选依据的记录已经变了。**没有**任何东西被覆盖；
          请按现在记录的样子重新核对一遍。
        </p>
      )}

      <p className="mt-2 text-xs text-ink-muted">
        确认后走既有的用药变更入口写入，必要安全检查按原有路径重新排队。
        未提及的字段保持原值，不会被清空。
      </p>

      {declaring
        ? (
          <div className="mt-2 space-y-2">
            <div className="flex flex-wrap gap-2">
              <select className={`${inputClass} md:w-48`} value={form.name}
                data-structured-name
                onChange={(event) => setForm({ ...form, name: event.target.value })}>
                <option value="">选择药物…</option>
                {meds.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <select className={`${inputClass} md:w-40`} value={form.field}
                onChange={(event) => setForm({ ...form, field: event.target.value })}>
                {Object.entries(CHANGE_FIELD_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
              <input className={`${inputClass} md:w-40`} value={form.value}
                placeholder="新值" data-structured-value
                onChange={(event) => setForm({ ...form, value: event.target.value })} />
            </div>
            <div className="flex gap-2">
              <button type="button" className={buttonClass} data-structured-submit
                disabled={busy || !form.name || !form.value.trim()}
                onClick={() => void onAction((key) => api.safetyCaseProposeChange(
                  caseId, visit.visit_id,
                  { key, name: form.name, field: form.field, value: form.value }))
                  .then(() => { setForm({ name: '', field: 'dose', value: '' }); setDeclaring(false); })}>
                记为待确认的变更
              </button>
              <button type="button" className="text-sm text-ink-secondary underline"
                onClick={() => setDeclaring(false)}>取消</button>
            </div>
          </div>
        )
        : (
          <button type="button" className="mt-2 text-xs text-ink-secondary underline"
            data-structured-open
            onClick={() => setDeclaring(true)}>
            我这边用药有变化，直接填一条待确认的变更
          </button>
        )}
    </div>
  );
}

function CandidateRow({ candidate, busy, onConfirm, onConfirmGroup, onDismiss, onEdit }: {
  candidate: SafetyChangeCandidateDto; busy: boolean;
  onConfirm: () => void; onConfirmGroup: () => void;
  onDismiss: () => void; onEdit: () => void;
}): React.ReactElement {
  // 新旧两种候选形状都能显示：字段形态的候选（结构化声明）没有 operation。
  const operation = candidate.operation ?? 'dose_change';
  const changes = candidate.changes ?? (
    candidate.field ? { [candidate.field]: candidate.after } : {});
  const before = typeof candidate.before === 'object' && candidate.before !== null
    ? candidate.before as Record<string, unknown>
    : (candidate.field ? { [candidate.field]: candidate.before } : {});
  const occurred = candidate.occurred ?? null;
  const grouped = Boolean(candidate.group?.id);

  return (
    <li className="rounded-lg border border-line p-3"
      data-candidate={candidate.id} data-candidate-operation={operation}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium" data-candidate-title>
          {candidate.target?.name ?? candidate.name} · {candidateOperationLabel(operation)}
        </span>
        <Badge tone={candidate.source === 'user_declared' ? 'neutral' : 'caution'}>
          {candidateSourceLabel(candidate.source)}
        </Badge>
        {grouped && (
          <Badge tone="neutral">
            {candidate.group?.role === 'replace_from' ? '换出' : '换入'}
          </Badge>
        )}
      </div>

      <ul className="mt-1 space-y-0.5">
        {Object.entries(changes).map(([field, value]) => (
          <li key={field} className="text-sm text-ink-secondary" data-candidate-change={field}>
            {changeFieldLabel(field)}：
            <span className="text-ink-muted">
              {before[field] === null || before[field] === undefined
                ? '（未记录）' : String(before[field])}
            </span>
            {' → '}
            <span className="font-medium">{String(value)}</span>
          </li>
        ))}
      </ul>

      <p className="mt-1 text-xs text-ink-muted" data-candidate-time>
        发生时间：{occurred?.text ?? (occurred?.value ?? '未提供')}
        （{timeBasisLabel(occurred?.basis)}）
      </p>
      {candidate.reported_overlap !== null && candidate.reported_overlap !== undefined && (
        <p className="text-xs text-ink-muted">
          {candidate.reported_overlap
            ? '您报告过这两种药有一段时间同时服用。'
            : '您报告过没有同时服用。'}
        </p>
      )}
      {candidate.basis?.quote && (
        <p className="mt-1 text-xs text-ink-muted">原话依据：{candidate.basis.quote}</p>
      )}

      {candidate.conflict && (
        <div className="mt-2 rounded-md border border-danger/40 bg-danger-soft/40 p-2"
          data-candidate-conflict>
          <p className="text-xs text-danger">
            记录已经变了，这一条**没有**被写进去：{candidate.conflict.detail}
          </p>
          {candidate.conflict.current && (
            <p className="mt-1 text-xs text-ink-secondary">
              现在记录里是：{Object.entries(candidate.conflict.current)
                .filter(([key]) => ['name', 'status', 'dose', 'schedule', 'route'].includes(key))
                .map(([key, value]) => `${key}=${String(value ?? '未记录')}`).join('，')}
            </p>
          )}
          {candidate.conflict.action && (
            <p className="mt-1 text-xs text-ink-muted">{candidate.conflict.action}</p>
          )}
        </div>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" className={buttonClass} data-candidate-confirm
          disabled={busy} onClick={onConfirm}>确认写入</button>
        {grouped && (
          <button type="button" className={buttonClass} data-candidate-confirm-group
            disabled={busy} onClick={onConfirmGroup}>
            连同这一组一起确认
          </button>
        )}
        <button type="button" className="text-sm text-ink-secondary underline disabled:opacity-50"
          data-candidate-edit disabled={busy} onClick={onEdit}>修改</button>
        <button type="button" className="text-sm text-ink-secondary underline disabled:opacity-50"
          data-candidate-dismiss disabled={busy} onClick={onDismiss}>取消这条</button>
      </div>
      <p className="mt-1 text-xs text-ink-muted">
        {CANDIDATE_SOURCE_LABELS[candidate.source] ?? candidate.source}
        {candidate.source === 'model_proposed'
          ? '：这条是模型从您的话里读出来的**待确认解释**，写进去之前请核对一遍。'
          : '：这条是您自己登记的。'}
      </p>
    </li>
  );
}
