/**
 * 类型化 API 客户端:所有端点的唯一出口(原有 + 本轮新增,见
 * docs/frontend/api-contract.md)。页面不得直接 fetch。
 */
import { request, newIdempotencyKey } from './http';
import type {
  AcceptanceDto, ArtifactDto, ArtifactReportDto, CancelRunDto,
  ChangeImpactDto, ConflictActionDto, ConflictDto, ConflictRecordDto, ConclusionDto,
  EpisodicEventDto, EventRequest, EvidenceReadDto, FactActionResponseDto, FailedEventDto,
  HealthDto, HistorySearchDto, MedicationRecordDto, MemoryItemDto,
  MemoryStateDto, OverviewDto, Page, RecheckTasksDto, RunProgressDto,
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
