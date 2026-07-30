CREATE TABLE IF NOT EXISTS task_instance (
  task_id CHAR(36) PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL,
  owner_type ENUM('workspace','customer') NOT NULL, owner_id VARCHAR(128) NOT NULL,
  customer_account_id VARCHAR(128), conversation_id CHAR(36) NOT NULL,
  title VARCHAR(255) NOT NULL, template_id VARCHAR(96) NOT NULL,
  template_version INT NOT NULL, status VARCHAR(32) NOT NULL,
  active_plan_version INT NOT NULL DEFAULT 1, current_step_key VARCHAR(96),
  context_json JSON NOT NULL, pause_requested BOOLEAN NOT NULL DEFAULT FALSE,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE, last_sequence BIGINT NOT NULL DEFAULT 0,
  version BIGINT NOT NULL DEFAULT 1, error_code VARCHAR(128),
  created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL, completed_at DATETIME(6),
  INDEX idx_task_owner(tenant_id,customer_account_id,conversation_id,status,updated_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_plan_revision (
  task_id CHAR(36) NOT NULL, plan_version INT NOT NULL, instruction_id CHAR(36),
  plan_json JSON NOT NULL, plan_hash CHAR(64) NOT NULL, diff_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL, PRIMARY KEY(task_id,plan_version),
  CONSTRAINT fk_task_plan_task FOREIGN KEY(task_id) REFERENCES task_instance(task_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_step (
  step_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, plan_version INT NOT NULL,
  step_key VARCHAR(96) NOT NULL, ordinal INT NOT NULL, label_key VARCHAR(128) NOT NULL,
  executor VARCHAR(128) NOT NULL, risk_level VARCHAR(32) NOT NULL,
  audience VARCHAR(32) NOT NULL, dependencies_json JSON NOT NULL, status VARCHAR(32) NOT NULL,
  input_hash CHAR(64), output_json JSON NOT NULL, output_hash CHAR(64), error_code VARCHAR(128),
  checkpoint_id CHAR(36), attempt_count INT NOT NULL DEFAULT 0,
  lease_owner VARCHAR(128), lease_until DATETIME(6), started_at DATETIME(6),
  completed_at DATETIME(6), superseded_at DATETIME(6), created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL, UNIQUE KEY uq_task_step(task_id,plan_version,step_key),
  INDEX idx_task_step_claim(status,lease_until,task_id,plan_version,ordinal),
  CONSTRAINT fk_task_step_plan FOREIGN KEY(task_id,plan_version)
    REFERENCES task_plan_revision(task_id,plan_version)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_step_attempt (
  attempt_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, step_id CHAR(36) NOT NULL,
  attempt_no INT NOT NULL, worker_id VARCHAR(128) NOT NULL, status VARCHAR(32) NOT NULL,
  input_hash CHAR(64), output_hash CHAR(64), error_code VARCHAR(128),
  started_at DATETIME(6) NOT NULL, completed_at DATETIME(6), UNIQUE KEY uq_step_attempt(step_id,attempt_no)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_checkpoint (
  checkpoint_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, step_id CHAR(36) NOT NULL,
  plan_version INT NOT NULL, step_key VARCHAR(96) NOT NULL, input_hash CHAR(64) NOT NULL,
  output_hash CHAR(64) NOT NULL, output_json JSON NOT NULL, artifact_refs_json JSON NOT NULL,
  executor_version VARCHAR(64) NOT NULL, valid BOOLEAN NOT NULL DEFAULT TRUE,
  invalidated_by_instruction_id CHAR(36), created_at DATETIME(6) NOT NULL,
  INDEX idx_task_checkpoint_latest(task_id,step_key,valid,created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_instruction (
  instruction_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, conversation_id CHAR(36) NOT NULL,
  actor_type VARCHAR(32) NOT NULL, actor_id VARCHAR(128) NOT NULL, content TEXT NOT NULL,
  changes_json JSON NOT NULL, impact_json JSON NOT NULL, status VARCHAR(32) NOT NULL,
  created_at DATETIME(6) NOT NULL, applied_at DATETIME(6)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_human_action (
  action_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, step_key VARCHAR(96) NOT NULL,
  audience VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL, prompt_key VARCHAR(128) NOT NULL,
  payload_json JSON NOT NULL, decision_json JSON, created_at DATETIME(6) NOT NULL,
  resolved_at DATETIME(6), resolved_by VARCHAR(128),
  INDEX idx_task_human_pending(task_id,audience,status,created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_artifact (
  artifact_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, step_key VARCHAR(96) NOT NULL,
  plan_version INT NOT NULL, visibility ENUM('internal','customer') NOT NULL, kind VARCHAR(32) NOT NULL,
  file_name VARCHAR(255) NOT NULL, storage_path TEXT NOT NULL, byte_size BIGINT NOT NULL,
  sha256 CHAR(64) NOT NULL, approved BOOLEAN NOT NULL DEFAULT FALSE, created_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_event (
  event_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, sequence BIGINT NOT NULL,
  type VARCHAR(96) NOT NULL, status VARCHAR(32) NOT NULL, step_key VARCHAR(96),
  safe_json JSON NOT NULL, internal_json JSON NOT NULL, occurred_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_task_event(task_id,sequence)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_resume_outbox (
  command_id CHAR(36) PRIMARY KEY, task_id CHAR(36) NOT NULL, command_type VARCHAR(32) NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL UNIQUE, payload_json JSON NOT NULL,
  status VARCHAR(32) NOT NULL, lease_owner VARCHAR(128), lease_until DATETIME(6),
  attempt_count INT NOT NULL DEFAULT 0, next_attempt_at DATETIME(6), error_code VARCHAR(128),
  created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
  INDEX idx_task_resume_claim(status,next_attempt_at,lease_until)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS task_idempotency (
  scope VARCHAR(255) NOT NULL, idempotency_key VARCHAR(255) NOT NULL,
  payload_hash CHAR(64) NOT NULL, response_json JSON NOT NULL, created_at DATETIME(6) NOT NULL,
  PRIMARY KEY(scope,idempotency_key)
) ENGINE=InnoDB;
