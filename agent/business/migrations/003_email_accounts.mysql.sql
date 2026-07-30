-- M1 non-sensitive mailbox account metadata. Secret values remain in the OS Secret Store.
-- MySQL runtime repository parity is delivered in M6; this file fixes the target schema only.
CREATE TABLE IF NOT EXISTS ops_email_account (
  account_id CHAR(36) PRIMARY KEY,
  display_name VARCHAR(50) NOT NULL,
  provider VARCHAR(32) NOT NULL,
  address VARCHAR(320) NOT NULL,
  secret_ref VARCHAR(255) NOT NULL,
  folder VARCHAR(255) NOT NULL DEFAULT 'INBOX',
  inbound_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  outbound_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  poll_seconds INT UNSIGNED NOT NULL DEFAULT 60,
  sender_name VARCHAR(80) NOT NULL DEFAULT 'NanoClaw Sales',
  allowed_senders_json JSON NOT NULL,
  allowed_recipients_json JSON NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'disabled',
  last_checked_at DATETIME(6) NULL,
  last_error_code VARCHAR(128) NULL,
  config_version BIGINT UNSIGNED NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  deleted_at DATETIME(6) NULL,
  UNIQUE KEY uk_email_account_provider_address(provider,address),
  UNIQUE KEY uk_email_account_secret_ref(secret_ref),
  KEY ix_email_account_status(status,deleted_at),
  CONSTRAINT ck_email_account_poll CHECK (poll_seconds BETWEEN 30 AND 3600)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ops_email_account_audit (
  audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  account_id CHAR(36) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  action VARCHAR(64) NOT NULL,
  changed_fields_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL,
  KEY ix_email_account_audit(account_id,created_at),
  CONSTRAINT fk_email_account_audit FOREIGN KEY(account_id) REFERENCES ops_email_account(account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
