-- M4/M6 controlled SMTP delivery outbox. Apply after 001 and 003.
CREATE TABLE IF NOT EXISTS ops_email_delivery (
  delivery_id CHAR(36) PRIMARY KEY,
  idempotency_key CHAR(64) NOT NULL,
  account_id CHAR(36) NOT NULL,
  quote_id BIGINT UNSIGNED NOT NULL COMMENT 'ops_quote.quote_key',
  quote_version INT NOT NULL,
  approval_key BIGINT UNSIGNED NOT NULL,
  recipient VARCHAR(320) NOT NULL,
  subject_snapshot VARCHAR(998) NOT NULL,
  body_snapshot MEDIUMTEXT NOT NULL,
  content_hash CHAR(64) NOT NULL,
  snapshot_hash CHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  max_attempts INT UNSIGNED NOT NULL DEFAULT 5,
  next_attempt_at DATETIME(6) NULL,
  lease_owner VARCHAR(128) NULL,
  lease_until DATETIME(6) NULL,
  smtp_message_id VARCHAR(255) NOT NULL,
  smtp_accepted_at DATETIME(6) NULL,
  last_error_code VARCHAR(128) NULL,
  internet_message_id VARCHAR(255) NULL,
  in_reply_to VARCHAR(512) NULL,
  created_by VARCHAR(128) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  content_redacted_at DATETIME(6) NULL,
  UNIQUE KEY uk_email_delivery_idempotency(idempotency_key),
  KEY ix_email_delivery_due(status,next_attempt_at,lease_until),
  KEY ix_email_delivery_account(account_id,created_at),
  KEY ix_email_delivery_quote(quote_id,quote_version),
  CONSTRAINT fk_email_delivery_account FOREIGN KEY(account_id) REFERENCES ops_email_account(account_id),
  CONSTRAINT fk_email_delivery_quote FOREIGN KEY(quote_id) REFERENCES ops_quote(quote_key),
  CONSTRAINT fk_email_delivery_approval FOREIGN KEY(approval_key) REFERENCES ops_approval_record(approval_key),
  CONSTRAINT ck_email_delivery_attempts CHECK(attempt_count >= 0 AND max_attempts > 0),
  CONSTRAINT ck_email_delivery_status CHECK(status IN
    ('pending','sending','retry_wait','accepted','dead_letter','stale','outcome_unknown'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ops_email_delivery_audit (
  audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  delivery_id CHAR(36) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  action VARCHAR(64) NOT NULL,
  error_code VARCHAR(128) NULL,
  created_at DATETIME(6) NOT NULL,
  KEY ix_email_delivery_audit(delivery_id,created_at),
  CONSTRAINT fk_email_delivery_audit FOREIGN KEY(delivery_id) REFERENCES ops_email_delivery(delivery_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

