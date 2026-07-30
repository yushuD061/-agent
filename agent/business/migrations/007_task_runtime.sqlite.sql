PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS task_instance (
  task_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  owner_type TEXT NOT NULL CHECK(owner_type IN ('workspace','customer')),
  owner_id TEXT NOT NULL,
  customer_account_id TEXT,
  conversation_id TEXT NOT NULL,
  title TEXT NOT NULL,
  template_id TEXT NOT NULL,
  template_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  active_plan_version INTEGER NOT NULL DEFAULT 1,
  current_step_key TEXT,
  context_json TEXT NOT NULL,
  pause_requested INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  last_sequence INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_owner
  ON task_instance(tenant_id,customer_account_id,conversation_id,status,updated_at);

CREATE TABLE IF NOT EXISTS task_plan_revision (
  task_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  instruction_id TEXT,
  plan_json TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  diff_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(task_id,plan_version),
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
);

CREATE TABLE IF NOT EXISTS task_step (
  step_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  step_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  label_key TEXT NOT NULL,
  executor TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  audience TEXT NOT NULL,
  dependencies_json TEXT NOT NULL,
  status TEXT NOT NULL,
  input_hash TEXT,
  output_json TEXT NOT NULL DEFAULT '{}',
  output_hash TEXT,
  error_code TEXT,
  checkpoint_id TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_until TEXT,
  started_at TEXT,
  completed_at TEXT,
  superseded_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(task_id,plan_version,step_key),
  FOREIGN KEY(task_id,plan_version) REFERENCES task_plan_revision(task_id,plan_version)
);
CREATE INDEX IF NOT EXISTS idx_task_step_claim
  ON task_step(status,lease_until,task_id,plan_version,ordinal);

CREATE TABLE IF NOT EXISTS task_step_attempt (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  worker_id TEXT NOT NULL,
  status TEXT NOT NULL,
  input_hash TEXT,
  output_hash TEXT,
  error_code TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(step_id,attempt_no),
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id),
  FOREIGN KEY(step_id) REFERENCES task_step(step_id)
);

CREATE TABLE IF NOT EXISTS task_checkpoint (
  checkpoint_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  step_key TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  output_hash TEXT NOT NULL,
  output_json TEXT NOT NULL,
  artifact_refs_json TEXT NOT NULL,
  executor_version TEXT NOT NULL,
  valid INTEGER NOT NULL DEFAULT 1,
  invalidated_by_instruction_id TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id),
  FOREIGN KEY(step_id) REFERENCES task_step(step_id)
);
CREATE INDEX IF NOT EXISTS idx_task_checkpoint_latest
  ON task_checkpoint(task_id,step_key,valid,created_at);

CREATE TABLE IF NOT EXISTS task_instruction (
  instruction_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  content TEXT NOT NULL,
  changes_json TEXT NOT NULL,
  impact_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  applied_at TEXT,
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
);

CREATE TABLE IF NOT EXISTS task_human_action (
  action_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_key TEXT NOT NULL,
  audience TEXT NOT NULL,
  status TEXT NOT NULL,
  prompt_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  decision_json TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_human_pending
  ON task_human_action(task_id,audience,status,created_at);

CREATE TABLE IF NOT EXISTS task_artifact (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_key TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  visibility TEXT NOT NULL CHECK(visibility IN ('internal','customer')),
  kind TEXT NOT NULL,
  file_name TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  approved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
);

CREATE TABLE IF NOT EXISTS task_event (
  event_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  step_key TEXT,
  safe_json TEXT NOT NULL,
  internal_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  UNIQUE(task_id,sequence),
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
);

CREATE TABLE IF NOT EXISTS task_resume_outbox (
  command_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  command_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
);

CREATE TABLE IF NOT EXISTS task_idempotency (
  scope TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope,idempotency_key)
);
