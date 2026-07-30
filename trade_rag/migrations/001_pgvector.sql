CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_schema_migration (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rag_child_vector (
    child_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    document_version INTEGER NOT NULL CHECK (document_version > 0),
    parent_id TEXT NOT NULL,
    child_text TEXT NOT NULL,
    child_location TEXT NOT NULL DEFAULT '',
    child_content_hash TEXT NOT NULL,
    child_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_uri TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_location TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'und',
    business_unit_id TEXT NOT NULL,
    allowed_roles TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    classification TEXT NOT NULL,
    document_status TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    parser_version TEXT NOT NULL,
    source_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding_model_id TEXT NOT NULL,
    embedding vector(64) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS rag_child_vector_document_idx
    ON rag_child_vector (document_id, document_version);
CREATE INDEX IF NOT EXISTS rag_child_vector_acl_idx
    ON rag_child_vector USING GIN (allowed_roles);
CREATE INDEX IF NOT EXISTS rag_child_vector_filter_idx
    ON rag_child_vector (business_unit_id, document_status, expires_at);

INSERT INTO rag_schema_migration (version) VALUES (1)
ON CONFLICT (version) DO NOTHING;
