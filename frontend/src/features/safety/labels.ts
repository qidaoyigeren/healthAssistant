/**
 * 安全事项主线的界面词汇。
 *
 * 这里只做**翻译**,不做改写:服务端枚举 → 中文短语;读不出来的取值一律
 * 如实标注"未识别",不猜、不省略。关闭依据的合法性由服务端强制,前端只
 * 负责把它的原话显示出来(见 `serverMessage`)。
 */
import { ApiError } from '../../api/http';
import type {
  SafetyAnswerAssessmentDto, SafetyBasisDto, SafetyCaseDto, SafetyFollowUpConditionDto,
  SafetyFollowUpDto, SafetyTriggerDto,
} from '../../api/types';

// ---- 枚举 → 中文 -------------------------------------------------------------

export const CASE_TYPE_LABELS: Record<string, string> = {
  interaction_risk: '药物相互作用风险',
  condition_risk: '个人情况相关的用药风险',
  evidence_gap: '依据不足',
  discrepancy: '记录不一致',
  source_invalidated: '来源失效',
};

/** `responsible_party` = 现在这棒在谁手上。 */
export const PARTY_LABELS: Record<string, string> = {
  system: '由程序继续检查',
  agent: '由系统继续调查',
  caregiver: '由您补充信息',
  professional: '由专业人员复核',
  none: '无需处理',
};

export const DISPOSITION_LABELS: Record<string, string> = {
  resolved_with_basis: '已有依据的处置',
  escalated_to_professional: '已提交专业复核',
  accepted_monitoring: '记录为持续跟进',
};

export const BASIS_LABELS: Record<string, string> = {
  deterministic_check_completed: '程序检查已完成',
  professional_review_applied: '已生效的专业复核决定',
  user_reported: '您转述的意见（未经核实）',
  // 持续跟进记下的不是关闭依据,而是"凭什么说风险还在"。
  monitoring_arrangement: '持续跟进安排（不是关闭依据）',
};

/** 「触发条件现在是什么状态」——本轮最重要的一处区分,不能含糊。 */
export const TRIGGER_STATE_LABELS: Record<string, string> = {
  risk_present: '检查显示风险仍然成立',
  trigger_eliminated: '触发条件已消失',
  unknown: '无法判断',
};

/** 持续跟进的安排种类。 */
export const FOLLOW_UP_KIND_LABELS: Record<string, string> = {
  review_at: '约定时间复核',
  on_event: '满足条件时复核',
  arrangement: '待确认的安排',
};

/** 服务端对一条补充回答的判定。 */
export const ANSWER_KIND_LABELS: Record<string, string> = {
  provided: '有内容地回答',
  unknown: '表示不知道',
  empty: '空回答（什么都没关）',
};

const MEDICATION_CHANGE_LABELS: Record<string, string> = {
  medication_add: '新增用药',
  medication_remove: '停用用药',
  medication_dose_change: '剂量或用法变更',
};

// ---- 文案 -------------------------------------------------------------------

/**
 * 本项目没有接入任何真实的医生/药师服务。这句话在主线上必须出现,
 * 因为它决定了"等待专业复核"到底意味着什么。
 */
export const NO_CLINICIAN_NOTICE =
  '本项目未连接任何真实的医生或药师服务。系统不会把事项送达任何人，也不会收到答复；'
  + '请把这里的内容交给您的医生或药师，拿到意见后回到本页记录。';

/**
 * 「持续跟进」到底是什么意思。它不是"等专业人员"，也不是"风险没了"——
 * 这句话跟着状态标签一起出现,防止它被读成上面两种。
 */
export const MONITORING_NOTICE =
  '「持续跟进」表示风险仍然成立，只是已经有一项安排接着做下去。'
  + '它不是「风险已经消除」，也不是「正在等专业人员」。';

/** 一项没有可信时间/触发条件的安排必须被说成待确认,不能显示成复查周期。 */
export const UNCONFIRMED_FOLLOW_UP_NOTICE =
  '尚无可信依据的复核时间或触发条件；这是一项待确认的安排。';

/** 用户明说"不知道"之后,这条问题处于什么状态。 */
export const UNKNOWN_ANSWER_NOTICE =
  '用户表示不知道 — 需要从其他来源核实';

// ---- 翻译函数 ---------------------------------------------------------------

export function caseTypeLabel(caseType: string): string {
  return CASE_TYPE_LABELS[caseType] ?? `未识别的事项类型（${caseType}）`;
}

export function partyLabel(party: string | null | undefined): string {
  if (!party) return '未记录由谁处理';
  return PARTY_LABELS[party] ?? `未识别的处理方（${party}）`;
}

export function medicationChangeLabel(eventType: string): string {
  return MEDICATION_CHANGE_LABELS[eventType] ?? `用药记录变化（${eventType}）`;
}

/** 触发条件状态 → 一句能直接读的话。读不出的取值如实标注,不猜。 */
export function triggerStateLabel(state: string | null | undefined): string {
  if (!state) return '触发条件未判定';
  return TRIGGER_STATE_LABELS[state] ?? `未识别的触发状态（${state}）`;
}

/** 三种触发状态各自**意味着什么**——这是本轮要用户看懂的那一点。 */
export const TRIGGER_STATE_MEANING =
  '结论上的「触发条件状态」说的是：当初引发这件事的条件，在「当前记录」下还在不在。'
  + '「检查显示风险仍然成立」= 按当前记录，风险还在，不能关；'
  + '「触发条件已消失」= 当初的条件已不成立；「无法判断」= 依据不足以判定，'
  + '既不能当成风险消失，也不等于风险确定存在。';

export function followUpKindLabel(kind: string | null | undefined): string {
  if (!kind) return '未记录安排种类';
  return FOLLOW_UP_KIND_LABELS[kind] ?? `未识别的安排（${kind}）`;
}

/**
 * 跟进安排的时间 → 一个**时刻**，不是一串被截断的字符。
 *
 * CONTRACT.md §4.4 之后服务端把 `at` 规范化成 `+00:00` 秒精度，所以 `（UTC）`
 * 这个标注本身是对的；但它此前是靠 `at.slice(0, 16)` 截出来的，遇到毫秒 + `Z`
 * 或别的偏移量就会把"截出来的墙钟"当成 UTC 显示。这里改成从**解析出的时刻**
 * 算 UTC 墙钟：无论存量记录写的是哪种偏移量，显示的都是同一个时刻。
 */
export function followUpAtText(at: string | null | undefined): string {
  if (!at) return '未记录时间';
  const parsed = new Date(at);
  if (Number.isNaN(parsed.getTime())) return `${at}（时间格式无法识别，按原样显示）`;
  return `${parsed.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

/** 触发条件白名单的种类(§4.3)，与 `safety_checks.py` 的触发器词汇同一套。 */
export const FOLLOW_UP_CONDITION_KIND_LABELS: Record<string, string> = {
  conclusion_recorded: '某条结论被记录时',
  necessary_check: '某项必要检查完成时',
  medication_change: '用药集合变化时',
  fact_change: '患者事实变化时',
};

/**
 * 触发条件 → 一句人话。
 *
 * 存量记录里可能还存着契约收紧之前的**自由文本**。那种值既不能被执行、也不能
 * 假装它是一条可用的触发条件，所以如实标注它是旧写法，而不是把它渲染成正常条件。
 */
export function followUpConditionText(
  condition: SafetyFollowUpConditionDto | string | null | undefined,
): string {
  if (!condition) return '未记录触发条件';
  if (typeof condition === 'string') {
    return `${condition}（旧版自由文本；契约收紧后新记录只接受白名单条件，本条不会被当成分辨得出的触发条件）`;
  }
  const kind = FOLLOW_UP_CONDITION_KIND_LABELS[condition.kind]
    ?? `未识别的触发条件种类（${condition.kind}）`;
  const parts = [kind];
  if (condition.ref) parts.push(`对象：${condition.ref}`);
  if (condition.conclusion_kind) parts.push(`结论种类：${condition.conclusion_kind}`);
  if (condition.check_id !== undefined && condition.check_id !== null) {
    parts.push(`检查编号：${condition.check_id}`);
  }
  return parts.join('，');
}

/** 这条安排现在走到哪了。与 `kind`(安排的种类)是两件事，不能合并。 */
export const SCHEDULE_STATE_LABELS: Record<string, string> = {
  scheduled: '已排期（尚未到期）',
  due: '已到期，等待执行',
  triggered: '已触发',
  blocked: '执行受阻',
  cancelled: '已取消',
  unscheduled: '未排期（既没有时间也没有触发条件）',
};

export function scheduleStateLabel(state: string | null | undefined): string {
  if (!state) return '调度状态未记录';
  return SCHEDULE_STATE_LABELS[state] ?? `未识别的调度状态（${state}）`;
}

export function scheduleStateTone(state: string | null | undefined):
'primary' | 'caution' | 'danger' | 'neutral' {
  if (state === 'triggered') return 'primary';
  if (state === 'blocked') return 'danger';
  if (state === 'due' || state === 'unscheduled') return 'caution';
  return 'neutral';
}

/** 安排的种类(按时间/按事件/仅备忘) → 谁来做这件事。个人提醒与专业复核不是一回事。 */
export function followUpOwnerText(owner: string | null | undefined): string {
  if (!owner) return '未记录这项安排由谁做';
  if (owner === 'caregiver') return '个人提醒（由您自己按这条安排跟进）';
  if (owner === 'professional') return '专业复核安排（需要医生或药师的判断）';
  return `未识别的执行方（${owner}）`;
}

/**
 * 这条安排的确认记录可信吗。
 *
 * §4.5:`confirmed: true` 必须同时有 `confirmed_at` 与 `confirmation_ref`；
 * 三者不一致的记录视为损坏。存量老记录(有 `confirmed=true` 但没有确认记录)
 * 也走这一条——一律按未确认读。界面绝不把一条没有确认记录的安排显示成已确认。
 */
export function confirmationIsRecorded(followUp: SafetyFollowUpDto): boolean {
  return followUp.confirmed === true
    && !!followUp.confirmed_at && !!followUp.confirmation_ref;
}

/** 「已安排但尚未确认」：本轮要建立的那一处关键区分。 */
export function isScheduledNotConfirmed(followUp: SafetyFollowUpDto): boolean {
  return !confirmationIsRecorded(followUp)
    && (followUp.schedule_state === 'scheduled' || followUp.schedule_state === 'due');
}

/**
 * 跟进安排 → 一句**说清它算不算数**的话。
 *
 * 三层区分,一层都不能省:
 *  1. 有没有安排(没拿到字段 ≠ 没有风险,也 ≠ 已经安排好);
 *  2. 排了期没有(`schedule_state`) —— "已安排"不是"已确认";
 *  3. 有没有**确认记录**(`confirmed_at` + `confirmation_ref`)。
 */
export function followUpText(followUp: SafetyFollowUpDto | null | undefined): string {
  if (!followUp) {
    return '本次读取没有拿到跟进安排（服务端未返回该字段）。'
      + '这不代表没有风险，也不代表已经安排好了。';
  }
  const owner = followUp.owner ? `，执行方：${followUpOwnerText(followUp.owner)}` : '';
  const confirmed = confirmationIsRecorded(followUp);
  const corrupted = followUp.confirmed === true && !confirmed;
  const detail = followUp.kind === 'review_at'
    ? `约定时间复核：${followUpAtText(followUp.at)}`
    : followUp.kind === 'on_event'
      ? `满足条件时复核：${followUpConditionText(followUp.condition)}`
      : followUpKindLabel(followUp.kind);

  if (confirmed) {
    return `${detail}；已确认（${followUpAtText(followUp.confirmed_at)}`
      + `${followUp.confirmed_by ? `，由 ${followUp.confirmed_by}` : ''}`
      + `${followUp.confirmation_ref ? `，确认记录 ${followUp.confirmation_ref}` : ''}）${owner}`;
  }

  // 排了期但没有确认记录 —— 这正是"已安排、尚未确认"。
  if (isScheduledNotConfirmed(followUp)) {
    return `${detail}；调度状态：${scheduleStateLabel(followUp.schedule_state)}。`
      + '这条安排已经排期，但还没有确认记录，因此不算一个已确认的复查周期。'
      + `${owner}`;
  }

  const note = followUp.note?.trim() ? followUp.note.trim() : UNCONFIRMED_FOLLOW_UP_NOTICE;
  const plain = note.includes('待确认') ? note : `${note}；${UNCONFIRMED_FOLLOW_UP_NOTICE}`;
  return `${followUpKindLabel(followUp.kind)}：${plain}`
    + `${corrupted
      ? '（这条记录自称已确认，但缺少确认时间或确认记录，按未确认显示）' : ''}`
    + `（这不是一个真实的复查周期）${owner}`;
}

/**
 * 取这道事项的跟进安排。视图里没有该字段时,退回历史里那次「持续跟进」处置
 * 登记下来的同一份安排——**不**自己合成一个时间。
 */
export function followUpOf(caseView: SafetyCaseDto): SafetyFollowUpDto | null {
  if (caseView.follow_up) return caseView.follow_up;
  for (let index = caseView.history.length - 1; index >= 0; index -= 1) {
    const entry = caseView.history[index];
    if (entry && entry.event === 'disposition' && entry.follow_up) return entry.follow_up;
  }
  return null;
}

export function answerKindLabel(kind: string | null | undefined): string {
  if (!kind) return '服务端未记录这条回答的分类';
  return ANSWER_KIND_LABELS[kind] ?? `未识别的回答分类（${kind}）`;
}

export function dispositionLabel(disposition: string | null | undefined): string {
  if (!disposition) return '未记录处置动作';
  return DISPOSITION_LABELS[disposition] ?? `未识别的处置（${disposition}）`;
}

/**
 * 「现在需要谁做什么」在**持续跟进**下是另一回事:那棒仍在照护人手上,但要做的是
 * 按安排继续跟进,不是"补一条信息"。同一个 `caregiver` 不能读成同一件事。
 */
export function partyActionLabel(party: string | null | undefined,
                                 status?: string | null): string {
  if (status === 'monitoring' && party === 'caregiver') return '由您按安排继续跟进';
  return partyLabel(party);
}

export function statusTone(status: string): 'primary' | 'caution' | 'danger' | 'neutral' {
  if (status === 'resolved') return 'primary';
  if (status === 'execution_failed') return 'danger';
  if (status === 'open' || status === 'investigating' || status === 'awaiting_user'
    || status === 'awaiting_professional' || status === 'needs_recheck'
    || status === 'monitoring') return 'caution';
  return 'neutral';
}

/** 触发原因:读得出就说清楚,读不出就说读不出。 */
export function triggerText(trigger: SafetyTriggerDto | null | undefined): string {
  if (!trigger) return '未记录触发原因。';
  if (trigger.kind === 'conclusion_recorded') {
    return '一次必要检查跑完后记录了结论，依此建立了这件事。';
  }
  if (trigger.kind === 'necessary_check') {
    // trigger_kind 的取值以 safety_checks.py 的 TRIGGER_* 常量为准。
    const source = String(trigger.trigger_kind ?? trigger.trigger ?? '');
    const label: Record<string, string> = {
      medication_set: '药单发生变化',
      condition_facts: '个人情况（年龄/过敏/肝肾功能等）发生变化',
    };
    const reason = label[source];
    return reason
      ? `${reason}，因此按当前记录重新做了一次必要检查。`
      : '记录变化触发了一次必要检查。';
  }
  if (trigger.kind) return `触发来源：未识别的类型（${trigger.kind}）。`;
  return '未记录触发原因。';
}

/** 处置依据 → 大白话。依据记录的版本与事项当前版本不一致时如实提示。 */
export function basisText(basis: SafetyBasisDto | null | undefined,
                          caseView?: SafetyCaseDto): string {
  if (!basis) return '未记录依据。';
  const parts: string[] = [];
  if (basis.kind === 'deterministic_check_completed') {
    parts.push('依据：程序检查已完成（检验的是被引用的检查结论仍有效，且记录的输入版本等于当前版本）。');
  } else if (basis.kind === 'professional_review_applied') {
    parts.push(`依据：一条已生效的专业复核决定${basis.review_action ? `（${basis.review_action}）` : ''}。`);
  } else if (basis.kind === 'user_reported') {
    parts.push('依据：您转述的意见，未经核实，不能用来关闭事项。');
  } else if (basis.kind === 'monitoring_arrangement') {
    // 这一条不是关闭依据,而是"凭什么说风险还在"。两者不能读成同一件事。
    parts.push('这不是关闭依据：持续跟进记下的是「凭什么说风险还在」，以及当时的安排。');
    if (basis.still_present?.length) {
      parts.push(`仍显示风险成立的检查结论有 ${basis.still_present.length} 条。`);
    } else {
      parts.push('服务端没有给出「风险仍然成立」的结论引用。');
    }
    if (basis.why_not_closed) parts.push(`未关闭的原因（服务端原话）：${basis.why_not_closed}`);
  } else if (basis.kind) {
    parts.push(`依据：未识别的依据类型（${basis.kind}）。`);
  } else {
    parts.push('未记录依据种类。');
  }
  if (basis.actor) parts.push(`记录人：${basis.actor}。`);
  if (basis.at) parts.push(`记录时间：${basis.at.slice(0, 16).replace('T', ' ')}（UTC）。`);
  if (basis.note) parts.push(basis.note);
  // 旧依据不能批准新状态:服务端会在同步时用当前版本重新核对,这里先说清楚。
  if (caseView && basis.input_revision) {
    const moved = Object.keys(basis.input_revision).some(
      (scope) => basis.input_revision?.[scope] !== caseView.input_versions[scope]);
    if (moved) {
      parts.push('注意：做出这个处置之后，记录又变过。依据记录的版本已不是当前版本，'
        + '这件事会在下次同步时重新打开。');
    }
  }
  return parts.join('');
}

/** 一句话说清"为什么又被重新翻出来"。优先读 history,读不到就如实说。 */
export function recheckReason(caseView: SafetyCaseDto): string {
  const history = caseView.history;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const entry = history[index];
    if (!entry) continue;
    if (entry.event === 'reopened') {
      const at = entry.at.slice(0, 16).replace('T', ' ');
      const because = entry.trigger ? triggerText(entry.trigger) : '有新的记录触发了重新核对。';
      return `${because}（${at} UTC）`;
    }
    if (entry.event === 'status_changed' && entry.to === 'needs_recheck') {
      const at = entry.at.slice(0, 16).replace('T', ' ');
      const from = entry.from ? `从「${stateLabel(entry.from, caseView)}」变为` : '变为';
      return `状态${from}「需要重新核对」：依据发生变化，需要按当前记录重新核对。（${at} UTC）`;
    }
  }
  const stale = caseView.conclusions.find((item) => item.status && item.status !== 'current');
  if (stale?.stale_reason) return `被引用的检查结论已失效：${stale.stale_reason}`;
  if (stale) return `被引用的检查结论已不是当前有效状态（${stale.status}）。`;
  return '记录里没有写明这次重新复核的具体原因（历史中没有对应的重新打开条目）。';
}

/** 事项状态的中文标签,与 `safety_cases.py` 的 STATUS_LABELS 同一口径。 */
export const STATUS_LABELS: Record<string, string> = {
  open: '待调查',
  investigating: '调查中',
  awaiting_user: '等待您补充',
  awaiting_professional: '等待专业复核',
  resolved: '已有依据的处置',
  monitoring: '持续跟进中（风险仍在）',
  needs_recheck: '需要重新核对',
  execution_failed: '本次执行未完成（事项仍未解决）',
};

/**
 * 状态标签:是**当前**状态时一律用服务端给的 `status_label`(它才是权威措辞),
 * 历史条目里的旧状态才退回本地映射。
 */
export function stateLabel(status: string, current?: SafetyCaseDto | undefined): string {
  if (!status) return '状态未记录';
  if (current && status === current.status) return current.status_label;
  return STATUS_LABELS[status] ?? `未识别的状态（${status}）`;
}

/** 历史条目 → 一行中文。 */
export function historyText(entry: {
  event: string; from?: string | null; to?: string | null; request_id?: string;
  for_professional?: boolean; answered?: boolean; disposition?: string | null;
  basis_kind?: string | null; actor?: string | null; note?: string | null;
  answer_kind?: string | null; reason?: string | null; why?: string | null;
  previous_disposition?: string | null; follow_up?: SafetyFollowUpDto | null;
}, statusLabel: (status: string) => string): string {
  switch (entry.event) {
    case 'opened': return '建立了这件事项。';
    case 'updated': return '有新信息更新了这件事项。';
    case 'reopened': return '因新信息重新打开（此前已有依据）。';
    case 'status_changed':
      return `状态变化：${entry.from ? statusLabel(entry.from) : '（此前状态未记录）'}`
        + ` → ${statusLabel(entry.to ?? '')}`
        + (entry.why ? `（${entry.why}）` : '');
    case 'seen_by_user': return '您已查看过这件事。';
    case 'input_requested':
      return entry.for_professional ? '请求专业人员补充信息。' : '请求您补充信息。';
    case 'input_recorded': {
      // answered 可能是缺省的历史记录:那时不替它宣称"已关闭"。
      if (entry.answer_kind === 'unknown') {
        return '收到一条回答：用户表示不知道。不再等这位用户，但这条问题没有解决，'
          + '仍阻止关闭，改从其他来源核实。';
      }
      if (entry.answer_kind === 'empty') return '收到一条空回答：没有关闭任何问题。';
      if (entry.answered === true) return '收到补充，并关闭了对应的那条问题。';
      if (entry.answered === false) return '收到一条补充，但没有对应到未决的问题（未关闭任何问题）。';
      return '收到一条补充。';
    }
    case 'answer_retired':
      // 回答过期是"要再问一次",不是"已经解决"。
      return `此前的一条回答不再适用于当前状态，该问题被重新打开`
        + (entry.request_id ? `（问题编号 ${entry.request_id}）` : '')
        + (entry.reason ? `：${entry.reason}` : '。');
    case 'resolution_basis_retired':
      // 旧处置不再生效,但它确实发生过——所以它留在历史里,只是不能被当成现行依据。
      return `此前的处置（${dispositionLabel(entry.previous_disposition)}）已经不再适用：`
        + (entry.reason ? `${entry.reason}。` : '认定它的事实发生了变化。')
        + '这条记录保留在历史里，因为它确实发生过；请勿据此认为它现在仍然有效。';
    case 'disposition':
      return `处置记录：${dispositionLabel(entry.disposition)}`
        + `（依据：${BASIS_LABELS[String(entry.basis_kind)] ?? entry.basis_kind ?? '未记录'}）`
        + (entry.actor ? `，记录人 ${entry.actor}` : '')
        + (entry.follow_up ? `，跟进安排：${followUpText(entry.follow_up)}` : '')
        + (entry.note ? `，备注：${entry.note}` : '');
    default: return `其他记录（${entry.event}）。`;
  }
}

// ---- 服务端错误 → 界面文案 ----------------------------------------------------

/**
 * 服务端给的中文说明**原样**返回:关闭依据被拒绝(409)时说清了为什么,
 * 改写它会把这个理由抹掉。网络层失败没有 body,才退回友好措辞。
 */
export function serverMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.body) return error.body.message;
    return error.displayMessage();
  }
  return error instanceof Error ? error.message : '操作失败，请重试。';
}

/** 追踪号单独显示,不混进说明正文。 */
export function traceId(error: unknown): string | null {
  return error instanceof ApiError ? error.body?.trace_id ?? null : null;
}

// ---- 处置:服务端规则 → 提交前先说清后果 ----------------------------------------

export const CLOSING_BASES = ['deterministic_check_completed', 'professional_review_applied'] as const;

/** 服务端接受的处置动作(与 safety_cases.py 的 DISPOSITION_* 一致)。 */
export const DISPOSITIONS = [
  'resolved_with_basis', 'escalated_to_professional', 'accepted_monitoring',
] as const;

/**
 * 提交后事项会变成什么。这段规则抄自 `SafetyCaseStore.disposition`:
 * 只有 `deterministic_check_completed` / `professional_review_applied` 能关闭事项;
 * 用户转述只能用于转专业复核;**持续跟进不关闭**——它说的是风险仍在。
 *
 * `professional_review_applied` 在当前项目里一律失败(没有真实医护服务),
 * 所以这里不把它当成一条可用的关闭路径介绍。
 */
export function dispositionOutcome(disposition: string, basisKind: string): string {
  if (disposition === 'resolved_with_basis') {
    if (basisKind === 'deterministic_check_completed') {
      return '提交后：服务端会现场核对触发条件是否已消除；通过才记为「已有依据的处置」，'
        + '记录以后再变化时会自动重新打开。';
    }
    if (basisKind === 'professional_review_applied') {
      return '服务端不会接受：这类依据要求先有一条已生效的真实专业复核决定，'
        + '而本项目未连接任何真实医护服务。';
    }
    return '服务端不会接受：用户转述与模型判断不能作为关闭事项的依据，只能提交专业复核。';
  }
  if (disposition === 'accepted_monitoring') {
    return '提交后：事项记为「持续跟进中（风险仍在）」——风险没有消除，只是有一项安排接着做下去；'
      + '它不转入等待专业复核。';
  }
  if (basisKind === 'user_reported') {
    return '提交后：事项转入「等待专业复核」，并如实记下这是未核实的转述。';
  }
  return '提交后：事项转入「等待专业复核」，等待专业结论。';
}


/** 信息目标:这条问题要弄清的是哪一类信息。 */
export const INFORMATION_TARGET_LABELS: Record<string, string> = {
  patient_actual_state: '这位患者实际是什么情况',
  material_record: '材料里记的是什么',
  general_reference: '一般参考知识',
  professional_judgment: '需要专业判断',
};

/** 取证来源:这一次从哪里取。它不决定问题身份,可以中途更换。 */
export const QUESTION_STRATEGY_LABELS: Record<string, string> = {
  patient_record: '读已有的患者记录',
  ask_user: '需要您补充',
  patient_material: '读您上传的材料',
  general_reference: '查一般药品资料',
  professional_review: '请专业人员复核',
};

export function informationTargetText(value?: string | null): string | null {
  if (!value) return null;
  return INFORMATION_TARGET_LABELS[value] ?? `未识别的信息类型（${value}）`;
}

export function questionStrategyText(value?: string | null): string | null {
  if (!value) return null;
  return QUESTION_STRATEGY_LABELS[value] ?? `未识别的来源（${value}）`;
}


/**
 * 一条问题查到了什么程度。它与事项状态、与执行结果都不是一回事:
 * "取不到"不等于"有答案","读到内容"也不等于"已确认"。
 */
export const INFORMATION_STATE_LABELS: Record<string, string> = {
  not_attempted: '还没开始查',
  attempted_no_result: '查过，没有取得可用信息',
  source_limited: '当前可用来源取不到（不代表资料不存在）',
  received_unconfirmed: '已经取到内容，尚未形成有依据的答案',
  available: '已有适用依据',
};

export function informationStateText(value?: string | null): string | null {
  if (!value) return null;
  return INFORMATION_STATE_LABELS[value] ?? `未识别的进度（${value}）`;
}

/** 答案的来源属性。用户报告与材料记录不等于已核实事实。 */
export const PROVENANCE_LABELS: Record<string, string> = {
  authoritative_record: '当前权威记录（可直接复用）',
  reference_evidence: '已回读的参考资料原文',
  material_record: '上传材料中的记录（未经核实）',
  user_reported: '用户报告（未经核实）',
  professional_opinion: '专业意见',
};

export function provenanceText(value?: string | null): string | null {
  if (!value) return null;
  return PROVENANCE_LABELS[value] ?? `未识别的来源属性（${value}）`;
}

// ---- 答案的可信性:assessment(CONTRACT.md §3)---------------------------------

/**
 * 「依据已核对」的含义边界。**这句话必须跟着 `verified` 一起出现**:
 * `verified` 只说明这条答案的依据在约定范围内核对过,绝不表示用药安全、
 * 风险已经排除,也不是任何专业医疗判断。
 */
export const VERIFIED_SCOPE_NOTICE =
  '「依据已核对」只说明这条答案所依据的材料在约定范围内核对过。'
  + '它不表示用药安全，不表示风险已经排除，也不是医生或药师的判断。';

/** 缺 assessment 时显示什么。**缺失不等于 verified**,这一条是硬约束。 */
export const ASSESSMENT_MISSING_LABEL = '未核实（没有核验记录）';

/** 四个 `status` 的标签。加上"缺失",一共五个视觉状态。 */
export const ASSESSMENT_STATUS_LABELS: Record<string, string> = {
  verified: '依据已核对',
  candidate: '候选依据，尚未核对完',
  stale: '来源已变化，需要重新核对',
  unsupported: '没有可支撑的依据',
};

export function assessmentLabel(status: string | null | undefined): string {
  if (!status) return ASSESSMENT_MISSING_LABEL;
  return ASSESSMENT_STATUS_LABELS[status] ?? `未识别的核验状态（${status}）`;
}

/** 五个视觉状态的配色。文字徽标同时表义,不靠颜色单独区分。 */
export function assessmentTone(status: string | null | undefined):
'primary' | 'caution' | 'danger' | 'neutral' {
  if (status === 'verified') return 'primary';
  if (status === 'stale') return 'danger';
  if (status === 'candidate' || status === 'unsupported') return 'caution';
  return 'neutral';   // 缺失
}

/**
 * 一条答案的**依据到底是什么** —— 把 `status` 与既有 `provenance` 合起来读。
 *
 * 五种要分清的情形:
 *  - 与当前有效记录一致        (verified + authoritative_record)
 *  - 来源原文记载              (verified + reference_evidence / material_record)
 *  - 用户提供、待确认          (candidate + user_reported)
 *  - 模型解释候选              (candidate,其余)
 *  - 来源变化，需要重新核对    (stale)
 * 外加"没有可支撑的依据"(unsupported)与"未核实"(缺 assessment)。
 */
export function assessmentMeaning(
  assessment: SafetyAnswerAssessmentDto | null | undefined,
  provenance: string | null | undefined,
): { headline: string; detail: string } {
  if (!assessment) {
    return {
      headline: ASSESSMENT_MISSING_LABEL,
      detail: '这条答案没有核验记录。没有核验记录不等于已经核对通过，'
        + '也不能按"已验证"理解——请按「依据尚未核对」来看它。',
    };
  }
  const reason = assessment.reason?.trim();
  const because = reason ? `服务端给出的原因：${reason}` : null;
  const join = (...parts: (string | null)[]) => parts.filter(Boolean).join('');
  // 注意:`VERIFIED_SCOPE_NOTICE` **不**写进 detail —— 它由调用方在 verified
  // 徽标下面单独渲染一次。写两遍只会让这句边界显得像套话。
  switch (assessment.status) {
    case 'verified':
      if (provenance === 'authoritative_record') {
        return {
          headline: '与当前有效记录一致',
          detail: join('这条答案取自当前有效的患者记录，核对了它依据的记录版本。', because),
        };
      }
      if (provenance === 'reference_evidence') {
        return {
          headline: '来源原文记载（已核对）',
          detail: join('这条答案的依据是一段已回读的参考资料原文，核对方式是与原文逐字比对。',
            because),
        };
      }
      if (provenance === 'material_record') {
        return {
          headline: '与上传材料的记载一致（材料本身未经核实）',
          detail: join('这条答案与您上传材料里的记载一致；材料写了什么已经核对，',
            '但材料本身说了什么不等于事实。', because),
        };
      }
      if (provenance === 'professional_opinion') {
        return {
          headline: '与已记录的专业意见一致（本系统未连接真实医护服务）',
          detail: join('依据是一条已记录的专业意见。', because),
        };
      }
      return {
        headline: '依据已核对（来源属性未记录）',
        detail: join('服务端判定这条答案的依据已核对，但没有记录来源属性。', because),
      };
    case 'candidate':
      if (provenance === 'user_reported') {
        return {
          headline: '用户提供、待确认',
          detail: join('这条答案来自您提供的信息。',
            '您陈述的内容不会因为被记下来就变成已核实的事实，需要另行核对。', because),
        };
      }
      if (provenance === 'professional_opinion') {
        return {
          headline: '专业意见候选（尚未核对完）',
          detail: join('这条答案的依据是一条专业意见，核对尚未完成。', because),
        };
      }
      return {
        headline: '模型解释候选（依据尚未核对完）',
        detail: join('这条答案有候选依据，但核对没有完成——它现在是一条候选，不是结论。',
          because),
      };
    case 'stale':
      // 依赖的版本由调用方单独渲染一条警示,这里不重复列一遍。
      return {
        headline: '来源变化，需要重新核对',
        detail: join('这条答案曾经有依据，但它依赖的记录版本已经变化，'
          + '因此不再适用于当前记录，需要重新核对。', because),
      };
    case 'unsupported':
      return {
        headline: '没有可支撑的依据',
        detail: join('服务端没有找到能支撑这条答案的依据。',
          '这不代表这条内容是错的，只表示它现在没有依据。', because),
      };
    default:
      return {
        headline: `未识别的核验状态（${assessment.status}）`,
        detail: join('这条核验状态读不出来，不能按"已核对"理解。', because),
      };
  }
}

/** 依赖版本引用 → 一句人话(空数组时说清楚"没有记录依赖")。 */
export function dependencyRefsText(refs: string[] | undefined): string {
  if (!refs || refs.length === 0) return '这条答案没有记录它依赖的记录版本。';
  return `它依赖的记录版本：${refs.join('、')}。`;
}

/** 原文定位:`locator` 定位不到时服务端给 null——界面不替它编一个位置。 */
export function locatorText(locator: string | null | undefined): string {
  return locator?.trim()
    ? `原文定位：${locator}`
    : '这条答案没有记录原文定位（服务端未给出可定位的位置）。';
}

// ---- 长期跟进:措辞约束 -------------------------------------------------------

/** 填写时间/条件**不**等于确认。这句必须出现在排期表单旁边。 */
export const SCHEDULE_IS_NOT_CONFIRMATION_NOTICE =
  '填写时间或触发条件只表示「排了期」，不表示已确认。'
  + '确认是另一次单独的操作，且只会记录您本人这一次的确认。';

/** 没有已排期的安排时，确认按钮为什么不可用。 */
export const NOTHING_TO_CONFIRM_NOTICE =
  '现在没有已排期（或已到期）的安排，因此没有可以确认的东西。'
  + '请先安排一次跟进，再做确认。';

/** 取消不删历史。 */
export const CANCEL_KEEPS_HISTORY_NOTICE =
  '取消不会删掉这条安排的时间与条件——历史留着，只是调度状态变成「已取消」。';

// ---- 执行任务状态 ------------------------------------------------------------

/**
 * 关联执行任务的状态。到期、排队、等待用户、执行失败必须各自说清 ——
 * 它们不是同一件事,也不能都读成"在处理中"。
 */
export const CARE_TASK_STATUS_LABELS: Record<string, string> = {
  ready: '可以继续',
  running: '正在处理',
  waiting_input: '等待补充',
  waiting_review: '等待本地模拟审核',
  completed: '已完成',
  cancelled: '已取消',
  failed: '处理未完成',
};

export function careTaskStatusLabel(status: string): string {
  return CARE_TASK_STATUS_LABELS[status] ?? `未识别状态（${status}）`;
}
