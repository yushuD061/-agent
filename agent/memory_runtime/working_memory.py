"""Physically separated SQLite working-memory stores."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    CustomerInquiryWorkingMemory,
    SourcedValue,
    WorkspaceMemoryValue,
    WorkspaceWorkingMemory,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkingMemoryConflict(Exception):
    pass


class CustomerWorkingMemoryStore:
    """Customer state always requires tenant, account and conversation ownership."""

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
            self.connection.execute("""CREATE TABLE IF NOT EXISTS customer_working_memory (
                tenant_id TEXT NOT NULL, account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                state_json TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,account_id,conversation_id))""")

    def get(self, tenant_id: str, account_id: str, conversation_id: str):
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM customer_working_memory
                   WHERE tenant_id=? AND account_id=? AND conversation_id=?""",
                (tenant_id, account_id, conversation_id),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["state_json"])
        payload["fields"] = {
            key: SourcedValue(**value) for key, value in payload.get("fields", {}).items()
        }
        return CustomerInquiryWorkingMemory(**payload)

    def put(
        self, tenant_id: str, state: CustomerInquiryWorkingMemory,
        expected_version: int | None,
    ) -> CustomerInquiryWorkingMemory:
        payload = asdict(state)
        with self._lock, self.connection:
            existing = self.connection.execute(
                """SELECT version FROM customer_working_memory
                   WHERE tenant_id=? AND account_id=? AND conversation_id=?""",
                (tenant_id, state.account_id, state.conversation_id),
            ).fetchone()
            if existing is None:
                if expected_version not in {None, 0}:
                    raise WorkingMemoryConflict("customer_version_conflict")
                next_version = 1
                self.connection.execute(
                    "INSERT INTO customer_working_memory VALUES (?,?,?,?,?,?)",
                    (tenant_id, state.account_id, state.conversation_id,
                     json.dumps({**payload, "version": next_version}, ensure_ascii=False),
                     next_version, _now()),
                )
            else:
                if expected_version != existing["version"]:
                    raise WorkingMemoryConflict("customer_version_conflict")
                next_version = existing["version"] + 1
                self.connection.execute(
                    """UPDATE customer_working_memory SET state_json=?,version=?,updated_at=?
                       WHERE tenant_id=? AND account_id=? AND conversation_id=? AND version=?""",
                    (json.dumps({**payload, "version": next_version}, ensure_ascii=False),
                     next_version, _now(), tenant_id, state.account_id,
                     state.conversation_id, existing["version"]),
                )
        return self.get(tenant_id, state.account_id, state.conversation_id)

    def delete_conversation(self, tenant_id: str, account_id: str, conversation_id: str) -> int:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """DELETE FROM customer_working_memory
                   WHERE tenant_id=? AND account_id=? AND conversation_id=?""",
                (tenant_id, account_id, conversation_id),
            )
        return cursor.rowcount

    def delete_account(self, tenant_id: str, account_id: str) -> int:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM customer_working_memory WHERE tenant_id=? AND account_id=?",
                (tenant_id, account_id),
            )
        return cursor.rowcount


class WorkspaceWorkingMemoryStore:
    """Workspace state is stored in its own database and has no account column."""

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
            self.connection.execute("""CREATE TABLE IF NOT EXISTS workspace_working_memory (
                tenant_id TEXT NOT NULL, subject_id TEXT NOT NULL, project_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, state_json TEXT NOT NULL,
                version INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,subject_id,project_id,conversation_id))""")

    def get(self, tenant_id: str, subject_id: str, project_id: str, conversation_id: str):
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM workspace_working_memory WHERE tenant_id=? AND subject_id=?
                   AND project_id=? AND conversation_id=?""",
                (tenant_id, subject_id, project_id, conversation_id),
            ).fetchone()
        return None if row is None else {"state": json.loads(row["state_json"]), "version": row["version"]}

    def put(
        self, tenant_id: str, subject_id: str, project_id: str, conversation_id: str,
        state: dict, expected_version: int | None,
    ):
        with self._lock, self.connection:
            row = self.connection.execute(
                """SELECT version FROM workspace_working_memory WHERE tenant_id=? AND subject_id=?
                   AND project_id=? AND conversation_id=?""",
                (tenant_id, subject_id, project_id, conversation_id),
            ).fetchone()
            if row is None and expected_version not in {None, 0}:
                raise WorkingMemoryConflict("workspace_version_conflict")
            if row is not None and expected_version != row["version"]:
                raise WorkingMemoryConflict("workspace_version_conflict")
            version = 1 if row is None else row["version"] + 1
            self.connection.execute(
                """INSERT INTO workspace_working_memory VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,subject_id,project_id,conversation_id) DO UPDATE SET
                   state_json=excluded.state_json,version=excluded.version,updated_at=excluded.updated_at""",
                (tenant_id, subject_id, project_id, conversation_id,
                 json.dumps(state, ensure_ascii=False), version, _now()),
            )
        return self.get(tenant_id, subject_id, project_id, conversation_id)

    @staticmethod
    def _decode_state(payload: dict, version: int) -> WorkspaceWorkingMemory:
        def value(item):
            if item is None or isinstance(item, WorkspaceMemoryValue):
                return item
            return WorkspaceMemoryValue(**item)

        def values(items):
            return [value(item) for item in items or ()]

        return WorkspaceWorkingMemory(
            goal=value(payload.get("goal")),
            confirmed_facts={
                key: value(item) for key, item in payload.get("confirmed_facts", {}).items()
            },
            confirmed_conclusions=values(payload.get("confirmed_conclusions")),
            pending_hypotheses=values(payload.get("pending_hypotheses")),
            pending_actions=values(payload.get("pending_actions")),
            completed_actions=values(payload.get("completed_actions")),
            version=version,
        )

    def get_state(
        self, tenant_id: str, subject_id: str, project_id: str, conversation_id: str,
    ) -> WorkspaceWorkingMemory | None:
        record = self.get(tenant_id, subject_id, project_id, conversation_id)
        if record is None:
            return None
        return self._decode_state(record["state"], int(record["version"]))

    def put_state(
        self, tenant_id: str, subject_id: str, project_id: str, conversation_id: str,
        state: WorkspaceWorkingMemory, expected_version: int | None,
    ) -> WorkspaceWorkingMemory:
        payload = asdict(state)
        payload.pop("version", None)
        result = self.put(
            tenant_id, subject_id, project_id, conversation_id,
            payload, expected_version,
        )
        return self._decode_state(result["state"], int(result["version"]))
