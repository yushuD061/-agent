"""Scope-first derived keyword index for long-term memories."""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ..models import MemoryItem, MemoryScope


@dataclass(frozen=True)
class IndexHit:
    memory_id: str
    score: float


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(sorted({
        token for token in re.findall(r"[\w\u4e00-\u9fff]+", text.casefold())
        if len(token) > 1
    }))


class KeywordMemoryIndex:
    """Derived index; exact ownership filters are applied before ranking."""

    def __init__(self, database: str | Path | sqlite3.Connection, *, readonly=False):
        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            path = Path(database)
            if readonly:
                if not path.is_file():
                    raise RuntimeError("memory_keyword_index_not_configured")
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
            if not {"memory_keyword_index", "memory_keyword_index_meta"} <= tables:
                raise RuntimeError("memory_keyword_index_schema_invalid")
        else:
            with self.connection:
                self.connection.executescript("""
              CREATE TABLE IF NOT EXISTS memory_keyword_index (
                memory_id TEXT PRIMARY KEY, realm TEXT NOT NULL, tenant_id TEXT NOT NULL,
                account_id TEXT, subject_id TEXT, project_id TEXT, conversation_id TEXT,
                purpose TEXT NOT NULL, normalized_text TEXT NOT NULL,
                source_version INTEGER NOT NULL, updated_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_memory_keyword_scope ON memory_keyword_index(
                realm,tenant_id,account_id,subject_id,project_id,purpose,conversation_id);
              CREATE TABLE IF NOT EXISTS memory_keyword_index_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                index_version INTEGER NOT NULL, index_model TEXT NOT NULL);
              INSERT OR IGNORE INTO memory_keyword_index_meta VALUES (1,0,'keyword-v1');
                """)

    def upsert(self, item: MemoryItem) -> None:
        text = " ".join(tokenize(f"{item.summary} {item.content}"))
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO memory_keyword_index VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                     realm=excluded.realm,tenant_id=excluded.tenant_id,
                     account_id=excluded.account_id,subject_id=excluded.subject_id,
                     project_id=excluded.project_id,conversation_id=excluded.conversation_id,
                     purpose=excluded.purpose,normalized_text=excluded.normalized_text,
                     source_version=excluded.source_version,updated_at=excluded.updated_at""",
                (item.memory_id, item.scope.realm, item.scope.tenant_id,
                 item.scope.account_id, item.scope.subject_id, item.scope.project_id,
                 item.scope.conversation_id, item.scope.purpose, text, item.version,
                 item.updated_at),
            )
            self.connection.execute(
                "UPDATE memory_keyword_index_meta SET index_version=index_version+1 WHERE singleton=1"
            )

    def delete(self, memory_id: str) -> None:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM memory_keyword_index WHERE memory_id=?", (memory_id,)
            )
            if cursor.rowcount:
                self.connection.execute(
                    "UPDATE memory_keyword_index_meta SET index_version=index_version+1 WHERE singleton=1"
                )

    def search(self, scope: MemoryScope, query: str, limit: int) -> list[IndexHit]:
        terms = set(tokenize(query))
        with self._lock:
            rows = self.connection.execute(
                """SELECT memory_id,normalized_text FROM memory_keyword_index
                   WHERE realm=? AND tenant_id=? AND purpose=?
                     AND (? IS NULL OR account_id=?)
                     AND (? IS NULL OR subject_id=?)
                     AND (? IS NULL OR project_id=?)
                     AND (? IS NULL OR conversation_id IS NULL OR conversation_id=?)""",
                (scope.realm, scope.tenant_id, scope.purpose,
                 scope.account_id, scope.account_id,
                 scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id,
                 scope.conversation_id, scope.conversation_id),
            ).fetchall()
        hits = []
        for row in rows:
            indexed = set(row["normalized_text"].split())
            overlap = len(terms & indexed)
            if not terms or overlap:
                score = overlap / max(1, len(terms | indexed))
                hits.append(IndexHit(row["memory_id"], score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.memory_id))[:limit]

    @property
    def index_version(self) -> int:
        return int(self.connection.execute(
            "SELECT index_version FROM memory_keyword_index_meta WHERE singleton=1"
        ).fetchone()[0])
