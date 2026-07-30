-- M6 shared runtime metadata. Apply explicitly with the migration credential.
-- Request coordination intentionally stores no message or response body.

CREATE TABLE IF NOT EXISTS runtime_conversation (
  conversation_id CHAR(36) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL DEFAULT '',
  account_id VARCHAR(128) NOT NULL DEFAULT '',
  owner_id VARCHAR(128) NOT NULL,
  channel VARCHAR(32) NOT NULL,
  title VARCHAR(120) NOT NULL,
  status ENUM('active','archived','deleted') NOT NULL DEFAULT 'active',
  message_file VARCHAR(255) NULL,
  version BIGINT UNSIGNED NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_message_at DATETIME(6) NULL,
  deleted_at DATETIME(6) NULL,
  PRIMARY KEY (conversation_id),
  KEY idx_runtime_conversation_owner
    (tenant_id,account_id,channel,status,updated_at,conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS runtime_message (
  message_id CHAR(36) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL DEFAULT '',
  account_id VARCHAR(128) NOT NULL DEFAULT '',
  conversation_id CHAR(36) NOT NULL,
  role ENUM('user','assistant','tool') NOT NULL,
  content_json JSON NOT NULL,
  request_id CHAR(36) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  archived_at DATETIME(6) NULL,
  PRIMARY KEY (message_id),
  UNIQUE KEY uq_runtime_message_request
    (tenant_id,account_id,conversation_id,request_id,role),
  KEY idx_runtime_message_owner_time
    (tenant_id,account_id,conversation_id,created_at,message_id),
  CONSTRAINT fk_runtime_message_conversation FOREIGN KEY (conversation_id)
    REFERENCES runtime_conversation(conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS runtime_request_ledger (
  scope_key CHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL DEFAULT '',
  account_id VARCHAR(128) NOT NULL DEFAULT '',
  channel VARCHAR(32) NOT NULL,
  conversation_id CHAR(36) NOT NULL,
  request_id CHAR(36) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  status ENUM('accepted','running','completed','retryable_failed') NOT NULL,
  lease_owner VARCHAR(96) NULL,
  lease_expires_at DATETIME(6) NULL,
  response_message_id CHAR(36) NULL,
  last_error_code VARCHAR(64) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  completed_at DATETIME(6) NULL,
  expires_at DATETIME(6) NOT NULL,
  PRIMARY KEY (scope_key),
  UNIQUE KEY uq_runtime_request_scope
    (tenant_id,account_id,channel,conversation_id,request_id),
  KEY idx_runtime_request_expiry (status,expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS runtime_conversation_lease (
  conversation_scope_key CHAR(64) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL DEFAULT '',
  account_id VARCHAR(128) NOT NULL DEFAULT '',
  channel VARCHAR(32) NOT NULL,
  conversation_id CHAR(36) NOT NULL,
  lease_owner VARCHAR(96) NOT NULL,
  lease_expires_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (conversation_scope_key),
  KEY idx_runtime_lease_expiry (lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS runtime_deletion_job (
  job_id CHAR(36) NOT NULL,
  tenant_id VARCHAR(128) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  conversation_id CHAR(36) NOT NULL,
  request_id VARCHAR(128) NOT NULL,
  status ENUM('pending','running','completed','failed') NOT NULL DEFAULT 'pending',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (job_id),
  UNIQUE KEY uq_runtime_deletion_request (tenant_id,account_id,request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
