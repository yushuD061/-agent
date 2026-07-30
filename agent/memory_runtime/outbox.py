"""Idempotent derived-index worker; authoritative memory remains the source of truth."""

from __future__ import annotations


class MemoryIndexWorker:
    def __init__(self, authoritative_store, keyword_index, vector_index):
        self.authoritative_store = authoritative_store
        self.keyword_index = keyword_index
        self.vector_index = vector_index

    def run_once(self) -> bool:
        event = self.authoritative_store.claim_index_event()
        if event is None:
            return False
        try:
            item = self.authoritative_store.get_index_item(event["aggregate_id"])
            if event["event_type"] == "delete" or item is None:
                self.keyword_index.delete(event["aggregate_id"])
                self.vector_index.delete(event["aggregate_id"])
            else:
                # Keyword success followed by vector failure is safe: outbox retries are idempotent.
                self.keyword_index.upsert(item)
                embedding = getattr(self.vector_index, "embedding", None)
                if (getattr(embedding, "external", False)
                        and item.sensitivity == "restricted"):
                    raise RuntimeError("workspace_memory_restricted_external_transfer_denied")
                self.vector_index.upsert(item)
            self.authoritative_store.complete_index_event(event["event_id"])
        except Exception as exc:
            self.authoritative_store.retry_index_event(
                event["event_id"], type(exc).__name__,
            )
        return True

    def drain(self, max_events: int = 1000) -> int:
        processed = 0
        while processed < max_events and self.run_once():
            processed += 1
        return processed
