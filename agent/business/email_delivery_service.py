"""M4 controlled-email application service and privacy-safe DTOs."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

from agent.business.config import load_business_config
from agent.business.email_account_repository import EmailAccountRepository
from agent.business.email_delivery_repository import (
    EmailDeliveryRepository,
    EmailDeliveryRepositoryError,
)


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class EmailDeliveryServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


class EmailDeliveryService:
    def __init__(self, repository: EmailDeliveryRepository):
        self.repository = repository

    @staticmethod
    def _positive_int(value, field: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise EmailDeliveryServiceError(f"email_invalid_{field}") from exc
        if result <= 0:
            raise EmailDeliveryServiceError(f"email_invalid_{field}")
        return result

    @staticmethod
    def _render_quote(quote_id: int, version: int, payload: dict) -> tuple[str, str]:
        subject = f"Quotation #{quote_id} v{version}"
        lines = [f"Quotation #{quote_id}", f"Version: {version}", ""]
        for index, item in enumerate(payload.get("items") or [], 1):
            name = str(item.get("product_name_en") or item.get("product_sku") or "Item")
            quantity = item.get("quantity", "")
            price = item.get("unit_price_usd", "")
            total = item.get("total_price_usd", "")
            lines.append(f"{index}. {name} | Qty: {quantity} | Unit USD: {price} | Total USD: {total}")
        lines.extend([
            "", f"Total USD: {payload.get('total_usd', '')}",
            f"Valid until: {payload.get('valid_until', '')}",
            f"Payment terms: {payload.get('payment_terms', '')}",
            f"Delivery term: {payload.get('delivery_term', '')}",
        ])
        remarks = str(payload.get("remarks_en") or "").strip()
        if remarks:
            lines.extend(["", remarks])
        return subject, "\n".join(lines).strip() + "\n"

    @staticmethod
    def safe_delivery(row: dict) -> dict:
        return {
            "delivery_id": row["delivery_id"],
            "account_id": row["account_id"],
            "quote_id": row["quote_id"],
            "quote_version": row["quote_version"],
            "approval_key": row["approval_key"],
            "recipient_masked": mask_email(row["recipient"]),
            "content_hash": row["content_hash"],
            "snapshot_hash": row["snapshot_hash"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "next_attempt_at": row["next_attempt_at"],
            "smtp_message_id": row["smtp_message_id"],
            "smtp_accepted_at": row["smtp_accepted_at"],
            "last_error_code": row["last_error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def queue(self, payload: dict, *, actor: str = "email_admin") -> dict:
        if not isinstance(payload, dict):
            raise EmailDeliveryServiceError("email_invalid_delivery_request")
        allowed = {"account_id", "quote_id", "quote_version", "approval_key", "recipient", "max_attempts"}
        if set(payload) - allowed:
            raise EmailDeliveryServiceError("email_invalid_delivery_request")
        try:
            account_id = str(uuid.UUID(str(payload.get("account_id", ""))))
        except (ValueError, AttributeError) as exc:
            raise EmailDeliveryServiceError("email_invalid_account_id") from exc
        quote_id = self._positive_int(payload.get("quote_id"), "quote_id")
        quote_version = self._positive_int(payload.get("quote_version"), "quote_version")
        approval_key = self._positive_int(payload.get("approval_key"), "approval_key")
        recipient = str(payload.get("recipient", "")).strip().lower()
        if not _EMAIL_PATTERN.fullmatch(recipient) or len(recipient) > 254:
            raise EmailDeliveryServiceError("email_invalid_recipient")
        max_attempts = self._positive_int(payload.get("max_attempts", 5), "max_attempts")
        if max_attempts > 10:
            raise EmailDeliveryServiceError("email_invalid_max_attempts")
        try:
            account, version_payload, content_hash = self.repository.approved_quote_snapshot(
                account_id=account_id, quote_id=quote_id, quote_version=quote_version,
                approval_key=approval_key,
            )
            raw_allowed = account["allowed_recipients_json"] or []
            allowed_values = json.loads(raw_allowed) if isinstance(raw_allowed, str) else raw_allowed
            allowed_recipients = {item.lower() for item in allowed_values}
            if recipient not in allowed_recipients:
                raise EmailDeliveryServiceError("email_recipient_not_allowed", 422)
            subject, body = self._render_quote(quote_id, quote_version, version_payload)
            row = self.repository.queue_approved_email(
                account_id=account_id, quote_id=quote_id, quote_version=quote_version,
                approval_key=approval_key, recipient=recipient, subject=subject, body=body,
                content_hash=content_hash, created_by=actor, max_attempts=max_attempts,
            )
            return self.safe_delivery(row)
        except EmailDeliveryRepositoryError as exc:
            raise _map_repository_error(exc) from exc

    def list_sendable_quotes(self, limit: int = 100) -> list[dict]:
        return self.repository.list_sendable_quotes(max(1, min(int(limit), 100)))

    def list_deliveries(self, limit: int = 100) -> list[dict]:
        return [self.safe_delivery(row) for row in self.repository.list_deliveries(max(1, min(int(limit), 100)))]

    def metrics(self) -> dict:
        return self.repository.metrics()

    def retry(self, delivery_id: str, *, actor: str = "email_admin") -> dict:
        try:
            delivery_id = str(uuid.UUID(delivery_id))
        except (ValueError, AttributeError) as exc:
            raise EmailDeliveryServiceError("email_invalid_delivery_id") from exc
        try:
            return self.safe_delivery(self.repository.retry_dead_letter(delivery_id, actor=actor))
        except EmailDeliveryRepositoryError as exc:
            raise _map_repository_error(exc) from exc


def _map_repository_error(exc: EmailDeliveryRepositoryError) -> EmailDeliveryServiceError:
    code = str(exc)
    status = {
        "email_account_not_found": 404,
        "email_quote_not_found": 404,
        "email_quote_version_not_found": 404,
        "email_delivery_not_found": 404,
        "email_quote_version_stale": 409,
        "email_quote_content_hash_mismatch": 409,
        "email_delivery_snapshot_tampered": 409,
        "email_delivery_not_retryable": 409,
        "email_quote_not_approved": 422,
        "email_outbound_account_disabled": 422,
        "email_recipient_not_allowed": 422,
    }.get(code, 500)
    return EmailDeliveryServiceError(code if status != 500 else "email_delivery_persistence_failed", status)


def create_default_email_delivery_service() -> EmailDeliveryService:
    config = load_business_config()
    if config.database_backend == "mysql":
        from agent.business.mysql_email_delivery_repository import MySQLEmailDeliveryRepository
        return EmailDeliveryService(MySQLEmailDeliveryRepository())
    # Ensure the local quote/approval truth tables and hash columns exist before
    # opening the outbox connection. Existing approvals without a hash remain
    # intentionally unsendable and must be approved again.
    from agent.business import sqlite_database
    sqlite_database.init_db()
    connection = sqlite3.connect(config.database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    EmailAccountRepository(connection)
    return EmailDeliveryService(EmailDeliveryRepository(connection))
