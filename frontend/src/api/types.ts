/**
 * 真实后端 DTO(以 stage0/server.py + memory.py 序列化字段为准,2026-09-06)。
 * 缺失字段就是 null/undefined —— 页面显示「未记录」,不做类型断言造假。
 */

// ---- memory state ---------------------------------------------------------

export interface SemanticFactDto {
  id: number;
  namespace: string;
  fact_key: string;
  value: unknown;
  status: 'active' | 'superseded' | 'disputed' | 'retracted' | string;
  valid_from: string | null;
  valid_to: string | null;
  source: string;
  source_uri: string | null;
  version: number;
  salience: number;
  created_at: string;
  updated_at: string;
  extraction_mode: string | null;
  verification_status: 'recorded_as_reported' | 'verified' | 'disputed' | string;
  ref: string;
}

export interface MedicationDto {
  id: number;
  medication_key: string;
  display_name: string;
  ingredients: string[];
  dose: string | null;
  route: string | null;
  schedule: string | null;
  status: 'active' | 'stopped' | 'superseded' | 'disputed' | string;
  start_at: string;
  end_at: string | null;
  source: string;
  source_uri: string | null;
  version: number;
  predecessor_id: number | null;
  created_at: string;
  ref: string;
}

export interface ConflictDto {
  id: number;
  conflict_type: string;
  subject_key: string;
  left_ref: string;
  right_ref: string;
  description: string;
  status: 'open' | 'resolved' | 'dismissed' | string;
  resolution?: unknown;
  resolution_json?: string | null;
  source: string;
  created_at: string;
  resolved_at: string | null;
  ref: string;
}

export interface MemoryStateDto {
  valid_at: string;
  known_at: string;
  facts: SemanticFactDto[];
  medications: MedicationDto[];
  open_conflicts: ConflictDto[];
  uncertainties: SemanticFactDto[];
  meta: {
    medications_revision: number;
    semantic_revision: number;
    future_leak_guard: boolean;
  };
}

// ---- events (async submission) --------------------------------------------

export type EventTypeName =
  | 'register_profile'
  | 'profile_update'
  | 'medication_change'
  | 'procedure_exposure'
  | 'query_current_medications'
  | 'user_message';

export interface EventRequest {
  event_type: EventTypeName;
  text: string;
  payload: Record<string, unknown>;
  source: string;
  occurred_at: string | null;
  session_id: string;
}

export interface AcceptanceDto {
  event_key: string;
  event_id?: string;
  run_id?: string;
  status: 'queued' | 'processing' | 'committed';
  status_url?: string;
  response?: EventResponseDto;
}

export interface SourceRefDto {
  source_type?: string;
  uri?: string | null;
  quote?: string | null;
  retrieval?: string;
  text?: string;
}

export interface WarningDto {
  drug_a: string;
  drug_b: string;
  severity: 'contraindicated' | 'major' | 'moderate' | 'minor' | 'unknown' | string;
  mechanism: string | null;
  effect: string | null;
  management: string | null;
  source_text: string | null;
  source_url: string | null;
  confidence: 'high' | 'medium' | 'low' | string;
  detection_path: string | null;
  citations?: SourceRefDto[];
  additional_sources?: SourceRefDto[];
  audit_trail?: {
    warning_memory?: string;
    conclusion?: number;
    memory_refs?: string[];
    source_refs?: SourceRefDto[];
  };
}

export interface OperationOutcomeDto {
  kind: 'semantic_fact' | 'medication_change' | string;
  outcome: string;
  ref?: string | null;
  namespace?: string | null;
  key?: string | null;
  display_name?: string | null;
  event_ref?: string | null;
  replayed?: boolean;
}

export interface EventResponseDto {
  text: string;
  warnings: WarningDto[];
  conflicts: ConflictDto[];
  audit_trail: {
    session_id?: string;
    turn_id?: string;
    memory_refs?: string[];
    source_refs?: SourceRefDto[];
    reflection?: string[];
    response_source?: 'llm' | 'template' | 'template_fallback' | string;
    response_fallback_reason?: string | null;
  };
  safety_status: string;
  operation_outcomes?: OperationOutcomeDto[];
  event_id?: string;
  run_id?: string;
}

export type FailedEventDto = {
  event_key: string;
  status: 'failed';
  error_class?: string | null;
  error?: string;
};

// ---- read models ----------------------------------------------------------

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total: number;
}

export interface ConclusionDto {
  id: number;
  session_id: string | null;
  turn_id: string | null;
  kind: string;
  text: string;
  memory_refs: string[];
  source_refs: SourceRefDto[];
  created_at: string;
  status: 'current' | 'stale' | string;
  input_revision?: { medications?: number; semantic?: number } | null;
  predecessor_id: number | null;
  superseded_by: number | null;
  stale_reason: string | null;
  ref: string;
  severity: null; // 无结构化严重度列 —— 如实为 null
  confidence: null;
  evidence_available: boolean;
  chain?: ConclusionChainDto;
}

export interface ConclusionChainDto {
  versions: ConclusionDto[];
  current_head: number | null;
  status: string | null;
  stale_reason: string | null;
}

export interface EpisodicEventDto {
  id: number;
  event_type: string;
  subject_key: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
  recorded_at: string;
  source: string;
  source_uri: string | null;
  session_id: string;
  turn_id: string;
  salience: number;
  severity: string | null;
  version: number;
  fingerprint: string;
  parent_id: number | null;
  needs_verification: number;
  ref: string;
}

export interface MedicationRecordDto extends MedicationDto {
  versions?: MedicationDto[];
}

export interface ConflictActionDto {
  id: number;
  conflict_id: number;
  action: 'resolved' | 'dismissed' | 'reopened' | 'undo' | string;
  basis: string;
  actor: string;
  chosen_ref: string | null;
  previous_status: string | null;
  created_at: string;
  undone_by: number | null;
}

export interface ConflictRecordDto extends ConflictDto {
  sides?: Record<'left_ref' | 'right_ref', {
    ref: string;
    layer?: string;
    item?: Record<string, unknown>;
    audit_log?: AuditEntryDto[];
    error?: string;
  }>;
  actions?: ConflictActionDto[];
}

export interface AuditEntryDto {
  id: number;
  action: string;
  actor: string;
  target_type: string;
  target_id: number | null;
  details?: Record<string, unknown>;
  source: string | null;
  created_at: string;
}

export interface MemoryItemDto {
  ref: string;
  layer: string;
  item_id: number;
  version: number;
  item: Record<string, unknown>;
  audit_log: AuditEntryDto[];
}

export interface RecheckTaskDto {
  id: number;
  subject_id: string;
  task_type: string;
  target_id: number | null;
  reason: string | null;
  status: 'open' | 'running' | 'done' | 'failed' | 'cancelled' | string;
  attempts: number;
  created_at: string;
  updated_at: string;
  target_conclusion: ConclusionDto | null;
}

export interface RecheckTasksDto {
  pending: RecheckTaskDto[];
  history: RecheckTaskDto[];
  pending_count: number;
}

export interface SessionDto {
  session_id: string;
  event_count: number;
  first_at: string;
  last_at: string;
}

export interface SessionEventDto {
  id: number;
  session_id: string;
  turn_id: string;
  user_text: string | null;
  assistant_text: string | null;
  source: string;
  created_at: string;
  event_key: string | null;
  process_status: 'pending' | 'committed' | 'failed' | string;
  response: EventResponseDto | null; // 来自 outbox 持久化结果;没有就是 null
  idempotency_key: string | null;
  request: {
    event_type: string | null;
    text: string | null;
    occurred_at: string | null;
    source: string | null;
  } | null;
  outbox_status: string | null;
  outbox_error: string | null;
}

export interface TurnTraceDto {
  session_id: string;
  turn_id: string;
  traces: {
    id: number;
    cycle: number | null;
    phase: string;
    payload: Record<string, unknown>;
    created_at: string;
  }[];
}

export interface HistorySearchDto {
  mode: 'no_query' | 'fts5_trigram' | 'like_fallback' | string;
  results: EpisodicEventDto[];
  tokens: string[];
}

export interface OverviewDto {
  counts: {
    medications_active: number;
    medications_records: number;
    facts_active: number;
    facts_uncertain_time: number;
    conclusions_current: number;
    conclusions_stale: number;
    conflicts_open: number;
    rechecks_pending: number;
    outbox_pending: number;
  };
  last_recorded: {
    recorded_at: string;
    occurred_at: string;
    event_type: string;
  } | null;
}

// ---- health / data artifacts ----------------------------------------------

export interface HealthDto {
  status: string;
  db: string;
  schema_version: string | null;
  pending_outbox_tasks: number;
  pending_rechecks: number;
  llm_planner_enabled: boolean;
  worker_thread: boolean;
  graph_runner_enabled: boolean;
  auth_mode: string;
  uptime_seconds: number;
}

export interface ArtifactDto {
  artifact_id: string;
  kind: 'export' | 'backup';
  size_bytes: number;
  created_at: number;
}

export interface ArtifactReportDto {
  artifact_id: string;
  kind: 'export' | 'backup';
  schema_version?: string | null;
  row_counts?: Record<string, number>;
  created_at?: string;
  [key: string]: unknown;
}

export interface FactActionResponseDto {
  outcome: 'verified' | 'blocked_by_conflict' | 'retracted' | string;
  item: SemanticFactDto;
}
