/**
 * 安全事项卡片。
 *
 * 三层必须看得出区别,这是这张卡存在的理由:
 *   (a) 程序判定 —— 已记录的检查结论与它们的来源(可核对);
 *   (b) 模型调查 —— 系统做过什么调查(模型判断不是依据);
 *   (c) 需要人确认 —— 只有人或专业人员能回答的部分。
 * 三层以左侧色条 + 文字徽标共同区分,不靠颜色单独表义。
 */
import React, { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import {
  ExternalLink, Eye, FileSearch, HelpCircle, Landmark, Pill, UserCheck,
} from 'lucide-react';
import { api } from '../../api/client';
import { newIdempotencyKey } from '../../api/http';
import { qk } from '../../api/queryKeys';
import type {
  CareTaskDto, SafetyCaseDto, SafetyConclusionDto, SafetyRequiredInputDto,
} from '../../api/types';
import { Badge, TimeText } from '../../components/ui';
import { inputClass, buttonClass } from '../materials/MaterialsPage';
import { RunProgressLine } from '../shared/runProgress';
import {
  MONITORING_NOTICE, NO_CLINICIAN_NOTICE, UNKNOWN_ANSWER_NOTICE, caseTypeLabel,
  followUpOf, followUpText, partyActionLabel, serverMessage, statusTone,
  triggerStateLabel, triggerText,
} from './labels';

export function CaseCard({ view, footer }: {
  view: SafetyCaseDto;
  footer?: React.ReactNode;
}): React.ReactElement {
  const openInputs = view.required_inputs.filter((item) => item.status === 'open');
  const mine = openInputs.filter((item) => !item.for_professional);
  const theirs = openInputs.filter((item) => item.for_professional);
  // "用户说不知道"不是"还在等他答",也不是"答完了":它是一条**仍未解决**的缺口。
  const unknownInputs = view.required_inputs.filter(
    (item) => item.needs_alternative_evidence || item.status === 'unknown');
  const followUp = view.status === 'monitoring' ? followUpOf(view) : null;
  return (
    <article className="rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        <Badge tone={statusTone(view.status)}>{view.status_label}</Badge>
        <span className="font-medium">{caseTypeLabel(view.case_type)}</span>
        <span className="text-xs text-ink-muted">
          最近更新 <TimeText iso={view.updated_at} />
        </span>
        <Link to={`/safety/${encodeURIComponent(view.case_id)}`}
          className="ml-auto text-xs text-primary underline">
          详情、调查进展与历史
        </Link>
      </div>

      <div className="space-y-3 px-4 py-3 text-sm">
        <div>
          <p className="text-xs text-ink-muted">为什么会出现这件事</p>
          <p className="mt-0.5">{triggerText(view.trigger)}</p>
        </div>

        {view.status === 'monitoring' && (
          <div className="rounded-lg border border-caution/30 bg-caution-soft/40 px-3 py-2">
            <p className="text-caution">{MONITORING_NOTICE}</p>
            <p className="mt-1 text-ink-secondary">跟进安排：{followUpText(followUp)}</p>
          </div>
        )}

        {(view.medications.length > 0 || view.facts.length > 0) && (
          <div>
            <p className="text-xs text-ink-muted">涉及的记录</p>
            <ul className="mt-1 flex flex-wrap gap-1.5">
              {view.medications.map((item) => (
                <RefChip key={item.ref} label={item.label} available={item.available} icon={<Pill size={12} aria-hidden />} />
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

        <Layer tone="primary" icon={<Landmark size={13} aria-hidden />}
          title="程序判定" note="来自已记录的必要检查结论与它们的来源">
          {view.conclusions.filter((item) => item.available).length === 0 ? (
            <p className="text-ink-muted">
              这件事还没有可展示的检查结论。没有结论不等于没有风险，只表示程序还没查到这里。
            </p>
          ) : (
            <ul className="space-y-2">
              {view.conclusions.map((item) => (
                <ConclusionLine key={item.ref} conclusion={item} />
              ))}
            </ul>
          )}
        </Layer>

        <Layer tone="neutral" icon={<FileSearch size={13} aria-hidden />}
          title="模型调查" note="系统围绕这件事做过的调查；模型判断不是依据">
          {view.linked_run_ids.length === 0 ? (
            <p className="text-ink-muted">还没有围绕这件事展开过调查。</p>
          ) : (
            <ul className="space-y-2">
              {view.linked_run_ids.map((runId, index) => (
                <li key={runId}>
                  <p className="text-xs text-ink-muted">第 {index + 1} 次调查</p>
                  <RunProgressLine runId={runId} active={false}
                    emptyHint="这次调查没有可展示的进度事件（服务端未推送或未开启进度记录）。" />
                </li>
              ))}
            </ul>
          )}
        </Layer>

        <Layer tone="caution" icon={<UserCheck size={13} aria-hidden />}
          title="需要确认的事" note="这一层只能由您或专业人员给出答案">
          {mine.length === 0 && theirs.length === 0 && unknownInputs.length === 0
            && view.open_questions.length === 0 ? (
            <p className="text-ink-muted">目前没有待确认的问题。</p>
          ) : (
            <ul className="space-y-1.5">
              {mine.map((item) => (
                <li key={item.request_id}>
                  <span className="font-medium">需要您回答：</span>{item.question}
                </li>
              ))}
              {theirs.map((item) => (
                <li key={item.request_id}>
                  <span className="font-medium">需要专业人员回答：</span>{item.question}
                </li>
              ))}
              {unknownInputs.map((item) => (
                <li key={item.request_id}>
                  <Badge tone="caution" icon={<HelpCircle size={12} aria-hidden />}>
                    {UNKNOWN_ANSWER_NOTICE}
                  </Badge>{' '}
                  {item.question}
                </li>
              ))}
              {view.open_questions.map((question) => (
                <li key={question.question_id}>
                  <span className="font-medium">待确认：</span>
                  {question.question ?? question.field ?? '问题内容未记录。'}
                </li>
              ))}
            </ul>
          )}
          {view.responsible_party === 'professional' && (
            <p className="mt-2 rounded-lg bg-caution-soft px-2.5 py-2 text-xs text-caution">
              {NO_CLINICIAN_NOTICE}
            </p>
          )}
        </Layer>

        <p className="rounded-lg bg-surface-alt px-3 py-2">
          <span className="font-medium">现在需要{partyActionLabel(view.responsible_party, view.status)}：</span>
          {view.next_action_summary ?? '服务端未记录下一步。'}
        </p>
      </div>

      {footer && <div className="border-t border-border px-4 py-3">{footer}</div>}
    </article>
  );
}

// ---- 三层的外壳 --------------------------------------------------------------

type LayerTone = 'primary' | 'neutral' | 'caution';

function Layer({ tone, icon, title, note, children }: {
  tone: LayerTone;
  icon: React.ReactNode; title: string; note: string; children: React.ReactNode;
}): React.ReactElement {
  const rule: Record<LayerTone, string> = {
    primary: 'border-l-primary bg-primary-soft/40',
    neutral: 'border-l-border-strong bg-surface-alt',
    caution: 'border-l-caution bg-caution-soft/40',
  };
  return (
    <section className={`rounded-r-lg border-l-2 py-2 pl-3 pr-2 ${rule[tone]}`}>
      <p className="flex flex-wrap items-center gap-2">
        <Badge tone={tone} icon={icon}>{title}</Badge>
        <span className="text-xs text-ink-muted">{note}</span>
      </p>
      <div className="mt-1.5">{children}</div>
    </section>
  );
}

/** 触发条件状态的徽标配色。三种状态必须看得出区别,不靠颜色单独表义。 */
export function triggerStateTone(state: string | null | undefined):
'primary' | 'caution' | 'neutral' {
  if (state === 'trigger_eliminated') return 'primary';
  if (state === 'risk_present') return 'caution';
  return 'neutral';
}

function ConclusionLine({ conclusion }: { conclusion: SafetyConclusionDto }): React.ReactElement {
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
      <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs">
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
              className="inline-flex items-center gap-1 break-all text-primary underline">
              <ExternalLink size={11} aria-hidden />{source}
            </a>
          ) : (
            <span className="break-all font-mono text-ink-muted">{source}（应用内不直接读取）</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function RefChip({ label, available, icon }: {
  label: string; available: boolean; icon?: React.ReactNode;
}): React.ReactElement {
  return (
    <li className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${
      available ? 'border-border bg-surface-alt text-ink-secondary'
        : 'border-caution/30 bg-caution-soft text-caution'
    }`}>
      {icon}
      {available ? label : `${label}（读取不到）`}
    </li>
  );
}

// ---- 「需要您提供的信息」的作答区 ----------------------------------------------

/** 作答区提交成功后交给页面的**事实**,页面用它显示"补充之后发生了什么变化"。 */
export interface AnswerReceipt {
  requestId: string;
  /** 用户实际填写的内容(原样保留,便于在变化说明里回显)。 */
  value: string;
  /** 服务端返回的**新**事项视图。 */
  updated: SafetyCaseDto;
}

/**
 * 补充信息直接提交到事项本身(`POST /v1/safety-cases/{id}/answer`),不再依赖
 * 是否有一轮调查任务在跑——没有任务时也该能补上一条记录。
 *
 * 三条界面规则:
 *  1. **一次只问一条**。服务端按 request_id 逐条关闭,一次提交多条会把同一段
 *     内容记到每条问题上;所以这里只显示下一条待回答的问题。
 *  2. 提交失败时**保留已填内容**,并把服务端原话照抄出来;重试复用同一条内容,
 *     同一个内容 + 同一个事项版本复用同一个幂等键(重发不会变成第二笔补充)。
 *  3. 「我不知道」是**第三种**结果:不再追问这位用户,但问题**没有解决**、
 *     仍阻止关闭,改由系统从其他来源核实。它不能显示成"已回答"。
 */
/**
 * 回访里用户对一条**跟进行动**的五种表态。它们含义不同,不能合并:把"我还没做"
 * 和"我暂时不想说"都记成已答,用户下次回来会看到一件他其实没做过的事被标成做完了。
 */
const FOLLOW_UP_ACTIONS: { kind: 'done' | 'not_done' | 'unknown' | 'changed' | 'declined'; label: string }[] = [
  { kind: 'done', label: '已完成' },
  { kind: 'not_done', label: '尚未完成' },
  { kind: 'unknown', label: '不清楚' },
  { kind: 'changed', label: '情况有变化' },
  { kind: 'declined', label: '暂不回答' },
];

export function AnswerPanel({ view, onAnswered, followUpActions = false }: {
  view: SafetyCaseDto;
  onAnswered?: (receipt: AnswerReceipt) => void;
  /** 回访页打开它:多给一组"那件事做了没有"的表态按钮。 */
  followUpActions?: boolean;
}): React.ReactElement {
  const client = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [usedKey, setUsedKey] = useState('');
  /** 同一个 (问题, 内容, 事项版本) 复用同一个键:重试不会变成第二笔补充。 */
  const keys = useRef(new Map<string, string>());
  const mine = view.required_inputs.filter(
    (item) => item.status === 'open' && !item.for_professional);
  const theirs = view.required_inputs.filter(
    (item) => item.status === 'open' && item.for_professional);
  const current = mine[0];
  const value = current ? values[current.request_id] ?? '' : '';

  function keyFor(item: SafetyRequiredInputDto, text: string): string {
    const cacheKey = `${item.request_id}|${text}|${view.revision}`;
    const cached = keys.current.get(cacheKey);
    if (cached) return cached;
    const created = newIdempotencyKey();
    keys.current.set(cacheKey, created);
    return created;
  }

  /**
   * `kind` 只在用户**显式**点「这条我不知道」时传 `'unknown'`;普通提交不传,
   * 由服务端按内容判定——否则用户手写"不知道"也会被本页判成"有内容地回答"。
   */
  async function submit(item: SafetyRequiredInputDto, text: string,
                       kind?: 'unknown' | 'done' | 'not_done' | 'declined' | 'changed') {
    if (!text.trim()) return;
    const key = keyFor(item, kind ? `${kind}:${text}` : text);
    setBusy(true);
    setError('');
    try {
      const updated = await api.safetyCaseAnswer(view.case_id, {
        key,
        expected_revision: view.revision,
        request_id: item.request_id,
        value: text,
        ...(kind ? { answer_kind: kind } : {}),
      });
      setUsedKey('');
      setValues((all) => ({ ...all, [item.request_id]: '' }));
      onAnswered?.({ requestId: item.request_id, value: text, updated });
      await client.invalidateQueries({ queryKey: qk.safetyMainline });
      await client.invalidateQueries({ queryKey: qk.safetyCases });
      await client.invalidateQueries({ queryKey: qk.careTasks });
      await client.invalidateQueries({ queryKey: qk.overview });
    } catch (caught) {
      // 内容原样留在输入框里:服务端拒绝时用户不需要重打一遍。
      setError(serverMessage(caught));
      setUsedKey(text);
    } finally {
      setBusy(false);
    }
  }

  if (!current) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-ink-muted">
          {view.status === 'monitoring'
            ? '这件事已登记为持续跟进，当前没有需要您回答的问题（这不表示风险已经消失）。'
            : '当前没有需要您回答的问题。'}
        </p>
        {theirs.length > 0 && (
          <p className="text-sm text-caution">
            另有 {theirs.length} 条问题需要专业人员回答，不能由您代答。{NO_CLINICIAN_NOTICE}
          </p>
        )}
        {error && (
          <p role="alert" className="rounded-lg border border-danger/30 bg-danger-soft/40 p-3 text-sm text-danger">
            服务端没有接受这次补充，下面是它的原话：{error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-ink-muted">
        一次回答一条。服务端按问题逐条关闭，答完这条才会显示下一条
        {mine.length > 1 ? `（还有 ${mine.length - 1} 条待回答）` : ''}。
      </p>
      <div key={current.request_id}>
        <label className="block text-sm font-medium" htmlFor={`answer-${current.request_id}`}>
          {current.question}
        </label>
        {current.why_needed && (
          <p className="text-xs text-ink-muted">
            为什么需要这个信息，它会影响哪一步：{current.why_needed}
          </p>
        )}
        <div className="mt-1 flex flex-col gap-2 md:flex-row md:items-start">
          <input id={`answer-${current.request_id}`} className={inputClass}
            value={value}
            onChange={(event) => setValues((all) => ({
              ...all, [current.request_id]: event.target.value,
            }))}
            placeholder="请按您知道的实际情况填写" />
          <button type="button" className={`${buttonClass} shrink-0`}
            disabled={busy || !value.trim()}
            onClick={() => void submit(current, value)}>
            {busy ? '提交中…' : (usedKey && usedKey === value.trim())
              ? '重试提交（同一内容）' : '提交这一条'}
          </button>
        </div>
        <button type="button"
          className="mt-2 text-xs text-ink-secondary underline disabled:opacity-50"
          disabled={busy}
          onClick={() => void submit(current, '不知道', 'unknown')}>
          这条我不知道
        </button>
        <p className="mt-1 text-xs text-ink-muted">
          选这条表示您确实无法提供。服务端会停止再问您这条问题，但它仍然没有解决，
          也仍然阻止关闭事项，改由系统从其他来源核实。
        </p>
        {followUpActions && (
          <div className="mt-3 border-t border-line pt-3">
            <p className="text-xs font-medium text-ink-secondary">
              如果这是问您「那件事做了没有」，也可以直接选一种：
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {FOLLOW_UP_ACTIONS.map((action) => (
                <button key={action.kind} type="button"
                  className="rounded-lg border border-line px-3 py-1.5 text-xs text-ink-secondary hover:bg-surface-muted disabled:opacity-50"
                  disabled={busy}
                  onClick={() => void submit(current, action.label, action.kind)}>
                  {action.label}
                </button>
              ))}
            </div>
            <p className="mt-2 text-xs text-ink-muted">
              这五种**含义各不相同**，不能合并成「已解决」：只有「已完成」可能把这条问题
              答上（而且只对问「做了没有」的问题）；「尚未完成」「暂不回答」保持未决，
              「不清楚」改由系统找其他来源，「情况有变化」要走变更确认那条路。
            </p>
          </div>
        )}
      </div>
      {theirs.length > 0 && (
        <p className="text-sm text-caution">
          另有 {theirs.length} 条问题需要专业人员回答，不能由您代答。{NO_CLINICIAN_NOTICE}
        </p>
      )}
      {error && (
        <div role="alert" className="space-y-1 rounded-lg border border-danger/30 bg-danger-soft/40 p-3 text-sm">
          <p className="font-medium text-danger">服务端没有接受这次补充，下面是它的原话：</p>
          <p className="whitespace-pre-wrap text-ink">{error}</p>
          <p className="text-xs text-ink-secondary">
            您填写的内容还在上面，可以原样重试；也可以先刷新页面确认事项是否已被别处更新过。
          </p>
        </div>
      )}
      <p className="text-xs text-ink-muted">
        补充内容按「用户报告」保存，不构成临床审批，也不会关闭这件事；它只关掉您实际回答的那一条问题。
      </p>
    </div>
  );
}

/**
 * 开始一次有界调查。调查排到队列里由后台 worker 推进——没有 worker 时它
 * 只是排着，所以这里**不**说"正在调查"，只说已经排队。
 */
export function InvestigateButton({ view, activeTask }: {
  view: SafetyCaseDto; activeTask: CareTaskDto | null;
}): React.ReactElement {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [started, setStarted] = useState('');
  const running = !!activeTask && !['completed', 'cancelled', 'failed'].includes(activeTask.status);
  if (running) {
    return (
      <p className="text-xs text-ink-muted">
        这件事已经有一轮调查在进行或等待补充，不再重复排队。进展见上方「调查进展」与「照护待办」。
      </p>
    );
  }
  async function start() {
    setBusy(true);
    setError('');
    setStarted('');
    try {
      await api.safetyCaseInvestigate(view.case_id, { key: newIdempotencyKey() });
      setStarted('已排队一次调查。后台 worker 推进后，这里会显示进展与新的待确认问题。');
      await client.invalidateQueries({ queryKey: qk.safetyMainline });
      await client.invalidateQueries({ queryKey: qk.safetyCases });
      await client.invalidateQueries({ queryKey: qk.careTasks });
    } catch (caught) {
      setError(serverMessage(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-1">
      <button type="button" className={`${buttonClass} inline-flex items-center gap-1`}
        disabled={busy} onClick={() => void start()}>
        <FileSearch size={13} aria-hidden />
        {busy ? '排队中…' : '开始一次调查'}
      </button>
      {started && <p role="status" className="text-xs text-primary-strong">{started}</p>}
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </div>
  );
}

/** 「我看到了」:只写一个时间戳,不关闭、不改状态。 */
export function SeenButton({ view }: { view: SafetyCaseDto }): React.ReactElement {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (view.user_seen_at) {
    return (
      <p className="text-xs text-ink-muted">
        您已于 <TimeText iso={view.user_seen_at} /> 查看过这件事；查看不改变它的状态。
      </p>
    );
  }
  async function markSeen() {
    setBusy(true);
    setError('');
    try {
      await api.safetyCaseSeen(view.case_id, newIdempotencyKey());
      await client.invalidateQueries({ queryKey: qk.safetyMainline });
      await client.invalidateQueries({ queryKey: qk.safetyCases });
    } catch (caught) {
      setError(serverMessage(caught));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button type="button" className={`${buttonClass} inline-flex items-center gap-1`}
        disabled={busy} onClick={() => void markSeen()}>
        <Eye size={13} aria-hidden />
        {busy ? '记录中…' : '我看到了'}
      </button>
      <span className="text-xs text-ink-muted">只记录您看过的时间，不会关闭这件事。</span>
      {error && <span role="alert" className="text-xs text-danger">{error}</span>}
    </div>
  );
}
