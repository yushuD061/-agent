"""Authoritative memory store interface; indexes remain derived data."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import (
    ActorContext,
    DeletionJob,
    MemoryExport,
    MemoryHit,
    MemoryItem,
    MemoryScope,
)


@runtime_checkable
class MemoryStore(Protocol):
    def create_candidate(self, actor: ActorContext, item: MemoryItem) -> MemoryItem: ...
    def activate(
        self,
        actor: ActorContext,
        memory_id: str,
        consent_record_id: str,
        expected_version: int,
    ) -> MemoryItem: ...
    def search(
        self,
        actor: ActorContext,
        scope: MemoryScope,
        query: str,
        top_k: int,
    ) -> list[MemoryHit]: ...
    def supersede(
        self,
        actor: ActorContext,
        old_id: str,
        new_item: MemoryItem,
        expected_version: int,
    ) -> MemoryItem: ...
    def invalidate(
        self,
        actor: ActorContext,
        memory_id: str,
        reason: str,
        expected_version: int,
    ) -> None: ...
    def delete_scope(
        self,
        actor: ActorContext,
        scope: MemoryScope,
        request_id: str,
    ) -> DeletionJob: ...
    def export_scope(
        self, actor: ActorContext, scope: MemoryScope
    ) -> MemoryExport: ...

