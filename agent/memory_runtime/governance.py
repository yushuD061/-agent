"""Deterministic TTL and account-subject governance."""

from __future__ import annotations

import json
import sqlite3

from .stores.sqlite import utcnow


class CustomerMemoryGovernance:
    def __init__(self, long_term_store):
        self.store = long_term_store
        with self.store.connection:
            self.store.connection.execute("""CREATE TABLE IF NOT EXISTS memory_account_merge_audit (
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
              source_account_id TEXT NOT NULL, target_account_id TEXT NOT NULL,
              actor_id TEXT NOT NULL, result TEXT NOT NULL, details_json TEXT NOT NULL,
              created_at TEXT NOT NULL)""")

    def review_ttl(self, *, now: str | None = None) -> int:
        return self.store.expire_due(now=now)

    def merge_accounts(
        self, *, actor_kind: str, actor_id: str, tenant_id: str,
        source_account_id: str, target_account_id: str,
        target_tenant_id: str | None = None,
    ) -> int:
        """Fail-closed merge for an approved identity-service caller only."""
        if actor_kind != "identity_service":
            raise PermissionError("memory_account_merge_actor_denied")
        if target_tenant_id is not None and target_tenant_id != tenant_id:
            raise PermissionError("memory_account_merge_cross_tenant_denied")
        if not all((actor_id, tenant_id, source_account_id, target_account_id)):
            raise ValueError("memory_account_merge_scope_invalid")
        if source_account_id == target_account_id:
            raise ValueError("memory_account_merge_same_account")
        with self.store._lock, self.store.connection:
            tables = {row[0] for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            required = {"customer_conversation", "customer_message", "customer_working_memory"}
            if not required <= tables:
                raise RuntimeError("memory_account_merge_dependencies_missing")
            conflict = self.store.connection.execute(
                """SELECT 1 FROM customer_memory_item s JOIN customer_memory_item t
                   ON t.tenant_id=s.tenant_id AND t.account_id=? AND t.purpose=s.purpose
                   AND t.content_hash=s.content_hash AND t.status IN ('active','pending_consent')
                   WHERE s.tenant_id=? AND s.account_id=?
                     AND s.status IN ('active','pending_consent') LIMIT 1""",
                (target_account_id, tenant_id, source_account_id),
            ).fetchone()
            if conflict:
                raise ValueError("memory_account_merge_conflict")
            ownership_conflict = self.store.connection.execute(
                """SELECT 1 FROM customer_conversation s JOIN customer_conversation t
                   ON t.conversation_id=s.conversation_id AND t.tenant_id=s.tenant_id
                   AND t.account_id=? WHERE s.tenant_id=? AND s.account_id=? LIMIT 1""",
                (target_account_id, tenant_id, source_account_id),
            ).fetchone()
            if ownership_conflict:
                raise ValueError("memory_account_merge_conversation_conflict")
            moved = self.store.connection.execute(
                """SELECT memory_id,version,status FROM customer_memory_item
                   WHERE tenant_id=? AND account_id=?""",
                (tenant_id, source_account_id),
            ).fetchall()
            # Consent IDs and memory IDs are stable; only their deterministic owner changes.
            changed = self.store.connection.execute(
                "UPDATE customer_memory_consent SET account_id=? WHERE tenant_id=? AND account_id=?",
                (target_account_id, tenant_id, source_account_id),
            ).rowcount
            changed += self.store.connection.execute(
                "UPDATE customer_memory_item SET account_id=?,version=version+1,updated_at=? WHERE tenant_id=? AND account_id=?",
                (target_account_id, utcnow(), tenant_id, source_account_id),
            ).rowcount
            changed += self.store.connection.execute(
                "UPDATE customer_conversation SET account_id=?,version=version+1,updated_at=? WHERE tenant_id=? AND account_id=?",
                (target_account_id, utcnow(), tenant_id, source_account_id),
            ).rowcount
            changed += self.store.connection.execute(
                "UPDATE customer_message SET account_id=? WHERE tenant_id=? AND account_id=?",
                (target_account_id, tenant_id, source_account_id),
            ).rowcount
            changed += self.store.connection.execute(
                "UPDATE customer_working_memory SET account_id=? WHERE tenant_id=? AND account_id=?",
                (target_account_id, tenant_id, source_account_id),
            ).rowcount
            for item in moved:
                event_type = "upsert" if item["status"] == "active" else "delete"
                self.store._enqueue_index(item["memory_id"], event_type, item["version"] + 1)
            self.store.connection.execute(
                """INSERT INTO memory_account_merge_audit
                   (tenant_id,source_account_id,target_account_id,actor_id,result,details_json,created_at)
                   VALUES (?,?,?,?, 'completed',?,?)""",
                (tenant_id, source_account_id, target_account_id, actor_id,
                 json.dumps({"changed_rows": changed}), utcnow()),
            )
        return changed
