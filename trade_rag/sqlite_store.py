"""Persistent local knowledge indexes backed by SQLite, sqlite-vec and FTS5."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import sqlite_vec

from .config import RagKeywordConfig, RagVectorConfig
from .contracts import (
    Actor,
    CanonicalDocument,
    ChildChunk,
    DocumentStatus,
    ParentChunk,
    SearchResult,
)


SCHEMA_VERSION = 1
DEFAULT_MODEL_ID = "mock-hash-v1"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORD = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]", re.IGNORECASE)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (_PROJECT_ROOT / path).resolve()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fts_text(value: str) -> str:
    """Tokenize English words and individual Han characters for deterministic FTS5 BM25."""
    return " ".join(token.casefold() for token in _WORD.findall(value))


def _fts_query(value: str) -> str:
    tokens = list(dict.fromkeys(_WORD.findall(value.casefold())))
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


class SqliteKnowledgeDatabase:
    """One connection and schema used by the three local knowledge-store adapters."""

    def __init__(self, path: str | Path, dimensions: int = 64,
                 model_id: str = DEFAULT_MODEL_ID):
        if dimensions <= 0:
            raise ValueError("embedding_dimensions_invalid")
        self.path = _resolve_path(path)
        self.dimensions = dimensions
        self.model_id = model_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.enable_load_extension(True)
            try:
                sqlite_vec.load(self.connection)
            finally:
                self.connection.enable_load_extension(False)
            self._ensure_schema()
        except Exception:
            self.connection.close()
            raise

    def _ensure_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS rag_sqlite_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL,
            embedding_model_id TEXT NOT NULL,
            dimensions INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rag_document (
            document_id TEXT NOT NULL,
            document_version INTEGER NOT NULL CHECK(document_version > 0),
            source_uri TEXT NOT NULL,
            source_title TEXT NOT NULL,
            source_content_hash TEXT NOT NULL,
            content_type TEXT NOT NULL,
            source_location TEXT NOT NULL,
            language TEXT NOT NULL,
            business_unit_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            document_status TEXT NOT NULL,
            expires_at TEXT,
            parser_version TEXT NOT NULL,
            source_metadata TEXT NOT NULL,
            PRIMARY KEY(document_id, document_version)
        );
        CREATE TABLE IF NOT EXISTS rag_document_role (
            document_id TEXT NOT NULL,
            document_version INTEGER NOT NULL,
            role TEXT NOT NULL,
            PRIMARY KEY(document_id, document_version, role),
            FOREIGN KEY(document_id, document_version)
                REFERENCES rag_document(document_id, document_version) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS rag_child (
            child_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            document_version INTEGER NOT NULL,
            parent_id TEXT NOT NULL,
            child_text TEXT NOT NULL,
            child_location TEXT NOT NULL,
            child_content_hash TEXT NOT NULL,
            child_metadata TEXT NOT NULL,
            FOREIGN KEY(document_id, document_version)
                REFERENCES rag_document(document_id, document_version) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS rag_child_document_idx
            ON rag_child(document_id, document_version);
        CREATE TABLE IF NOT EXISTS rag_parent (
            parent_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            document_version INTEGER NOT NULL,
            parent_text TEXT NOT NULL,
            parent_location TEXT NOT NULL,
            parent_content_hash TEXT NOT NULL,
            parent_metadata TEXT NOT NULL,
            FOREIGN KEY(document_id, document_version)
                REFERENCES rag_document(document_id, document_version) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS rag_parent_document_idx
            ON rag_parent(document_id, document_version);
        CREATE TABLE IF NOT EXISTS rag_index_state (
            document_id TEXT NOT NULL,
            document_version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            embedding_model_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(document_id, document_version)
        );
        """
        with self._lock, self.connection:
            self.connection.executescript(schema)
            current = self.connection.execute(
                "SELECT schema_version,embedding_model_id,dimensions "
                "FROM rag_sqlite_meta WHERE singleton=1"
            ).fetchone()
            if current is None:
                self.connection.execute(
                    "INSERT INTO rag_sqlite_meta VALUES (1,?,?,?)",
                    (SCHEMA_VERSION, self.model_id, self.dimensions),
                )
            elif (current["schema_version"] != SCHEMA_VERSION
                  or current["embedding_model_id"] != self.model_id
                  or current["dimensions"] != self.dimensions):
                raise RuntimeError("sqlite_index_model_or_schema_mismatch")
            self.connection.execute(
                f"""CREATE VIRTUAL TABLE IF NOT EXISTS rag_child_vec USING vec0(
                    child_id TEXT PRIMARY KEY,
                    embedding float[{self.dimensions}] distance_metric=cosine
                )"""
            )
            self.connection.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS rag_child_fts USING fts5(
                    child_id UNINDEXED,
                    document_id UNINDEXED,
                    document_version UNINDEXED,
                    child_text,
                    source_title,
                    tokenize='unicode61 remove_diacritics 2'
                )"""
            )

    def close(self) -> None:
        self.connection.close()

    def check_ready(self) -> None:
        with self._lock:
            row = self.connection.execute("PRAGMA quick_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError("sqlite_index_integrity_failed")
            meta = self.connection.execute(
                "SELECT schema_version,embedding_model_id,dimensions "
                "FROM rag_sqlite_meta WHERE singleton=1"
            ).fetchone()
            if (meta is None or meta[0] != SCHEMA_VERSION or meta[1] != self.model_id
                    or meta[2] != self.dimensions):
                raise RuntimeError("sqlite_index_model_or_schema_mismatch")
            if not self.connection.execute("SELECT vec_version()").fetchone()[0]:
                raise RuntimeError("sqlite_vec_not_ready")
            required = {"rag_child_vec", "rag_child_fts", "rag_child", "rag_parent"}
            present = {row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN (?,?,?,?)", tuple(required)
            )}
            if present != required:
                raise RuntimeError("sqlite_index_schema_not_ready")

    def upsert_document(self, source: CanonicalDocument) -> None:
        self.connection.execute(
            """INSERT INTO rag_document VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(document_id,document_version) DO UPDATE SET
                 source_uri=excluded.source_uri,source_title=excluded.source_title,
                 source_content_hash=excluded.source_content_hash,
                 content_type=excluded.content_type,source_location=excluded.source_location,
                 language=excluded.language,business_unit_id=excluded.business_unit_id,
                 classification=excluded.classification,
                 document_status=excluded.document_status,expires_at=excluded.expires_at,
                 parser_version=excluded.parser_version,
                 source_metadata=excluded.source_metadata""",
            (source.document_id, source.version, source.source_uri, source.title,
             source.content_hash, source.content_type, source.location, source.language,
             source.business_unit_id, source.classification, source.status.value,
             _iso(source.expires_at), source.parser_version, _json(source.metadata)),
        )
        self.connection.execute(
            "DELETE FROM rag_document_role WHERE document_id=? AND document_version=?",
            (source.document_id, source.version),
        )
        self.connection.executemany(
            "INSERT INTO rag_document_role VALUES (?,?,?)",
            [(source.document_id, source.version, role) for role in sorted(source.allowed_roles)],
        )

    @staticmethod
    def visibility(actor: Actor, alias: str = "d") -> tuple[str, list[object]]:
        now = datetime.now(timezone.utc).isoformat()
        sql = [
            f"{alias}.business_unit_id=?",
            f"{alias}.document_status IN ('approved','published')",
            f"({alias}.expires_at IS NULL OR {alias}.expires_at>?)",
        ]
        params: list[object] = [actor.business_unit_id, now]
        role_sql = (
            "NOT EXISTS (SELECT 1 FROM rag_document_role ar "
            f"WHERE ar.document_id={alias}.document_id "
            f"AND ar.document_version={alias}.document_version)"
        )
        if actor.roles:
            placeholders = ",".join("?" for _ in actor.roles)
            role_sql += (
                " OR EXISTS (SELECT 1 FROM rag_document_role ar "
                f"WHERE ar.document_id={alias}.document_id "
                f"AND ar.document_version={alias}.document_version "
                f"AND ar.role IN ({placeholders}))"
            )
            params.extend(sorted(actor.roles))
        sql.append(f"({role_sql})")
        return " AND ".join(sql), params

    def cleanup_document(self, document_id: str, version: int) -> None:
        child = self.connection.execute(
            "SELECT 1 FROM rag_child WHERE document_id=? AND document_version=? LIMIT 1",
            (document_id, version),
        ).fetchone()
        parent = self.connection.execute(
            "SELECT 1 FROM rag_parent WHERE document_id=? AND document_version=? LIMIT 1",
            (document_id, version),
        ).fetchone()
        keyword = self.connection.execute(
            "SELECT 1 FROM rag_child_fts WHERE document_id=? AND document_version=? LIMIT 1",
            (document_id, version),
        ).fetchone()
        if child is None and parent is None and keyword is None:
            self.connection.execute(
                "DELETE FROM rag_document WHERE document_id=? AND document_version=?",
                (document_id, version),
            )

    def state(self) -> dict[tuple[str, int], tuple[str, str]]:
        with self._lock:
            return {
                (row[0], int(row[1])): (row[2], row[3])
                for row in self.connection.execute(
                    "SELECT document_id,document_version,content_hash,embedding_model_id "
                    "FROM rag_index_state"
                )
            }

    def document_keys(self) -> set[tuple[str, int]]:
        with self._lock:
            return {
                (row[0], int(row[1]))
                for row in self.connection.execute(
                    "SELECT document_id,document_version FROM rag_document"
                )
            }

    def mark_indexed(self, source: CanonicalDocument, fingerprint: str | None = None) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO rag_index_state VALUES (?,?,?,?,?)
                   ON CONFLICT(document_id,document_version) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     embedding_model_id=excluded.embedding_model_id,
                     updated_at=excluded.updated_at""",
                (source.document_id, source.version, fingerprint or source.content_hash, self.model_id,
                 datetime.now(timezone.utc).isoformat()),
            )

    def unmark(self, document_id: str, version: int | None = None) -> None:
        with self._lock, self.connection:
            if version is None:
                self.connection.execute(
                    "DELETE FROM rag_index_state WHERE document_id=?", (document_id,)
                )
            else:
                self.connection.execute(
                    "DELETE FROM rag_index_state WHERE document_id=? AND document_version=?",
                    (document_id, version),
                )


def _document(row: sqlite3.Row, child_text: str) -> CanonicalDocument:
    return CanonicalDocument(
        document_id=row["document_id"], version=int(row["document_version"]),
        source_uri=row["source_uri"], title=row["source_title"], content=child_text,
        content_hash=row["source_content_hash"], content_type=row["content_type"],
        location=row["source_location"], language=row["language"],
        business_unit_id=row["business_unit_id"],
        allowed_roles=frozenset(json.loads(row["allowed_roles_json"])),
        classification=row["classification"], status=DocumentStatus(row["document_status"]),
        expires_at=_parse_datetime(row["expires_at"]), parser_version=row["parser_version"],
        metadata=json.loads(row["source_metadata"]),
    )


def _source_select() -> str:
    return """d.document_id,d.document_version,d.source_uri,d.source_title,
              d.source_content_hash,d.content_type,d.source_location,d.language,
              d.business_unit_id,d.classification,d.document_status,d.expires_at,
              d.parser_version,d.source_metadata,
              COALESCE((SELECT json_group_array(role) FROM (
                SELECT role FROM rag_document_role rr
                 WHERE rr.document_id=d.document_id
                   AND rr.document_version=d.document_version ORDER BY role
              )), '[]') AS allowed_roles_json"""


class SqliteVectorStore:
    def __init__(self, config: RagVectorConfig, *, model_id: str = DEFAULT_MODEL_ID,
                 database: SqliteKnowledgeDatabase | None = None):
        config.validate()
        if config.backend != "sqlite":
            raise ValueError("SqliteVectorStore requires RAG_VECTOR_BACKEND=sqlite")
        self.config = config
        self.db = database or SqliteKnowledgeDatabase(
            config.sqlite_path, config.dimensions, model_id
        )

    def close(self) -> None:
        self.db.close()

    def check_ready(self) -> None:
        self.db.check_ready()

    def upsert(self, children: Iterable[ChildChunk], source: CanonicalDocument,
               vectors: Iterable[Iterable[float]]) -> int:
        rows = list(children)
        values = [list(vector) for vector in vectors]
        if len(rows) != len(values):
            raise ValueError("embedding_count_mismatch")
        if any(len(vector) != self.config.dimensions for vector in values):
            raise ValueError("embedding_dimension_mismatch")
        if any(not math.isfinite(value) for vector in values for value in vector):
            raise ValueError("embedding_value_invalid")
        if any(child.document_id != source.document_id for child in rows):
            raise ValueError("child_document_mismatch")
        with self.db._lock, self.db.connection:
            self.db.upsert_document(source)
            for child, vector in zip(rows, values):
                self.db.connection.execute(
                    "DELETE FROM rag_child_vec WHERE child_id=?", (child.child_id,)
                )
                self.db.connection.execute(
                    """INSERT INTO rag_child VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(child_id) DO UPDATE SET
                         document_id=excluded.document_id,
                         document_version=excluded.document_version,
                         parent_id=excluded.parent_id,child_text=excluded.child_text,
                         child_location=excluded.child_location,
                         child_content_hash=excluded.child_content_hash,
                         child_metadata=excluded.child_metadata""",
                    (child.child_id, source.document_id, source.version, child.parent_id,
                     child.text, child.location, child.content_hash, _json(child.metadata)),
                )
                self.db.connection.execute(
                    "INSERT INTO rag_child_vec(child_id,embedding) VALUES (?,?)",
                    (child.child_id, sqlite_vec.serialize_float32(vector)),
                )
        return len(rows)

    def _eligible(self, actor: Actor) -> set[str]:
        visibility, params = self.db.visibility(actor)
        return {row[0] for row in self.db.connection.execute(
            f"SELECT c.child_id FROM rag_child c JOIN rag_document d "
            f"ON d.document_id=c.document_id AND d.document_version=c.document_version "
            f"WHERE {visibility}", params,
        )}

    def _result(self, child_id: str, actor: Actor, score: float) -> SearchResult | None:
        visibility, params = self.db.visibility(actor)
        row = self.db.connection.execute(
            f"""SELECT c.child_id,c.parent_id,c.child_text,c.child_location,
                       c.child_content_hash,c.child_metadata,{_source_select()}
                  FROM rag_child c JOIN rag_document d
                    ON d.document_id=c.document_id
                   AND d.document_version=c.document_version
                 WHERE c.child_id=? AND {visibility}""",
            [child_id, *params],
        ).fetchone()
        if row is None:
            return None
        child = ChildChunk(
            row["child_id"], row["parent_id"], row["document_id"], row["child_text"],
            row["child_location"], row["child_content_hash"], json.loads(row["child_metadata"]),
        )
        return SearchResult(child, score, _document(row, child.text))

    def search(self, vector: Iterable[float], actor: Actor,
               limit: int = 30) -> list[SearchResult]:
        values = list(vector)
        if len(values) != self.config.dimensions:
            raise ValueError("embedding_dimension_mismatch")
        if any(not math.isfinite(value) for value in values):
            raise ValueError("embedding_value_invalid")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.db._lock:
            eligible = self._eligible(actor)
            if not eligible:
                return []
            total = int(self.db.connection.execute(
                "SELECT count(*) FROM rag_child_vec"
            ).fetchone()[0])
            query = sqlite_vec.serialize_float32(values)
            k = min(total, max(limit * 4, limit))
            selected: list[tuple[str, float]] = []
            while k:
                candidates = self.db.connection.execute(
                    """SELECT child_id,distance FROM rag_child_vec
                        WHERE embedding MATCH ? AND k=?
                        ORDER BY distance""", (query, k),
                ).fetchall()
                selected = [(row[0], float(row[1])) for row in candidates if row[0] in eligible]
                if len(selected) >= limit or k >= total:
                    break
                k = min(total, max(k + 1, k * 2))
            results = []
            for child_id, distance in selected[:limit]:
                result = self._result(child_id, actor, max(-1.0, min(1.0, 1.0 - distance)))
                if result is not None:
                    results.append(result)
            return sorted(results, key=lambda item: (-item.score, item.child.child_id))[:limit]

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        with self.db._lock, self.db.connection:
            if version is None:
                rows = self.db.connection.execute(
                    "SELECT child_id FROM rag_child WHERE document_id=?", (document_id,)
                ).fetchall()
                versions = [row[0] for row in self.db.connection.execute(
                    "SELECT document_version FROM rag_document WHERE document_id=?",
                    (document_id,),
                )]
                self.db.connection.execute("DELETE FROM rag_child WHERE document_id=?", (document_id,))
            else:
                rows = self.db.connection.execute(
                    "SELECT child_id FROM rag_child WHERE document_id=? AND document_version=?",
                    (document_id, version),
                ).fetchall()
                versions = [version]
                self.db.connection.execute(
                    "DELETE FROM rag_child WHERE document_id=? AND document_version=?",
                    (document_id, version),
                )
            self.db.connection.executemany(
                "DELETE FROM rag_child_vec WHERE child_id=?", [(row[0],) for row in rows]
            )
            for current_version in versions:
                self.db.cleanup_document(document_id, int(current_version))
            return len(rows)


class SqliteKeywordStore:
    """FTS5 BM25 keyword adapter sharing the persistent knowledge database."""

    def __init__(self, config: RagKeywordConfig, *, dimensions: int = 64,
                 model_id: str = DEFAULT_MODEL_ID,
                 database: SqliteKnowledgeDatabase | None = None):
        config.validate()
        if config.backend != "sqlite":
            raise ValueError("SqliteKeywordStore requires RAG_KEYWORD_BACKEND=sqlite")
        self.config = config
        self.db = database or SqliteKnowledgeDatabase(config.sqlite_path, dimensions, model_id)

    def close(self) -> None:
        self.db.close()

    def check_ready(self) -> None:
        self.db.check_ready()

    def upsert(self, children: Iterable[ChildChunk], source: CanonicalDocument) -> int:
        rows = list(children)
        if any(child.document_id != source.document_id for child in rows):
            raise ValueError("child_document_mismatch")
        with self.db._lock, self.db.connection:
            self.db.upsert_document(source)
            for child in rows:
                self.db.connection.execute(
                    "DELETE FROM rag_child_fts WHERE child_id=?", (child.child_id,)
                )
                self.db.connection.execute(
                    "INSERT INTO rag_child_fts VALUES (?,?,?,?,?)",
                    (child.child_id, source.document_id, source.version,
                     _fts_text(child.text), _fts_text(source.title)),
                )
        return len(rows)

    def search(self, query: str, actor: Actor, limit: int = 30) -> list[SearchResult]:
        expression = _fts_query(query)
        if not expression:
            return []
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        visibility, params = self.db.visibility(actor)
        with self.db._lock:
            rows = self.db.connection.execute(
                f"""SELECT c.child_id,c.parent_id,c.child_text,c.child_location,
                           c.child_content_hash,c.child_metadata,{_source_select()},
                           bm25(rag_child_fts,0.0,0.0,0.0,1.0,2.0) AS rank
                      FROM rag_child_fts
                      JOIN rag_child c ON c.child_id=rag_child_fts.child_id
                      JOIN rag_document d ON d.document_id=c.document_id
                       AND d.document_version=c.document_version
                     WHERE rag_child_fts MATCH ? AND {visibility}
                     ORDER BY rank ASC,c.child_id ASC LIMIT ?""",
                [expression, *params, limit],
            ).fetchall()
        strengths = [abs(float(row["rank"])) for row in rows]
        strongest = max(strengths, default=1.0) or 1.0
        results = []
        for row, strength in zip(rows, strengths):
            child = ChildChunk(
                row["child_id"], row["parent_id"], row["document_id"], row["child_text"],
                row["child_location"], row["child_content_hash"], json.loads(row["child_metadata"]),
            )
            results.append(SearchResult(
                child, strength / strongest, _document(row, child.text),
                retrieval_source="keyword",
            ))
        return results

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        with self.db._lock, self.db.connection:
            if version is None:
                count = int(self.db.connection.execute(
                    "SELECT count(*) FROM rag_child_fts WHERE document_id=?", (document_id,)
                ).fetchone()[0])
                versions = [row[0] for row in self.db.connection.execute(
                    "SELECT document_version FROM rag_document WHERE document_id=?", (document_id,)
                )]
                self.db.connection.execute(
                    "DELETE FROM rag_child_fts WHERE document_id=?", (document_id,)
                )
            else:
                count = int(self.db.connection.execute(
                    "SELECT count(*) FROM rag_child_fts WHERE document_id=? AND document_version=?",
                    (document_id, version),
                ).fetchone()[0])
                versions = [version]
                self.db.connection.execute(
                    "DELETE FROM rag_child_fts WHERE document_id=? AND document_version=?",
                    (document_id, version),
                )
            for current_version in versions:
                self.db.cleanup_document(document_id, int(current_version))
            return count


class SqliteParentStore:
    def __init__(self, config: RagVectorConfig, *, model_id: str = DEFAULT_MODEL_ID,
                 database: SqliteKnowledgeDatabase | None = None):
        config.validate()
        if config.backend != "sqlite":
            raise ValueError("SqliteParentStore requires RAG_VECTOR_BACKEND=sqlite")
        self.db = database or SqliteKnowledgeDatabase(
            config.sqlite_path, config.dimensions, model_id
        )

    def close(self) -> None:
        self.db.close()

    def upsert(self, parents: Iterable[ParentChunk], source: CanonicalDocument) -> int:
        rows = list(parents)
        if any(parent.document_id != source.document_id for parent in rows):
            raise ValueError("parent_document_mismatch")
        with self.db._lock, self.db.connection:
            self.db.upsert_document(source)
            for parent in rows:
                self.db.connection.execute(
                    """INSERT INTO rag_parent VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(parent_id) DO UPDATE SET
                         document_id=excluded.document_id,
                         document_version=excluded.document_version,
                         parent_text=excluded.parent_text,
                         parent_location=excluded.parent_location,
                         parent_content_hash=excluded.parent_content_hash,
                         parent_metadata=excluded.parent_metadata""",
                    (parent.parent_id, source.document_id, source.version, parent.text,
                     parent.location, parent.content_hash, _json(parent.metadata)),
                )
        return len(rows)

    def get(self, parent_id: str, actor: Actor | None = None) -> ParentChunk | None:
        params: list[object] = [parent_id]
        clause = ""
        if actor is not None:
            visibility, visible_params = self.db.visibility(actor)
            clause = f" AND {visibility}"
            params.extend(visible_params)
        with self.db._lock:
            row = self.db.connection.execute(
                f"""SELECT p.parent_id,p.document_id,p.parent_text,p.parent_location,
                           p.parent_content_hash,p.parent_metadata
                      FROM rag_parent p JOIN rag_document d
                        ON d.document_id=p.document_id
                       AND d.document_version=p.document_version
                     WHERE p.parent_id=?{clause}""", params,
            ).fetchone()
        if row is None:
            return None
        return ParentChunk(
            row["parent_id"], row["document_id"], row["parent_text"],
            row["parent_location"], row["parent_content_hash"],
            json.loads(row["parent_metadata"]),
        )

    def get_many(self, parent_ids: Iterable[str], actor: Actor | None = None) -> list[ParentChunk]:
        return [parent for parent_id in parent_ids
                if (parent := self.get(parent_id, actor)) is not None]

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        with self.db._lock, self.db.connection:
            if version is None:
                rows = self.db.connection.execute(
                    "SELECT document_version,count(*) FROM rag_parent WHERE document_id=? "
                    "GROUP BY document_version", (document_id,),
                ).fetchall()
                count = sum(int(row[1]) for row in rows)
                versions = [int(row[0]) for row in rows]
                self.db.connection.execute("DELETE FROM rag_parent WHERE document_id=?", (document_id,))
            else:
                count = int(self.db.connection.execute(
                    "SELECT count(*) FROM rag_parent WHERE document_id=? AND document_version=?",
                    (document_id, version),
                ).fetchone()[0])
                versions = [version]
                self.db.connection.execute(
                    "DELETE FROM rag_parent WHERE document_id=? AND document_version=?",
                    (document_id, version),
                )
            for current_version in versions:
                self.db.cleanup_document(document_id, int(current_version))
            return count
