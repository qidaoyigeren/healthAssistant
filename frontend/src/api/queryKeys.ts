/** TanStack Query 查询 key 注册表 —— 提交 committed 后按 key 失效刷新。 */

export const qk = {
  health: ['health'] as const,
  overview: ['overview'] as const,
  memoryState: (validAt?: string, knownAt?: string) =>
    ['memoryState', validAt ?? 'current', knownAt ?? 'latest'] as const,
  conflicts: ['conflicts', 'open'] as const,
  conflictRecords: (status: string) => ['conflictRecords', status] as const,
  conflictRecord: (id: number | string) => ['conflictRecord', id] as const,
  alertRecords: (status: string, kind?: string) => ['alertRecords', status, kind ?? 'all'] as const,
  alertRecord: (id: number | string) => ['alertRecord', id] as const,
  medicationRecords: (status: string) => ['medicationRecords', status] as const,
  medicationRecord: (id: number | string) => ['medicationRecord', id] as const,
  historyEvents: (filters: Record<string, string>) => ['historyEvents', filters] as const,
  eventTypes: ['eventTypes'] as const,
  historySearch: (q: string) => ['historySearch', q] as const,
  recheckTasks: ['recheckTasks'] as const,
  sessions: ['sessions'] as const,
  sessionEvents: (sessionId: string) => ['sessionEvents', sessionId] as const,
  turnTrace: (sessionId: string, turnId: string) => ['turnTrace', sessionId, turnId] as const,
  artifacts: ['dataArtifacts'] as const,
};

/** 一次业务事件提交成功后需要刷新的查询族。 */
export const INVALIDATE_AFTER_COMMIT = [
  qk.overview,
  qk.memoryState(),
  qk.conflicts,
  qk.conflictRecords('all'),
  qk.alertRecords('current'),
  qk.alertRecords('all'),
  qk.medicationRecords('all'),
  qk.recheckTasks,
  qk.sessions,
  qk.eventTypes,
] as const;
