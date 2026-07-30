"""Stable M3 human-review contracts for inbound RFQ email."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final


class EmailReviewErrorCode(StrEnum):
    INVALID_REQUEST = "email_review_invalid_request"
    INVALID_EVIDENCE = "email_review_invalid_evidence"
    INVALID_REASON = "email_review_invalid_reason"
    INBOUND_NOT_FOUND = "email_inbound_not_found"
    VERSION_CONFLICT = "email_review_version_conflict"
    IDEMPOTENCY_CONFLICT = "email_review_idempotency_conflict"
    SUPERSEDED = "email_review_superseded"
    PENDING_FIELDS = "email_review_pending_fields"
    VALIDATION_FAILED = "email_review_validation_failed"
    REVIEWER_NOT_CONFIGURED = "email_reviewer_not_configured"


class ReviewSourceType(StrEnum):
    EMAIL_EVIDENCE = "email_evidence"
    HEADER_EVIDENCE = "header_evidence"
    OPERATOR_INPUT = "operator_input"


OPERATOR_REASON_CODES: Final = frozenset({
    "phone_verified",
    "customer_portal_verified",
    "account_manager_verified",
    "corrected_internal_record",
})

_STATIC_PATHS = {
    "customer.name", "customer.company", "customer.email", "country",
    "delivery_deadline.raw", "delivery_deadline.normalized",
    "trade_term.incoterm", "trade_term.named_place", "trade_term.version",
}
_ITEM_PATH = re.compile(
    r"^items\[(?P<index>0|[1-9]\d*)\]\.(?P<field>product|specification|quantity\.value|quantity\.unit)$"
)


def parse_review_path(path: str) -> tuple[int | None, str]:
    if path in _STATIC_PATHS:
        return None, path
    match = _ITEM_PATH.fullmatch(path)
    if not match:
        raise ValueError(EmailReviewErrorCode.INVALID_REQUEST.value)
    return int(match.group("index")), match.group("field")


def review_etag(email_id: int, review_version: int) -> str:
    return f'"email-{email_id}-review-{review_version}"'

