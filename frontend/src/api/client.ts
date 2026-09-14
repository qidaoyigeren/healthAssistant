/**
 * 类型化 API 客户端:所有端点的唯一出口(原有 + 本轮新增,见
 * docs/frontend/api-contract.md)。页面不得直接 fetch。
 */
import { request, newIdempotencyKey } from './http';
import type {
  AcceptanceDto, ArtifactDto, ArtifactReportDto, CancelRunDto, CareTaskDto,
  CareTaskInputResultDto, ChangeImpactDto, ConflictActionDto, ConflictDto,
  ConflictRecordDto, ConclusionDto, EpisodicEventDto, EventRequest, EvidenceReadDto,
  FactActionResponseDto, FailedEventDto, HealthDto, HistorySearchDto, MedicationRecordDto,
  MemoryItemDto, MemoryStateDto, OverviewDto, Page, RecheckTasksDto,
  RunProgressDto, SafetyCaseDto, SafetyCaseListDto, SafetyClosureEvidenceDto,
  SafetyFollowUpConditionDto, SafetyFollowUpConfirmationDto, SafetyFollowUpInputDto,
  SafetyChangeNoteDto, SafetyMainlineDto, SafetyVisitDto,
  SessionDto, SessionEventDto, TurnTraceDto, WarningDto,
} from './types';

export const api = {
  // ---- 既有端点 ---------------------------------------------------------

  submitEvent(body: EventRequest, idempotencyKey: string, signal?: AbortSignal) {
    return request<AcceptanceDto>('/v1/events', {
      method: 'POST',
      body,
      headers: { 'Idempotency-Key': idempotencyKey },
      signal,
    });
  },

  eventStatus(idempotencyKey: string, signal?: AbortSignal) {
    // 202 = queued/processing;200 = committed;500 = failed(另一种错误结构)
    return request<AcceptanceDto | FailedEventDto>(
      `/v1/events/${encodeURIComponent(idempotencyKey)}`, { signal });
  },

  retryEvent(idempotencyKey: string) {
    return request<AcceptanceDto>(`/v1/events/${encodeURIComponent(idempotencyKey)}/retry`, {
      method: 'POST',
    });
  },

  memoryState(params: { valid_at?: string; known_at?: string } = {}, signal?: AbortSignal) {
    const query = new URLSearchParams();
    if (params.valid_at) query.set('valid_at', params.valid_at);
    if (params.known_at) query.set('known_at', params.known_at);
    const qs = query.toString();
    return request<MemoryStateDto>(`/v1/memory/state${qs ? `?${qs}` : ''}`, { signal });
  },

  conflicts(signal?: AbortSignal) {
    return request<ConflictDto[]>('/v1/memory/conflicts', { signal });
  },

  conflictAction(conflictId: number, body: {
    action: string; basis: string; actor: string; chosen_ref?: string;
  }) {
    return request<ConflictRecordDto>(`/v1/conflicts/${conflictId}/actions`, {
      method: 'POST', body,
    });
  },

  runRechecks(maxJobs = 5) {
    return request<{ status: string; pending: number; completed: Record<string, unknown>[] }>(
      '/v1/rechecks', { method: 'POST', body: { max_jobs: maxJobs } });
  },

  health(signal?: AbortSignal) {
    return request<HealthDto>('/v1/health', { signal });
  },

  // ---- Harness P2: run progress + cancellation ---------------------------

  runProgress(runId: string, after = 0, signal?: AbortSignal) {
    const query = after > 0 ? `?after=${after}` : '';
    return request<RunProgressDto>(
      `/v1/runs/${encodeURIComponent(runId)}/progress${query}`, { signal });
  },

  cancelRun(runId: string, reason?: string) {
    return request<CancelRunDto>(`/v1/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      body: reason ? { reason } : {},
    });
  },

  // ---- 本轮新增读模型 ----------------------------------------------------

  overview(signal?: AbortSignal) {
    return request<OverviewDto>('/v1/overview', { signal });
  },

  alertRecords(params: { status?: string; kind?: string; limit?: number; cursor?: string } = {},
               signal?: AbortSignal) {
    return pageRequest<ConclusionDto>('/v1/alert-records', params, signal);
  },

  alertRecord(id: number, signal?: AbortSignal) {
    return request<ConclusionDto>(`/v1/alert-records/${id}`, { signal });
  },

  // ---- Product P1: 证据原文回读 / 变更影响 --------------------------------

  evidenceRead(evidenceId: string, offset = 0, limit = 2000, signal?: AbortSignal) {
    const query = `?offset=${offset}&limit=${limit}`;
    return request<EvidenceReadDto>(
      `/v1/evidence/${encodeURIComponent(evidenceId)}${query}`, { signal });
  },

  changeImpact(since?: string | null, limit = 50, signal?: AbortSignal, runId?: string | null) {
    const query = new URLSearchParams();
    if (runId) query.set('run_id', runId);
    else if (since) query.set('since', since);
    query.set('limit', String(limit));
    return request<ChangeImpactDto>(`/v1/change-impact?${query.toString()}`, { signal });
  },

  conclusionHistory(id: number, signal?: AbortSignal) {
    return request<ConclusionChainOnly>(`/v1/conclusions/${id}/history`, { signal });
  },

  historyEvents(params: {
    limit?: number; cursor?: string; event_type?: string; q?: string;
    occurred_from?: string; occurred_to?: string;
  } = {}, signal?: AbortSignal) {
    return pageRequest<EpisodicEventDto>('/v1/history/events', params, signal);
  },

  eventTypes(signal?: AbortSignal) {
    return request<{ event_type: string; count: number }[]>('/v1/history/event-types', { signal });
  },

  historySearch(q: string, limit = 10, signal?: AbortSignal) {
    return request<HistorySearchDto>(
      `/v1/history/search?q=${encodeURIComponent(q)}&limit=${limit}`, { signal });
  },

  medicationRecords(params: { status?: string; limit?: number; cursor?: string } = {},
                    signal?: AbortSignal) {
    return pageRequest<MedicationRecordDto>('/v1/medication-records', params, signal);
  },

  medicationRecord(id: number, signal?: AbortSignal) {
    return request<MedicationRecordDto>(`/v1/medication-records/${id}`, { signal });
  },

  conflictRecords(params: { status?: string; limit?: number; cursor?: string } = {},
                  signal?: AbortSignal) {
    return pageRequest<ConflictRecordDto>('/v1/conflict-records', params, signal);
  },

  conflictRecord(id: number, signal?: AbortSignal) {
    return request<ConflictRecordDto>(`/v1/conflict-records/${id}`, { signal });
  },

  conflictActions(conflictId: number, signal?: AbortSignal) {
    return request<ConflictActionDto[]>(`/v1/conflicts/${conflictId}/history`, { signal });
  },

  memoryItem(ref: string, signal?: AbortSignal) {
    return request<MemoryItemDto>(`/v1/memory/item?ref=${encodeURIComponent(ref)}`, { signal });
  },

  factActions(body: { ref: string; action: 'verify' | 'retract'; actor: string; basis: string }) {
    return request<FactActionResponseDto>('/v1/memory/fact-actions', { method: 'POST', body });
  },

  recheckTasks(signal?: AbortSignal) {
    return request<RecheckTasksDto>('/v1/recheck-tasks', { signal });
  },

  sessions(signal?: AbortSignal) {
    return request<SessionDto[]>('/v1/sessions', { signal });
  },

  sessionEvents(sessionId: string, params: { limit?: number; cursor?: string } = {},
                signal?: AbortSignal) {
    return pageRequest<SessionEventDto>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/events`, params, signal);
  },

  turnTrace(sessionId: string, turnId: string, signal?: AbortSignal) {
    return request<TurnTraceDto>(
      `/v1/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/trace`,
      { signal });
  },

  // ---- 导出 / 备份 -------------------------------------------------------

  createExport() {
    return request<ArtifactReportDto>('/v1/data/exports', { method: 'POST' });
  },

  createBackup() {
    return request<ArtifactReportDto>('/v1/data/backups', { method: 'POST' });
  },

  artifacts(signal?: AbortSignal) {
    return request<ArtifactDto[]>('/v1/data/artifacts', { signal });
  },

  artifactVerify(artifactId: string) {
    return request<Record<string, unknown>>(
      `/v1/data/artifacts/${encodeURIComponent(artifactId)}/verify`);
  },

  artifactDownloadUrl(artifactId: string): string {
    const base = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '');
    return `${base}/v1/data/artifacts/${encodeURIComponent(artifactId)}/download`;
  },

  // ---- 长期用药安全事项(主线)------------------------------------------

  safetyMainline(signal?: AbortSignal) {
    return request<SafetyMainlineDto>('/v1/safety-mainline', { signal });
  },

  safetyCases(signal?: AbortSignal) {
    return request<SafetyCaseListDto>('/v1/safety-cases', { signal });
  },

  safetyCase(caseId: string, signal?: AbortSignal) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}`, { signal });
  },

  /**
   * 记录"我看到了"。**只**写一个时间戳:不关闭事项、不清空未决项,
   * 所以界面上不能把它说成"已处理"。
   */
  safetyCaseSeen(caseId: string, key: string) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/seen`, {
      method: 'POST', body: { key },
    });
  },

  /**
   * 关闭前先问服务端「能不能关、为什么」。只读,不写入。
   * 界面用它决定关闭按钮是否可用——不让用户去按一个一定会被拒绝的按钮。
   */
  safetyCaseClosureEvidence(caseId: string, signal?: AbortSignal) {
    return request<SafetyClosureEvidenceDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/closure-evidence`, { signal });
  },

  /**
   * 回答事项上的一条补问。空值、"明说不知道"与有内容地回答走**不同**路径,
   * 所以 `answer_kind` 省略时由服务端按内容判定(provided / unknown / empty)。
   */
  safetyCaseAnswer(caseId: string, body: {
    key: string; expected_revision: number; request_id: string; value: string;
    /**
     * 省略时由**服务端**按内容判定,界面不猜。
     * 回访里另加四种表态:`done` 已完成 / `not_done` 尚未完成 /
     * `declined` 暂不回答 / `changed` 情况有变化。它们**含义不同**,
     * 只有 `done`(且问题确实是跟进行动)才可能把那条问题答上。
     */
    answer_kind?: 'provided' | 'unknown' | 'empty' | 'done' | 'not_done' | 'declined' | 'changed';
  }) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/answer`, {
      method: 'POST', body,
    });
  },

  /**
   * 按依据处置事项。关闭条件由服务端强制:依据不合法时返回 409 + 中文说明,
   * 调用方必须**原样**显示这条说明,不能自己改写成别的结论。
   *
   * **不发送 `actor`**:操作者身份由服务端从认证上下文取,请求体里的身份不被读取。
   */
  safetyCaseDisposition(caseId: string, body: {
    key: string; expected_revision: number; disposition: string; basis_kind: string;
    note?: string; decision_id?: string; follow_up?: SafetyFollowUpInputDto;
  }) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/disposition`, {
      method: 'POST', body,
    });
  },

  /**
   * 安排 / 改期一条长期跟进(CONTRACT.md §4.6.1)。
   *
   * 请求体里**没有 `confirmed`**:送出时间或触发条件只表示"排了期",
   * 不表示"已确认" —— 确认是另一个动作(`safetyCaseFollowUpConfirmation`)。
   * 事项已 `resolved` 时服务端返回 409(终态不可再安排)。
   */
  safetyCaseFollowUpSchedule(caseId: string, body: {
    key: string; expected_revision: number;
    kind: 'review_at' | 'on_event' | 'arrangement';
    at?: string;
    condition?: SafetyFollowUpConditionDto;
    owner?: string;
    note?: string;
  }) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/follow-up`, {
      method: 'POST', body: { ...body, action: 'schedule' },
    });
  },

  /**
   * 取消一条长期跟进(§4.6.2)。取消**不清空** `at`/`condition`/`owner`/`note`:
   * 历史留着,靠 `schedule_state='cancelled'` 表达"已取消"。
   */
  safetyCaseFollowUpCancel(caseId: string, body: {
    key: string; expected_revision: number; reason?: string;
  }) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/follow-up`, {
      method: 'POST', body: { ...body, action: 'cancel' },
    });
  },

  /**
   * 确认一条**已经排期**的安排(§4.6.3)。这是产生 `confirmed: true` 的**唯一**路径。
   * 没有已安排(`schedule_state ∈ {scheduled, due}`)的安排时服务端返回 409。
   * `confirmed_by` 由服务端从认证上下文取,请求体自称无效。
   */
  safetyCaseFollowUpConfirmation(caseId: string, body: SafetyFollowUpConfirmationDto) {
    return request<SafetyCaseDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/follow-up/confirmation`, {
        method: 'POST', body,
      });
  },

  /**
   * 开始或**继续**一次回访。
   *
   * 已经有未结束的回访就接着它走,不新开一次——新开会让用户已经答过的问题变成
   * 上一访的遗留,他回来看到的第一题又是原来那道。返回新的 CaseView(`visit` 块
   * 里有这次回访的全部内容)。
   */
  safetyCaseStartVisit(caseId: string, body: { key: string; expected_revision: number }) {
    return request<SafetyCaseDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/visits`, {
      method: 'POST', body,
    });
  },

  /** 读一次回访的**持久结果**:本次为什么跟进、新增了什么、做完了什么、还剩什么。 */
  safetyCaseVisit(caseId: string, visitId: string) {
    return request<SafetyVisitDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}`);
  },

  /**
   * 声明一条**待确认**的用药变更候选。这不是写入:确认之前当前药单一个字节都不变。
   * `value` 是新值;`before` 由服务端从当前权威记录取(不采信调用方自报的前值)。
   */
  safetyCaseProposeChange(caseId: string, visitId: string,
                          body: { key: string; name: string; field: string; value: string; note?: string }) {
    return request<SafetyCaseDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}/candidates`, {
        method: 'POST', body,
      });
  },

  /** 确认候选 → 沿**既有权威入口**写入,必要安全检查按既有路径重新排队。 */
  safetyCaseConfirmChange(caseId: string, visitId: string, candidateId: string,
                          body: { key: string }) {
    return request<SafetyCaseDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}` +
      `/candidates/${encodeURIComponent(candidateId)}/confirm`, { method: 'POST', body });
  },

  /** 放弃候选。**什么都不写**——权威记录本来就没被它碰过。 */
  safetyCaseDismissChange(caseId: string, visitId: string, candidateId: string,
                          body: { key: string }) {
    return request<SafetyCaseDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}` +
      `/candidates/${encodeURIComponent(candidateId)}/dismiss`, { method: 'POST', body });
  },

  /**
   * 提交一段**自由文本**的「补充情况」。
   *
   * 服务端把它收录下来（原文照留），再跑一次**有预算上限**的理解，产出待确认的
   * 候选、需要补问的问题，以及**仍在计划中**的变更。调用方不必先把用户的话翻译
   * 成结构化字段——但确认之前当前药单一个字节都不会变。
   */
  safetyCaseSubmitNote(caseId: string, visitId: string,
                       body: { key: string; text: string; speech_act?: string; tz?: string }) {
    return request<SafetyChangeNoteDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}/notes`, {
        method: 'POST', body,
      });
  },

  /** 这次回访上收到过的补充及其理解结果（刷新后仍在）。 */
  safetyCaseNotes(caseId: string, visitId: string, signal?: AbortSignal) {
    return request<{ items: SafetyChangeNoteDto[] }>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}/notes`,
      { signal });
  },

  /**
   * **显式**重试一次理解。不自动重试、不换模型、不扩大预算——
   * 重试是一次有人按下的动作。
   */
  safetyCaseRetryNote(caseId: string, visitId: string, noteId: string,
                      body: { key: string }) {
    return request<SafetyChangeNoteDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}` +
      `/notes/${encodeURIComponent(noteId)}/retry`, { method: 'POST', body });
  },

  /**
   * 确认**整组**换药变更——一次事务原子写入。
   * 组状态由各成员派生，这里不判断"换药是否完成"。
   */
  safetyCaseConfirmGroup(caseId: string, visitId: string, candidateId: string,
                         body: { key: string }) {
    return request<SafetyCaseDto>(
      `/v1/safety-cases/${encodeURIComponent(caseId)}/visits/${encodeURIComponent(visitId)}` +
      `/candidates/${encodeURIComponent(candidateId)}/confirm-group`, { method: 'POST', body });
  },

  /** 围绕这一件事项发起一次有界调查。返回排队中的 care_task,进度另轮询。 */
  safetyCaseInvestigate(caseId: string, body: { key: string; budget?: number; goal?: string }) {
    return request<CareTaskDto>(`/v1/safety-cases/${encodeURIComponent(caseId)}/investigate`, {
      method: 'POST', body,
    });
  },

  careTasks(signal?: AbortSignal) {
    return request<{ items: CareTaskDto[] }>('/v1/care-tasks', { signal });
  },

  /**
   * 补充信息。只关闭**指名回答**的那一条请求;`answers[].kind` 只能是
   * `user_report`(用户报告)或 `material_note`——用户提供的信息不是权威记录。
   */
  careTaskInput(taskId: string, body: {
    key: string; revision: number; review_request_ids: string[];
    answers?: { request_id: string; value: string; kind?: 'user_report' | 'material_note' }[];
  }) {
    return request<CareTaskInputResultDto>(
      `/v1/care-tasks/${encodeURIComponent(taskId)}/input`, { method: 'POST', body });
  },

  careTaskResume(taskId: string, body: { key: string; revision: number; action?: string }) {
    return request<CareTaskDto>(
      `/v1/care-tasks/${encodeURIComponent(taskId)}/resume`, { method: 'POST', body });
  },
};

export { newIdempotencyKey };
export type { WarningDto, ConclusionDto, ConflictDto };

interface ConclusionChainOnly {
  versions: ConclusionDto[];
  current_head: number | null;
  status: string | null;
  stale_reason: string | null;
}

function pageRequest<T>(path: string, params: Record<string, unknown>,
                        signal?: AbortSignal) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const qs = query.toString();
  return request<Page<T>>(`${path}${qs ? `?${qs}` : ''}`, { signal });
}
