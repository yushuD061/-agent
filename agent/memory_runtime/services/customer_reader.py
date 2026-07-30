"""Audited one-way workspace access to customer long-term memory."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path

from ..errors import MemoryAccessDenied
from ..models import ActorContext, CustomerMemoryReadModel, MemoryHit, MemoryScope
from ..policy import MemoryPolicy
from ..stores.sqlite import CustomerSQLiteMemoryStore, utcnow


class ReadonlyCustomerMemoryStore:
    """Query-only SQLite adapter; it exposes no customer-memory write methods."""

    REQUIRED_COLUMNS = {
        "memory_id", "tenant_id", "account_id", "conversation_id", "memory_type",
        "purpose", "content", "summary", "source_refs_json", "status", "confidence",
        "importance", "sensitivity", "consent_record_id", "version", "supersedes",
        "content_hash", "embedding_model", "created_at", "updated_at", "valid_from",
        "expires_at",
    }

    def __init__(self, database: str | Path):
        path = Path(database).resolve()
        if not path.is_file():
            raise RuntimeError("customer_memory_read_database_not_configured")
        self.database_path = path
        self.connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        columns = {
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(customer_memory_item)"
            ).fetchall()
        }
        if not self.REQUIRED_COLUMNS <= columns:
            self.connection.close()
            raise RuntimeError("customer_memory_read_schema_invalid")
        consent_columns = {
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(customer_memory_consent)"
            ).fetchall()
        }
        if not {
            "consent_record_id", "tenant_id", "account_id", "purpose", "status",
            "expires_at",
        } <= consent_columns:
            self.connection.close()
            raise RuntimeError("customer_memory_read_schema_invalid")
        self._lock = threading.RLock()

    def search_owned(
        self, *, tenant_id: str, account_id: str, conversation_id: str | None,
        purpose: str, query: str, top_k: int,
    ) -> list[MemoryHit]:
        terms = {
            term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold())
            if len(term) > 1
        }
        with self._lock:
            rows = self.connection.execute(
                """SELECT i.* FROM customer_memory_item i
                   JOIN customer_memory_consent c
                     ON c.consent_record_id=i.consent_record_id
                   WHERE i.tenant_id=? AND i.account_id=? AND i.purpose=?
                     AND i.status='active' AND c.status='active'
                     AND (i.expires_at IS NULL OR i.expires_at>?)
                     AND (c.expires_at IS NULL OR c.expires_at>?)
                     AND (? IS NULL OR i.conversation_id IS NULL OR i.conversation_id=?)""",
                (tenant_id, account_id, purpose, utcnow(), utcnow(),
                 conversation_id, conversation_id),
            ).fetchall()
        hits: list[MemoryHit] = []
        for row in rows:
            text = f"{row['summary']} {row['content']}".casefold()
            lexical = sum(1 for term in terms if term in text) / max(1, len(terms))
            if not terms or lexical > 0:
                hits.append(MemoryHit(
                    CustomerSQLiteMemoryStore._item(row),
                    lexical + float(row["importance"]) * 0.01,
                ))
        return sorted(hits, key=lambda hit: (-hit.score, hit.item.memory_id))[:top_k]

    def get_active_by_ids(self, actor, scope: MemoryScope, memory_ids):
        MemoryPolicy().require_search(actor, scope)
        ids = tuple(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.connection.execute(
                f"""SELECT i.* FROM customer_memory_item i
                    JOIN customer_memory_consent c ON c.consent_record_id=i.consent_record_id
                    WHERE i.memory_id IN ({placeholders}) AND i.tenant_id=? AND i.account_id=?
                      AND i.purpose=? AND i.status='active' AND c.status='active'
                      AND (i.expires_at IS NULL OR i.expires_at>?)
                      AND (c.expires_at IS NULL OR c.expires_at>?)
                      AND (? IS NULL OR i.conversation_id IS NULL OR i.conversation_id=?)""",
                (*ids, scope.tenant_id, scope.account_id, scope.purpose, utcnow(), utcnow(),
                 scope.conversation_id, scope.conversation_id),
            ).fetchall()
        return {
            row["memory_id"]: CustomerSQLiteMemoryStore._item(row) for row in rows
        }


class CustomerMemoryAccessAudit:
    """Workspace-owned audit sink; customer memory content is never copied here."""

    def __init__(self, database: str | Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            path = Path(database)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self.connection:
            self.connection.execute("""CREATE TABLE IF NOT EXISTS customer_memory_access_audit (
              audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
              operator_id TEXT NOT NULL, account_id TEXT NOT NULL,
              purpose TEXT NOT NULL, conversation_id TEXT,
              returned_memory_ids_json TEXT NOT NULL, result_count INTEGER NOT NULL,
              outcome TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL)""")

    def record(
        self, actor: ActorContext, *, account_id: str, purpose: str,
        conversation_id: str | None, memory_ids: tuple[str, ...] = (),
        outcome: str, error_code: str | None = None,
    ) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO customer_memory_access_audit VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), actor.tenant_id, actor.actor_id, account_id,
                 purpose, conversation_id, json.dumps(memory_ids), len(memory_ids),
                 outcome, error_code, utcnow()),
            )

    def rows(self) -> list[dict]:
        with self._lock:
            return [
                dict(row) for row in self.connection.execute(
                    "SELECT * FROM customer_memory_access_audit ORDER BY rowid"
                ).fetchall()
            ]


class CustomerMemoryReader:
    """Policy-gated, minimized and audited M4 read service."""

    def __init__(
        self, readonly_store: ReadonlyCustomerMemoryStore,
        audit: CustomerMemoryAccessAudit, *, policy: MemoryPolicy | None = None,
        max_top_k: int = 5, search_backend=None,
    ):
        if max_top_k < 1 or max_top_k > 50:
            raise ValueError("memory_top_k_invalid")
        self.readonly_store = readonly_store
        self.audit = audit
        self.policy = policy or MemoryPolicy()
        self.max_top_k = max_top_k
        self.search_backend = search_backend

    def search_for_workspace(
        self, actor: ActorContext, *, customer_account_id: str, purpose: str,
        query: str, conversation_id: str | None = None, top_k: int = 3,
    ) -> list[CustomerMemoryReadModel]:
        account_id = customer_account_id.strip()
        purpose = purpose.strip()
        query = query.strip()
        conversation_id = conversation_id.strip() if conversation_id else None
        if not account_id or not purpose or not query or not 1 <= top_k <= self.max_top_k:
            self.audit.record(
                actor, account_id=account_id, purpose=purpose,
                conversation_id=conversation_id, outcome="denied",
                error_code="memory_read_request_invalid",
            )
            raise MemoryAccessDenied()
        try:
            self.policy.require_workspace_customer_read(actor, purpose)
        except MemoryAccessDenied:
            self.audit.record(
                actor, account_id=account_id, purpose=purpose,
                conversation_id=conversation_id, outcome="denied",
                error_code="memory_scope_denied",
            )
            raise
        if self.search_backend is None:
            hits = self.readonly_store.search_owned(
                tenant_id=actor.tenant_id, account_id=account_id,
                conversation_id=conversation_id, purpose=purpose, query=query,
                top_k=top_k,
            )
        else:
            scope = MemoryScope(
                "customer_conversation" if conversation_id else "customer_private",
                actor.tenant_id, account_id=account_id,
                conversation_id=conversation_id, purpose=purpose,
            )
            hits = self.search_backend.search(actor, scope, query, top_k)
        models = [
            CustomerMemoryReadModel(
                memory_id=hit.item.memory_id,
                memory_type=hit.item.memory_type,
                summary=hit.item.summary,
                confidence=hit.item.confidence,
                valid_from=hit.item.valid_from,
            )
            for hit in hits
        ]
        self.audit.record(
            actor, account_id=account_id, purpose=purpose,
            conversation_id=conversation_id,
            memory_ids=tuple(item.memory_id for item in models), outcome="allowed",
        )
        return models
