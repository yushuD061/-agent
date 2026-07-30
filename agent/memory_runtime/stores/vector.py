"""Versioned vector adapter contract and a local, no-network test implementation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import MemoryItem, MemoryScope
from .keyword import IndexHit, tokenize


@runtime_checkable
class EmbeddingAdapter(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalHashEmbeddingAdapter:
    """Deterministic local adapter for plumbing/tests; not a semantic production model."""

    def __init__(self, dimensions: int = 64):
        if dimensions < 8:
            raise ValueError("embedding_dimensions_invalid")
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"local-hash-v1-{self.dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in tokenize(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[bucket] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors

    @property
    def external(self) -> bool:
        return False


class OpenAICompatibleEmbeddingAdapter:
    """Explicitly approved OpenAI-compatible embedding adapter."""

    def __init__(self, *, base_url: str, api_key: str, model: str,
                 dimensions: int, transfer_approved: bool, timeout_seconds: int = 20):
        if not transfer_approved:
            raise RuntimeError("workspace_memory_external_transfer_not_approved")
        parsed_url = urlparse(base_url.strip())
        if (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc
                or not api_key.strip() or not model.strip() or dimensions < 8):
            raise ValueError("workspace_memory_external_embedding_invalid")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self.timeout_seconds = timeout_seconds

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def external(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.model_id, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings", data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        rows = sorted(body.get("data", []), key=lambda row: int(row.get("index", 0)))
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or any(
            not isinstance(vector, list) or len(vector) != self.dimensions
            for vector in vectors
        ):
            raise RuntimeError("workspace_memory_embedding_response_invalid")
        try:
            numeric = [[float(value) for value in vector] for vector in vectors]
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("workspace_memory_embedding_response_invalid") from exc
        if any(not math.isfinite(value) for vector in numeric for value in vector):
            raise RuntimeError("workspace_memory_embedding_response_invalid")
        return numeric


class VectorMemoryIndex:
    def __init__(
        self, database: str | Path | sqlite3.Connection,
        embedding: EmbeddingAdapter, *, readonly=False,
    ):
        self.embedding = embedding
        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            path = Path(database)
            if readonly:
                if not path.is_file():
                    raise RuntimeError("memory_vector_index_not_configured")
                self.connection = sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False,
                )
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if readonly:
            self.connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"memory_vector_index", "memory_vector_index_meta"} <= tables:
                raise RuntimeError("memory_vector_index_schema_invalid")
        else:
            with self.connection:
                self.connection.executescript("""
              CREATE TABLE IF NOT EXISTS memory_vector_index (
                memory_id TEXT PRIMARY KEY, realm TEXT NOT NULL, tenant_id TEXT NOT NULL,
                account_id TEXT, subject_id TEXT, project_id TEXT, conversation_id TEXT,
                purpose TEXT NOT NULL, vector_json TEXT NOT NULL,
                embedding_model TEXT NOT NULL, dimensions INTEGER NOT NULL,
                source_version INTEGER NOT NULL, updated_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_memory_vector_scope ON memory_vector_index(
                realm,tenant_id,account_id,subject_id,project_id,purpose,conversation_id);
              CREATE TABLE IF NOT EXISTS memory_vector_index_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                index_version INTEGER NOT NULL, embedding_model TEXT NOT NULL,
                dimensions INTEGER NOT NULL);
                """)
        current = self.connection.execute(
            "SELECT embedding_model,dimensions FROM memory_vector_index_meta WHERE singleton=1"
        ).fetchone()
        if current is None and not readonly:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO memory_vector_index_meta VALUES (1,0,?,?)",
                    (embedding.model_id, embedding.dimensions),
                )
        elif current is None or (current["embedding_model"] != embedding.model_id or
              current["dimensions"] != embedding.dimensions):
            raise RuntimeError("memory_embedding_model_version_mismatch")

    def upsert(self, item: MemoryItem) -> None:
        vector = self.embedding.embed([f"{item.summary} {item.content}"])[0]
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO memory_vector_index VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                     realm=excluded.realm,tenant_id=excluded.tenant_id,
                     account_id=excluded.account_id,subject_id=excluded.subject_id,
                     project_id=excluded.project_id,conversation_id=excluded.conversation_id,
                     purpose=excluded.purpose,vector_json=excluded.vector_json,
                     embedding_model=excluded.embedding_model,dimensions=excluded.dimensions,
                     source_version=excluded.source_version,updated_at=excluded.updated_at""",
                (item.memory_id, item.scope.realm, item.scope.tenant_id,
                 item.scope.account_id, item.scope.subject_id, item.scope.project_id,
                 item.scope.conversation_id, item.scope.purpose, json.dumps(vector),
                 self.embedding.model_id, self.embedding.dimensions, item.version,
                 item.updated_at),
            )
            self.connection.execute(
                "UPDATE memory_vector_index_meta SET index_version=index_version+1 WHERE singleton=1"
            )

    def delete(self, memory_id: str) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM memory_vector_index WHERE memory_id=?", (memory_id,)
            )
            if cursor.rowcount:
                self.connection.execute(
                    "UPDATE memory_vector_index_meta SET index_version=index_version+1 WHERE singleton=1"
                )

    def search(self, scope: MemoryScope, query: str, limit: int) -> list[IndexHit]:
        query_vector = self.embedding.embed([query])[0]
        with self._lock:
            rows = self.connection.execute(
                """SELECT memory_id,vector_json FROM memory_vector_index
                   WHERE realm=? AND tenant_id=? AND purpose=?
                     AND embedding_model=? AND dimensions=?
                     AND (? IS NULL OR account_id=?)
                     AND (? IS NULL OR subject_id=?)
                     AND (? IS NULL OR project_id=?)
                     AND (? IS NULL OR conversation_id IS NULL OR conversation_id=?)""",
                (scope.realm, scope.tenant_id, scope.purpose,
                 self.embedding.model_id, self.embedding.dimensions,
                 scope.account_id, scope.account_id,
                 scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id,
                 scope.conversation_id, scope.conversation_id),
            ).fetchall()
        hits = []
        for row in rows:
            vector = json.loads(row["vector_json"])
            score = sum(a * b for a, b in zip(query_vector, vector))
            if score > 0:
                hits.append(IndexHit(row["memory_id"], score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.memory_id))[:limit]

    @property
    def index_version(self) -> int:
        return int(self.connection.execute(
            "SELECT index_version FROM memory_vector_index_meta WHERE singleton=1"
        ).fetchone()[0])
