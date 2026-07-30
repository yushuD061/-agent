"""M3 deterministic human review, immutable confirmation, and quote gate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
import os
from datetime import datetime

from agent.business.email_repository import EmailRepository, EmailReviewRepositoryError
from channels.email.review_contracts import (
    EmailReviewErrorCode,
    OPERATOR_REASON_CODES,
    ReviewSourceType,
    parse_review_path,
    review_etag,
)


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_UNIT = re.compile(r"^[A-Za-z][A-Za-z0-9 ._/-]{0,31}$")
_INCOTERMS = {"EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF"}


class EmailReviewServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if separator and domain else "***"


class EmailReviewService:
    def __init__(self, repository: EmailRepository, reviewer_id: str = ""):
        self.repository = repository
        self.reviewer_id = reviewer_id.strip()

    @staticmethod
    def _parse_extraction(row: dict) -> dict:
        try:
            value = json.loads(row.get("extraction_json") or "{}")
        except json.JSONDecodeError as exc:
            raise EmailReviewServiceError(EmailReviewErrorCode.VALIDATION_FAILED.value, 422) from exc
        if not isinstance(value, dict):
            raise EmailReviewServiceError(EmailReviewErrorCode.VALIDATION_FAILED.value, 422)
        return value

    def list_reviews(self, *, status: str = "needs_review", account_id: str | None = None,
                     cursor: str | None = None, limit: int = 20) -> dict:
        try:
            rows, next_cursor = self.repository.list_inbound_reviews(
                status=status, account_id=account_id, cursor=cursor, limit=limit
            )
        except EmailReviewRepositoryError as exc:
            raise self._repository_error(exc) from exc
        items = []
        for row in rows:
            extraction = self._parse_extraction(row)
            agent_check = self._agent_check(
                extraction, email_status=row["status"],
                extraction_mode=row.get("extraction_mode"),
                extractor_version=row.get("extractor_version"),
            )
            try:
                envelope = json.loads(row.get("envelope_json") or "{}")
            except json.JSONDecodeError:
                envelope = {}
            items.append({
                "email_id": int(row["email_id"]),
                "account_id": row["account_id"],
                "provider": row["provider"],
                "subject_preview": str(row.get("subject") or "")[:160],
                "sender_masked": _mask_email(str(row.get("from_address") or "")),
                "status": row["status"],
                "missing_count": len(extraction.get("missing_fields") or []),
                "item_count": len(extraction.get("items") or []),
                "extraction_mode": row.get("extraction_mode"),
                "extractor_version": row.get("extractor_version"),
                "review_version": int(row.get("review_version") or 0),
                "classification_code": row.get("ingestion_classification_code"),
                "agent_check": agent_check,
                "received_at": envelope.get("received_at") or row["created_at"],
            })
        return {"items": items, "next_cursor": next_cursor}

    def detail(self, email_id: int) -> tuple[dict, str]:
        row = self.repository.get_inbound_review_detail(email_id)
        if row is None:
            raise EmailReviewServiceError(EmailReviewErrorCode.INBOUND_NOT_FOUND.value, 404)
        extraction = self._parse_extraction(row)
        agent_check = self._agent_check(
            extraction, email_status=row["status"],
            extraction_mode=row.get("extraction_mode"),
            extractor_version=row.get("extractor_version"),
        )
        payload = {
            "email_id": int(row["email_id"]), "account_id": row["account_id"],
            "provider": row["provider"], "status": row["status"],
            "from_address": row["from_address"], "from_name": row["from_name"],
            "subject": row["subject"], "text_body": row["text_body"],
            "attachments": row["attachments"], "extraction": extraction,
            "agent_check": agent_check,
            "extraction_mode": row.get("extraction_mode"),
            "extractor_version": row.get("extractor_version"),
            "review_version": int(row.get("review_version") or 0),
            "confirmed_review_id": row.get("confirmed_review_id"),
            "confirmed_review_hash": row.get("confirmed_review_hash"),
            "confirmed_at": row.get("confirmed_at"),
            "classification_code": row.get("ingestion_classification_code"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        return payload, review_etag(email_id, payload["review_version"])

    @staticmethod
    def _agent_check(extraction: dict, *, email_status: str,
                     extraction_mode: str | None, extractor_version: str | None) -> dict:
        """Summarize NanoClaw's RFQ field check without inventing missing values."""
        nodes = []
        customer = extraction.get("customer") if isinstance(extraction, dict) else None
        if isinstance(customer, dict):
            nodes.extend(customer.get(name) for name in ("name", "company", "email"))
        nodes.extend(extraction.get(name) for name in ("country", "delivery_deadline", "trade_term"))
        for item in extraction.get("items") or []:
            if isinstance(item, dict):
                nodes.extend(item.get(name) for name in ("product", "specification", "quantity"))
        nodes = [node for node in nodes if isinstance(node, dict)]
        pending_fields = list(extraction.get("missing_fields") or [])
        if email_status == "confirmed":
            status = "business_confirmed"
        elif email_status == "needs_review" and pending_fields:
            status = "business_input_required"
        elif email_status == "needs_review":
            status = "ready_for_business_confirmation"
        else:
            status = "not_applicable"
        return {
            "checker": "nanoclaw",
            "status": status,
            "checked_field_count": sum(
                1 for node in nodes if node.get("status") != "pending_confirmation"
            ),
            "total_field_count": len(nodes),
            "pending_fields": pending_fields,
            "warning_count": len(extraction.get("warnings") or []),
            "extraction_mode": extraction_mode,
            "checker_version": extractor_version,
        }

    @staticmethod
    def _node(snapshot: dict, path: str) -> tuple[dict, str]:
        index, field = parse_review_path(path)
        if index is not None:
            items = snapshot.get("items")
            if not isinstance(items, list) or index >= len(items) or len(items) > 50:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
            if field.startswith("quantity."):
                return items[index]["quantity"], field.split(".", 1)[1]
            return items[index][field], "value"
        if path.startswith("customer."):
            field = path.split(".", 1)[1]
            return snapshot["customer"][field], "value"
        if path == "country":
            return snapshot["country"], "value"
        group, key = path.split(".", 1)
        return snapshot[group], key

    @staticmethod
    def _validate_value(path: str, value) -> object:
        if path.endswith("quantity.value"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
            return value
        if not isinstance(value, str):
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        value = value.strip()
        limits = {"customer.name": 120, "customer.company": 200, "customer.email": 254,
                  "country": 100, "delivery_deadline.raw": 200,
                  "delivery_deadline.normalized": 32, "trade_term.named_place": 160,
                  "trade_term.version": 32}
        limit = limits.get(path, 500)
        if not value or len(value) > limit or "\x00" in value:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        if path == "customer.email" and not _EMAIL.fullmatch(value):
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        if path.endswith("quantity.unit") and not _UNIT.fullmatch(value):
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        if path == "trade_term.incoterm":
            value = value.upper()
            if value not in _INCOTERMS:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        if path == "delivery_deadline.normalized":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400) from exc
        return value

    @staticmethod
    def _source(change: dict, *, path: str, value, detail: dict) -> tuple[str, str, dict]:
        try:
            source_type = ReviewSourceType(str(change.get("source_type", "")))
        except ValueError as exc:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_EVIDENCE.value, 400) from exc
        meta = {"path": path, "source_type": source_type.value, "value_hash": _sha(value)}
        if source_type == ReviewSourceType.EMAIL_EVIDENCE:
            part = change.get("source_part")
            source = detail["subject"] if part == "subject" else detail["text_body"] if part == "body" else None
            try:
                start, end = int(change.get("start")), int(change.get("end"))
            except (TypeError, ValueError) as exc:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_EVIDENCE.value, 400) from exc
            quote = change.get("quote")
            if source is None or not isinstance(quote, str) or start < 0 or end <= start or end > len(source):
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_EVIDENCE.value, 400)
            if source[start:end] != quote or not quote or len(quote) > 500:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_EVIDENCE.value, 400)
            meta.update({"source_part": part, "start": start, "end": end, "quote_hash": _sha(quote)})
            return "extracted", quote, meta
        if source_type == ReviewSourceType.HEADER_EVIDENCE:
            if path != "customer.email" or str(value).lower() != str(detail["from_address"]).lower():
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_EVIDENCE.value, 400)
            return "header_confirmed", "From header", meta
        reason = str(change.get("reason_code", ""))
        note = str(change.get("note", "")).strip()
        if reason not in OPERATOR_REASON_CODES or not 1 <= len(note) <= 200:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REASON.value, 400)
        meta.update({"reason_code": reason, "note_hash": _sha(note)})
        return "human_confirmed", "", meta

    @staticmethod
    def _recompute(snapshot: dict) -> list[str]:
        missing = []
        for path, node in (
            ("customer.name", snapshot["customer"]["name"]),
            ("customer.company", snapshot["customer"]["company"]),
            ("country", snapshot["country"]),
        ):
            if node.get("status") == "pending_confirmation" or not node.get("value"):
                missing.append(path)
        deadline = snapshot["delivery_deadline"]
        if deadline.get("status") == "pending_confirmation" or not deadline.get("raw"):
            missing.append("delivery_deadline")
        term = snapshot["trade_term"]
        if (term.get("status") == "pending_confirmation" or not term.get("incoterm")
                or not term.get("named_place")):
            missing.append("trade_term")
        for index, item in enumerate(snapshot["items"]):
            for name in ("product", "specification"):
                node = item[name]
                if node.get("status") == "pending_confirmation" or not node.get("value"):
                    missing.append(f"items[{index}].{name}")
            quantity = item["quantity"]
            if (quantity.get("status") == "pending_confirmation" or not quantity.get("value")
                    or not quantity.get("unit")):
                missing.append(f"items[{index}].quantity")
        snapshot["missing_fields"] = missing
        return missing

    def preview(self, email_id: int, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"base_review_version", "changes"}:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        try:
            base_version = int(payload.get("base_review_version"))
        except (TypeError, ValueError) as exc:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400) from exc
        changes = payload.get("changes")
        if base_version < 0 or not isinstance(changes, list) or len(changes) > 100:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        detail, _ = self.detail(email_id)
        if detail["status"] != "needs_review" or detail["review_version"] != base_version:
            raise EmailReviewServiceError(EmailReviewErrorCode.VERSION_CONFLICT.value, 409)
        snapshot = copy.deepcopy(detail["extraction"])
        if not isinstance(snapshot.get("items"), list) or not 1 <= len(snapshot["items"]) <= 50:
            raise EmailReviewServiceError(EmailReviewErrorCode.VALIDATION_FAILED.value, 422)
        meta = []
        seen = set()
        for change in changes:
            if not isinstance(change, dict) or "path" not in change or "value" not in change:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
            path = str(change["path"])
            if path in seen:
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
            seen.add(path)
            value = self._validate_value(path, change["value"])
            try:
                node, key = self._node(snapshot, path)
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, EmailReviewServiceError):
                    raise
                raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400) from exc
            status, evidence, item_meta = self._source(change, path=path, value=value, detail=detail)
            node[key] = value
            node["status"] = status
            node["evidence"] = evidence
            item_meta["result_status"] = status
            meta.append(item_meta)
        missing = self._recompute(snapshot)
        warnings = snapshot.get("warnings")
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise EmailReviewServiceError(EmailReviewErrorCode.VALIDATION_FAILED.value, 422)
        stored = self.repository.get_inbound_review_detail(email_id)
        if stored is None:
            raise EmailReviewServiceError(EmailReviewErrorCode.INBOUND_NOT_FOUND.value, 404)
        return {
            "email_id": email_id, "base_review_version": base_version,
            "candidate": snapshot, "missing_fields": missing,
            "warnings": warnings, "confirmable": not missing, "change_meta": meta,
            "base_extraction_hash": _sha(stored["extraction_json"] or "{}"),
        }

    def confirm(self, email_id: int, payload: dict, *, idempotency_key: str, if_match: str) -> dict:
        if not self.reviewer_id:
            raise EmailReviewServiceError(EmailReviewErrorCode.REVIEWER_NOT_CONFIGURED.value, 503)
        if not isinstance(payload, dict) or "reviewer" in payload:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400)
        try:
            idempotency_key = str(uuid.UUID(idempotency_key))
            base_version = int(payload.get("base_review_version"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise EmailReviewServiceError(EmailReviewErrorCode.INVALID_REQUEST.value, 400) from exc
        if if_match != review_etag(email_id, base_version):
            raise EmailReviewServiceError(EmailReviewErrorCode.VERSION_CONFLICT.value, 409)
        request_hash = _sha({"base_review_version": base_version, "changes": payload.get("changes")})
        existing = self.repository.get_review_by_idempotency(email_id, idempotency_key)
        if existing:
            if existing["request_hash"] != request_hash:
                raise EmailReviewServiceError(EmailReviewErrorCode.IDEMPOTENCY_CONFLICT.value, 409)
            return self._confirmation(existing)
        preview = self.preview(email_id, payload)
        if not preview["confirmable"]:
            raise EmailReviewServiceError(EmailReviewErrorCode.PENDING_FIELDS.value, 422)
        snapshot = preview["candidate"]
        review_hash = _sha(snapshot)
        try:
            row = self.repository.confirm_review(
                email_id=email_id, base_version=base_version, idempotency_key=idempotency_key,
                request_hash=request_hash, base_extraction_hash=preview["base_extraction_hash"],
                snapshot=snapshot, review_hash=review_hash, change_meta=preview["change_meta"],
                reviewer=self.reviewer_id,
            )
        except EmailReviewRepositoryError as exc:
            raise self._repository_error(exc) from exc
        return self._confirmation(row)

    @staticmethod
    def _confirmation(row: dict) -> dict:
        return {
            "email_id": int(row["email_id"]), "status": "confirmed",
            "review_id": row["review_id"], "review_version": int(row["review_version"]),
            "review_hash": row["review_hash"], "quote_eligible": True,
            "confirmed_at": row["created_at"],
        }

    @staticmethod
    def _repository_error(exc: EmailReviewRepositoryError) -> EmailReviewServiceError:
        code = str(exc)
        status = {
            EmailReviewErrorCode.INVALID_REQUEST.value: 400,
            EmailReviewErrorCode.INBOUND_NOT_FOUND.value: 404,
            EmailReviewErrorCode.VERSION_CONFLICT.value: 409,
            EmailReviewErrorCode.IDEMPOTENCY_CONFLICT.value: 409,
            EmailReviewErrorCode.SUPERSEDED.value: 409,
        }.get(code, 500)
        return EmailReviewServiceError(code if status != 500 else "email_review_internal_error", status)


class EmailReviewGate:
    def __init__(self, repository: EmailRepository):
        self.repository = repository

    def require_confirmed(self, *, email_id: int, review_id: str, review_hash: str) -> dict:
        row = self.repository.get_confirmed_review(email_id, review_id, review_hash)
        if row is None:
            raise EmailReviewServiceError(EmailReviewErrorCode.SUPERSEDED.value, 409)
        snapshot = json.loads(row["reviewed_json"])
        if snapshot.get("missing_fields"):
            raise EmailReviewServiceError(EmailReviewErrorCode.PENDING_FIELDS.value, 422)
        return snapshot


def create_default_email_review_service(reviewer_id: str | None = None) -> EmailReviewService:
    from agent.business.config import load_business_config
    if load_business_config().database_backend != "sqlite":
        raise EmailReviewServiceError("email_review_backend_not_supported", 503)
    return EmailReviewService(
        EmailRepository(),
        os.environ.get("NANOCLAW_EMAIL_REVIEWER_ID", "") if reviewer_id is None else reviewer_id,
    )
