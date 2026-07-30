"""Physically separated authoritative SQLite long-term memory stores."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..errors import MemoryAccessDenied, MemoryVersionConflict
from ..models import (
    ActorContext, DeletionJob, MemoryConsent, MemoryExport, MemoryHit,
    MemoryItem, MemoryScope,
)
from ..policy import MemoryPolicy


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def content_hash(content: str) -> str:
    return hashlib.sha256(" ".join(content.split()).casefold().encode()).hexdigest()


def _connect(database):
    if isinstance(database, sqlite3.Connection):
        connection = database
    else:
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


class CustomerSQLiteMemoryStore:
    """Customer main store: every operation is tenant/account scoped."""

    def __init__(self, database, policy: MemoryPolicy | None = None, *, indexing_enabled=False):
        self.connection = _connect(database)
        self.policy = policy or MemoryPolicy()
        self.indexing_enabled = bool(indexing_enabled)
        self._lock = threading.RLock()
        with self.connection:
            self.connection.executescript("""
              PRAGMA foreign_keys=ON;
              CREATE TABLE IF NOT EXISTS customer_memory_consent (
                consent_record_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                account_id TEXT NOT NULL, purpose TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','withdrawn','expired')),
                granted_at TEXT NOT NULL, expires_at TEXT, withdrawn_at TEXT);
              CREATE TABLE IF NOT EXISTS customer_memory_item (
                memory_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, account_id TEXT NOT NULL,
                conversation_id TEXT, memory_type TEXT NOT NULL, purpose TEXT NOT NULL,
                content TEXT NOT NULL, summary TEXT NOT NULL, source_refs_json TEXT NOT NULL,
                status TEXT NOT NULL, confidence REAL NOT NULL, importance REAL NOT NULL,
                sensitivity TEXT NOT NULL, consent_record_id TEXT, version INTEGER NOT NULL,
                supersedes TEXT, content_hash TEXT NOT NULL, embedding_model TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, valid_from TEXT NOT NULL,
                expires_at TEXT, invalid_reason TEXT,
                FOREIGN KEY(consent_record_id) REFERENCES customer_memory_consent(consent_record_id));
              CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_memory_active_hash
                ON customer_memory_item(tenant_id,account_id,purpose,content_hash)
                WHERE status IN ('pending_consent','active');
              CREATE INDEX IF NOT EXISTS idx_customer_memory_scope
                ON customer_memory_item(tenant_id,account_id,purpose,status,expires_at);
              CREATE TABLE IF NOT EXISTS memory_deletion_job (
                job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, account_id TEXT,
                conversation_id TEXT, request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                steps_json TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT);
              CREATE TABLE IF NOT EXISTS memory_index_outbox (
                event_id TEXT PRIMARY KEY, store_kind TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_memory_index_outbox_due
                ON memory_index_outbox(status,available_at,created_at);
            """)

    def _enqueue_index(self, memory_id: str, event_type: str, version: int) -> None:
        if not self.indexing_enabled:
            return
        event_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"customer:{memory_id}:{event_type}:{version}",
        ))
        now = utcnow()
        self.connection.execute(
            """INSERT OR IGNORE INTO memory_index_outbox
               VALUES (?,'customer',?,?,?,'pending',0,?,?,?)""",
            (event_id, memory_id, event_type,
             json.dumps({"source_version": version}), now, now, now),
        )

    def grant_consent(self, actor: ActorContext, consent: MemoryConsent) -> MemoryConsent:
        scope = MemoryScope("customer_private", consent.tenant_id, account_id=consent.account_id,
                            purpose=consent.purpose)
        self.policy.require_search(actor, scope)
        if consent.status != "active" or not consent.categories or not consent.purpose:
            raise ValueError("customer_consent_invalid")
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO customer_memory_consent VALUES (?,?,?,?,?,?,?,?,?)",
                (consent.consent_record_id, consent.tenant_id, consent.account_id,
                 consent.purpose, json.dumps(consent.categories), consent.status,
                 consent.granted_at, consent.expires_at, consent.withdrawn_at),
            )
        return consent

    def withdraw_consent(self, actor: ActorContext, consent_id: str) -> int:
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM customer_memory_consent WHERE consent_record_id=? AND tenant_id=? AND account_id=?",
                (consent_id, actor.tenant_id, actor.actor_id),
            ).fetchone()
            if row is None:
                raise MemoryAccessDenied()
            self.policy.require_search(actor, MemoryScope(
                "customer_private", row["tenant_id"], account_id=row["account_id"],
                purpose=row["purpose"],
            ))
            now = utcnow()
            active_rows = self.connection.execute(
                """SELECT memory_id,version FROM customer_memory_item
                   WHERE tenant_id=? AND account_id=? AND consent_record_id=?
                     AND status='active'""",
                (actor.tenant_id, actor.actor_id, consent_id),
            ).fetchall()
            self.connection.execute(
                "UPDATE customer_memory_consent SET status='withdrawn',withdrawn_at=? WHERE consent_record_id=?",
                (now, consent_id),
            )
            cursor = self.connection.execute(
                """UPDATE customer_memory_item SET status='invalid',invalid_reason='consent_withdrawn',
                   version=version+1,updated_at=? WHERE tenant_id=? AND account_id=?
                   AND consent_record_id=? AND status='active'""",
                (now, actor.tenant_id, actor.actor_id, consent_id),
            )
            for item in active_rows:
                self._enqueue_index(item["memory_id"], "delete", item["version"] + 1)
        return cursor.rowcount

    def create_candidate(self, actor: ActorContext, item: MemoryItem) -> MemoryItem:
        self.policy.require_search(actor, item.scope)
        item.validate()
        if item.scope.realm not in {"customer_private", "customer_conversation"}:
            raise MemoryAccessDenied()
        if item.status != "pending_consent" or item.consent_record_id is not None:
            raise ValueError("customer_memory_candidate_invalid")
        return self._insert(item)

    def _insert(self, item: MemoryItem) -> MemoryItem:
        with self._lock, self.connection:
            self._insert_row(item)
        return item

    def _insert_row(self, item: MemoryItem) -> None:
        self.connection.execute(
            """INSERT INTO customer_memory_item
            (memory_id,tenant_id,account_id,conversation_id,memory_type,purpose,content,
             summary,source_refs_json,status,confidence,importance,sensitivity,
             consent_record_id,version,supersedes,content_hash,embedding_model,created_at,
             updated_at,valid_from,expires_at,invalid_reason)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (item.memory_id, item.scope.tenant_id, item.scope.account_id,
             item.scope.conversation_id, item.memory_type, item.scope.purpose,
             item.content, item.summary, json.dumps(item.source_refs), item.status,
             item.confidence, item.importance, item.sensitivity, item.consent_record_id,
             item.version, item.supersedes, item.content_hash, item.embedding_model,
             item.created_at, item.updated_at, item.valid_from, item.expires_at),
        )

    def activate(self, actor, memory_id, consent_record_id, expected_version):
        with self._lock, self.connection:
            row = self._owned_row(actor, memory_id)
            consent = self.connection.execute(
                """SELECT * FROM customer_memory_consent WHERE consent_record_id=?
                   AND tenant_id=? AND account_id=? AND purpose=? AND status='active'
                   AND (expires_at IS NULL OR expires_at>?)""",
                (consent_record_id, actor.tenant_id, actor.actor_id, row["purpose"], utcnow()),
            ).fetchone()
            if consent is None:
                raise ValueError("customer_consent_invalid")
            cursor = self.connection.execute(
                """UPDATE customer_memory_item SET status='active',consent_record_id=?,
                   version=version+1,updated_at=? WHERE memory_id=? AND tenant_id=? AND account_id=?
                   AND status='pending_consent' AND version=?""",
                (consent_record_id, utcnow(), memory_id, actor.tenant_id,
                 actor.actor_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise MemoryVersionConflict()
            self._enqueue_index(memory_id, "upsert", expected_version + 1)
        return self.get_owned(actor, memory_id)

    def _owned_row(self, actor, memory_id):
        row = self.connection.execute(
            "SELECT * FROM customer_memory_item WHERE memory_id=? AND tenant_id=? AND account_id=?",
            (memory_id, actor.tenant_id, actor.actor_id),
        ).fetchone()
        if row is None:
            raise MemoryAccessDenied()
        realm = "customer_conversation" if row["conversation_id"] else "customer_private"
        self.policy.require_search(actor, MemoryScope(
            realm, row["tenant_id"], account_id=row["account_id"],
            conversation_id=row["conversation_id"], purpose=row["purpose"],
        ))
        return row

    def get_owned(self, actor, memory_id):
        with self._lock:
            return self._item(self._owned_row(actor, memory_id))

    @staticmethod
    def _item(row):
        realm = "customer_conversation" if row["conversation_id"] else "customer_private"
        return MemoryItem(
            row["memory_id"], MemoryScope(realm, row["tenant_id"],
                account_id=row["account_id"], conversation_id=row["conversation_id"],
                purpose=row["purpose"]), row["memory_type"], row["content"], row["summary"],
            tuple(json.loads(row["source_refs_json"])), row["status"], row["confidence"],
            row["importance"], row["sensitivity"], row["consent_record_id"], row["version"],
            row["supersedes"], row["created_at"], row["updated_at"], row["valid_from"],
            row["expires_at"], row["content_hash"], row["embedding_model"],
        )

    def search(self, actor, scope, query, top_k):
        self.policy.require_search(actor, scope)
        if not 1 <= top_k <= 50:
            raise ValueError("memory_top_k_invalid")
        terms = {term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if len(term) > 1}
        with self._lock:
            rows = self.connection.execute(
                """SELECT i.* FROM customer_memory_item i JOIN customer_memory_consent c
                   ON c.consent_record_id=i.consent_record_id WHERE i.tenant_id=? AND i.account_id=?
                   AND i.purpose=? AND i.status='active' AND c.status='active'
                   AND (i.expires_at IS NULL OR i.expires_at>?)
                   AND (c.expires_at IS NULL OR c.expires_at>?)
                   AND (? IS NULL OR i.conversation_id IS NULL OR i.conversation_id=?)""",
                (scope.tenant_id, scope.account_id, scope.purpose, utcnow(), utcnow(),
                 scope.conversation_id, scope.conversation_id),
            ).fetchall()
        hits = []
        for row in rows:
            text = f"{row['summary']} {row['content']}".casefold()
            lexical = sum(1 for term in terms if term in text) / max(1, len(terms))
            score = lexical + float(row["importance"]) * 0.01
            if not terms or lexical > 0:
                hits.append(MemoryHit(self._item(row), score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.item.memory_id))[:top_k]

    def supersede(self, actor, old_id, new_item, expected_version):
        self.policy.require_search(actor, new_item.scope)
        if new_item.status != "active" or new_item.supersedes != old_id:
            raise ValueError("customer_memory_correction_invalid")
        with self._lock, self.connection:
            old = self._owned_row(actor, old_id)
            if old["status"] != "active" or old["version"] != expected_version:
                raise MemoryVersionConflict()
            consent = self.connection.execute(
                "SELECT status FROM customer_memory_consent WHERE consent_record_id=?",
                (new_item.consent_record_id,),
            ).fetchone()
            if consent is None or consent["status"] != "active":
                raise ValueError("customer_consent_invalid")
            self.connection.execute(
                """UPDATE customer_memory_item SET status='superseded',version=version+1,updated_at=?
                   WHERE memory_id=? AND tenant_id=? AND account_id=?""",
                (utcnow(), old_id, actor.tenant_id, actor.actor_id),
            )
            self._insert_row(new_item)
            self._enqueue_index(old_id, "delete", expected_version + 1)
            self._enqueue_index(new_item.memory_id, "upsert", new_item.version)
        return new_item

    def invalidate(self, actor, memory_id, reason, expected_version):
        with self._lock, self.connection:
            self._owned_row(actor, memory_id)
            cursor = self.connection.execute(
                """UPDATE customer_memory_item SET status='invalid',invalid_reason=?,
                   version=version+1,updated_at=? WHERE memory_id=? AND tenant_id=? AND account_id=?
                   AND version=? AND status!='deleted'""",
                (reason[:200], utcnow(), memory_id, actor.tenant_id, actor.actor_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise MemoryVersionConflict()
            self._enqueue_index(memory_id, "delete", expected_version + 1)

    def delete_owned(self, actor, memory_id, expected_version):
        """Logically erase one owned item while retaining its version tombstone."""
        with self._lock, self.connection:
            self._owned_row(actor, memory_id)
            cursor = self.connection.execute(
                """UPDATE customer_memory_item SET status='deleted',content='',summary='',
                   source_refs_json='[]',invalid_reason='customer_deleted',content_hash='',
                   version=version+1,updated_at=? WHERE memory_id=? AND tenant_id=?
                   AND account_id=? AND version=? AND status!='deleted'""",
                (utcnow(), memory_id, actor.tenant_id, actor.actor_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise MemoryVersionConflict()
            self._enqueue_index(memory_id, "delete", expected_version + 1)

    def delete_scope(self, actor, scope, request_id):
        self.policy.require_search(actor, scope)
        now, job_id = utcnow(), str(uuid.uuid4())
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM memory_deletion_job WHERE request_id=? AND tenant_id=? AND account_id=?",
                (request_id, scope.tenant_id, scope.account_id),
            ).fetchone()
            if existing:
                return DeletionJob(existing["job_id"], request_id, existing["status"])
            select_params = [scope.tenant_id, scope.account_id]
            select_suffix = " AND purpose=?"
            select_params.append(scope.purpose)
            if scope.conversation_id:
                select_suffix += " AND conversation_id=?"
                select_params.append(scope.conversation_id)
            deleting = self.connection.execute(
                """SELECT memory_id,version FROM customer_memory_item
                   WHERE tenant_id=? AND account_id=? AND status!='deleted'"""
                + select_suffix, select_params,
            ).fetchall()
            params = [now, scope.tenant_id, scope.account_id]
            suffix = " AND purpose=?"
            params.append(scope.purpose)
            if scope.conversation_id:
                suffix += " AND conversation_id=?"
                params.append(scope.conversation_id)
            self.connection.execute(
                """UPDATE customer_memory_item SET status='deleted',version=version+1,updated_at=?
                   WHERE tenant_id=? AND account_id=? AND status!='deleted'""" + suffix, params,
            )
            final_status = "pending" if deleting and self.indexing_enabled else "completed"
            completed_at = None if final_status == "pending" else now
            self.connection.execute(
                "INSERT INTO memory_deletion_job VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, scope.tenant_id, scope.account_id, scope.conversation_id, request_id,
                 final_status, json.dumps(["primary_store_deleted"]), 0,
                 now, now, completed_at),
            )
            for item in deleting:
                self._enqueue_index(item["memory_id"], "delete", item["version"] + 1)
        return DeletionJob(job_id, request_id, final_status)

    def expire_due(self, *, now: str | None = None) -> int:
        """Expire explicit TTLs. Age alone never expires stable memory."""
        cutoff = now or utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                """UPDATE customer_memory_consent SET status='expired'
                   WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?""",
                (cutoff,),
            )
            rows = self.connection.execute(
                """SELECT DISTINCT i.memory_id,i.version FROM customer_memory_item i
                   LEFT JOIN customer_memory_consent c
                     ON c.consent_record_id=i.consent_record_id
                   WHERE i.status='active' AND (
                     (i.expires_at IS NOT NULL AND i.expires_at<=?) OR
                     c.status IN ('withdrawn','expired') OR
                     (c.expires_at IS NOT NULL AND c.expires_at<=?))""",
                (cutoff, cutoff),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    """UPDATE customer_memory_item SET status='invalid',
                       invalid_reason='ttl_or_consent_expired',version=version+1,updated_at=?
                       WHERE memory_id=? AND status='active'""",
                    (cutoff, row["memory_id"]),
                )
                self._enqueue_index(row["memory_id"], "delete", row["version"] + 1)
        return len(rows)

    def pending_index_events(self, memory_ids: tuple[str, ...] | None = None) -> int:
        params: tuple = ()
        suffix = ""
        if memory_ids:
            placeholders = ",".join("?" for _ in memory_ids)
            suffix = f" AND aggregate_id IN ({placeholders})"
            params = memory_ids
        row = self.connection.execute(
            """SELECT COUNT(*) FROM memory_index_outbox
               WHERE status!='completed'""" + suffix, params,
        ).fetchone()
        return int(row[0])

    def active_items_for_rebuild(self) -> tuple[MemoryItem, ...]:
        """Return only authoritative, recallable rows for a fresh derived index."""
        now = utcnow()
        with self._lock:
            rows = self.connection.execute(
                """SELECT i.* FROM customer_memory_item i
                   JOIN customer_memory_consent c ON c.consent_record_id=i.consent_record_id
                   WHERE i.status='active' AND c.status='active'
                     AND (i.expires_at IS NULL OR i.expires_at>?)
                     AND (c.expires_at IS NULL OR c.expires_at>?)
                   ORDER BY i.memory_id""", (now, now),
            ).fetchall()
        return tuple(self._item(row) for row in rows)

    def export_scope(self, actor, scope):
        self.policy.require_search(actor, scope)
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM customer_memory_item WHERE tenant_id=? AND account_id=?
                   AND purpose=? AND status!='deleted'
                   AND (? IS NULL OR conversation_id=?) ORDER BY created_at,memory_id""",
                (scope.tenant_id, scope.account_id, scope.purpose,
                 scope.conversation_id, scope.conversation_id),
            ).fetchall()
        return MemoryExport(scope, tuple(self._item(row) for row in rows))

    def get_active_by_ids(self, actor, scope, memory_ids):
        self.policy.require_search(actor, scope)
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
        return {row["memory_id"]: self._item(row) for row in rows}

    def get_index_item(self, memory_id):
        with self._lock:
            row = self.connection.execute(
                """SELECT i.* FROM customer_memory_item i
                   JOIN customer_memory_consent c ON c.consent_record_id=i.consent_record_id
                   WHERE i.memory_id=? AND i.status='active' AND c.status='active'
                     AND (i.expires_at IS NULL OR i.expires_at>?)
                     AND (c.expires_at IS NULL OR c.expires_at>?)""",
                (memory_id, utcnow(), utcnow()),
            ).fetchone()
        return self._item(row) if row else None

    def enqueue_active_for_indexing(self) -> int:
        if not self.indexing_enabled:
            return 0
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT memory_id,version FROM customer_memory_item WHERE status='active'"
            ).fetchall()
            before = self.connection.total_changes
            for row in rows:
                self._enqueue_index(row["memory_id"], "upsert", row["version"])
            return self.connection.total_changes - before

    def claim_index_event(self):
        with self._lock, self.connection:
            row = self.connection.execute(
                """SELECT * FROM memory_index_outbox
                   WHERE status IN ('pending','retry_wait') AND available_at<=?
                   ORDER BY created_at,event_id LIMIT 1""", (utcnow(),),
            ).fetchone()
            if row is None:
                return None
            cursor = self.connection.execute(
                """UPDATE memory_index_outbox SET status='processing',updated_at=?
                   WHERE event_id=? AND status IN ('pending','retry_wait')""",
                (utcnow(), row["event_id"]),
            )
            return dict(row) if cursor.rowcount == 1 else None

    def complete_index_event(self, event_id):
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE memory_index_outbox SET status='completed',updated_at=? WHERE event_id=?",
                (utcnow(), event_id),
            )

    def retry_index_event(self, event_id, error_code):
        del error_code  # Keep ordinary outbox rows content-free and error-detail-free.
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT attempt_count FROM memory_index_outbox WHERE event_id=?", (event_id,),
            ).fetchone()
            attempts = int(row["attempt_count"]) + 1 if row else 1
            status = "dead_letter" if attempts >= 5 else "retry_wait"
            available = (datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** attempts)))
            self.connection.execute(
                """UPDATE memory_index_outbox SET status=?,attempt_count=?,available_at=?,updated_at=?
                   WHERE event_id=?""",
                (status, attempts, available.isoformat().replace("+00:00", "Z"), utcnow(), event_id),
            )


class WorkspaceSQLiteMemoryStore:
    """Separate workspace database with no customer account column."""

    def __init__(self, database, policy: MemoryPolicy | None = None, *, indexing_enabled=False):
        self.connection = _connect(database)
        self.policy = policy or MemoryPolicy()
        self.indexing_enabled = bool(indexing_enabled)
        self._lock = threading.RLock()
        with self.connection:
            self.connection.executescript("""CREATE TABLE IF NOT EXISTS workspace_memory_item (
              memory_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, subject_id TEXT,
              project_id TEXT, memory_type TEXT NOT NULL, purpose TEXT NOT NULL,
              content TEXT NOT NULL, summary TEXT NOT NULL, source_refs_json TEXT NOT NULL,
              status TEXT NOT NULL, confidence REAL NOT NULL, importance REAL NOT NULL,
              sensitivity TEXT NOT NULL, version INTEGER NOT NULL, supersedes TEXT,
              content_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              valid_from TEXT NOT NULL, expires_at TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_memory_live_hash
                ON workspace_memory_item(tenant_id,subject_id,project_id,purpose,content_hash)
                WHERE status IN ('pending_confirmation','active');
              CREATE INDEX IF NOT EXISTS ix_workspace_memory_scope
                ON workspace_memory_item(tenant_id,subject_id,project_id,purpose,status,expires_at);
              CREATE TABLE IF NOT EXISTS workspace_memory_index_outbox (
                event_id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_workspace_memory_index_due
                ON workspace_memory_index_outbox(status,available_at,created_at);
              CREATE TABLE IF NOT EXISTS workspace_memory_review_suggestion (
                suggestion_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                subject_id TEXT NOT NULL, project_id TEXT NOT NULL,
                action TEXT NOT NULL, memory_ids_json TEXT NOT NULL,
                rationale TEXT NOT NULL, status TEXT NOT NULL,
                content_hash TEXT NOT NULL, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)

    def _enqueue_index(self, memory_id: str, event_type: str, version: int) -> None:
        if not self.indexing_enabled:
            return
        event_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"workspace:{memory_id}:{event_type}:{version}",
        ))
        now = utcnow()
        self.connection.execute(
            """INSERT OR IGNORE INTO workspace_memory_index_outbox
               VALUES (?,?,?,?, 'pending',0,?,?,?)""",
            (event_id, memory_id, event_type,
             json.dumps({"source_version": version}), now, now, now),
        )

    def create_candidate(self, actor, item):
        self.policy.require_search(actor, item.scope)
        item.validate()
        if item.scope.realm != "workspace_private":
            raise MemoryAccessDenied()
        if item.consent_record_id is not None or item.status not in {
            "pending_confirmation", "active"
        }:
            raise ValueError("workspace_memory_consent_invalid")
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO workspace_memory_item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item.memory_id,item.scope.tenant_id,item.scope.subject_id,item.scope.project_id,
                 item.memory_type,item.scope.purpose,item.content,item.summary,json.dumps(item.source_refs),
                 item.status,item.confidence,item.importance,item.sensitivity,item.version,item.supersedes,
                 item.content_hash,item.created_at,item.updated_at,item.valid_from,item.expires_at),
            )
            if item.status == "active":
                self._enqueue_index(item.memory_id, "upsert", item.version)
        return item

    @staticmethod
    def _item(row):
        return MemoryItem(
            row["memory_id"], MemoryScope(
                "workspace_private", row["tenant_id"], subject_id=row["subject_id"],
                project_id=row["project_id"], purpose=row["purpose"],
            ), row["memory_type"], row["content"], row["summary"],
            tuple(json.loads(row["source_refs_json"])), row["status"],
            row["confidence"], row["importance"], row["sensitivity"], None,
            row["version"], row["supersedes"], row["created_at"], row["updated_at"],
            row["valid_from"], row["expires_at"], row["content_hash"], None,
        )

    def search(self, actor, scope, query, top_k):
        self.policy.require_search(actor, scope)
        if not 1 <= top_k <= 50:
            raise ValueError("memory_top_k_invalid")
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM workspace_memory_item WHERE tenant_id=? AND purpose=?
                   AND status='active' AND (? IS NULL OR subject_id=?)
                   AND (? IS NULL OR project_id=?) AND (expires_at IS NULL OR expires_at>?)""",
                (scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id, utcnow()),
            ).fetchall()
        terms = {term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if len(term)>1}
        hits = []
        for row in rows:
            text = f"{row['summary']} {row['content']}".casefold()
            lexical = sum(1 for term in terms if term in text) / max(1, len(terms))
            if not terms or lexical:
                hits.append(MemoryHit(self._item(row), lexical + row["importance"] * .01))
        return sorted(hits, key=lambda hit: (-hit.score, hit.item.memory_id))[:top_k]

    def _owned(self, actor, memory_id):
        row = self.connection.execute(
            """SELECT * FROM workspace_memory_item WHERE memory_id=? AND tenant_id=?
               AND (subject_id IS NULL OR subject_id=?)""",
            (memory_id, actor.tenant_id, actor.actor_id),
        ).fetchone()
        if row is None:
            raise MemoryAccessDenied()
        self.policy.require_search(actor, self._item(row).scope)
        return row

    def activate(self, actor, memory_id, consent_record_id, expected_version):
        del consent_record_id
        with self._lock, self.connection:
            row = self._owned(actor, memory_id)
            cursor = self.connection.execute(
                """UPDATE workspace_memory_item SET status='active',version=version+1,updated_at=?
                   WHERE memory_id=? AND tenant_id=? AND version=? AND status='pending_confirmation'""",
                (utcnow(), memory_id, actor.tenant_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise MemoryVersionConflict()
            self._enqueue_index(memory_id, "upsert", expected_version + 1)
        return self._item(self._owned(actor, memory_id))

    def supersede(self, actor, old_id, new_item, expected_version):
        self.policy.require_search(actor, new_item.scope)
        new_item.validate()
        if new_item.status != "active" or new_item.supersedes != old_id:
            raise ValueError("workspace_memory_correction_invalid")
        with self._lock, self.connection:
            old = self._owned(actor, old_id)
            if old["status"] != "active" or old["version"] != expected_version:
                raise MemoryVersionConflict()
            old_scope = self._item(old).scope
            if new_item.scope != old_scope:
                raise ValueError("workspace_memory_scope_change_denied")
            self.connection.execute(
                "UPDATE workspace_memory_item SET status='superseded',version=version+1,updated_at=? WHERE memory_id=?",
                (utcnow(), old_id),
            )
            self.connection.execute(
                """INSERT INTO workspace_memory_item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_item.memory_id,new_item.scope.tenant_id,new_item.scope.subject_id,
                 new_item.scope.project_id,new_item.memory_type,new_item.scope.purpose,
                 new_item.content,new_item.summary,json.dumps(new_item.source_refs),new_item.status,
                 new_item.confidence,new_item.importance,new_item.sensitivity,new_item.version,
                 new_item.supersedes,new_item.content_hash,new_item.created_at,new_item.updated_at,
                 new_item.valid_from,new_item.expires_at),
            )
            self._enqueue_index(old_id, "delete", expected_version + 1)
            self._enqueue_index(new_item.memory_id, "upsert", new_item.version)
        return new_item

    def invalidate(self, actor, memory_id, reason, expected_version):
        del reason
        with self._lock, self.connection:
            self._owned(actor, memory_id)
            cursor = self.connection.execute(
                """UPDATE workspace_memory_item SET status='invalid',version=version+1,updated_at=?
                   WHERE memory_id=? AND tenant_id=? AND version=?""",
                (utcnow(), memory_id, actor.tenant_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise MemoryVersionConflict()
            self._enqueue_index(memory_id, "delete", expected_version + 1)

    def get_owned(self, actor, memory_id):
        with self._lock:
            return self._item(self._owned(actor, memory_id))

    def delete_owned(self, actor, memory_id, expected_version):
        with self._lock, self.connection:
            self._owned(actor, memory_id)
            cursor = self.connection.execute(
                """UPDATE workspace_memory_item SET status='deleted',content='',summary='',
                   source_refs_json='[]',content_hash='',version=version+1,updated_at=?
                   WHERE memory_id=? AND tenant_id=? AND version=? AND status!='deleted'""",
                (utcnow(), memory_id, actor.tenant_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise MemoryVersionConflict()
            self._enqueue_index(memory_id, "delete", expected_version + 1)

    def delete_scope(self, actor, scope, request_id):
        self.policy.require_search(actor, scope)
        with self._lock, self.connection:
            rows = self.connection.execute(
                """SELECT memory_id,version FROM workspace_memory_item
                   WHERE tenant_id=? AND status!='deleted' AND purpose=?
                   AND (? IS NULL OR subject_id=?) AND (? IS NULL OR project_id=?)""",
                (scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id),
            ).fetchall()
            self.connection.execute(
                """UPDATE workspace_memory_item SET status='deleted',version=version+1,updated_at=?
                   WHERE tenant_id=? AND status!='deleted' AND purpose=?
                   AND (? IS NULL OR subject_id=?) AND (? IS NULL OR project_id=?)""",
                (utcnow(), scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id),
            )
            for row in rows:
                self._enqueue_index(row["memory_id"], "delete", row["version"] + 1)
        return DeletionJob(str(uuid.uuid5(uuid.NAMESPACE_URL, request_id)), request_id, "completed")

    def export_scope(self, actor, scope):
        self.policy.require_search(actor, scope)
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM workspace_memory_item WHERE tenant_id=? AND status!='deleted'
                   AND purpose=? AND (? IS NULL OR subject_id=?)
                   AND (? IS NULL OR project_id=?)""",
                (scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id),
            ).fetchall()
        return MemoryExport(scope, tuple(self._item(row) for row in rows))

    def list_scope(self, actor, scope, *, status: str | None = None,
                   limit: int = 50, after: str = "") -> tuple[MemoryItem, ...]:
        self.policy.require_search(actor, scope)
        if not 1 <= limit <= 100:
            raise ValueError("memory_limit_invalid")
        params: list = [scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                        scope.project_id, scope.project_id, after]
        status_sql = ""
        if status:
            status_sql = " AND status=?"
            params.append(status)
        params.append(limit)
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM workspace_memory_item WHERE tenant_id=? AND purpose=?
                   AND (? IS NULL OR subject_id=?) AND (? IS NULL OR project_id=?)
                   AND memory_id>?""" + status_sql + " ORDER BY memory_id LIMIT ?",
                params,
            ).fetchall()
        return tuple(self._item(row) for row in rows)

    def count_scope(self, actor, scope, *, status: str | None = None) -> int:
        self.policy.require_search(actor, scope)
        params: list = [scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                        scope.project_id, scope.project_id]
        status_sql = ""
        if status:
            status_sql = " AND status=?"
            params.append(status)
        with self._lock:
            row = self.connection.execute(
                """SELECT COUNT(*) FROM workspace_memory_item WHERE tenant_id=? AND purpose=?
                   AND (? IS NULL OR subject_id=?) AND (? IS NULL OR project_id=?)"""
                + status_sql,
                params,
            ).fetchone()
        return int(row[0])

    def get_active_by_ids(self, actor, scope, memory_ids):
        self.policy.require_search(actor, scope)
        ids = tuple(dict.fromkeys(memory_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.connection.execute(
                f"""SELECT * FROM workspace_memory_item WHERE memory_id IN ({placeholders})
                    AND tenant_id=? AND purpose=? AND status='active'
                    AND (? IS NULL OR subject_id=?) AND (? IS NULL OR project_id=?)
                    AND (expires_at IS NULL OR expires_at>?)""",
                (*ids, scope.tenant_id, scope.purpose, scope.subject_id, scope.subject_id,
                 scope.project_id, scope.project_id, utcnow()),
            ).fetchall()
        return {row["memory_id"]: self._item(row) for row in rows}

    def get_index_item(self, memory_id):
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM workspace_memory_item WHERE memory_id=? AND status='active'
                   AND (expires_at IS NULL OR expires_at>?)""", (memory_id, utcnow()),
            ).fetchone()
        return self._item(row) if row else None

    def active_items_for_rebuild(self):
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM workspace_memory_item WHERE status='active'
                   AND (expires_at IS NULL OR expires_at>?) ORDER BY memory_id""",
                (utcnow(),),
            ).fetchall()
        return tuple(self._item(row) for row in rows)

    def enqueue_active_for_indexing(self):
        if not self.indexing_enabled:
            return 0
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT memory_id,version FROM workspace_memory_item WHERE status='active'"
            ).fetchall()
            before = self.connection.total_changes
            for row in rows:
                self._enqueue_index(row["memory_id"], "upsert", row["version"])
            return self.connection.total_changes - before

    def claim_index_event(self):
        with self._lock, self.connection:
            row = self.connection.execute(
                """SELECT * FROM workspace_memory_index_outbox
                   WHERE status IN ('pending','retry_wait') AND available_at<=?
                   ORDER BY created_at,event_id LIMIT 1""", (utcnow(),),
            ).fetchone()
            if row is None:
                return None
            cursor = self.connection.execute(
                """UPDATE workspace_memory_index_outbox SET status='processing',updated_at=?
                   WHERE event_id=? AND status IN ('pending','retry_wait')""",
                (utcnow(), row["event_id"]),
            )
            return dict(row) if cursor.rowcount == 1 else None

    def complete_index_event(self, event_id):
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workspace_memory_index_outbox SET status='completed',updated_at=? WHERE event_id=?",
                (utcnow(), event_id),
            )

    def retry_index_event(self, event_id, error_code):
        del error_code
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT attempt_count FROM workspace_memory_index_outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
            attempts = int(row["attempt_count"]) + 1 if row else 1
            status = "dead_letter" if attempts >= 5 else "retry_wait"
            available = datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** attempts))
            self.connection.execute(
                """UPDATE workspace_memory_index_outbox SET status=?,attempt_count=?,
                   available_at=?,updated_at=? WHERE event_id=?""",
                (status, attempts, available.isoformat().replace("+00:00", "Z"),
                 utcnow(), event_id),
            )

    def expire_due(self, *, now: str | None = None):
        cutoff = now or utcnow()
        with self._lock, self.connection:
            rows = self.connection.execute(
                """SELECT memory_id,version FROM workspace_memory_item WHERE status='active'
                   AND expires_at IS NOT NULL AND expires_at<=?""", (cutoff,),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    """UPDATE workspace_memory_item SET status='invalid',version=version+1,
                       updated_at=? WHERE memory_id=? AND status='active'""",
                    (cutoff, row["memory_id"]),
                )
                self._enqueue_index(row["memory_id"], "delete", row["version"] + 1)
        return len(rows)


class PublicApprovedMemoryStore:
    """Approved public records in a separate database; customer side is read-only."""

    def __init__(self, database):
        self.connection = _connect(database)
        with self.connection:
            self.connection.execute("""CREATE TABLE IF NOT EXISTS public_memory_item (
              memory_id TEXT PRIMARY KEY, content TEXT NOT NULL, summary TEXT NOT NULL,
              source_ref TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
              content_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def import_approved_markdown(self, path: str | Path) -> int:
        source = Path(path)
        if not source.is_file():
            return 0
        entries = [line[2:].strip() for line in source.read_text(encoding="utf-8").splitlines()
                   if line.startswith("- ") and line[2:].strip()]
        now, count = utcnow(), 0
        with self.connection:
            for entry in entries:
                digest = content_hash(entry)
                cursor = self.connection.execute(
                    """INSERT OR IGNORE INTO public_memory_item VALUES (?,?,?,?, 'active',1,?,?,?)""",
                    (str(uuid.uuid4()), entry, entry, str(source), digest, now, now),
                )
                count += cursor.rowcount
        return count

    def search(self, query: str, top_k: int = 3) -> list[str]:
        terms = {term for term in re.findall(r"[\w\u4e00-\u9fff]+", query.casefold()) if len(term)>1}
        rows = self.connection.execute(
            "SELECT * FROM public_memory_item WHERE status='active'"
        ).fetchall()
        ranked = []
        for row in rows:
            text = row["content"].casefold()
            score = sum(1 for term in terms if term in text)
            if not terms or score:
                ranked.append((score, row["content"]))
        return [content for _, content in sorted(ranked, key=lambda x: (-x[0], x[1]))[:top_k]]
