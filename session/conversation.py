"""Persistent conversation metadata and read-only legacy session indexing."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_WEB_SESSION = re.compile(r"^web_([0-9a-fA-F-]{36})\.jsonl$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Conversation:
    conversation_id: str
    owner_id: str
    title: str
    channel: str
    message_file: str
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    version: int = 1


class ConversationError(Exception):
    """Stable service error safe to expose through the HTTP API."""

    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ConversationService:
    """Single-process conversation index with atomic file replacement."""

    def __init__(self, sessions_dir: str | Path = "workspace/sessions", *, auto_index_legacy: bool = True) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.sessions_dir / "conversations.json"
        self._lock = threading.RLock()
        self._ensure_index(auto_index_legacy)

    def _ensure_index(self, auto_index_legacy: bool) -> None:
        with self._lock:
            if not self.index_path.exists():
                self._write([])
            if auto_index_legacy:
                self.index_legacy_web_sessions()

    def _read(self) -> list[Conversation]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            records = payload.get("conversations", [])
            return [Conversation(**item) for item in records]
        except (OSError, ValueError, TypeError):
            raise ConversationError("conversation_index_invalid", 500)

    def _write(self, records: list[Conversation]) -> None:
        payload = {"schema_version": 1, "conversations": [asdict(item) for item in records]}
        temporary = self.index_path.with_suffix(f".tmp-{uuid.uuid4().hex}")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.index_path)

    @staticmethod
    def _validate_id(conversation_id: str) -> str:
        try:
            return str(uuid.UUID(conversation_id))
        except (ValueError, TypeError, AttributeError):
            raise ConversationError("conversation_not_found", 404)

    def _find(self, records: list[Conversation], conversation_id: str) -> Conversation:
        normalized = self._validate_id(conversation_id)
        for item in records:
            if item.conversation_id == normalized and item.owner_id == "local":
                return item
        raise ConversationError("conversation_not_found", 404)

    def index_legacy_web_sessions(self, dry_run: bool = False) -> dict[str, int]:
        """Index recognizable legacy Web files without changing their contents."""
        with self._lock:
            records = self._read() if self.index_path.exists() else []
            known_files = {item.message_file for item in records}
            candidates: list[tuple[Path, str]] = []
            for path in self.sessions_dir.glob("web_*.jsonl"):
                match = _WEB_SESSION.match(path.name)
                if match and path.name not in known_files:
                    try:
                        conversation_id = str(uuid.UUID(match.group(1)))
                    except ValueError:
                        continue
                    candidates.append((path, conversation_id))
            if not dry_run and candidates:
                for path, conversation_id in candidates:
                    timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
                    records.append(Conversation(
                        conversation_id=conversation_id,
                        owner_id="local",
                        title="Imported conversation",
                        channel="web",
                        message_file=path.name,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ))
                self._write(records)
            return {"indexed": len(candidates), "existing": len(records) - (0 if dry_run else len(candidates))}

    def create(self, title: str = "New conversation") -> Conversation:
        with self._lock:
            records = self._read()
            conversation_id = str(uuid.uuid4())
            now = _now()
            item = Conversation(conversation_id, "local", self.clean_title(title), "web", f"web_local_{conversation_id}.jsonl", now, now)
            records.append(item)
            self._write(records)
            return item

    @staticmethod
    def clean_title(title: str) -> str:
        cleaned = _CONTROL.sub("", str(title)).strip()
        if not cleaned:
            cleaned = "New conversation"
        if len(cleaned) > 120:
            raise ConversationError("title_too_long", 422)
        return cleaned

    def list(self, *, include_deleted: bool = False, search: str = "", offset: int = 0, limit: int = 50) -> tuple[list[Conversation], int]:
        with self._lock:
            records = self._read()
        query = search.strip().casefold()
        records = [item for item in records if (include_deleted or item.deleted_at is None) and (not query or query in item.title.casefold())]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[offset:offset + limit], len(records)

    def get(self, conversation_id: str, *, include_deleted: bool = False) -> Conversation:
        with self._lock:
            item = self._find(self._read(), conversation_id)
        if item.deleted_at and not include_deleted:
            raise ConversationError("conversation_deleted", 404)
        return item

    def rename(self, conversation_id: str, title: str) -> Conversation:
        with self._lock:
            records = self._read()
            item = self._find(records, conversation_id)
            if item.deleted_at:
                raise ConversationError("conversation_deleted", 404)
            item.title = self.clean_title(title)
            item.updated_at = _now()
            self._write(records)
            return item

    def touch(self, conversation_id: str) -> None:
        with self._lock:
            records = self._read()
            item = self._find(records, conversation_id)
            if item.deleted_at:
                raise ConversationError("conversation_deleted", 404)
            item.updated_at = _now()
            self._write(records)

    def delete(self, conversation_id: str) -> Conversation:
        with self._lock:
            records = self._read()
            item = self._find(records, conversation_id)
            if item.deleted_at is None:
                item.deleted_at = _now()
                item.updated_at = item.deleted_at
                self._write(records)
            return item

    def restore(self, conversation_id: str) -> Conversation:
        with self._lock:
            records = self._read()
            item = self._find(records, conversation_id)
            if item.deleted_at is None:
                raise ConversationError("conversation_not_deleted", 409)
            item.deleted_at = None
            item.updated_at = _now()
            self._write(records)
            return item

    def messages(self, conversation_id: str, *, offset: int = 0, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        item = self.get(conversation_id)
        path = self.sessions_dir / item.message_file
        allowed: list[dict[str, Any]] = []
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        role, content = record.get("role"), record.get("content")
                        if role in {"user", "assistant"} and isinstance(content, str) and content:
                            allowed.append({"role": role, "content": content, "timestamp": record.get("timestamp")})
            except (OSError, UnicodeError):
                raise ConversationError("conversation_messages_unavailable", 500)
        return allowed[offset:offset + limit], len(allowed)

    def append_message(self, conversation_id: str, *, role: str, content: str) -> None:
        """Persist a task-runtime chat turn without routing it through the legacy Agent."""
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
            raise ConversationError("conversation_message_invalid", 422)
        with self._lock:
            records = self._read()
            item = self._find(records, conversation_id)
            if item.deleted_at:
                raise ConversationError("conversation_deleted", 404)
            path = self.sessions_dir / item.message_file
            record = {"role": role, "content": content, "timestamp": _now()}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            item.updated_at = record["timestamp"]
            self._write(records)

    def session_key(self, conversation_id: str) -> str:
        self.get(conversation_id)
        return f"web:local:{self._validate_id(conversation_id)}"
