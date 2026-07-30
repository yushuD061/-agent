"""MySQL 8 adapter for the unified task runtime repository.

It reuses the state-machine implementation after translating DB-API syntax;
the MySQL migration remains explicit and fail-closed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import threading
from typing import Any, Iterator

from agent.business.mysql_database import create_connection
from agent.business.task_runtime_repository import SQLiteTaskRuntimeRepository, TaskRuntimeError


def _mysql_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.endswith("Z") and "T" in value:
        try:
            return datetime.fromisoformat(value[:-1])
        except ValueError:
            return value
    return value


class _CursorResult:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    @property
    def rowcount(self):
        return self.cursor.rowcount


class _ConnectionAdapter:
    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _sql(statement: str) -> str:
        statement = statement.replace("INSERT OR IGNORE", "INSERT IGNORE")
        return statement.replace("?", "%s")

    def execute(self, statement: str, parameters=()):
        cursor = self.raw.cursor()
        cursor.execute(self._sql(statement), tuple(_mysql_value(value) for value in parameters))
        return _CursorResult(cursor)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


class MySQLTaskRuntimeRepository(SQLiteTaskRuntimeRepository):
    REQUIRED_TABLES = {
        "task_instance", "task_plan_revision", "task_step", "task_step_attempt",
        "task_checkpoint", "task_instruction", "task_human_action", "task_artifact",
        "task_event", "task_resume_outbox", "task_idempotency",
    }
    REQUIRED_COLUMNS = {
        "task_instance": {"task_id", "tenant_id", "customer_account_id", "conversation_id",
                          "status", "active_plan_version", "last_sequence", "version"},
        "task_step": {"step_id", "task_id", "plan_version", "step_key", "status",
                      "lease_owner", "lease_until", "checkpoint_id"},
        "task_checkpoint": {"checkpoint_id", "task_id", "input_hash", "output_hash", "valid"},
        "task_event": {"task_id", "sequence", "safe_json", "internal_json"},
        "task_resume_outbox": {"command_id", "idempotency_key", "status", "lease_until"},
    }

    def __init__(self, connection=None):
        raw = connection or create_connection()
        self._raw_connection = raw
        self.connection = _ConnectionAdapter(raw)
        self.database_path = "mysql://configured-trade-ops/task-runtime"
        self._lock = threading.RLock()
        self._idempotency_lock = threading.RLock()
        self.migrate()

    @contextmanager
    def tx(self, *, immediate: bool = False) -> Iterator[_ConnectionAdapter]:
        del immediate
        with self._lock:
            self._raw_connection.begin()
            try:
                yield self.connection
                self._raw_connection.commit()
            except Exception:
                self._raw_connection.rollback()
                raise

    def migrate(self) -> None:
        with self._raw_connection.cursor() as cursor:
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()")
            tables = {row["table_name"] for row in cursor.fetchall()}
            missing = sorted(self.REQUIRED_TABLES - tables)
            if missing:
                raise TaskRuntimeError("task_runtime_mysql_migration_incomplete:" + ",".join(missing), 503)
            cursor.execute("""SELECT table_name,column_name FROM information_schema.columns
              WHERE table_schema=DATABASE()""")
            columns: dict[str, set[str]] = {}
            for row in cursor.fetchall():
                columns.setdefault(row["table_name"], set()).add(row["column_name"])
        missing_columns = sorted(
            f"{table}.{column}" for table, required in self.REQUIRED_COLUMNS.items()
            for column in required - columns.get(table, set()))
        if missing_columns:
            raise TaskRuntimeError(
                "task_runtime_mysql_migration_incomplete:" + ",".join(missing_columns), 503)

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json"):
                raw = result.pop(key)
                result[key[:-5]] = json.loads(raw) if isinstance(raw, str) else (raw or {})
        for key in ("pause_requested", "cancel_requested", "valid", "approved"):
            if key in result:
                result[key] = bool(result[key])
        for key, value in tuple(result.items()):
            if isinstance(value, datetime):
                result[key] = value.isoformat(timespec="microseconds") + "Z"
        return result
