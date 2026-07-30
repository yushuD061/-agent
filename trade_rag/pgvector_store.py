from __future__ import annotations

from datetime import timezone
from typing import Iterable

from .config import RagVectorConfig
from .contracts import (
    Actor,
    CanonicalDocument,
    ChildChunk,
    DocumentStatus,
    SearchResult,
)


class PgvectorStore:
    """Synchronous PostgreSQL/pgvector implementation of the semantic store."""

    def __init__(self, config: RagVectorConfig, *, model_id: str = "mock-hash-v1"):
        config.validate()
        if config.backend != "pgvector":
            raise ValueError("PgvectorStore requires RAG_VECTOR_BACKEND=pgvector")
        self.config = config
        self.model_id = model_id

    def _connect(self):
        import psycopg
        from pgvector.psycopg import register_vector
        connection = psycopg.connect(**self.config.connection_kwargs)
        register_vector(connection)
        return connection

    def check_ready(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT format_type(a.atttypid, a.atttypmod)
                   FROM pg_attribute a
                   JOIN pg_class c ON c.oid = a.attrelid
                   WHERE c.relname = 'rag_child_vector'
                     AND a.attname = 'embedding' AND NOT a.attisdropped"""
            ).fetchone()
            if row is None or row[0] != f"vector({self.config.dimensions})":
                raise RuntimeError("pgvector_schema_not_ready")

    def _validated_rows(self, children, vectors) -> tuple[list[ChildChunk], list[list[float]]]:
        rows = list(children)
        values = [list(vector) for vector in vectors]
        if len(rows) != len(values):
            raise ValueError("embedding_count_mismatch")
        if any(len(vector) != self.config.dimensions for vector in values):
            raise ValueError("embedding_dimension_mismatch")
        return rows, values

    def upsert(self, children, source: CanonicalDocument, vectors) -> int:
        from psycopg.types.json import Jsonb
        rows, values = self._validated_rows(children, vectors)
        if not rows:
            return 0
        statement = """
            INSERT INTO rag_child_vector (
                child_id, document_id, document_version, parent_id, child_text,
                child_location, child_content_hash, child_metadata, source_uri,
                source_title, source_content_hash, content_type, source_location,
                language, business_unit_id, allowed_roles, classification,
                document_status, expires_at, parser_version, source_metadata,
                embedding_model_id, embedding
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (child_id) DO UPDATE SET
                document_id = EXCLUDED.document_id,
                document_version = EXCLUDED.document_version,
                parent_id = EXCLUDED.parent_id,
                child_text = EXCLUDED.child_text,
                child_location = EXCLUDED.child_location,
                child_content_hash = EXCLUDED.child_content_hash,
                child_metadata = EXCLUDED.child_metadata,
                source_uri = EXCLUDED.source_uri,
                source_title = EXCLUDED.source_title,
                source_content_hash = EXCLUDED.source_content_hash,
                content_type = EXCLUDED.content_type,
                source_location = EXCLUDED.source_location,
                language = EXCLUDED.language,
                business_unit_id = EXCLUDED.business_unit_id,
                allowed_roles = EXCLUDED.allowed_roles,
                classification = EXCLUDED.classification,
                document_status = EXCLUDED.document_status,
                expires_at = EXCLUDED.expires_at,
                parser_version = EXCLUDED.parser_version,
                source_metadata = EXCLUDED.source_metadata,
                embedding_model_id = EXCLUDED.embedding_model_id,
                embedding = EXCLUDED.embedding,
                updated_at = CURRENT_TIMESTAMP
        """
        params = []
        expires_at = source.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        for child, vector in zip(rows, values):
            if child.document_id != source.document_id:
                raise ValueError("child_document_mismatch")
            params.append((
                child.child_id, source.document_id, source.version, child.parent_id,
                child.text, child.location, child.content_hash, Jsonb(child.metadata),
                source.source_uri, source.title, source.content_hash, source.content_type,
                source.location, source.language, source.business_unit_id,
                sorted(source.allowed_roles), source.classification, source.status.value,
                expires_at, source.parser_version, Jsonb(source.metadata), self.model_id,
                vector,
            ))
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(statement, params)
        return len(rows)

    def search(self, vector, actor: Actor, limit: int = 30) -> list[SearchResult]:
        values = list(vector)
        if len(values) != self.config.dimensions:
            raise ValueError("embedding_dimension_mismatch")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        statement = """
            SELECT child_id, document_id, document_version, parent_id, child_text,
                   child_location, child_content_hash, child_metadata, source_uri,
                   source_title, source_content_hash, content_type, source_location,
                   language, business_unit_id, allowed_roles, classification,
                   document_status, expires_at, parser_version, source_metadata,
                   1 - (embedding <=> %s::vector(64)) AS score
              FROM rag_child_vector
             WHERE business_unit_id = %s
               AND (cardinality(allowed_roles) = 0 OR allowed_roles && %s::text[])
               AND document_status IN ('approved', 'published')
               AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
             ORDER BY score DESC, child_id ASC
             LIMIT %s
        """
        with self._connect() as connection:
            rows = connection.execute(statement, (values, actor.business_unit_id,
                                                    sorted(actor.roles), limit)).fetchall()
        results = []
        for row in rows:
            child = ChildChunk(row[0], row[3], row[1], row[4], row[5], row[6], row[7] or {})
            source = CanonicalDocument(
                document_id=row[1], version=row[2], source_uri=row[8], title=row[9],
                content=row[4], content_hash=row[10], content_type=row[11],
                location=row[12], language=row[13], business_unit_id=row[14],
                allowed_roles=frozenset(row[15] or ()), classification=row[16],
                status=DocumentStatus(row[17]), expires_at=row[18],
                parser_version=row[19], metadata=row[20] or {},
            )
            results.append(SearchResult(child, float(row[21]), source))
        return results

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        with self._connect() as connection:
            if version is None:
                cursor = connection.execute(
                    "DELETE FROM rag_child_vector WHERE document_id = %s", (document_id,))
            else:
                cursor = connection.execute(
                    "DELETE FROM rag_child_vector WHERE document_id = %s AND document_version = %s",
                    (document_id, version),
                )
            return cursor.rowcount
