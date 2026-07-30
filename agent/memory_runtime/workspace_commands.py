"""Deterministic slash-command surface for workspace memory confirmation."""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict

from .errors import MemoryAccessDenied, MemoryVersionConflict


class WorkspaceMemoryCommandRouter:
    def __init__(self, service, actor, scope, *, review_service=None):
        self.service, self.actor, self.scope = service, actor, scope
        self.review_service = review_service

    async def execute(self, command: str) -> str:
        try:
            parts = shlex.split(command)
        except ValueError:
            return self._usage("memory_command_invalid")
        if not parts or parts[0] != "/memory":
            return self._usage("memory_command_invalid")
        action = parts[1] if len(parts) > 1 else "candidates"
        try:
            if action == "candidates" and len(parts) == 2:
                items = self.service.store.list_scope(
                    self.actor, self.scope, status="pending_confirmation", limit=20,
                )
                return json.dumps({
                    "status": "ok", "items": [self._public(item) for item in items],
                }, ensure_ascii=False)
            if action in {"confirm", "reject", "forget"} and len(parts) in {5, 6}:
                memory_id, version, expected_hash = parts[2], int(parts[3]), parts[4]
                if action == "confirm":
                    item = self.service.confirm(
                        self.actor, memory_id, version=version, expected_hash=expected_hash,
                        scope=self.scope,
                    )
                    return json.dumps({"status": "active", "item": self._public(item)},
                                      ensure_ascii=False)
                if action == "reject":
                    self.service.reject(
                        self.actor, memory_id, version=version, expected_hash=expected_hash,
                        reason=parts[5] if len(parts) == 6 else "operator_rejected",
                        scope=self.scope,
                    )
                    return json.dumps({"status": "rejected", "memory_id": memory_id})
                current = self.service.get(self.actor, memory_id, scope=self.scope)
                if current.content_hash != expected_hash:
                    raise MemoryVersionConflict()
                self.service.store.delete_owned(self.actor, memory_id, version)
                return json.dumps({"status": "deleted", "memory_id": memory_id})
            if action == "review" and len(parts) == 2 and self.review_service is not None:
                count = await self.review_service.run_once(self.actor, self.scope)
                return json.dumps({"status": "review_completed", "suggestions": count})
        except (ValueError, MemoryVersionConflict):
            return json.dumps({"status": "conflict", "error_code": "memory_version_or_hash_conflict"})
        except MemoryAccessDenied:
            return json.dumps({"status": "denied", "error_code": "memory_scope_denied"})
        return self._usage("memory_command_invalid")

    @staticmethod
    def _public(item):
        payload = asdict(item)
        payload.pop("content", None)
        payload.pop("source_refs", None)
        return payload

    @staticmethod
    def _usage(code: str) -> str:
        return json.dumps({
            "status": "invalid", "error_code": code,
            "usage": [
                "/memory candidates",
                "/memory confirm <id> <version> <hash>",
                "/memory reject <id> <version> <hash> [reason]",
                "/memory forget <id> <version> <hash>",
                "/memory review",
            ],
        }, ensure_ascii=False)
