-- DESIGN DRAFT ONLY. Execute only against an empty temporary database.
-- Not a migration; application policies/overlap checks are specified in the design.
PRAGMA foreign_keys = ON;
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE subjects (
  subject_id TEXT PRIMARY KEY,
  display_label TEXT NOT NULL,
  baseline_known_us INTEGER
);
CREATE TABLE commits (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  known_us INTEGER NOT NULL UNIQUE,
  observed_clock_us INTEGER NOT NULL,
  policy_version TEXT NOT NULL
);
CREATE TABLE source_events (
  event_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
  client_instance_id TEXT NOT NULL,
  client_event_id TEXT NOT NULL,
  payload_hash TEXT,
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN
    ('caregiver_report','record_document','external_text','tool_observation','model_inference','legacy_import','user_record_action')),
  source_actor TEXT NOT NULL,
  quoted_actor TEXT,
  independent_source_group TEXT NOT NULL,
  source_uri TEXT,
  raw_text TEXT,
  payload_json TEXT CHECK(payload_json IS NULL OR json_valid(payload_json)),
  received_us INTEGER NOT NULL,
  timezone TEXT NOT NULL,
  time_anchor_us INTEGER NOT NULL,
  process_status TEXT NOT NULL CHECK(process_status IN ('pending','processing','committed','failed','deleted')),
  attempt_token TEXT,
  lease_until_us INTEGER,
  committed_seq INTEGER REFERENCES commits(seq),
  last_error_code TEXT,
  deleted_us INTEGER,
  UNIQUE(subject_id,client_instance_id,client_event_id),
  UNIQUE(subject_id,event_id)
);
CREATE INDEX events_pending ON source_events(process_status,lease_until_us,received_us);
CREATE TABLE extraction_attempts (
  attempt_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES source_events(event_id),
  extractor_version TEXT NOT NULL,
  model_id TEXT,
  prompt_version TEXT,
  normalization_revision TEXT NOT NULL,
  mode TEXT NOT NULL,
  proposals_json TEXT CHECK(proposals_json IS NULL OR json_valid(proposals_json)),
  error_code TEXT,
  started_us INTEGER NOT NULL,
  finished_us INTEGER
);
-- Registry makes dependencies/ref existence and subject boundaries enforceable by FK.
CREATE TABLE memory_objects (
  ref TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
  object_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version > 0),
  kind TEXT NOT NULL CHECK(kind IN ('assertion','artifact','conflict','scope','legacy')),
  created_seq INTEGER NOT NULL REFERENCES commits(seq),
  deleted_seq INTEGER REFERENCES commits(seq),
  UNIQUE(subject_id,kind,object_id,version),
  UNIQUE(subject_id,ref)
);
CREATE TABLE assertions (
  ref TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  attempt_id TEXT REFERENCES extraction_attempts(attempt_id),
  candidate_index INTEGER NOT NULL,
  subject_relation TEXT NOT NULL CHECK(subject_relation IN ('target','other','unknown')),
  namespace TEXT NOT NULL,
  canonical_key TEXT NOT NULL,
  assertion_mode TEXT NOT NULL CHECK(assertion_mode IN ('affirmed','negated','uncertain','hypothetical')),
  raw_value_json TEXT CHECK(raw_value_json IS NULL OR json_valid(raw_value_json)),
  normalized_value_json TEXT CHECK(normalized_value_json IS NULL OR json_valid(normalized_value_json)),
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
  span_start INTEGER,
  span_end INTEGER,
  span_kind TEXT NOT NULL CHECK(span_kind IN ('text','payload_pointer','legacy_missing')),
  payload_pointer TEXT,
  extraction_reliability TEXT NOT NULL CHECK(extraction_reliability IN ('rule_supported','model_proposed','unresolved','legacy_unknown')),
  reliability_reasons_json TEXT NOT NULL CHECK(json_valid(reliability_reasons_json)),
  source_verification TEXT NOT NULL CHECK(source_verification IN ('unverified','document_linked','document_checked','legacy_unknown')),
  proposed_valid_from_us INTEGER,
  proposed_valid_to_us INTEGER,
  time_precision TEXT NOT NULL CHECK(time_precision IN ('instant','day','month','interval','unknown','legacy_unknown')),
  time_expression TEXT,
  time_basis TEXT NOT NULL,
  FOREIGN KEY(subject_id,ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,event_id) REFERENCES source_events(subject_id,event_id),
  UNIQUE(event_id,attempt_id,candidate_index),
  CHECK((span_start IS NULL AND span_end IS NULL) OR (span_start >= 0 AND span_end > span_start)),
  CHECK(proposed_valid_to_us IS NULL OR proposed_valid_from_us IS NULL OR proposed_valid_to_us > proposed_valid_from_us)
);
CREATE INDEX assertions_key ON assertions(subject_id,namespace,canonical_key);
CREATE INDEX assertions_source ON assertions(subject_id,event_id);
CREATE TABLE mutations (
  mutation_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  commit_seq INTEGER NOT NULL REFERENCES commits(seq),
  operation TEXT NOT NULL CHECK(operation IN ('accept_report','quarantine','dispute','verify_record','correct','retract','restore','time_change','exclude_subject','legacy_baseline','delete')),
  target_ref TEXT,
  new_ref TEXT,
  actor TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  basis_json TEXT CHECK(basis_json IS NULL OR json_valid(basis_json)),
  valid_from_us INTEGER,
  valid_to_us INTEGER,
  reverses_mutation_id TEXT REFERENCES mutations(mutation_id),
  FOREIGN KEY(subject_id,event_id) REFERENCES source_events(subject_id,event_id),
  FOREIGN KEY(subject_id,target_ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,new_ref) REFERENCES memory_objects(subject_id,ref)
);
CREATE INDEX mutations_target ON mutations(subject_id,target_ref,commit_seq);
-- Bitemporal projection. Payload assertions are immutable except explicit erasure.
-- Closing sys_to_seq is the only ordinary UPDATE to an existing slice.
CREATE TABLE state_slices (
  slice_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  namespace TEXT NOT NULL,
  slot_key TEXT NOT NULL,
  branch_key TEXT NOT NULL,
  assertion_ref TEXT NOT NULL,
  state_json TEXT CHECK(state_json IS NULL OR json_valid(state_json)),
  record_status TEXT NOT NULL CHECK(record_status IN ('recorded_as_reported','verified','pending_verification','disputed','retracted','excluded_subject','legacy_unverified')),
  valid_from_us INTEGER,
  valid_to_us INTEGER,
  time_precision TEXT NOT NULL,
  sys_from_seq INTEGER NOT NULL REFERENCES commits(seq),
  sys_to_seq INTEGER REFERENCES commits(seq),
  cause_mutation_id TEXT NOT NULL REFERENCES mutations(mutation_id),
  FOREIGN KEY(subject_id,assertion_ref) REFERENCES memory_objects(subject_id,ref),
  CHECK(valid_to_us IS NULL OR valid_from_us IS NULL OR valid_to_us > valid_from_us),
  CHECK(sys_to_seq IS NULL OR sys_to_seq > sys_from_seq)
);
CREATE INDEX state_temporal ON state_slices(subject_id,namespace,sys_from_seq,sys_to_seq,valid_from_us,valid_to_us);
CREATE INDEX state_slot ON state_slices(subject_id,namespace,slot_key,branch_key,sys_to_seq,valid_from_us);
CREATE INDEX state_ref ON state_slices(subject_id,assertion_ref,sys_to_seq);
CREATE TABLE scope_revisions (
  subject_id TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision >= 0),
  ref TEXT NOT NULL UNIQUE,
  commit_seq INTEGER NOT NULL REFERENCES commits(seq),
  change_manifest_json TEXT CHECK(change_manifest_json IS NULL OR json_valid(change_manifest_json)),
  PRIMARY KEY(subject_id,scope_key,revision),
  FOREIGN KEY(subject_id,ref) REFERENCES memory_objects(subject_id,ref)
);
CREATE INDEX scopes_at ON scope_revisions(subject_id,scope_key,commit_seq DESC);
CREATE TABLE artifacts (
  ref TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('snapshot','tool_observation','conclusion','summary','context_packet','evidence','version_manifest','task_event')),
  logical_key TEXT NOT NULL,
  predecessor_ref TEXT,
  body_json TEXT CHECK(body_json IS NULL OR json_valid(body_json)),
  valid_at_us INTEGER,
  known_seq INTEGER REFERENCES commits(seq),
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
  input_vector_json TEXT NOT NULL CHECK(json_valid(input_vector_json)),
  tool_version TEXT,
  rule_version TEXT,
  corpus_revision TEXT,
  policy_version TEXT NOT NULL,
  result_code TEXT NOT NULL,
  complete INTEGER NOT NULL CHECK(complete IN (0,1)),
  cache_key TEXT,
  FOREIGN KEY(subject_id,ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,predecessor_ref) REFERENCES memory_objects(subject_id,ref)
);
CREATE INDEX artifacts_logical ON artifacts(subject_id,artifact_kind,logical_key);
CREATE INDEX artifacts_cache ON artifacts(subject_id,cache_key);
CREATE TABLE artifact_status (
  ref TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  commit_seq INTEGER NOT NULL REFERENCES commits(seq),
  status TEXT NOT NULL CHECK(status IN ('current','historical','stale','pending_recheck','superseded','deleted')),
  reason_code TEXT NOT NULL,
  cause_ref TEXT,
  PRIMARY KEY(ref,commit_seq),
  FOREIGN KEY(subject_id,ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,cause_ref) REFERENCES memory_objects(subject_id,ref)
);
CREATE TABLE dependencies (
  subject_id TEXT NOT NULL,
  child_ref TEXT NOT NULL,
  parent_ref TEXT NOT NULL,
  dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('fact','collection','query_result','artifact','evidence','tool_version','rule_version','corpus','policy')),
  selector_json TEXT NOT NULL CHECK(json_valid(selector_json)),
  PRIMARY KEY(child_ref,parent_ref,dependency_kind),
  FOREIGN KEY(subject_id,child_ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,parent_ref) REFERENCES memory_objects(subject_id,ref),
  CHECK(child_ref <> parent_ref)
);
CREATE INDEX reverse_dependency ON dependencies(subject_id,parent_ref,child_ref);
CREATE TABLE conflicts_v4 (
  ref TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  conflict_type TEXT NOT NULL,
  left_ref TEXT NOT NULL,
  right_ref TEXT NOT NULL,
  scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
  fingerprint TEXT NOT NULL,
  FOREIGN KEY(subject_id,ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,left_ref) REFERENCES memory_objects(subject_id,ref),
  FOREIGN KEY(subject_id,right_ref) REFERENCES memory_objects(subject_id,ref),
  UNIQUE(subject_id,fingerprint),
  CHECK(left_ref <> right_ref)
);
CREATE TABLE conflict_actions (
  action_id TEXT PRIMARY KEY,
  conflict_ref TEXT NOT NULL REFERENCES conflicts_v4(ref),
  mutation_id TEXT NOT NULL REFERENCES mutations(mutation_id),
  commit_seq INTEGER NOT NULL REFERENCES commits(seq),
  status TEXT NOT NULL CHECK(status IN ('open','resolved','dismissed','reopened')),
  record_checked_by TEXT,
  evidence_refs_json TEXT CHECK(evidence_refs_json IS NULL OR json_valid(evidence_refs_json)),
  resolution_scope_json TEXT NOT NULL CHECK(json_valid(resolution_scope_json)),
  reverses_action_id TEXT REFERENCES conflict_actions(action_id),
  UNIQUE(conflict_ref,commit_seq)
);
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
  task_kind TEXT NOT NULL CHECK(task_kind IN ('verify_record','resolve_conflict','recheck','rebuild_cache','erase')),
  logical_key TEXT NOT NULL,
  target_ref TEXT,
  generation INTEGER NOT NULL CHECK(generation > 0),
  expected_vector_json TEXT NOT NULL CHECK(json_valid(expected_vector_json)),
  status TEXT NOT NULL CHECK(status IN ('pending','running','retry','succeeded','needs_input','cancelled','obsolete','failed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  available_us INTEGER NOT NULL,
  lease_until_us INTEGER,
  lease_token TEXT,
  last_error_code TEXT,
  created_seq INTEGER NOT NULL REFERENCES commits(seq),
  completed_seq INTEGER REFERENCES commits(seq),
  FOREIGN KEY(subject_id,target_ref) REFERENCES memory_objects(subject_id,ref),
  UNIQUE(subject_id,task_kind,logical_key,generation)
);
CREATE INDEX task_queue ON tasks(status,available_us,lease_until_us);
CREATE TABLE working_items (
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  expires_us INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','resolved','expired')),
  PRIMARY KEY(subject_id,session_id,turn_id,item_key)
);
CREATE TABLE legacy_refs (
  legacy_ref TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  imported_ref TEXT NOT NULL,
  legacy_layer TEXT NOT NULL,
  legacy_id INTEGER NOT NULL,
  legacy_version INTEGER NOT NULL CHECK(legacy_version > 0),
  baseline_seq INTEGER NOT NULL REFERENCES commits(seq),
  imported_payload_json TEXT CHECK(imported_payload_json IS NULL OR json_valid(imported_payload_json)),
  history_quality TEXT NOT NULL CHECK(history_quality IN ('baseline_only','supported_partial')),
  FOREIGN KEY(subject_id,imported_ref) REFERENCES memory_objects(subject_id,ref),
  UNIQUE(legacy_layer,legacy_id,legacy_version)
);
CREATE TABLE deletion_requests (
  request_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
  event_ids_json TEXT NOT NULL CHECK(json_valid(event_ids_json)),
  target_refs_json TEXT NOT NULL CHECK(json_valid(target_refs_json)),
  requested_seq INTEGER NOT NULL REFERENCES commits(seq),
  status TEXT NOT NULL CHECK(status IN ('blocked_reads','purging','completed','failed')),
  backup_disposition TEXT NOT NULL,
  completed_us INTEGER
);
-- Do not INSERT schema_version='4' here: only a verified migration may do that.
