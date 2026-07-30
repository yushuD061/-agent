-- trade_ops core schema from doc/DATA_WAREHOUSE_DESIGN.md
-- MySQL 8 / InnoDB / utf8mb4. Run only after setting up the trade_ops database.
CREATE DATABASE IF NOT EXISTS trade_ops CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE trade_ops;

CREATE TABLE IF NOT EXISTS ops_customer (
  customer_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  customer_id VARCHAR(64) NOT NULL,
  company_name_masked VARCHAR(255) NOT NULL,
  country_code CHAR(2) NOT NULL,
  owner_user_id VARCHAR(64) NOT NULL,
  business_unit_id VARCHAR(64) NOT NULL,
  customer_status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uk_customer_id (customer_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_customer_contact (
  contact_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  contact_id VARCHAR(64) NOT NULL,
  customer_id VARCHAR(64) NOT NULL,
  encrypted_name BLOB NULL,
  encrypted_email BLOB NULL,
  encrypted_phone BLOB NULL,
  is_primary TINYINT(1) NOT NULL DEFAULT 0,
  valid_from DATETIME(6) NOT NULL,
  valid_to DATETIME(6) NULL,
  UNIQUE KEY uk_contact_id (contact_id),
  KEY ix_contact_customer (customer_id),
  CONSTRAINT fk_contact_customer FOREIGN KEY (customer_id) REFERENCES ops_customer(customer_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_product (
  product_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  sku VARCHAR(64) NOT NULL,
  name_cn VARCHAR(255) NOT NULL,
  name_en VARCHAR(255) NOT NULL,
  category_code VARCHAR(64) NOT NULL,
  specification_text TEXT NULL,
  quantity_unit VARCHAR(32) NOT NULL,
  moq DECIMAL(20,6) NOT NULL,
  lead_time_days INT NOT NULL,
  sale_status VARCHAR(32) NOT NULL DEFAULT 'active',
  UNIQUE KEY uk_product_sku (sku)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_product_price_rule (
  price_rule_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  sku VARCHAR(64) NOT NULL,
  min_qty DECIMAL(20,6) NOT NULL,
  max_qty DECIMAL(20,6) NULL,
  unit_price DECIMAL(20,6) NOT NULL,
  currency_code CHAR(3) NOT NULL,
  discount_rate DECIMAL(12,6) NOT NULL DEFAULT 0,
  valid_from DATETIME(6) NOT NULL,
  valid_to DATETIME(6) NULL,
  approval_level VARCHAR(32) NOT NULL DEFAULT 'standard',
  KEY ix_price_sku_valid (sku, valid_from, valid_to),
  CONSTRAINT fk_price_product FOREIGN KEY (sku) REFERENCES ops_product(sku)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_inventory_snapshot (
  inventory_snapshot_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  snapshot_at DATETIME(6) NOT NULL,
  location_code VARCHAR(64) NOT NULL,
  sku VARCHAR(64) NOT NULL,
  on_hand_qty DECIMAL(20,6) NOT NULL,
  reserved_qty DECIMAL(20,6) NOT NULL DEFAULT 0,
  available_qty DECIMAL(20,6) NOT NULL,
  KEY ix_inventory_lookup (sku, location_code, snapshot_at),
  CONSTRAINT fk_inventory_product FOREIGN KEY (sku) REFERENCES ops_product(sku)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_rfq_request (
  rfq_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  rfq_id VARCHAR(64) NOT NULL,
  customer_id VARCHAR(64) NULL,
  source_channel VARCHAR(32) NOT NULL,
  source_message_id VARCHAR(255) NULL,
  source_hash CHAR(64) NOT NULL,
  received_at DATETIME(6) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'received',
  current_extraction_version INT NOT NULL DEFAULT 0,
  UNIQUE KEY uk_rfq_id (rfq_id),
  UNIQUE KEY uk_rfq_source_hash (source_channel, source_hash),
  CONSTRAINT fk_rfq_customer FOREIGN KEY (customer_id) REFERENCES ops_customer(customer_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_rfq_extraction_version (
  extraction_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  rfq_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  extractor_type VARCHAR(32) NOT NULL,
  model_name VARCHAR(128) NULL,
  parent_version_no INT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  change_reason VARCHAR(255) NULL,
  UNIQUE KEY uk_rfq_extraction_version (rfq_id, version_no),
  CONSTRAINT fk_extraction_rfq FOREIGN KEY (rfq_id) REFERENCES ops_rfq_request(rfq_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_rfq_field_value (
  field_value_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  rfq_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  field_name VARCHAR(128) NOT NULL,
  value_text TEXT NULL,
  value_number DECIMAL(20,6) NULL,
  value_date DATE NULL,
  value_code VARCHAR(64) NULL,
  field_status VARCHAR(32) NOT NULL,
  source_span VARCHAR(255) NULL,
  confidence DECIMAL(8,6) NULL,
  UNIQUE KEY uk_rfq_field_version (rfq_id, version_no, field_name),
  CONSTRAINT fk_field_extraction FOREIGN KEY (rfq_id, version_no) REFERENCES ops_rfq_extraction_version(rfq_id, version_no)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_rfq_item (
  rfq_item_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  rfq_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  item_no INT NOT NULL,
  raw_product_text TEXT NOT NULL,
  specification_text TEXT NULL,
  quantity DECIMAL(20,6) NULL,
  quantity_unit VARCHAR(32) NULL,
  packaging_requirement TEXT NULL,
  certification_requirement TEXT NULL,
  UNIQUE KEY uk_rfq_item_version (rfq_id, version_no, item_no),
  CONSTRAINT fk_item_extraction FOREIGN KEY (rfq_id, version_no) REFERENCES ops_rfq_extraction_version(rfq_id, version_no)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_product_match_candidate (
  match_run_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  match_run_id VARCHAR(64) NOT NULL,
  rfq_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  item_no INT NOT NULL,
  sku VARCHAR(64) NOT NULL,
  candidate_rank INT NOT NULL,
  lexical_score DECIMAL(12,6) NULL,
  semantic_score DECIMAL(12,6) NULL,
  selection_status VARCHAR(32) NOT NULL DEFAULT 'candidate',
  evidence_refs_json JSON NULL,
  UNIQUE KEY uk_match_candidate (match_run_id, sku),
  CONSTRAINT fk_match_item FOREIGN KEY (rfq_id, version_no, item_no) REFERENCES ops_rfq_item(rfq_id, version_no, item_no),
  CONSTRAINT fk_match_product FOREIGN KEY (sku) REFERENCES ops_product(sku)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_fx_rate_snapshot (
  fx_snapshot_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  from_currency CHAR(3) NOT NULL,
  to_currency CHAR(3) NOT NULL,
  rate DECIMAL(20,10) NOT NULL,
  source_code VARCHAR(64) NOT NULL,
  quoted_at DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NULL,
  KEY ix_fx_lookup (from_currency, to_currency, quoted_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_quote (
  quote_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  quote_id VARCHAR(64) NOT NULL,
  rfq_id VARCHAR(64) NOT NULL,
  customer_id VARCHAR(64) NULL,
  current_version_no INT NOT NULL DEFAULT 0,
  status VARCHAR(32) NOT NULL DEFAULT 'draft',
  created_at DATETIME(6) NOT NULL,
  UNIQUE KEY uk_quote_id (quote_id),
  CONSTRAINT fk_quote_rfq FOREIGN KEY (rfq_id) REFERENCES ops_rfq_request(rfq_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_quote_version (
  quote_version_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  quote_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  calculation_id VARCHAR(64) NOT NULL,
  subtotal_amount DECIMAL(20,6) NOT NULL,
  discount_amount DECIMAL(20,6) NOT NULL DEFAULT 0,
  packaging_amount DECIMAL(20,6) NOT NULL DEFAULT 0,
  freight_amount DECIMAL(20,6) NOT NULL DEFAULT 0,
  total_amount DECIMAL(20,6) NOT NULL,
  currency_code CHAR(3) NOT NULL,
  valid_until DATE NOT NULL,
  content_hash CHAR(64) NOT NULL,
  calculation_hash CHAR(64) NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  UNIQUE KEY uk_quote_version (quote_id, version_no),
  CONSTRAINT fk_quote_version_quote FOREIGN KEY (quote_id) REFERENCES ops_quote(quote_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_quote_line (
  quote_line_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  quote_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  line_no INT NOT NULL,
  sku VARCHAR(64) NOT NULL,
  quantity DECIMAL(20,6) NOT NULL,
  unit_price DECIMAL(20,6) NOT NULL,
  discount_rate DECIMAL(12,6) NOT NULL DEFAULT 0,
  line_amount DECIMAL(20,6) NOT NULL,
  lead_time_days INT NOT NULL,
  UNIQUE KEY uk_quote_line (quote_id, version_no, line_no),
  CONSTRAINT fk_line_version FOREIGN KEY (quote_id, version_no) REFERENCES ops_quote_version(quote_id, version_no),
  CONSTRAINT fk_line_product FOREIGN KEY (sku) REFERENCES ops_product(sku)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_approval_record (
  approval_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  quote_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  action VARCHAR(32) NOT NULL,
  approval_status VARCHAR(32) NOT NULL DEFAULT 'pending',
  required_role VARCHAR(64) NOT NULL,
  reviewer_user_id VARCHAR(64) NULL,
  content_hash CHAR(64) NOT NULL,
  calculation_hash CHAR(64) NOT NULL,
  comment TEXT NULL,
  acted_at DATETIME(6) NULL,
  KEY ix_approval_version (quote_id, version_no),
  CONSTRAINT fk_approval_version FOREIGN KEY (quote_id, version_no) REFERENCES ops_quote_version(quote_id, version_no)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_outbox_message (
  outbox_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  quote_id VARCHAR(64) NOT NULL,
  version_no INT NOT NULL,
  approval_key BIGINT UNSIGNED NOT NULL,
  channel VARCHAR(32) NOT NULL DEFAULT 'mock_mailbox',
  recipient_masked VARCHAR(255) NOT NULL,
  body_text TEXT NOT NULL,
  content_hash CHAR(64) NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'queued',
  created_at DATETIME(6) NOT NULL,
  UNIQUE KEY uk_outbox_idempotency (idempotency_key),
  CONSTRAINT fk_outbox_approval FOREIGN KEY (approval_key) REFERENCES ops_approval_record(approval_key)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_followup_task (
  followup_key BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  rfq_id VARCHAR(64) NOT NULL,
  quote_id VARCHAR(64) NULL,
  quote_version_no INT NULL,
  task_type VARCHAR(32) NOT NULL,
  assignee_user_id VARCHAR(64) NOT NULL,
  due_at DATETIME(6) NOT NULL,
  priority VARCHAR(16) NOT NULL DEFAULT 'normal',
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  completed_at DATETIME(6) NULL,
  KEY ix_followup_due (status, due_at)
) ENGINE=InnoDB;
