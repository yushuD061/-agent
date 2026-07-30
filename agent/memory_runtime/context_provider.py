"""Memory-to-model context provider contracts."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .models import PreparedMemory


@runtime_checkable
class MemoryContextProvider(Protocol):
    def build_context_messages(
        self, prepared: PreparedMemory
    ) -> list[dict[str, Any]]: ...


class NoOpMemoryContextProvider:
    """M0 provider that injects no structured memory."""

    def build_context_messages(
        self, prepared: PreparedMemory
    ) -> list[dict[str, Any]]:
        return []


class StructuredWorkingMemoryContextProvider:
    """Inject only scoped structured working state; never a whole long-term store."""

    def build_context_messages(self, prepared: PreparedMemory) -> list[dict[str, Any]]:
        if not prepared.working_memory and not prepared.recalled:
            return []
        import json
        messages = []
        if prepared.working_memory:
            messages.append({
            "role": "system",
            "content": (
                "Trusted scoped working memory. Values marked pending are not confirmed facts. "
                "Do not override source or confirmation state.\n"
                + json.dumps(prepared.working_memory, ensure_ascii=False, sort_keys=True)
            ),
            })
        if prepared.recalled:
            messages.append({
                "role": "system",
                "content": (
                    "Trusted scoped long-term memory. Treat it as supporting context, not as "
                    "authoritative inventory, quotation, approval, or transaction state.\n"
                    + json.dumps([
                        {
                            "memory_id": hit.item.memory_id,
                            "summary": hit.item.summary,
                            "source_refs": hit.item.source_refs,
                            "confidence": hit.item.confidence,
                        } for hit in prepared.recalled
                    ], ensure_ascii=False)
                ),
            })
        return messages
