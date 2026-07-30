"""Internal-only tool for audited, one-way customer-memory reads."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agent.memory_runtime.errors import MemoryAccessDenied
from agent.memory_runtime.models import ActorContext
from agent.memory_runtime.services.customer_reader import CustomerMemoryReader
from agent.tools.base import Tool


class ReadCustomerMemoryTool(Tool):
    allow_subagent_inheritance = False

    def __init__(self, reader: CustomerMemoryReader, actor: ActorContext):
        self.reader = reader
        self.actor = actor

    @property
    def name(self) -> str:
        return "read_customer_memory"

    @property
    def description(self) -> str:
        return (
            "Read minimized, consented customer memory for an approved internal purpose. "
            "Results are internal-only and are not authoritative quotation, inventory, "
            "approval, or transaction state."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "customer_account_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "purpose": {
                    "type": "string",
                    "enum": sorted(self.reader.policy.CUSTOMER_READ_PURPOSES),
                },
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "conversation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["customer_account_id", "purpose", "query"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            items = self.reader.search_for_workspace(
                self.actor,
                customer_account_id=str(kwargs.get("customer_account_id", ""))[:128],
                purpose=str(kwargs.get("purpose", ""))[:80],
                query=str(kwargs.get("query", ""))[:500],
                conversation_id=(
                    str(kwargs["conversation_id"])[:128]
                    if kwargs.get("conversation_id") else None
                ),
                top_k=min(3, self.reader.max_top_k),
            )
        except MemoryAccessDenied:
            return json.dumps({
                "status": "DENIED", "error_code": "memory_scope_denied",
                "classification": "internal_only", "items": [],
            })
        return json.dumps({
            "status": "ANSWERED" if items else "NO_EVIDENCE",
            "classification": "internal_only",
            "authority": "supporting_memory_only",
            "items": [asdict(item) for item in items],
        }, ensure_ascii=False)
