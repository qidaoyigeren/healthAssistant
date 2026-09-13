/**
 * 长期跟进的**查看与操作**(CONTRACT.md §4)。
 *
 * 这一块要建立的核心区分是「**已安排**」与「**已确认**」不是一回事:
 * 填一个时间或触发条件只表示排了期;确认是另一次单独的操作,而且只能由
 * **确认端点**产生(`confirmed` 不接受请求体自称)。
 *
 * 另外三条:
 *  1. `kind`(按时间/按事件/仅备忘)与 `schedule_state`(走到哪了)分开显示;
 *  2. 个人提醒(owner=caregiver)与专业复核安排(owner=professional)分开说,
 *     后者必须带上"本项目没有连接真实医护服务"这句;
 *  3. 提交失败**保留已填内容**;版本冲突时给刷新入口,但不替用户清空输入。
 */
import React, { useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { CalendarClock, CalendarPlus, CheckCircle2, XCircle } from 'lucide-react';
import { newIdempotencyKey } from '../../api/http';
import { qk } from '../../api/queryKeys';
import {
  followUpCancel, followUpConfirmation, followUpSchedule,
} from './fixtureBridge';
import type {
  CareTaskDto, SafetyCaseDto, SafetyFollowUpConditionDto, SafetyFollowUpDto,
} from '../../api/types';
import { Badge, Card, LiveAnnouncement, TimeText } from '../../components/ui';
import { inputClass, buttonClass } from '../materials/MaterialsPage';
import {
  CANCEL_KEEPS_HISTORY_NOTICE, FOLLOW_UP_CONDITION_KIND_LABELS, MONITORING_NOTICE,
  NO_CLINICIAN_NOTICE, NOTHING_TO_CONFIRM_NOTICE, SCHEDULE_IS_NOT_CONFIRMATION_NOTICE,
  careTaskStatusLabel, confirmationIsRecorded, followUpAtText, followUpConditionText,
  followUpKindLabel, followUpOwnerText, isScheduledNotConfirmed, scheduleStateLabel,
  scheduleStateTone, serverMessage, traceId,
} from './labels';

type Mode = 'schedule' | 'confirm' | 'cancel';

/** 触发条件白名单的四个种类(§4.3)。未知种类服务端会 422,界面不给自由填写。 */
const CONDITION_KINDS = [
  'conclusion_recorded', 'necessary_check', 'medication_change', 'fact_change',
] as const;

/**
 * 一条安排现在能不能确认。
 * §4.6.3 的前置条件是"存在一条已安排(`schedule_state ∈ {scheduled, due}`)的安排";
 * 没有安排就没有可确认的东西,界面不让用户去撞一次必输的 409。
 */
function confirmability(followUp: SafetyFollowUpDto | null): {
  ok: boolean; reason: string | null;
} {
  if (!followUp) return { ok: false, reason: NOTHING_TO_CONFIRM_NOTICE };
  if (confirmationIsRecorded(followUp)) {
    return { ok: false, reason: '这条安排已经确认过了，不需要再确认一次。' };
  }
  const state = followUp.schedule_state;
  if (state === 'scheduled' || state === 'due') return { ok: true, reason: null };
  if (state === 'cancelled') {
    return { ok: false, reason: '这条安排已经取消，没有可确认的东西。要重新跟进请先重新安排。' };
  }
  if (state === 'triggered') {
    return { ok: false, reason: '这条安排已经触发过了，没有还在等确认的安排。' };
  }
  if (state === 'blocked') {
    return { ok: false, reason: '这条安排处于「执行受阻」，先处理受阻原因再确认。' };
  }
  if (state === 'unscheduled' || !state) {
    return { ok: false, reason: NOTHING_TO_CONFIRM_NOTICE };
  }
  return { ok: false, reason: `这条安排的调度状态是「${scheduleStateLabel(state)}」，没有可确认的东西。` };
}

export function FollowUpCard({ view, task }: {
  view: SafetyCaseDto; task: CareTaskDto | null;
}): React.ReactElement {
  const client = useQueryClient();
  const followUp = view.follow_up ?? null;
  const terminal = view.status === 'resolved';
  const canConfirm = confirmability(followUp);

  const [mode, setMode] = useState<Mode>(canConfirm.ok ? 'confirm' : 'schedule');
  const [kind, setKind] = useState<'review_at' | 'on_event' | 'arrangement'>(
    followUp?.kind === 'on_event' ? 'on_event'
      : followUp?.kind === 'arrangement' ? 'arrangement' : 'review_at');
  const [at, setAt] = useState('');
  const [conditionKind, setConditionKind] = useState<string>('conclusion_recorded');
  const [conditionRef, setConditionRef] = useState('');
  const [conditionExtra, setConditionExtra] = useState('');
  const [owner, setOwner] = useState(followUp?.owner ?? 'caregiver');
  const [note, setNote] = useState('');
  const [cancelReason, setCancelReason] = useState('');
  const [confirmNote, setConfirmNote] = useState('');
  const [confirmArmed, setConfirmArmed] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [trace, setTrace] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [done, setDone] = useState('');
  /** 同一个 (动作, 内容) 复用同一个幂等键:重试不会变成第二笔写入。 */
  const keys = useRef(new Map<string, string>());

  function keyFor(fingerprint: string): string {
    const cached = keys.current.get(fingerprint);
    if (cached) return cached;
    const created = newIdempotencyKey();
    keys.current.set(fingerprint, created);
    return created;
  }

  function resetTransient() {
    setAt('');
    setConditionRef('');
    setConditionExtra('');
    setNote('');
    setCancelReason('');
    setConfirmNote('');
    setConfirmArmed(false);
  }

  function buildCondition(): SafetyFollowUpConditionDto | null {
    const ref = conditionRef.trim();
    if (!ref) return null;
    const condition: SafetyFollowUpConditionDto = { kind: conditionKind, ref };
    const extra = conditionExtra.trim();
    if (extra && conditionKind === 'conclusion_recorded') condition.conclusion_kind = extra;
    if (extra && conditionKind === 'necessary_check') condition.check_id = extra;
    return condition;
  }

  async function run(action: () => Promise<SafetyCaseDto>,
                     success: (updated: SafetyCaseDto) => string) {
    setBusy(true);
    setError('');
    setTrace(null);
    setConflict(false);
    setDone('');
    try {
      const updated = await action();
      resetTransient();
      setDone(success(updated));
      // 事项视图、主线、列表都可能跟着变。
      await client.invalidateQueries({ queryKey: qk.safetyCase(view.case_id) });
      await client.invalidateQueries({ queryKey: qk.safetyMainline });
      await client.invalidateQueries({ queryKey: qk.safetyCases });
      await client.invalidateQueries({ queryKey: qk.careTasks });
    } catch (caught) {
      // 失败时**不清空**输入:服务端拒绝的原因不该让用户重打一遍。
      setError(serverMessage(caught));
      setTrace(traceId(caught));
      setConflict(isRevisionConflict(caught));
    } finally {
      setBusy(false);
    }
  }

  function submitSchedule() {
    if (kind === 'review_at') {
      if (!at) { setError('请选择复核时间；不填时间可以改选「按条件触发」或「仅备忘」。'); return; }
      const parsed = new Date(at);
      if (Number.isNaN(parsed.getTime())) {
        setError('复核时间的格式无法识别，请重新选择时间后再提交。');
        return;
      }
      const body = {
        key: keyFor(`schedule|review_at|${parsed.toISOString()}|${owner}|${note.trim()}`),
        expected_revision: view.revision,
        kind: 'review_at' as const,
        at: parsed.toISOString(),
        owner,
        note: note.trim() || undefined,
      };
      void run(() => followUpSchedule(view.case_id, body),
        () => `跟进安排已记录：约定时间复核 ${followUpAtText(body.at)}。`
          + '它现在是「已安排、尚未确认」——填了时间不等于确认。');
      return;
    }
    if (kind === 'on_event') {
      const condition = buildCondition();
      if (!condition) { setError('按条件触发需要填写触发对象的引用（服务端不接受自由文本条件）。'); return; }
      const body = {
        key: keyFor(`schedule|on_event|${condition.kind}|${condition.ref}|${conditionExtra.trim()}|${owner}|${note.trim()}`),
        expected_revision: view.revision,
        kind: 'on_event' as const,
        condition,
        owner,
        note: note.trim() || undefined,
      };
      void run(() => followUpSchedule(view.case_id, body),
        (updated) => `跟进安排已记录：${followUpConditionText(
          updated.follow_up?.condition ?? condition)}。`
          + '它现在是「已安排、尚未确认」——填了触发条件不等于确认。');
      return;
    }
    const body = {
      key: keyFor(`schedule|arrangement|${owner}|${note.trim()}`),
      expected_revision: view.revision,
      kind: 'arrangement' as const,
      owner,
      note: note.trim() || undefined,
    };
    void run(() => followUpSchedule(view.case_id, body),
      () => '已记下一条跟进备忘。它没有时间也没有触发条件，因此不会自己到期——'
        + '这是一条提醒，不是会自动执行的复查安排。');
  }

  function submitConfirm() {
    const body = {
      key: keyFor(`confirm|${view.revision}|${confirmNote.trim()}`),
      expected_revision: view.revision,
      note: confirmNote.trim() || undefined,
    };
    void run(() => followUpConfirmation(view.case_id, body),
      (updated) => {
        const info = updated.follow_up;
        const who = info?.confirmed_by ? `记录人 ${info.confirmed_by}` : '记录人未返回';
        const when = info?.confirmed_at ? followUpAtText(info.confirmed_at) : '确认时间未返回';
        return `已确认（${who}，${when}）。`
          + '确认只表示这一次由您本人确认了这条安排，不表示风险已经排除。';
      });
  }

  function submitCancel() {
    const body = {
      key: keyFor(`cancel|${view.revision}|${cancelReason.trim()}`),
      expected_revision: view.revision,
      reason: cancelReason.trim() || undefined,
    };
    void run(() => followUpCancel(view.case_id, body),
      () => '这条安排已取消。它的时间与条件仍然留在记录里，调度状态是「已取消」——'
        + '取消不会让风险消失，也不会关闭这件事。');
  }

  return (
    <Card>
      <LiveAnnouncement message={done || null} />
      <div className="px-4 pt-4">
        <h2 className="flex items-center gap-2 font-serif text-lg font-semibold">
          <CalendarClock size={17} aria-hidden /> 下一次跟进
        </h2>
        <p className="mt-1 text-sm text-ink-secondary">
          这项安排接下来会怎么走、算不算数，以及您可以对它做的三件事。
          安排与确认是两件事：排了期不等于确认过。
        </p>
      </div>

      <div className="space-y-3 px-4 pb-4 pt-3 text-sm">
        <FollowUpSummary followUp={followUp} task={task} />

        {terminal ? (
          <p className="rounded-lg border border-border bg-surface-alt px-3 py-2 text-ink-secondary">
            这件事已经是终态（{view.status_label}），不能再安排、改期或取消跟进。
            如果记录又变化，服务端会重新打开它。
          </p>
        ) : (
          <>
            <fieldset className="space-y-2">
              <legend className="text-xs text-ink-muted">选择这次要做什么</legend>

              <label className="flex gap-2 rounded-lg border border-border px-3 py-2">
                <input type="radio" name="follow-up-mode" value="schedule" className="mt-1"
                  checked={mode === 'schedule'} onChange={() => setMode('schedule')} />
                <span>
                  <span className="inline-flex items-center gap-1 font-medium">
                    <CalendarPlus size={13} aria-hidden /> 安排 / 改期
                  </span>
                  <span className="mt-0.5 block text-ink-secondary">
                    排一个时间或一条白名单触发条件。{SCHEDULE_IS_NOT_CONFIRMATION_NOTICE}
                  </span>
                </span>
              </label>

              <label className={`flex gap-2 rounded-lg border px-3 py-2 ${
                canConfirm.ok ? 'border-border' : 'border-border bg-surface-alt opacity-70'}`}>
                <input type="radio" name="follow-up-mode" value="confirm" className="mt-1"
                  checked={mode === 'confirm'} disabled={!canConfirm.ok}
                  onChange={() => setMode('confirm')} />
                <span>
                  <span className="inline-flex items-center gap-1 font-medium">
                    <CheckCircle2 size={13} aria-hidden /> 确认这条安排
                  </span>
                  <span className="mt-0.5 block text-ink-secondary">
                    {canConfirm.ok
                      ? '记录一次由您本人做出的确认。只有确认端点能产生「已确认」。'
                      : `现在不可用：${canConfirm.reason ?? '没有可确认的安排。'}`}
                  </span>
                </span>
              </label>

              <label className={`flex gap-2 rounded-lg border px-3 py-2 ${
                followUp ? 'border-border' : 'border-border bg-surface-alt opacity-70'}`}>
                <input type="radio" name="follow-up-mode" value="cancel" className="mt-1"
                  checked={mode === 'cancel'} disabled={!followUp}
                  onChange={() => { setMode('cancel'); setConfirmArmed(false); }} />
                <span>
                  <span className="inline-flex items-center gap-1 font-medium">
                    <XCircle size={13} aria-hidden /> 取消这条安排
                  </span>
                  <span className="mt-0.5 block text-ink-secondary">
                    {followUp ? CANCEL_KEEPS_HISTORY_NOTICE : '现在没有可以取消的安排。'}
                  </span>
                </span>
              </label>
            </fieldset>

            {mode === 'schedule' && (
              <div className="space-y-2 rounded-lg bg-surface-alt px-3 py-2">
                <label className="block">
                  <span className="text-xs text-ink-muted">安排种类</span>
                  <select className={inputClass} value={kind}
                    onChange={(event) => setKind(event.target.value as typeof kind)}>
                    <option value="review_at">约定时间复核</option>
                    <option value="on_event">满足条件时复核</option>
                    <option value="arrangement">仅备忘（不会自动到期）</option>
                  </select>
                </label>

                {kind === 'review_at' && (
                  <label className="block">
                    <span className="text-xs text-ink-muted">复核时间（本地时间，提交时带时区）</span>
                    <input type="datetime-local" className={inputClass} value={at}
                      onChange={(event) => setAt(event.target.value)} />
                  </label>
                )}

                {kind === 'on_event' && (
                  <div className="space-y-2">
                    <label className="block">
                      <span className="text-xs text-ink-muted">触发条件种类（白名单）</span>
                      <select className={inputClass} value={conditionKind}
                        onChange={(event) => { setConditionKind(event.target.value); setConditionExtra(''); }}>
                        {CONDITION_KINDS.map((value) => (
                          <option key={value} value={value}>
                            {FOLLOW_UP_CONDITION_KIND_LABELS[value]}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="block">
                      <span className="text-xs text-ink-muted">
                        触发对象引用（必填，例如 memory:conclusion:123@1）
                      </span>
                      <input className={inputClass} value={conditionRef}
                        onChange={(event) => setConditionRef(event.target.value)} />
                    </label>
                    {(conditionKind === 'conclusion_recorded'
                      || conditionKind === 'necessary_check') && (
                      <label className="block">
                        <span className="text-xs text-ink-muted">
                          {conditionKind === 'conclusion_recorded' ? '结论种类（可选）' : '检查编号（可选）'}
                        </span>
                        <input className={inputClass} value={conditionExtra}
                          onChange={(event) => setConditionExtra(event.target.value)} />
                      </label>
                    )}
                    <p className="text-xs text-ink-muted">
                      服务端只接受上面这四种结构化条件；自由文本会被拒绝（422），
                      因为一条分辨不出的条件会变成"永远不会触发"的安排。
                    </p>
                  </div>
                )}

                <label className="block">
                  <span className="text-xs text-ink-muted">这项安排由谁做</span>
                  <select className={inputClass} value={owner}
                    onChange={(event) => setOwner(event.target.value)}>
                    <option value="caregiver">个人提醒（由我自己跟进）</option>
                    <option value="professional">专业复核安排（需要医生或药师判断）</option>
                  </select>
                </label>
                {owner === 'professional' && (
                  <p className="rounded-lg bg-caution-soft px-2.5 py-2 text-xs text-caution">
                    {NO_CLINICIAN_NOTICE}
                    {' '}把它记成"专业复核安排"只是登记了一项待办，
                    不表示已经有人接下了这件事，也不表示已经获得专业确认。
                  </p>
                )}

                <label className="block">
                  <span className="text-xs text-ink-muted">备注（可选，会写进历史）</span>
                  <textarea className={inputClass} rows={2} value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="例如：下次复诊时把这份药单给医生看" />
                </label>
              </div>
            )}

            {mode === 'confirm' && (
              <div className="space-y-2 rounded-lg bg-surface-alt px-3 py-2">
                <p className="text-ink-secondary">
                  确认会记下您本人这一次的确认（服务端从认证上下文取记录人，
                  请求体里自称的身份不会被读取）。它不改变这条安排的内容，
                  也不表示风险已经排除。
                </p>
                <label className="block">
                  <span className="text-xs text-ink-muted">确认说明（可选）</span>
                  <textarea className={inputClass} rows={2} value={confirmNote}
                    onChange={(event) => setConfirmNote(event.target.value)}
                    placeholder="例如：已与家人确认由我负责这次复核" />
                </label>
              </div>
            )}

            {mode === 'cancel' && (
              <div className="space-y-2 rounded-lg bg-surface-alt px-3 py-2">
                <p className="text-ink-secondary">{CANCEL_KEEPS_HISTORY_NOTICE}</p>
                <label className="block">
                  <span className="text-xs text-ink-muted">取消原因（可选）</span>
                  <textarea className={inputClass} rows={2} value={cancelReason}
                    onChange={(event) => setCancelReason(event.target.value)} />
                </label>
                {!confirmArmed ? (
                  <button type="button" className={buttonClass}
                    onClick={() => setConfirmArmed(true)}>
                    准备好取消这条安排
                  </button>
                ) : (
                  <p className="text-xs text-caution">已就绪——再按一次下面的按钮才会真的取消。</p>
                )}
              </div>
            )}

            <button type="button"
              className={`${buttonClass} inline-flex items-center gap-1`}
              disabled={busy || (mode === 'confirm' && !canConfirm.ok)
                || (mode === 'cancel' && (!followUp || !confirmArmed))}
              onClick={() => {
                if (mode === 'schedule') submitSchedule();
                else if (mode === 'confirm') submitConfirm();
                else submitCancel();
              }}>
              {busy ? '提交中…' : mode === 'schedule' ? '记录这项安排'
                : mode === 'confirm' ? '确认这条安排' : '取消这条安排'}
            </button>

            {error && (
              <div role="alert" className="space-y-1 rounded-lg border border-danger/30 bg-danger-soft/40 p-3">
                <p className="font-medium text-danger">
                  {conflict ? '服务端拒绝了这次提交（版本冲突或被规则挡住）：' : '服务端没有接受这次提交，下面是它的原话：'}
                </p>
                <p className="whitespace-pre-wrap text-ink">{error}</p>
                {trace && <p className="font-mono text-xs text-ink-muted">追踪号 {trace}</p>}
                <p className="text-xs text-ink-secondary">
                  您填的内容还在上面，没有丢失。
                  {conflict && ' 如果这条安排已被别处改过，请先刷新到最新版本再决定是否重发。'}
                </p>
                {conflict && (
                  <button type="button" className="text-xs text-primary underline"
                    onClick={() => {
                      // 只刷新服务端视图;表单状态留在本地,不被清空。
                      void client.invalidateQueries({ queryKey: qk.safetyCase(view.case_id) });
                      void client.invalidateQueries({ queryKey: qk.safetyClosureEvidence(view.case_id) });
                    }}>
                    刷新这件事项（保留我已填的内容）
                  </button>
                )}
              </div>
            )}
            {done && <p role="status" className="text-primary-strong">{done}</p>}
          </>
        )}
      </div>
    </Card>
  );
}

/** 版本冲突:409。服务端对这个状态码只给了 `product_validation` 这一类错误码, */
/** 所以这里按 HTTP 状态判断,并把服务端原话原样交给用户。 */
function isRevisionConflict(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'status' in error
    && (error as { status?: number }).status === 409;
}

// ---- 安排的展示 --------------------------------------------------------------

function FollowUpSummary({ followUp, task }: {
  followUp: SafetyFollowUpDto | null; task: CareTaskDto | null;
}): React.ReactElement {
  if (!followUp) {
    return (
      <div className="rounded-lg border border-border bg-surface-alt px-3 py-2">
        <p className="text-ink-secondary">
          本次读取没有拿到跟进安排（服务端未返回这个字段）。
          这不代表没有风险，也不代表已经安排好了——它只表示这一页读不到安排。
        </p>
      </div>
    );
  }
  const confirmed = confirmationIsRecorded(followUp);
  const legacyCorrupt = followUp.confirmed === true && !confirmed;
  const unconfirmedButScheduled = isScheduledNotConfirmed(followUp);
  const state = followUp.schedule_state ?? null;

  return (
    <div className="space-y-2 rounded-lg border border-border bg-surface-alt px-3 py-2">
      {/* 第一行:这算不算数。这是本轮要建立的那处关键区分。 */}
      <p className="flex flex-wrap items-center gap-1.5" data-follow-up-confirmed={confirmed ? 'true' : 'false'}>
        {confirmed ? (
          <Badge tone="primary" icon={<CheckCircle2 size={12} aria-hidden />}>已确认</Badge>
        ) : unconfirmedButScheduled ? (
          <Badge tone="caution" icon={<CalendarClock size={12} aria-hidden />}>已安排，尚未确认</Badge>
        ) : (
          <Badge tone="caution">待确认的安排</Badge>
        )}
        {state && <Badge tone={scheduleStateTone(state)}>调度状态：{scheduleStateLabel(state)}</Badge>}
        <Badge tone="neutral">{followUpKindLabel(followUp.kind)}</Badge>
      </p>

      {confirmed && (
        <p className="text-ink-secondary">
          确认记录：{followUpAtText(followUp.confirmed_at)}
          {followUp.confirmed_by ? ` · 由 ${followUp.confirmed_by} 确认` : ''}
          {followUp.confirmation_ref ? ` · ${followUp.confirmation_ref}` : ''}。
          确认只针对这条安排，不代表风险已经排除。
        </p>
      )}
      {legacyCorrupt && (
        <p className="rounded bg-danger-soft/50 px-2 py-1 text-caution">
          这条记录自称已确认，但缺少确认时间或确认记录，因此按未确认显示。
          这通常是一条契约收紧之前留下的旧记录。
        </p>
      )}
      {unconfirmedButScheduled && (
        <p className="text-caution">
          已经排了期，但还没有任何确认记录。在确认之前，它是一项约定，不是一个已确认的复查周期。
        </p>
      )}

      {/* 第二行:这项安排的内容。 */}
      <p className="text-ink-secondary">
        {followUp.kind === 'review_at'
          ? <>约定时间复核：{followUpAtText(followUp.at)}</>
          : followUp.kind === 'on_event'
            ? <>满足条件时复核：{followUpConditionText(followUp.condition)}</>
            : <>这项安排没有时间也没有触发条件：{followUpKindLabel(followUp.kind)}</>}
      </p>
      {followUp.kind === 'on_event' && (
        <p className="text-xs text-ink-muted">
          上面这条就是"下一次跟进会在什么条件下启动"——它是白名单触发条件，
          记录一变就按它判断；它不需要有人记得。
        </p>
      )}
      <p className="text-ink-secondary">{followUpOwnerText(followUp.owner)}</p>
      {followUp.owner === 'professional' && (
        <p className="rounded bg-caution-soft px-2 py-1 text-xs text-caution">{NO_CLINICIAN_NOTICE}</p>
      )}

      {followUp.note && <p className="text-ink-secondary">备注：{followUp.note}</p>}
      {followUp.recorded_at && (
        <p className="text-xs text-ink-muted">这条安排记录于 <TimeText iso={followUp.recorded_at} /></p>
      )}

      {/* 第三行:最近一次触发与执行状态。 */}
      <div className="border-t border-border pt-2">
        {followUp.last_triggered_at ? (
          <p className="text-ink-secondary">
            最近一次触发：<TimeText iso={followUp.last_triggered_at} />
            {followUp.last_trigger_reason ? ` · 原因：${followUp.last_trigger_reason}` : ''}
          </p>
        ) : (
          <p className="text-xs text-ink-muted">这条安排还没有触发过。</p>
        )}
        {followUp.blocked_reason && (
          <p className="mt-1 rounded bg-danger-soft/50 px-2 py-1 text-caution">
            执行受阻：{followUp.blocked_reason}
          </p>
        )}
        <p className="mt-1 text-xs text-ink-muted">
          {followUp.care_task_id
            ? <>这条安排关联的执行任务：<span className="break-all font-mono">{followUp.care_task_id}</span></>
            : '这条安排没有关联的执行任务。'}
        </p>
        {task ? (
          <p className="mt-1 text-xs text-ink-secondary">
            这件事当前的执行任务状态：{careTaskStatusLabel(task.status)}
            {task.waiting_reason ? ` · ${task.waiting_reason}` : ''}
            {task.budget ? ` · 已用 ${task.budget.spent}/${task.budget.limit} 步` : ''}
          </p>
        ) : (
          <p className="mt-1 text-xs text-ink-muted">
            这件事当前没有执行任务。没有任务不等于已经执行过，也不等于风险已经排除。
          </p>
        )}
      </div>

      <p className="text-xs text-ink-muted">{MONITORING_NOTICE}</p>
    </div>
  );
}
