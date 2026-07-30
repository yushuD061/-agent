"""Explicit working-memory updates; no model output becomes confirmed implicitly."""

from __future__ import annotations

from dataclasses import replace

from ..models import CustomerInquiryWorkingMemory, SourcedValue
from ..working_memory import CustomerWorkingMemoryStore


class CustomerWorkingMemoryService:
    def __init__(self, store: CustomerWorkingMemoryStore):
        self.store = store

    def set_sourced_value(
        self, *, tenant_id: str, state: CustomerInquiryWorkingMemory,
        field_name: str, value, source_message_id: str, updated_at: str,
        customer_confirmed: bool = False, authoritative: bool = False,
    ) -> CustomerInquiryWorkingMemory:
        next_fields = dict(state.fields)
        next_fields[field_name] = SourcedValue(
            value=value,
            state="confirmed" if customer_confirmed or authoritative else "pending",
            source_message_id=source_message_id,
            updated_at=updated_at,
        )
        next_state = replace(state, fields=next_fields)
        return self.store.put(tenant_id, next_state, expected_version=state.version)
