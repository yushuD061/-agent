"""Explicit M6 MySQL conversation repositories.

These adapters are enabled only with ``NANOCLAW_CONVERSATION_BACKEND=mysql``.
The existing JSON/SQLite repositories remain the default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from agent.memory_runtime.models import DeletionJob
from session.conversation import Conversation, ConversationError, ConversationService
from session.customer_conversation import (
    CustomerConversation, CustomerConversationError, CustomerMessage,
    CustomerOwner, Page,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


class _MySQLBase:
    def __init__(self, connection_factory=None) -> None:
        if connection_factory is None:
            from agent.business.mysql_database import create_connection
            connection_factory = create_connection
        self._connection_factory = connection_factory

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class MySQLWorkspaceConversationService(_MySQLBase):
    """Multi-instance-safe metadata with the legacy JSONL message format."""

    def __init__(self, sessions_dir: str | Path = "workspace/sessions",
                 connection_factory=None) -> None:
        super().__init__(connection_factory)
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def clean_title(title: str) -> str:
        return ConversationService.clean_title(title)

    @staticmethod
    def _item(row: dict[str, Any]) -> Conversation:
        return Conversation(
            row["conversation_id"], row["owner_id"], row["title"], row["channel"],
            row.get("message_file") or "", _iso(row["created_at"]) or "",
            _iso(row["updated_at"]) or "", _iso(row.get("deleted_at")), int(row["version"]),
        )

    def create(self, title: str = "New conversation") -> Conversation:
        conversation_id, now = str(uuid.uuid4()), _now()
        filename = f"web_local_{conversation_id}.jsonl"
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO runtime_conversation
                   (conversation_id,owner_id,channel,title,status,message_file,created_at,updated_at)
                   VALUES (%s,'local','web',%s,'active',%s,%s,%s)""",
                (conversation_id, self.clean_title(title), filename, now, now))
        return self.get(conversation_id)

    def list(self, *, include_deleted: bool = False, search: str = "", offset: int = 0,
             limit: int = 50) -> tuple[list[Conversation], int]:
        clauses = ["tenant_id=''", "account_id=''", "owner_id='local'", "channel='web'"]
        params: list[Any] = []
        if not include_deleted:
            clauses.append("status!='deleted'")
        if search.strip():
            clauses.append("title LIKE %s")
            params.append(f"%{search.strip()}%")
        where = " AND ".join(clauses)
        with self._tx() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM runtime_conversation WHERE {where}", params)
            total = int(cur.fetchone()["total"])
            cur.execute(f"SELECT * FROM runtime_conversation WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                        params + [limit, offset])
            rows = cur.fetchall()
        return [self._item(row) for row in rows], total

    def get(self, conversation_id: str, *, include_deleted: bool = False) -> Conversation:
        try:
            normalized = str(uuid.UUID(conversation_id))
        except (ValueError, TypeError):
            raise ConversationError("conversation_not_found", 404) from None
        with self._tx() as cur:
            cur.execute("""SELECT * FROM runtime_conversation WHERE conversation_id=%s
                           AND tenant_id='' AND account_id='' AND owner_id='local' AND channel='web'""",
                        (normalized,))
            row = cur.fetchone()
        if row is None or (row["status"] == "deleted" and not include_deleted):
            raise ConversationError("conversation_not_found", 404)
        return self._item(row)

    def _update(self, conversation_id: str, **changes: Any) -> Conversation:
        self.get(conversation_id, include_deleted=True)
        assignments, params = [], []
        for key, value in changes.items():
            assignments.append(f"{key}=%s")
            params.append(value)
        assignments.extend(("updated_at=%s", "version=version+1")); params.append(_now())
        with self._tx() as cur:
            cur.execute(f"UPDATE runtime_conversation SET {','.join(assignments)} WHERE conversation_id=%s",
                        params + [conversation_id])
        return self.get(conversation_id, include_deleted=True)

    def rename(self, conversation_id: str, title: str) -> Conversation:
        item = self.get(conversation_id)
        del item
        return self._update(conversation_id, title=self.clean_title(title))

    def touch(self, conversation_id: str) -> None:
        self.get(conversation_id)
        with self._tx() as cur:
            cur.execute("UPDATE runtime_conversation SET updated_at=%s WHERE conversation_id=%s",
                        (_now(), conversation_id))

    def delete(self, conversation_id: str) -> Conversation:
        return self._update(conversation_id, status="deleted", deleted_at=_now())

    def restore(self, conversation_id: str) -> Conversation:
        item = self.get(conversation_id, include_deleted=True)
        if item.deleted_at is None:
            raise ConversationError("conversation_not_deleted", 409)
        return self._update(conversation_id, status="active", deleted_at=None)

    def messages(self, conversation_id: str, *, offset: int = 0,
                 limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        item = self.get(conversation_id)
        path = (self.sessions_dir / item.message_file).resolve()
        if self.sessions_dir.resolve() not in path.parents:
            raise ConversationError("conversation_messages_unavailable", 500)
        messages: list[dict[str, Any]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("role") in {"user", "assistant"} and isinstance(row.get("content"), str):
                    messages.append({key: row.get(key) for key in ("role", "content", "timestamp")})
        return messages[offset:offset + limit], len(messages)

    def session_key(self, conversation_id: str) -> str:
        return f"web:local:{self.get(conversation_id).conversation_id}"


class MySQLCustomerConversationRepository(_MySQLBase):
    def __init__(self, *, cursor_secret: bytes, connection_factory=None) -> None:
        super().__init__(connection_factory)
        self._secret = cursor_secret

    @staticmethod
    def _clean_title(title: str) -> str:
        cleaned = "".join(ch for ch in str(title).strip() if ch >= " ")
        if not cleaned or len(cleaned) > 120:
            raise CustomerConversationError("customer_input_invalid", 422)
        return cleaned

    @staticmethod
    def _conversation(row: dict[str, Any]) -> CustomerConversation:
        return CustomerConversation(
            row["conversation_id"], row["tenant_id"], row["account_id"], row["title"],
            row["status"], _iso(row["created_at"]) or "", _iso(row["updated_at"]) or "",
            _iso(row.get("last_message_at")), int(row["version"]),
        )

    def _encode_cursor(self, timestamp: str, item_id: str) -> str:
        payload = json.dumps([timestamp, item_id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload + hmac.new(self._secret, payload, hashlib.sha256).digest()).decode().rstrip("=")

    def _decode_cursor(self, cursor: str) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.new(self._secret, payload, hashlib.sha256).digest()):
                raise ValueError
            first, second = json.loads(payload)
            return str(first), str(second)
        except Exception:
            raise CustomerConversationError("customer_cursor_invalid", 400) from None

    def create(self, owner: CustomerOwner, title: str) -> CustomerConversation:
        conversation_id, now = str(uuid.uuid4()), _now()
        with self._tx() as cur:
            cur.execute("""INSERT INTO runtime_conversation
                (conversation_id,tenant_id,account_id,owner_id,channel,title,status,created_at,updated_at)
                VALUES (%s,%s,%s,%s,'customer_portal',%s,'active',%s,%s)""",
                (conversation_id, owner.tenant_id, owner.account_id, owner.account_id,
                 self._clean_title(title), now, now))
        return self.get_owned(owner, conversation_id)

    def get_owned(self, owner: CustomerOwner, conversation_id: str) -> CustomerConversation:
        with self._tx() as cur:
            cur.execute("""SELECT * FROM runtime_conversation WHERE tenant_id=%s AND account_id=%s
                           AND conversation_id=%s AND channel='customer_portal' AND status!='deleted'""",
                        (owner.tenant_id, owner.account_id, conversation_id))
            row = cur.fetchone()
        if row is None:
            raise CustomerConversationError("customer_resource_not_found", 404)
        return self._conversation(row)

    def list_owned(self, owner: CustomerOwner, cursor: str | None, limit: int) -> Page:
        if not 1 <= limit <= 100:
            raise CustomerConversationError("customer_input_invalid", 422)
        boundary, params = "", [owner.tenant_id, owner.account_id]
        if cursor:
            updated_at, conversation_id = self._decode_cursor(cursor)
            boundary = " AND (updated_at<%s OR (updated_at=%s AND conversation_id<%s))"
            params.extend((updated_at, updated_at, conversation_id))
        with self._tx() as cur:
            cur.execute("""SELECT * FROM runtime_conversation WHERE tenant_id=%s AND account_id=%s
                AND channel='customer_portal' AND status!='deleted'""" + boundary +
                " ORDER BY updated_at DESC,conversation_id DESC LIMIT %s", params + [limit + 1])
            rows = cur.fetchall()
        items = tuple(self._conversation(row) for row in rows[:limit])
        next_cursor = self._encode_cursor(items[-1].updated_at, items[-1].conversation_id) if len(rows) > limit and items else None
        return Page(items, next_cursor)

    def append_message(self, owner: CustomerOwner, conversation_id: str, *, role: str,
                       content: Any, request_id: str | None = None) -> CustomerMessage:
        if role not in {"user", "assistant", "tool"}:
            raise CustomerConversationError("customer_input_invalid", 422)
        self.get_owned(owner, conversation_id)
        message_id, now = str(uuid.uuid4()), _now()
        with self._tx() as cur:
            cur.execute("""INSERT INTO runtime_message
                (message_id,tenant_id,account_id,conversation_id,role,content_json,request_id,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE message_id=message_id""",
                (message_id, owner.tenant_id, owner.account_id, conversation_id, role,
                 json.dumps(content, ensure_ascii=False), request_id, now))
            if cur.rowcount != 1:
                cur.execute("""SELECT * FROM runtime_message WHERE tenant_id=%s AND account_id=%s
                    AND conversation_id=%s AND request_id=%s AND role=%s""",
                    (owner.tenant_id, owner.account_id, conversation_id, request_id, role))
                row = cur.fetchone()
                return CustomerMessage(row["message_id"], row["role"], json.loads(row["content_json"]),
                                       _iso(row["created_at"]) or "", row.get("request_id"))
            cur.execute("UPDATE runtime_conversation SET last_message_at=%s,updated_at=%s WHERE conversation_id=%s",
                        (now, now, conversation_id))
        return CustomerMessage(message_id, role, content, _iso(now) or "", request_id)

    def list_messages(self, owner: CustomerOwner, conversation_id: str,
                      cursor: str | None, limit: int) -> Page:
        self.get_owned(owner, conversation_id)
        if not 1 <= limit <= 200:
            raise CustomerConversationError("customer_input_invalid", 422)
        boundary, params = "", [owner.tenant_id, owner.account_id, conversation_id]
        if cursor:
            created_at, message_id = self._decode_cursor(cursor)
            boundary = " AND (created_at>%s OR (created_at=%s AND message_id>%s))"
            params.extend((created_at, created_at, message_id))
        with self._tx() as cur:
            cur.execute("""SELECT * FROM runtime_message WHERE tenant_id=%s AND account_id=%s
                AND conversation_id=%s""" + boundary + " ORDER BY created_at,message_id LIMIT %s",
                params + [limit + 1])
            rows = cur.fetchall()
        items = tuple(CustomerMessage(row["message_id"], row["role"], json.loads(row["content_json"]),
                                      _iso(row["created_at"]) or "", row.get("request_id")) for row in rows[:limit])
        next_cursor = self._encode_cursor(items[-1].created_at, items[-1].message_id) if len(rows) > limit and items else None
        return Page(items, next_cursor)

    def update(self, owner: CustomerOwner, conversation_id: str, *, title: str | None,
               status: str | None, expected_version: int) -> CustomerConversation:
        current = self.get_owned(owner, conversation_id)
        if status not in {None, "active", "archived"}:
            raise CustomerConversationError("customer_input_invalid", 422)
        with self._tx() as cur:
            cur.execute("""UPDATE runtime_conversation SET title=%s,status=%s,version=version+1,updated_at=%s
                WHERE tenant_id=%s AND account_id=%s AND conversation_id=%s AND version=%s""",
                (self._clean_title(title) if title is not None else current.title, status or current.status,
                 _now(), owner.tenant_id, owner.account_id, conversation_id, expected_version))
            if cur.rowcount != 1:
                raise CustomerConversationError("customer_version_conflict", 409)
        return self.get_owned(owner, conversation_id)

    def soft_delete(self, owner: CustomerOwner, conversation_id: str,
                    expected_version: int, request_id: str) -> DeletionJob:
        self.get_owned(owner, conversation_id)
        job_id, now = str(uuid.uuid4()), _now()
        with self._tx() as cur:
            cur.execute("""UPDATE runtime_conversation SET status='deleted',deleted_at=%s,
                updated_at=%s,version=version+1 WHERE tenant_id=%s AND account_id=%s
                AND conversation_id=%s AND version=%s""",
                (now, now, owner.tenant_id, owner.account_id, conversation_id, expected_version))
            if cur.rowcount != 1:
                raise CustomerConversationError("customer_version_conflict", 409)
            cur.execute("""INSERT IGNORE INTO runtime_deletion_job
                (job_id,tenant_id,account_id,conversation_id,request_id,status)
                VALUES (%s,%s,%s,%s,%s,'pending')""",
                (job_id, owner.tenant_id, owner.account_id, conversation_id, request_id))
            if cur.rowcount != 1:
                cur.execute("SELECT job_id,status FROM runtime_deletion_job WHERE tenant_id=%s AND account_id=%s AND request_id=%s",
                            (owner.tenant_id, owner.account_id, request_id))
                row = cur.fetchone(); job_id = row["job_id"]
        return DeletionJob(job_id, request_id, "pending")


def conversation_backend() -> str:
    value = os.environ.get("NANOCLAW_CONVERSATION_BACKEND", "local").strip().lower()
    if value not in {"local", "mysql"}:
        raise ValueError("NANOCLAW_CONVERSATION_BACKEND must be local or mysql")
    return value
