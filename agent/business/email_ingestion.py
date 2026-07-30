"""Deterministic orchestration for email persistence and RFQ extraction."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from channels.email.contracts import EmailEnvelope, EmailSource

from .email_repository import EmailRepository
from .email_notification import build_qq_extraction_failure_notice, build_qq_rfq_summary
from .rfq_extractor import (
    DETERMINISTIC_RULE_VERSION,
    deterministic_extract_rfq_fields,
    extract_rfq_fields,
    extract_rfq_fields_compact,
)
from .email_trade_classifier import TradeEmailClassification


class EmailIngestionService:
    def __init__(self, repository: EmailRepository,
                 extractor: Callable[[str, dict], Awaitable[dict]] = extract_rfq_fields,
                 worker_id: str = "email-worker", qq_target_id: str = "", qq_target_type: str = "c2c",
                 extraction_timeout_seconds: int = 60,
                 message_filter: Callable[[EmailEnvelope], TradeEmailClassification] | None = None,
                 extractor_mode: str = "llm_full", extractor_version: str = "configured-model"):
        self.repository = repository
        self.extractor = extractor
        self.worker_id = worker_id
        self.qq_target_id = qq_target_id
        self.qq_target_type = qq_target_type
        self.extraction_timeout_seconds = extraction_timeout_seconds
        self.message_filter = message_filter
        self.extractor_mode = extractor_mode
        self.extractor_version = extractor_version

    async def ingest(self, envelope: EmailEnvelope) -> dict:
        classification_code = None
        if self.message_filter is not None:
            classification = self.message_filter(envelope)
            if not classification.accepted:
                if classification.code in {
                    "email_trade_obvious_non_rfq", "email_trade_self_sent",
                    "email_trade_sender_not_allowed",
                }:
                    email_id = self.repository.record_skipped(envelope, classification.code)
                    return {"email_id": email_id, "created": True, "status": "ignored_non_trade",
                            "classification_code": classification.code}
                email_id, created = self.repository.persist(
                    envelope, classification_code=classification.code, status="received"
                )
                self.repository.advance_cursor(
                    envelope.account_id, envelope.folder, envelope.uidvalidity, envelope.uid
                )
                return {"email_id": email_id, "created": created, "status": "received",
                        "classification_code": classification.code}
            classification_code = classification.code
        email_id, created = self.repository.persist(
            envelope, classification_code=classification_code
        )
        # Cursor advances after durable persistence, whether this was a safe duplicate or a new row.
        self.repository.advance_cursor(envelope.account_id, envelope.folder, envelope.uidvalidity, envelope.uid)
        if not created:
            return {"email_id": email_id, "created": False, "status": self.repository.get(email_id)["status"]}
        return await self.process_stored(email_id, created=True)

    async def process_stored(self, email_id: int, *, created: bool = False) -> dict:
        if not self.repository.acquire(email_id, self.worker_id):
            return {"email_id": email_id, "created": True, "status": "persisted"}
        row = self.repository.get(email_id)
        try:
            context = {"subject": row["subject"], "from_address": row["from_address"], "source": "email"}
            extraction_mode = self.extractor_mode
            extractor_version = self.extractor_version
            try:
                result = await asyncio.wait_for(
                    self.extractor(row["text_body"], context), timeout=self.extraction_timeout_seconds)
            except Exception:
                if self.extractor is not extract_rfq_fields:
                    raise
                extraction_mode = "llm_compact"
                try:
                    result = await asyncio.wait_for(
                        extract_rfq_fields_compact(row["text_body"], context),
                        timeout=self.extraction_timeout_seconds)
                except Exception:
                    extraction_mode = "deterministic_fallback"
                    extractor_version = DETERMINISTIC_RULE_VERSION
                    result = deterministic_extract_rfq_fields(row["text_body"], context)
            if self.qq_target_id:
                self.repository.complete_extraction_with_notification(
                    email_id, result, target_id=self.qq_target_id, target_type=self.qq_target_type,
                    content=build_qq_rfq_summary(email_id, result), extraction_mode=extraction_mode,
                    extractor_version=extractor_version)
            else:
                self.repository.complete_extraction(email_id, result, extraction_mode=extraction_mode,
                                                    extractor_version=extractor_version)
            return {"email_id": email_id, "created": created, "status": "needs_review",
                    "extraction_mode": extraction_mode}
        except Exception as exc:
            self.repository.fail(email_id, type(exc).__name__)
            status = self.repository.get(email_id)["status"]
            if self.qq_target_id:
                self.repository.enqueue_notification(
                    email_id, target_id=self.qq_target_id, target_type=self.qq_target_type,
                    content=build_qq_extraction_failure_notice(email_id, type(exc).__name__))
            return {"email_id": email_id, "created": created, "status": status,
                    "error_code": type(exc).__name__}

    async def recover_pending(self, limit: int = 20) -> list[dict]:
        results = []
        required_classification = "email_trade_rfq_accepted" if self.message_filter else None
        for email_id in self.repository.recoverable_extractions(
                limit, classification_code=required_classification):
            results.append(await self.process_stored(email_id))
        return results

    async def poll_once(self, source: EmailSource, account_id: str, folder: str, limit: int = 50) -> list[dict]:
        _, last_uid = self.repository.get_cursor(account_id, folder)
        envelopes = await asyncio.to_thread(source.fetch_after, last_uid, limit)
        results = []
        for envelope in envelopes:
            results.append(await self.ingest(envelope))
        results.extend(await self.recover_pending(limit))
        return results
