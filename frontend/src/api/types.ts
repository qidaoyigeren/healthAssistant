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
  ingredients: Array<string | { name_cn?: string; name_en?: string; kegg?: string }>;
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
  // Product P1: 结论引用与不可变证据记录的关联。历史记录没有该字段 ——
  // 此时证据原文如实标记为不可用，不虚构。
  evidence_id?: string | null;
}

// Product P1: 受控证据原文读取（GET /v1/evidence/{id}）
export interface EvidenceReadDto {
  evidence_id: string;
  content: string;
  offset: number;
  returned_chars: number;
  total_chars: number;
  truncated: boolean;
  integrity: 'verified' | 'hash_mismatch' | string;
  source: {
    source_type?: string | null;
    uri?: string | null;
    content_ref?: string | null;
    corpus_version?: string | null;
    retrieved_at?: string | null;
    access_class?: string | null;
  };
}

// 预警详情中每条 source_ref 的证据可读性解析结果
export interface EvidenceRefStatusDto {
  evidence_id: string | null;
  status: 'available' | 'unavailable' | string;
  reason?: 'no_evidence_link' | 'evidence_missing' | string;
  integrity?: 'verified' | string;
  meta?: {
    content_chars?: number | null;
    corpus_version?: string | null;
    retrieved_at?: string | null;
    uri?: string | null;
  };
  source_index?: number;
  uri?: string | null;
  quote?: string | null;
}

export interface ConclusionExplanationDto {
  status: string;
  stale_reason: string | null;
  patient_revision: { medications: number; semantic: number };
  input_revision?: { medications?: number; semantic?: number } | null;
  fact_refs: string[];
  recheck: {
    id: number;
    task_type: string;
    target_id: number;
    reason: string | null;
    status: 'open' | 'running' | 'done' | 'failed' | 'cancelled' | string;
    attempts?: number;
    updated_at?: string;
  } | null;
  successor: { id: number; ref: string; kind: string; text: string; status: string; created_at: string } | null;
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
  // Product P1: 结构化结果包，与正文/卡片同源；可选字段保持向后兼容。
  answer_bundle?: AnswerBundleDto | null;
  event_id?: string;
  run_id?: string;
  // Harness P2: waiting_review / cancelled runs publish run status at the
  // top level so clients see non-terminal/取消 states without parsing audit.
  run_status?: string;
  review_case?: { id: number; status: string; round?: number } | null;
}

export type FailedEventDto = {
  event_key: string;
  status: 'failed';
  error_class?: string | null;
  error?: string;
};

// ---- Harness P2: run progress + cancellation --------------------------------

export interface RunProgressEventDto {
  event_id: string;
  seq: number;
  kind: string;
  cycle?: number | null;
  tool?: string | null;
  detail?: Record<string, unknown>;
  created_at: string;
}

export interface RunProgressDto {
  run_id: string;
  run_status?: string;
  events: RunProgressEventDto[];
  latest_seq: number;
  snapshot: boolean;
  note?: string;
}

export interface CancelRunDto {
  run_id: string;
  cancel_state: 'requested' | 'cancelled' | 'already_final' | 'unknown_run';
  run_status?: string | null;
  status_url?: string;
  note?: string;
}

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
  // Product P1: 证据可读性解析 + 解释结果（详情接口返回）
  evidence_refs?: EvidenceRefStatusDto[];
  explanation?: ConclusionExplanationDto;
}

// Product P1: 变更影响摘要（GET /v1/change-impact）
export interface ChangeImpactDto {
  generated_at: string;
  attribution?: 'run_audit' | 'audit_window';
  run_id?: string | null;
  affected_conclusions_total?: number;
  truncated?: boolean;
  patient_revision: { medications: number; semantic: number };
  summary: {
    changed_facts: number;
    affected_conclusions: number;
    pending_rechecks: number;
    failed_rechecks: number;
  };
  changed_facts: {
    id: number;
    action: string;
    actor: string;
    target_type: string;
    target_id: number | null;
    details: Record<string, unknown> | null;
    memory_refs: string[];
    source: string;
    created_at: string;
  }[];
  changed_facts_total: number;
  affected_conclusions: {
    conclusion_id: number;
    ref: string;
    kind: string;
    text: string;
    status: string;
    stale_reason: string | null;
    input_revision?: { medications?: number; semantic?: number } | null;
    recheck: ConclusionExplanationDto['recheck'];
    successor: ConclusionExplanationDto['successor'];
  }[];
  unaffected_conclusions: ConclusionDto[];
  note: string;
}

// Product P1: 最小 AnswerBundle（附加在事件响应上；旧客户端可忽略）
export interface MultiReviewDto {
  status?: 'completed' | 'incomplete' | 'unavailable';
  reason?: string;
  review_version: string;
  trigger: string;
  workers: { claim_id: string; status: string; worker_kind?: string | null; reason?: string }[];
  divergences: { claim_id: string; kind: string; refs: string[] }[];
  usage: { cycles: number; calls: number; usage_unknown: boolean };
  note?: string;
}

export interface AnswerBundleDto {
  execution_status?: string;
  goal_status?: string;
  answer_status?: string;
  investigation?: InvestigationDto | null;
  multi_review?: MultiReviewDto | null;
  route?: string | null;
  route_basis?: string | null;
  bundle_version: string;
  safety_status: string;
  claims: {
    claim_id: string;
    kind: 'warning' | 'conflict' | string;
    statement: string;
    status: string;
    evidence_refs: string[];
  }[];
  fact_refs: string[];
  evidence_refs: string[];
  patient_revision: { medications: number; semantic: number };
  unresolved_questions: string[];
  coverage: {
    consolidated?: boolean;
    response_source?: string | null;
    degraded_reason?: string | null;
  };
}

export interface InvestigationDto {
  version: string;
  contract_version: string;
  goal: string;
  mode: 'deterministic' | 'scripted' | 'llm';
  checks: Record<string, string>;
  gaps: { gap_id: string; kind: string; description: string; status: string }[];
  questions: { gap_id: string; field: string; question: string }[];
  claims: { claim_id: string; statement: string; status: string; supporting_evidence: string[]; opposing_evidence: string[] }[];
  evidence_refs: string[];
  termination_reason: string | null;
  retrieval_feedback?: { status: string; result_kind?: string }[];
  patient_version: { medications: number; semantic: number };
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

// ---- 长期用药安全事项(主线,stage0/safety_cases.py)-----------------------------
//
// 事项是一条**引用层**:它保存 ref,真相仍在 medications / semantic_memory /
// conclusions 里。所以 `available: false` 表示引用解析不了——如实显示"读取不到",
// 不替它编一个值。

/** 事项引用的一条记录:能读到时给出展示标签,读不到时保留 ref 并标记不可用。 */
export interface SafetyResolvedRefDto {
  ref: string;
  label: string;
  available: boolean;
  layer?: string | null;
  version?: number | null;
}

/**
 * 事项引用的检查结论(程序判定层)。
 *
 * `trigger_state` 是本轮的重点,三种取值含义完全不同,不能混着读:
 *  - `risk_present`      —— 检查显示风险仍然成立;
 *  - `trigger_eliminated`—— 触发条件已消失;
 *  - `unknown`           —— 无法判断。
 */
export interface SafetyConclusionDto {
  ref: string;
  available: boolean;
  conclusion_id?: number;
  kind?: string;
  text?: string;
  status?: string;
  stale_reason?: string | null;
  sources?: string[];
  trigger_state?: string;
  trigger_reasons?: string[];
}

/**
 * 一条等待补充的信息。`for_professional` 为真表示不能由用户代答。
 *
 * `status` 的三种取值不是同一件事,界面必须分开显示:
 *  - `open`     —— 还在等这位用户回答;
 *  - `unknown`  —— 用户明说不知道:**不再等他**,但问题**没有解决**,仍然阻止关闭,
 *                  需要改从其他来源核实(`needs_alternative_evidence: true`);
 *  - `answered` —— 有内容地回答过了。
 */
export interface SafetyRequiredInputDto {
  request_id: string;
  question: string;
  fields: string[];
  for_professional: boolean;
  why_needed?: string | null;
  status: string;
  asked_at?: string | null;
  answer_ref?: string | null;
  answered_at?: string | null;
  /** 服务端按内容判定的回答分类:provided / unknown / empty。 */
  answer_kind?: string | null;
  /** 用户明说不知道 —— 这条问题改由系统去找替代证据。 */
  needs_alternative_evidence?: boolean;
  /** 回答过期后被重新打开的原因(事实变了)。 */
  reopened_reason?: string | null;
  /**
   * 这条问题要弄清的是**哪一类信息** —— 它决定问题的身份。
   * patient_actual_state / material_record / general_reference / professional_judgment
   */
  question_kind?: string | null;
  /**
   * 这一次**从哪里取** —— 不参与身份,可以中途更换并留痕。
   * patient_record / ask_user / patient_material / general_reference / professional_review
   */
  question_strategy?: string | null;
  /** 换过来源就留在这里:from / to / reason(为什么换)。 */
  strategy_history?: { from?: string | null; to?: string | null; reason?: string | null }[];
  /**
   * 这条问题查到了什么程度:
   * not_attempted / attempted_no_result / source_limited / received_unconfirmed / available
   */
  information_state?: string | null;
  /** 已经答上的那部分(含来源属性与仍不确定的地方)。 */
  answered_parts?: SafetyAnsweredPartDto[];
  still_uncertain?: string[];
}

/**
 * 一条答案的**核验评估**(CONTRACT.md §3.2,由 A 产出)。
 *
 * `status` 的四种取值与「根本没有这个键」是**五个**不同的状态,不能合并:
 *  - `verified`    —— 在约定范围内,这条答案的**依据**已核对;
 *  - `candidate`   —— 有候选依据,但核对未完成;
 *  - `stale`       —— 曾有依据,但依赖的版本已变化,需要重新核对;
 *  - `unsupported` —— 没有可支撑这条答案的依据;
 *  - **缺失**      —— 未核实。缺失**不等于** `verified`,消费方不得补默认值。
 *
 * `verified` 的含义边界:它**只**说明依据核对过。它不是"用药安全",不是
 * "风险已排除",也不是任何专业医疗判断。
 */
export interface SafetyAnswerAssessmentDto {
  status: 'verified' | 'candidate' | 'stale' | 'unsupported' | string;
  /** 具体原因,服务端原话。界面原样显示,不改写成结论。 */
  reason: string;
  /** 来源引用;定位不到时为 null。 */
  source_ref: string | null;
  /** 字段路径或片段位置;定位不到时为 null(服务端不编造)。 */
  locator: string | null;
  /** 版本化依赖引用,例如 `memory:medication:45@2`。 */
  dependency_refs: string[];
}

/**
 * 一条已答上来的答案。
 *
 * 后端实际下发的键比历史声明宽(11 个);这里按**兼容式扩展**补齐,不改动既有
 * 键的语义。`assessment` 缺失时按「未核实」显示。
 */
export interface SafetyAnsweredPartDto {
  value?: string | null;
  field?: string | null;
  source?: string | null;
  provenance?: string | null;
  still_uncertain?: string[];
  source_ref?: string | null;
  quote?: string | null;
  origin?: string | null;
  answer_ref?: string | null;
  at?: string | null;
  /** 这条答案写下时的记录版本快照。 */
  version?: Record<string, number> | null;
  /** 核验评估。**缺失 = 未核实**,不是 verified。 */
  assessment?: SafetyAnswerAssessmentDto | null;
}

/**
 * 跟进安排的触发条件。**只接受白名单结构**(CONTRACT.md §4.3);
 * 不执行自然语言,也不接受任意表达式。未知 `kind` 服务端返回 422,
 * 并且**不得**静默降级成"永不触发"。
 */
export interface SafetyFollowUpConditionDto {
  kind: 'conclusion_recorded' | 'necessary_check' | 'medication_change'
    | 'fact_change' | string;
  /** 版本化引用,例如 `memory:conclusion:123@1`。 */
  ref: string;
  /** 仅 `conclusion_recorded` 使用。 */
  conclusion_kind?: string | null;
  /** 仅 `necessary_check` 使用。 */
  check_id?: number | string | null;
}

/**
 * 长期跟进安排(CONTRACT.md §4.2)。
 *
 * 两处必须分清:
 *  1. **`kind` 与 `schedule_state` 是两件事**。`kind` 是安排的种类(按时间 /
 *     按事件 / 仅备忘);`schedule_state` 是这条安排现在走到哪了。
 *     `kind='arrangement'` 的安排可以永久停在 `unscheduled`,这是合法的。
 *  2. **`confirmed` 与"有时间/有条件"是两件事**。§4.5 之后,仅有 `at` 或
 *     `condition` ⇒ `confirmed: false`,`schedule_state` 仍可为 `scheduled`。
 *     「已安排」不等于「已确认」。
 *
 * `confirmed: true` 时 `confirmed_at` 与 `confirmation_ref` 必须非空;三者不一致
 * 的记录视为损坏,按 `confirmed: false` 读(存量老记录一律按 false 读)。
 */
export interface SafetyFollowUpDto {
  kind: string;                  // review_at | on_event | arrangement
  confirmed: boolean;
  at?: string | null;
  /**
   * 白名单结构(§4.3)。类型里保留 `string`,是因为存量记录可能还存着契约收紧
   * 之前的自由文本 —— 界面如实显示它,不解析、不执行、不静默丢弃。
   */
  condition?: SafetyFollowUpConditionDto | string | null;
  owner?: string | null;
  note?: string | null;
  recorded_at?: string | null;

  confirmed_at?: string | null;
  confirmed_by?: string | null;
  confirmation_ref?: string | null;

  revision?: number;
  /** scheduled | due | triggered | blocked | cancelled | unscheduled */
  schedule_state?: string | null;
  last_triggered_at?: string | null;
  last_trigger_reason?: string | null;
  care_task_id?: string | null;
  blocked_reason?: string | null;
}

/** 提交「持续跟进」时可以带上的安排(服务端会自行判定是否可信)。 */
export interface SafetyFollowUpInputDto {
  kind?: 'review_at' | 'on_event' | 'arrangement';
  at?: string;
  /** §4.3:只接受白名单结构,自由文本会被 422。 */
  condition?: SafetyFollowUpConditionDto;
  owner?: string;
  note?: string;
}

/**
 * `POST /v1/safety-cases/{case_id}/follow-up` 的请求体(§4.6.1 安排/改期、
 * §4.6.2 取消)。**没有 `confirmed` 字段**:确认只能由确认端点产生,
 * 请求体里自称的确认一律被忽略。
 */
export interface SafetyFollowUpCommandDto {
  key: string;
  expected_revision: number;
  action: 'schedule' | 'cancel';
  // action='schedule' 时:
  kind?: 'review_at' | 'on_event' | 'arrangement';
  at?: string;
  condition?: SafetyFollowUpConditionDto;
  owner?: string;
  note?: string;
  // action='cancel' 时:
  reason?: string;
}

/** `POST /v1/safety-cases/{case_id}/follow-up/confirmation` 的请求体(§4.6.3)。 */
export interface SafetyFollowUpConfirmationDto {
  key: string;
  expected_revision: number;
  note?: string;
}

/**
 * 关闭所需证据的现场核对结果(GET /v1/safety-cases/{id}/closure-evidence)。
 * 只回答「能不能关、为什么」,不做任何写入;界面用它决定关闭按钮是否可用,
 * 不自己另算一套关闭条件。
 */
export interface SafetyClosureEvidenceDto {
  ok: boolean;
  reason: string | null;
  refs: string[];
  checked: { ref: string; state: string; reasons?: string[] }[];
  eliminated: string[];
  still_present: string[];
  blocking_inputs: string[];
}

/** 调查留下的未决问题(问句身份由服务端派生,跨会话稳定)。 */
export interface SafetyOpenQuestionDto {
  question_id: string;
  question?: string | null;
  field?: string | null;
  why?: string | null;
  direction?: string | null;
  status?: string | null;
}

/** 事项历史的单条记录;`event` 的取值见 safety_cases.py 的写入点。 */
export interface SafetyHistoryEntryDto {
  at: string;
  event: string;
  from?: string | null;
  to?: string | null;
  trigger?: SafetyTriggerDto | null;
  request_id?: string;
  for_professional?: boolean;
  answered?: boolean;
  answer_ref?: string | null;
  /** 服务端对这条回答的判定:provided / unknown / empty。 */
  answer_kind?: string | null;
  value?: unknown;
  disposition?: string | null;
  basis_kind?: string | null;
  actor?: string | null;
  note?: string | null;
  case_type?: string;
  /** 状态迁移/重新打开的原因(服务端原话)。 */
  why?: string | null;
  /** `answer_retired` / `resolution_basis_retired` 的原因。 */
  reason?: string | null;
  /** `resolution_basis_retired`:那条**不再生效**的旧依据,原样保留在历史里。 */
  previous_basis?: SafetyBasisDto | null;
  previous_disposition?: string | null;
  /** 持续跟进登记下来的安排。 */
  follow_up?: SafetyFollowUpDto | null;
}

/** 事项的触发原因(建事项时写下的那一笔)。 */
export interface SafetyTriggerDto {
  kind?: string;
  ref?: string;
  conclusion_kind?: string;
  check_id?: number | string;
  trigger?: string;
  trigger_kind?: string;
  [key: string]: unknown;
}

/** 处置依据:服务端现场校验后写下的那一份,种类决定它能不能关闭事项。 */
export interface SafetyBasisDto {
  kind?: string;
  actor?: string;
  at?: string;
  note?: string | null;
  decision_id?: string;
  review_case_id?: string;
  review_action?: string;
  conclusion_refs?: string[];
  input_revision?: Record<string, number> | null;
  checked_at?: string;
  /**
   * `kind === 'monitoring_arrangement'` 时才有:记下的是「凭什么说风险还在」,
   * **不是**关闭依据。`why_not_closed` 是服务端原话。
   */
  still_present?: string[];
  why_not_closed?: string | null;
}

/** 一个安全事项的完整视图(GET /v1/safety-cases/{id} 与各 POST 的返回体)。 */
export interface SafetyCaseDto {
  case_id: string;
  case_type: string;
  status: string;
  status_label: string;
  subject_keys: string[];
  trigger: SafetyTriggerDto | null;
  medications: SafetyResolvedRefDto[];
  facts: SafetyResolvedRefDto[];
  conclusions: SafetyConclusionDto[];
  evidence_refs: string[];
  open_questions: SafetyOpenQuestionDto[];
  required_inputs: SafetyRequiredInputDto[];
  /** 已回答的请求:它们补上了哪一部分、来源属性是什么。 */
  answered_inputs: SafetyRequiredInputDto[];
  answered_inputs_count: number;
  linked_run_ids: string[];
  next_action_summary: string | null;
  responsible_party: string | null;
  resolution_basis: SafetyBasisDto | null;
  disposition: string | null;
  /**
   * 持续跟进安排。服务端可能在视图里省略它——那时界面如实说「本次读取没有拿到
   * 跟进安排」,不替它编一个复查周期,也不假装"没有安排"就等于"没有风险"。
   * 视图里没有时,可以从历史里那次「持续跟进」处置记录里读到同一份安排。
   */
  follow_up?: SafetyFollowUpDto | null;
  /** 与本事项关联的复核决定(事项级审查用,不用于界面主张"已获专业确认")。 */
  linked_review_case_ids?: string[];
  user_seen_at: string | null;
  input_versions: Record<string, number>;
  revision: number;
  created_at: string;
  updated_at: string;
  history: SafetyHistoryEntryDto[];
}

export interface SafetyCaseListDto {
  items: SafetyCaseDto[];
  /** 服务端给出的状态 → 中文标签(界面不自己造标签)。 */
  statuses: Record<string, string>;
}

/**
 * 必要检查队列的真实状态。`available: false` = 读不到队列——这时**不能**把
 * "没有提示"当成"检查都通过了";`note` 是服务端对队列语义的原文说明。
 */
export interface NecessaryChecksDto {
  available: boolean;
  total?: number;
  open?: number;
  running?: number;
  failed?: number;
  last_at?: string | null;
  note?: string;
}

/** 主线页六块内容(GET /v1/safety-mainline)。顺序就是用户读它的顺序。 */
export interface SafetyMainlineDto {
  generated_at: string;
  current_medications: MedicationDto[];
  recent_medication_changes: EpisodicEventDto[];
  attention: SafetyCaseDto[];
  awaiting_user: SafetyCaseDto[];
  awaiting_professional: SafetyCaseDto[];
  needs_recheck: SafetyCaseDto[];
  recently_settled: SafetyCaseDto[];
  counts: {
    attention: number;
    awaiting_user: number;
    awaiting_professional: number;
    needs_recheck: number;
    settled: number;
  };
  necessary_checks: NecessaryChecksDto;
}

/** 照护待办(安全事项的调查任务也在这里)。字段以 /v1/care-tasks 实际序列化为准。 */
export interface CareTaskDto {
  id: string;
  goal_type: string;
  goal?: string;
  revision: number;
  status: string;
  case_id?: string | null;
  safety_case_id?: string | null;
  waiting_reason?: string | null;
  due_at?: string | null;
  budget?: { spent: number; limit: number };
  result_refs?: string[];
  partial_report_refs?: string[];
  missing_inputs?: {
    request_id?: string; gap_id?: string; field?: string; question?: string;
  }[];
  runs?: {
    run_id: string; runner?: string; status?: string;
    workflow_run_id?: string; started_at?: string; finished_at?: string;
  }[];
  active_run_id?: string | null;
  degraded_label?: string | null;
  retry_available?: boolean;
}

/** 补充提交的归因结果:哪些补充真的改了当前记录,哪些只是被如实记下来。 */
export interface CareTaskInputResultDto {
  task_id: string;
  applied_medications: number;
  applied_semantic_facts: number;
  additional_questions: string[];
  /** 被这次补充真正关闭的请求。 */
  answered_requests: string[];
  /** 记下来但没有关闭的请求(例如这条问题不该由用户回答)。 */
  recorded_only_requests: string[];
  authoritative_writes: number;
  recorded_answers: number;
}
