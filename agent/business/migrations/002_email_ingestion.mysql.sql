-- Read-only inbound email persistence. Apply after 001_trade_ops_core.mysql.sql.
CREATE TABLE IF NOT EXISTS ops_inbound_email (
  email_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, account_id VARCHAR(128) NOT NULL,
  provider VARCHAR(32) NOT NULL, folder VARCHAR(255) NOT NULL, uidvalidity BIGINT UNSIGNED NOT NULL,
  uid BIGINT UNSIGNED NOT NULL, internet_message_id VARCHAR(512) NOT NULL DEFAULT '', raw_sha256 CHAR(64) NOT NULL,
  from_address_ciphertext TEXT NULL, from_name_ciphertext TEXT NULL, subject_ciphertext TEXT NULL,
  body_object_ref VARCHAR(1024) NULL, envelope_json JSON NOT NULL, status VARCHAR(32) NOT NULL,
  extraction_json JSON NULL, extraction_mode VARCHAR(32) NULL, extractor_version VARCHAR(128) NULL,
  rfq_id VARCHAR(64) NULL, lease_owner VARCHAR(128) NULL, lease_until DATETIME(6) NULL,
  next_retry_at DATETIME(6) NULL, attempt_count INT UNSIGNED NOT NULL DEFAULT 0, last_error_code VARCHAR(128) NULL,
  created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uk_email_uid (account_id,folder,uidvalidity,uid),
  KEY ix_email_fallback (account_id,internet_message_id(191),raw_sha256), KEY ix_email_work (status,next_retry_at,lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS ops_inbound_email_attachment (
  attachment_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, email_id BIGINT UNSIGNED NOT NULL,
  filename_ciphertext TEXT NULL, mime_type VARCHAR(255) NOT NULL, size_bytes BIGINT UNSIGNED NOT NULL,
  sha256 CHAR(64) NOT NULL, scan_status VARCHAR(32) NOT NULL, object_ref VARCHAR(1024) NULL,
  CONSTRAINT fk_email_attachment FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS ops_email_sync_cursor (
  account_id VARCHAR(128) NOT NULL, folder VARCHAR(255) NOT NULL, uidvalidity BIGINT UNSIGNED NOT NULL,
  last_uid BIGINT UNSIGNED NOT NULL, version BIGINT UNSIGNED NOT NULL DEFAULT 1, updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY(account_id,folder)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS ops_email_processing_attempt (
  attempt_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, email_id BIGINT UNSIGNED NOT NULL, attempt_no INT UNSIGNED NOT NULL,
  stage VARCHAR(32) NOT NULL, error_code VARCHAR(128) NULL, started_at DATETIME(6) NOT NULL, ended_at DATETIME(6) NULL,
  CONSTRAINT fk_email_attempt FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id),
  UNIQUE KEY uk_email_attempt(email_id,attempt_no,stage)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS ops_email_review_audit (
  audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, email_id BIGINT UNSIGNED NOT NULL, reviewer VARCHAR(128) NOT NULL,
  action VARCHAR(32) NOT NULL, changes_json JSON NOT NULL, created_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_email_review FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS ops_email_notification_outbox (
  notification_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, email_id BIGINT UNSIGNED NOT NULL,
  channel VARCHAR(32) NOT NULL, target_id VARCHAR(255) NOT NULL, target_type VARCHAR(16) NOT NULL,
  notification_version INT UNSIGNED NOT NULL DEFAULT 1, content TEXT NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending', attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
  next_retry_at DATETIME(6) NULL, last_error_code VARCHAR(128) NULL, created_at DATETIME(6) NOT NULL, sent_at DATETIME(6) NULL,
  UNIQUE KEY uk_email_notification(email_id,channel,target_id,target_type,notification_version),
  KEY ix_email_notification_work(status,next_retry_at),
  CONSTRAINT fk_email_notification FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
