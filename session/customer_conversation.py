"""Account-owned customer conversation and message repository."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.memory_runtime.models import DeletionJob


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CustomerConversationError(Exception):
    def __init__(self, code: str, status_code: int):
        self.code, self.status_code = code, status_code
        super().__init__(code)


@dataclass(frozen=True)
class CustomerOwner:
    tenant_id: str
    account_id: str


@dataclass(frozen=True)
class CustomerConversation:
    conversation_id: str
    tenant_id: str
    account_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    last_message_at: str | None
    version: int


@dataclass(frozen=True)
class CustomerMessage:
    message_id: str
    role: str
    content: Any
    created_at: str
    request_id: str | None = None


@dataclass(frozen=True)
class Page:
    items: tuple[Any, ...]
    next_cursor: str | None


class CustomerConversationRepository:
    def __init__(self, database: str | Path | sqlite3.Connection, *, cursor_secret: bytes) -> None:
        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            path = Path(database)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._secret = cursor_secret
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self.connection:
            self.connection.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS customer_conversation (
                  conversation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  account_id TEXT NOT NULL, title TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','archived','deleted')),
                  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, last_message_at TEXT, deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_customer_conversation_owner
                  ON customer_conversation(tenant_id,account_id,status,updated_at,conversation_id);
                CREATE TABLE IF NOT EXISTS customer_message (
                  message_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, account_id TEXT NOT NULL,
                  conversation_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                  content_json TEXT NOT NULL, tool_group_id TEXT, request_id TEXT,
                  created_at TEXT NOT NULL, archived_at TEXT,
                  FOREIGN KEY(conversation_id) REFERENCES customer_conversation(conversation_id),
                  UNIQUE(tenant_id,account_id,conversation_id,request_id,role)
                );
                CREATE INDEX IF NOT EXISTS idx_customer_message_owner_time
                  ON customer_message(tenant_id,account_id,conversation_id,created_at,message_id);
                CREATE TABLE IF NOT EXISTS memory_deletion_job (
                  job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, account_id TEXT,
                  conversation_id TEXT, request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                  steps_json TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
                );
            """)

    @staticmethod
    def _clean_title(title: str) -> str:
        cleaned = "".join(ch for ch in str(title).strip() if ch >= " ")
        if not cleaned or len(cleaned) > 120:
            raise CustomerConversationError("customer_input_invalid", 422)
        return cleaned

    @staticmethod
    def _conversation(row: sqlite3.Row) -> CustomerConversation:
        return CustomerConversation(
            row["conversation_id"], row["tenant_id"], row["account_id"],
            row["title"], row["status"], row["created_at"], row["updated_at"],
            row["last_message_at"], row["version"],
        )

    def create(self, owner: CustomerOwner, title: str) -> CustomerConversation:
        now, conversation_id = _now(), str(uuid.uuid4())
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO customer_conversation
                   (conversation_id,tenant_id,account_id,title,status,version,created_at,updated_at)
                   VALUES (?,?,?,?, 'active',1,?,?)""",
                (conversation_id, owner.tenant_id, owner.account_id,
                 self._clean_title(title), now, now),
            )
        return self.get_owned(owner, conversation_id)

    def get_owned(self, owner: CustomerOwner, conversation_id: str) -> CustomerConversation:
        with self._lock:
            row = self.connection.execute(
                """SELECT * FROM customer_conversation WHERE tenant_id=? AND account_id=?
                   AND conversation_id=? AND status!='deleted'""",
                (owner.tenant_id, owner.account_id, conversation_id),
            ).fetchone()
        if row is None:
            raise CustomerConversationError("customer_resource_not_found", 404)
        return self._conversation(row)

    def _encode_cursor(self, updated_at: str, conversation_id: str) -> str:
        payload = json.dumps([updated_at, conversation_id], separators=(",", ":")).encode()
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def _decode_cursor(self, cursor: str) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.new(self._secret, payload, hashlib.sha256).digest()):
                raise ValueError
            updated_at, conversation_id = json.loads(payload)
            return str(updated_at), str(conversation_id)
        except Exception:
            raise CustomerConversationError("customer_cursor_invalid", 400) from None

    def list_owned(self, owner: CustomerOwner, cursor: str | None, limit: int) -> Page:
        if not 1 <= limit <= 100:
            raise CustomerConversationError("customer_input_invalid", 422)
        params: list[Any] = [owner.tenant_id, owner.account_id]
        boundary = ""
        if cursor:
            updated_at, conversation_id = self._decode_cursor(cursor)
            boundary = " AND (updated_at<? OR (updated_at=? AND conversation_id<?))"
            params.extend([updated_at, updated_at, conversation_id])
        params.append(limit + 1)
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM customer_conversation WHERE tenant_id=? AND account_id=?
                   AND status!='deleted'""" + boundary +
                " ORDER BY updated_at DESC, conversation_id DESC LIMIT ?", params,
            ).fetchall()
        items = tuple(self._conversation(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = self._encode_cursor(last.updated_at, last.conversation_id)
        return Page(items, next_cursor)

    def append_message(
        self, owner: CustomerOwner, conversation_id: str, *, role: str,
        content: Any, request_id: str | None = None,
    ) -> CustomerMessage:
        if role not in {"user", "assistant", "tool"}:
            raise CustomerConversationError("customer_input_invalid", 422)
        self.get_owned(owner, conversation_id)
        now, message_id = _now(), str(uuid.uuid4())
        with self._lock, self.connection:
            try:
                self.connection.execute(
                    """INSERT INTO customer_message
                       (message_id,tenant_id,account_id,conversation_id,role,content_json,request_id,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (message_id, owner.tenant_id, owner.account_id, conversation_id,
                     role, json.dumps(content, ensure_ascii=False), request_id, now),
                )
            except sqlite3.IntegrityError:
                row = self.connection.execute(
                    """SELECT * FROM customer_message WHERE tenant_id=? AND account_id=?
                       AND conversation_id=? AND request_id=? AND role=?""",
                    (owner.tenant_id, owner.account_id, conversation_id, request_id, role),
                ).fetchone()
                if row is None:
                    raise
                return self._message(row)
            self.connection.execute(
                """UPDATE customer_conversation SET last_message_at=?,updated_at=?
                   WHERE tenant_id=? AND account_id=? AND conversation_id=?""",
                (now, now, owner.tenant_id, owner.account_id, conversation_id),
            )
        return CustomerMessage(message_id, role, content, now, request_id)

    @staticmethod
    def _message(row: sqlite3.Row) -> CustomerMessage:
        return CustomerMessage(
            row["message_id"], row["role"], json.loads(row["content_json"]),
            row["created_at"], row["request_id"],
        )

    def list_messages(
        self, owner: CustomerOwner, conversation_id: str,
        cursor: str | None, limit: int,
    ) -> Page:
        self.get_owned(owner, conversation_id)
        if not 1 <= limit <= 200:
            raise CustomerConversationError("customer_input_invalid", 422)
        boundary = ""
        params: list[Any] = [owner.tenant_id, owner.account_id, conversation_id]
        if cursor:
            created_at, message_id = self._decode_cursor(cursor)
            boundary = " AND (created_at>? OR (created_at=? AND message_id>?))"
            params.extend([created_at, created_at, message_id])
        params.append(limit + 1)
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM customer_message WHERE tenant_id=? AND account_id=?
                   AND conversation_id=?""" + boundary +
                " ORDER BY created_at,message_id LIMIT ?", params,
            ).fetchall()
        items = tuple(self._message(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = self._encode_cursor(last.created_at, last.message_id)
        return Page(items, next_cursor)

    def update(
        self, owner: CustomerOwner, conversation_id: str, *, title: str | None,
        status: str | None, expected_version: int,
    ) -> CustomerConversation:
        current = self.get_owned(owner, conversation_id)
        if status not in {None, "active", "archived"}:
            raise CustomerConversationError("customer_input_invalid", 422)
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE customer_conversation SET title=?,status=?,version=version+1,updated_at=?
                   WHERE tenant_id=? AND account_id=? AND conversation_id=? AND version=?""",
                (self._clean_title(title) if title is not None else current.title,
                 status or current.status, _now(), owner.tenant_id, owner.account_id,
                 conversation_id, expected_version),
            )
        if cursor.rowcount != 1:
            raise CustomerConversationError("customer_version_conflict", 409)
        return self.get_owned(owner, conversation_id)

    def soft_delete(
        self, owner: CustomerOwner, conversation_id: str,
        expected_version: int, request_id: str,
    ) -> DeletionJob:
        self.get_owned(owner, conversation_id)
        now, job_id = _now(), str(uuid.uuid4())
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE customer_conversation SET status='deleted',deleted_at=?,updated_at=?,version=version+1
                   WHERE tenant_id=? AND account_id=? AND conversation_id=? AND version=?""",
                (now, now, owner.tenant_id, owner.account_id, conversation_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise CustomerConversationError("customer_version_conflict", 409)
            self.connection.execute(
                """INSERT OR IGNORE INTO memory_deletion_job
                   (job_id,tenant_id,account_id,conversation_id,request_id,status,steps_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,'pending','[]',?,?)""",
                (job_id, owner.tenant_id, owner.account_id, conversation_id, request_id, now, now),
            )
            table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_working_memory'"
            ).fetchone()
            if table is not None:
                self.connection.execute(
                    """DELETE FROM customer_working_memory
                       WHERE tenant_id=? AND account_id=? AND conversation_id=?""",
                    (owner.tenant_id, owner.account_id, conversation_id),
                )
        return DeletionJob(job_id, request_id, "pending")
