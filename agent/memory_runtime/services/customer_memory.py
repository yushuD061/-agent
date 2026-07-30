"""Customer long-term memory governance: consent, candidates and corrections."""

from __future__ import annotations

import uuid
from dataclasses import replace

from ..models import ActorContext, MemoryConsent, MemoryItem, MemoryScope
from ..stores.sqlite import CustomerSQLiteMemoryStore, content_hash, utcnow
from ..errors import MemoryVersionConflict


class CustomerMemoryService:
    def __init__(self, store: CustomerSQLiteMemoryStore):
        self.store = store

    @staticmethod
    def actor(tenant_id: str, account_id: str) -> ActorContext:
        return ActorContext("customer", account_id, tenant_id, frozenset(), True)

    def grant_consent(
        self, actor: ActorContext, *, purpose: str, categories: tuple[str, ...],
        expires_at: str | None,
    ) -> MemoryConsent:
        now = utcnow()
        consent = MemoryConsent(
            str(uuid.uuid4()), actor.tenant_id, actor.actor_id, purpose,
            categories, "active", now, expires_at,
        )
        return self.store.grant_consent(actor, consent)

    def withdraw_consent(self, actor: ActorContext, consent_record_id: str) -> int:
        return self.store.withdraw_consent(actor, consent_record_id)

    def create_candidate(
        self, actor: ActorContext, *, content: str, summary: str,
        memory_type: str, purpose: str, source_refs: tuple[str, ...],
        conversation_id: str | None, confidence: float, importance: float,
        sensitivity: str = "customer_private", expires_at: str | None = None,
    ) -> MemoryItem:
        content, summary = content.strip(), summary.strip()
        if not content or not summary or not source_refs:
            raise ValueError("customer_memory_source_required")
        if sensitivity not in {"customer_private"}:
            raise ValueError("customer_memory_sensitivity_invalid")
        now = utcnow()
        scope = MemoryScope(
            "customer_conversation" if conversation_id else "customer_private",
            actor.tenant_id, account_id=actor.actor_id,
            conversation_id=conversation_id, purpose=purpose,
        )
        item = MemoryItem(
            str(uuid.uuid4()), scope, memory_type, content, summary, source_refs,
            "pending_consent", confidence, importance, sensitivity, None, 1, None,
            now, now, now, expires_at, content_hash(content), None,
        )
        return self.store.create_candidate(actor, item)

    def activate_candidate(
        self, actor: ActorContext, memory_id: str,
        consent_record_id: str, expected_version: int,
    ) -> MemoryItem:
        item = self.store.get_owned(actor, memory_id)
        consent = self.store.connection.execute(
            """SELECT categories_json,purpose FROM customer_memory_consent
               WHERE consent_record_id=? AND tenant_id=? AND account_id=?""",
            (consent_record_id, actor.tenant_id, actor.actor_id),
        ).fetchone()
        if consent is None or consent["purpose"] != item.scope.purpose:
            raise ValueError("customer_consent_invalid")
        import json
        categories = set(json.loads(consent["categories_json"]))
        if item.memory_type not in categories:
            raise ValueError("customer_consent_category_denied")
        return self.store.activate(actor, memory_id, consent_record_id, expected_version)

    def correct(
        self, actor: ActorContext, memory_id: str, *, content: str, summary: str,
        source_refs: tuple[str, ...], expected_version: int,
    ) -> MemoryItem:
        old = self.store.get_owned(actor, memory_id)
        if old.status != "active":
            raise MemoryVersionConflict()
        if not source_refs:
            raise ValueError("customer_memory_correction_invalid")
        now = utcnow()
        new_item = replace(
            old,
            memory_id=str(uuid.uuid4()),
            content=content.strip(), summary=summary.strip(), source_refs=source_refs,
            version=1, supersedes=old.memory_id, created_at=now, updated_at=now,
            valid_from=now, content_hash=content_hash(content), embedding_model=None,
        )
        new_item.validate()
        return self.store.supersede(actor, memory_id, new_item, expected_version)
