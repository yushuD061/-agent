CREATE TABLE IF NOT EXISTS trade_campaign (
  campaign_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
  current_stage TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  paused INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_input_snapshot (
  snapshot_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, input_type TEXT NOT NULL,
  version INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
  source_name TEXT, created_at TEXT NOT NULL,
  UNIQUE(campaign_id,input_type,version), FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE TABLE IF NOT EXISTS trade_stage_run (
  run_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, stage TEXT NOT NULL,
  version INTEGER NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, risks_json TEXT NOT NULL, missing_inputs_json TEXT NOT NULL,
  next_stage TEXT, next_required_inputs_json TEXT NOT NULL, human_review_required INTEGER NOT NULL,
  input_hash TEXT NOT NULL, output_hash TEXT, error_code TEXT, started_at TEXT NOT NULL,
  completed_at TEXT, UNIQUE(campaign_id,stage,version),
  FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_stage_campaign ON trade_stage_run(campaign_id,stage,version DESC);
CREATE TABLE IF NOT EXISTS trade_stage_job (
  job_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, stage TEXT NOT NULL,
  input_json TEXT NOT NULL, business_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  lease_owner TEXT, lease_until TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_stage_job_claim ON trade_stage_job(status,next_attempt_at,lease_until,created_at);
CREATE TABLE IF NOT EXISTS trade_evidence (
  evidence_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, stage TEXT NOT NULL,
  source_type TEXT NOT NULL, source_ref TEXT NOT NULL, fetched_at TEXT NOT NULL,
  content_hash TEXT NOT NULL, excerpt TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE TABLE IF NOT EXISTS trade_product_profile (
  sku TEXT PRIMARY KEY, product_size TEXT, packing_size TEXT, weight_kg TEXT,
  hs_code TEXT, certifications_json TEXT NOT NULL DEFAULT '[]', materials_json TEXT NOT NULL DEFAULT '[]',
  applications_json TEXT NOT NULL DEFAULT '[]', source_kind TEXT NOT NULL DEFAULT 'pending',
  source_ref TEXT, version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_price_rule (
  rule_id TEXT PRIMARY KEY, sku TEXT NOT NULL, min_qty TEXT NOT NULL, max_qty TEXT,
  unit_price TEXT NOT NULL, currency TEXT NOT NULL, incoterm TEXT NOT NULL,
  payment_terms TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
  approval_status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
  source_ref TEXT NOT NULL, UNIQUE(sku,min_qty,currency,incoterm,version)
);
CREATE TABLE IF NOT EXISTS trade_prospect (
  prospect_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, company_name TEXT NOT NULL,
  normalized_domain TEXT, website TEXT, country TEXT, business_type TEXT,
  source_urls_json TEXT NOT NULL, source_notes_json TEXT NOT NULL,
  contact_email TEXT NOT NULL DEFAULT '没有', contact_phone TEXT NOT NULL DEFAULT '没有',
  email_result TEXT NOT NULL DEFAULT '没有', phone_result TEXT NOT NULL DEFAULT '没有',
  do_not_contact INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(campaign_id,normalized_domain), FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE TABLE IF NOT EXISTS trade_outreach_draft (
  draft_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, prospect_id TEXT NOT NULL,
  version INTEGER NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  content_hash TEXT NOT NULL, status TEXT NOT NULL, approved_by TEXT, approved_at TEXT,
  created_at TEXT NOT NULL, UNIQUE(prospect_id,version),
  FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id), FOREIGN KEY(prospect_id) REFERENCES trade_prospect(prospect_id)
);
CREATE TABLE IF NOT EXISTS trade_outreach_outbox (
  command_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, draft_version INTEGER NOT NULL,
  content_hash TEXT NOT NULL, account_id TEXT NOT NULL, recipient TEXT NOT NULL,
  status TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5, next_attempt_at TEXT, smtp_message_id TEXT,
  accepted_at TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(draft_id,draft_version), FOREIGN KEY(draft_id) REFERENCES trade_outreach_draft(draft_id)
);
CREATE TABLE IF NOT EXISTS trade_quote_draft (
  quote_draft_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, prospect_id TEXT,
  version INTEGER NOT NULL, quotation_number TEXT NOT NULL, payload_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, status TEXT NOT NULL, artifact_dir TEXT,
  approved_by TEXT, approved_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(campaign_id,quotation_number,version), FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE TABLE IF NOT EXISTS trade_artifact (
  artifact_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, object_type TEXT NOT NULL,
  object_id TEXT NOT NULL, object_version INTEGER NOT NULL, artifact_kind TEXT NOT NULL,
  file_name TEXT NOT NULL, storage_path TEXT NOT NULL, byte_size INTEGER NOT NULL,
  artifact_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(object_type,object_id,object_version,artifact_kind),
  FOREIGN KEY(campaign_id) REFERENCES trade_campaign(campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_artifact_object
  ON trade_artifact(object_type,object_id,object_version);
CREATE TABLE IF NOT EXISTS trade_ai_audit (
  audit_id TEXT PRIMARY KEY, campaign_id TEXT, stage TEXT NOT NULL, provider_type TEXT NOT NULL,
  model TEXT NOT NULL, prompt_version TEXT NOT NULL, input_hash TEXT NOT NULL,
  output_hash TEXT, duration_ms INTEGER, status TEXT NOT NULL, actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_audit_event (
  audit_id TEXT PRIMARY KEY, campaign_id TEXT, actor_id TEXT NOT NULL, action TEXT NOT NULL,
  object_type TEXT NOT NULL, object_id TEXT NOT NULL, result TEXT NOT NULL,
  metadata_json TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_idempotency (
  scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
  response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope,idempotency_key)
);
