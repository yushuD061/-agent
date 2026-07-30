"""Crash-safe bounded active JSONL history with cold raw-message archive."""

from __future__ import annotations

import json
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.memory_runtime.compaction import SummaryFunction, plan_complete_turns
from privacy import sanitize_data
from session.manager import SessionManager


class BoundedSessionManager(SessionManager):
    def __init__(self, sessions_dir: str, *, max_turns: int, summarizer: SummaryFunction):
        super().__init__(sessions_dir)
        self.max_turns = max_turns
        self.summarizer = summarizer
        self.archive_dir = Path(sessions_dir) / "archive"
        self.audit_dir = Path(sessions_dir) / "compaction_audit"

    async def prepare_active_history(self, session_key: str) -> list[dict[str, Any]]:
        original = self.get_history(session_key)
        replaced = False
        try:
            plan = plan_complete_turns(original, self.max_turns)
            if not plan.needed:
                return original
            summary = await self.summarizer(list(plan.evicted))
            if not summary.strip():
                return original
            compaction_id = str(uuid.uuid4())
            self._append_archive(session_key, compaction_id, list(plan.evicted))
            active = [{
                "role": "system",
                "content": f"[Bounded history summary]\n{summary.strip()}",
                "memory_compaction_id": compaction_id,
            }, *plan.retained]
            self.replace_history(session_key, active)
            replaced = True
            self._append_audit(session_key, compaction_id, len(plan.evicted), len(plan.retained))
            return active
        except Exception:
            if replaced:
                try:
                    self._atomic_write(session_key, original)
                except Exception:
                    pass
            return original

    def replace_history(self, session_key: str, messages: list[dict[str, Any]]) -> None:
        self._atomic_write(session_key, messages)

    def _atomic_write(self, session_key: str, messages: list[dict[str, Any]]) -> None:
        target = Path(self._get_session_path(session_key))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".tmp-{uuid.uuid4().hex}")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                for message in messages:
                    record = sanitize_data(dict(message))
                    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _append_archive(
        self, session_key: str, compaction_id: str, messages: list[dict[str, Any]]
    ) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        target = self.archive_dir / (Path(self._get_session_path(session_key)).stem + ".jsonl")
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for sequence, message in enumerate(messages):
                record = {
                    "compaction_id": compaction_id,
                    "sequence": sequence,
                    "message": sanitize_data(message),
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _append_audit(
        self, session_key: str, compaction_id: str, evicted: int, retained: int
    ) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        target = self.audit_dir / "events.jsonl"
        record = {
            "compaction_id": compaction_id,
            "session_key_hash": hashlib.sha256(session_key.encode()).hexdigest(),
            "evicted_count": evicted,
            "retained_count": retained,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
