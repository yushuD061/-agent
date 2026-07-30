"""Durable, resumable customer-memory deletion orchestration."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from .stores.sqlite import utcnow


@dataclass(frozen=True)
class GovernanceDeletionJob:
    job_id: str
    request_id: str
    status: str
    steps: dict[str, str]
    attempt_count: int
    last_error_code: str | None


class CustomerDeletionCoordinator:
    """Coordinates primary and derived deletion; never reports early success."""

    STEP_NAMES = ("conversations", "working_memory", "long_term_memory", "indexes", "external")

    def __init__(
        self, database: sqlite3.Connection, *, conversation_repository,
        working_memory_store, long_term_store, index_worker=None,
        external_cleanups: tuple[Callable[[str, str], bool], ...] = (),
    ):
        self.connection = database
        self.connection.row_factory = sqlite3.Row
        self.conversations = conversation_repository
        self.working = working_memory_store
        self.long_term = long_term_store
        self.index_worker = index_worker
        self.external_cleanups = external_cleanups
        self._lock = threading.RLock()
        with self.connection:
            self.connection.execute("""CREATE TABLE IF NOT EXISTS memory_governance_deletion_job (
              job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
              tenant_id TEXT NOT NULL, account_id TEXT NOT NULL,
              status TEXT NOT NULL, steps_json TEXT NOT NULL,
              memory_ids_json TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              completed_at TEXT)""")

    def request_account_deletion(
        self, *, tenant_id: str, account_id: str, request_id: str,
    ) -> GovernanceDeletionJob:
        if not tenant_id or not account_id or not request_id:
            raise ValueError("memory_deletion_scope_invalid")
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM memory_governance_deletion_job WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing:
                if (existing["tenant_id"], existing["account_id"]) != (tenant_id, account_id):
                    raise ValueError("memory_deletion_request_scope_conflict")
                return self._job(existing)
            rows = self.long_term.connection.execute(
                "SELECT memory_id FROM customer_memory_item WHERE tenant_id=? AND account_id=?",
                (tenant_id, account_id),
            ).fetchall()
            now = utcnow()
            self.connection.execute(
                "INSERT INTO memory_governance_deletion_job VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (str(uuid.uuid4()), request_id, tenant_id, account_id, "pending",
                 json.dumps({name: "pending" for name in self.STEP_NAMES}),
                 json.dumps([row[0] for row in rows]), 0, None, now, now),
            )
            row = self.connection.execute(
                "SELECT * FROM memory_governance_deletion_job WHERE request_id=?", (request_id,),
            ).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row) -> GovernanceDeletionJob:
        return GovernanceDeletionJob(
            row["job_id"], row["request_id"], row["status"],
            json.loads(row["steps_json"]), row["attempt_count"], row["last_error_code"],
        )

    def run(self, request_id: str) -> GovernanceDeletionJob:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM memory_governance_deletion_job WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError("memory_deletion_job_not_found")
            if row["status"] == "completed":
                return self._job(row)
            steps = json.loads(row["steps_json"])
            tenant_id, account_id = row["tenant_id"], row["account_id"]
            memory_ids = tuple(json.loads(row["memory_ids_json"]))
            try:
                if steps["conversations"] != "completed":
                    with self.conversations._lock, self.conversations.connection:
                        self.conversations.connection.execute(
                            "DELETE FROM customer_message WHERE tenant_id=? AND account_id=?",
                            (tenant_id, account_id),
                        )
                        self.conversations.connection.execute(
                            """UPDATE customer_conversation SET status='deleted',deleted_at=?,
                               updated_at=?,version=version+1
                               WHERE tenant_id=? AND account_id=? AND status!='deleted'""",
                            (utcnow(), utcnow(), tenant_id, account_id),
                        )
                    steps["conversations"] = "completed"
                if steps["working_memory"] != "completed":
                    self.working.delete_account(tenant_id, account_id)
                    steps["working_memory"] = "completed"
                if steps["long_term_memory"] != "completed":
                    with self.long_term._lock, self.long_term.connection:
                        rows = self.long_term.connection.execute(
                            """SELECT memory_id,version FROM customer_memory_item
                               WHERE tenant_id=? AND account_id=? AND status!='deleted'""",
                            (tenant_id, account_id),
                        ).fetchall()
                        self.long_term.connection.execute(
                            """UPDATE customer_memory_item SET status='deleted',content='',summary='',
                               source_refs_json='[]',content_hash='',invalid_reason='account_deleted',
                               version=version+1,updated_at=?
                               WHERE tenant_id=? AND account_id=? AND status!='deleted'""",
                            (utcnow(), tenant_id, account_id),
                        )
                        for item in rows:
                            self.long_term._enqueue_index(item["memory_id"], "delete", item["version"] + 1)
                    steps["long_term_memory"] = "completed"
                if steps["indexes"] != "completed":
                    if self.index_worker is not None:
                        self.index_worker.drain()
                    if self.long_term.pending_index_events(memory_ids):
                        raise RuntimeError("memory_index_cleanup_pending")
                    steps["indexes"] = "completed"
                if steps["external"] != "completed":
                    if not all(cleanup(tenant_id, account_id) for cleanup in self.external_cleanups):
                        raise RuntimeError("memory_external_cleanup_pending")
                    steps["external"] = "completed"
                self._save(row["job_id"], "completed", steps, None, completed=True)
            except Exception as exc:
                code = str(exc) if str(exc).startswith("memory_") else type(exc).__name__
                self._save(row["job_id"], "retry_wait", steps, code, completed=False)
            current = self.connection.execute(
                "SELECT * FROM memory_governance_deletion_job WHERE job_id=?", (row["job_id"],),
            ).fetchone()
            return self._job(current)

    def _save(self, job_id, status, steps, error, *, completed):
        now = utcnow()
        with self.connection:
            self.connection.execute(
                """UPDATE memory_governance_deletion_job SET status=?,steps_json=?,
                   attempt_count=attempt_count+1,last_error_code=?,updated_at=?,completed_at=?
                   WHERE job_id=?""",
                (status, json.dumps(steps), error, now, now if completed else None, job_id),
            )
