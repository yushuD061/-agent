"""Stable M0 contracts for the customer inquiry-to-contract workflow.

This module is intentionally side-effect free.  It freezes state, authorization,
visibility, route, hashing, and command contracts for later milestones; it does
not register HTTP routes, persist records, render documents, or send email.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import hashlib
import json
import unicodedata
from typing import Any, Final, Mapping


TRADE_CASE_CONTRACT_VERSION: Final = "trade-case.v1"


class InquiryStatus(StrEnum):
    DRAFT = "draft"
    AI_PROCESSING = "ai_processing"
    CUSTOMER_ACTION_REQUIRED = "customer_action_required"
    READY_FOR_QUOTE = "ready_for_quote"
    CANCELLED = "cancelled"


class QuoteStatus(StrEnum):
    NOT_STARTED = "not_started"
    INTERNAL_DRAFT = "internal_draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED_PENDING_PUBLICATION = "approved_pending_publication"
    PUBLISHED = "published"
    REVISION_REQUIRED = "revision_required"
    CUSTOMER_REVISION_REQUESTED = "customer_revision_requested"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ContractStatus(StrEnum):
    NOT_STARTED = "not_started"
    RENDERING = "rendering"
    PENDING_APPROVAL = "pending_approval"
    REVISION_REQUIRED = "revision_required"
    APPROVED_WAITING_EMAIL = "approved_waiting_email"
    ISSUED_UNSIGNED = "issued_unsigned"
    SIGNED_COPY_RECEIVED = "signed_copy_received"
    SIGNATURE_CONFIRMED = "signature_confirmed"
    EFFECTIVE = "effective"
    REVOKED = "revoked"


class DeliveryStatus(StrEnum):
    NOT_QUEUED = "not_queued"
    PENDING = "pending"
    SENDING = "sending"
    ACCEPTED = "accepted"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"
    STALE = "stale"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ProcessNodeStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CUSTOMER = "waiting_customer"
    WAITING_STAFF = "waiting_staff"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessNode(StrEnum):
    INQUIRY_RECEIVED = "inquiry_received"
    REQUIREMENTS_REVIEWING = "requirements_reviewing"
    QUOTE_DRAFTING = "quote_drafting"
    WAITING_QUOTE_APPROVAL = "waiting_quote_approval"
    QUOTE_PUBLISHING = "quote_publishing"
    WAITING_CUSTOMER_QUOTE_DECISION = "waiting_customer_quote_decision"
    CONTRACT_RENDERING = "contract_rendering"
    WAITING_CONTRACT_APPROVAL = "waiting_contract_approval"
    WAITING_CONTRACT_EMAIL = "waiting_contract_email"
    EMAIL_SENDING = "email_sending"
    WAITING_SIGNATURE = "waiting_signature"
    SIGNATURE_REVIEW = "signature_review"
    COMPLETED = "completed"


class ActorRole(StrEnum):
    CUSTOMER = "customer"
    TRADE_VIEWER = "trade_viewer"
    TRADE_REVIEWER = "trade_reviewer"
    CONTRACT_APPROVER = "contract_approver"
    SYSTEM_WORKER = "system_worker"


class TransitionAction(StrEnum):
    SUBMIT_INQUIRY = "submit_inquiry"
    REQUEST_CUSTOMER_INPUT = "request_customer_input"
    RESUBMIT_CUSTOMER_INPUT = "resubmit_customer_input"
    CREATE_INTERNAL_QUOTE = "create_internal_quote"
    SUBMIT_QUOTE_REVIEW = "submit_quote_review"
    RECORD_QUOTE_REMINDER = "record_quote_reminder"
    RECORD_QUOTE_SLA_BREACH = "record_quote_sla_breach"
    REQUEST_QUOTE_REVISION = "request_quote_revision"
    START_QUOTE_REVISION = "start_quote_revision"
    APPROVE_QUOTE = "approve_quote"
    PUBLISH_QUOTE = "publish_quote"
    CUSTOMER_REQUEST_QUOTE_REVISION = "customer_request_quote_revision"
    START_CUSTOMER_QUOTE_REVISION = "start_customer_quote_revision"
    CUSTOMER_ACCEPT_QUOTE = "customer_accept_quote"
    START_CONTRACT_RENDER = "start_contract_render"
    SUBMIT_CONTRACT_REVIEW = "submit_contract_review"
    REQUEST_CONTRACT_REVISION = "request_contract_revision"
    START_CONTRACT_REVISION = "start_contract_revision"
    APPROVE_CONTRACT = "approve_contract"
    QUEUE_CONTRACT_EMAIL = "queue_contract_email"
    CLAIM_CONTRACT_EMAIL = "claim_contract_email"
    RECORD_EMAIL_RETRY = "record_email_retry"
    RECORD_EMAIL_DEAD_LETTER = "record_email_dead_letter"
    RECORD_EMAIL_STALE = "record_email_stale"
    RECORD_EMAIL_OUTCOME_UNKNOWN = "record_email_outcome_unknown"
    RECORD_SMTP_ACCEPTED = "record_smtp_accepted"
    RECEIVE_SIGNED_COPY = "receive_signed_copy"
    REJECT_SIGNED_COPY = "reject_signed_copy"
    CONFIRM_SIGNATURE = "confirm_signature"
    MARK_EFFECTIVE = "mark_effective"


class TradeErrorCode(StrEnum):
    CONTRACT_VERSION_UNSUPPORTED = "trade_contract_version_unsupported"
    INVALID_REQUEST = "trade_invalid_request"
    FORBIDDEN = "trade_forbidden"
    CASE_NOT_FOUND = "trade_case_not_found"
    INVALID_TRANSITION = "trade_invalid_transition"
    PRECONDITION_REQUIRED = "trade_precondition_required"
    VERSION_CONFLICT = "trade_version_conflict"
    IDEMPOTENCY_CONFLICT = "trade_idempotency_conflict"
    QUOTE_FIELDS_PENDING_CONFIRMATION = "trade_quote_fields_pending_confirmation"
    QUOTE_NOT_CURRENT = "trade_quote_not_current"
    QUOTE_HASH_MISMATCH = "trade_quote_hash_mismatch"
    QUOTE_SNAPSHOT_STALE = "trade_quote_snapshot_stale"
    QUOTE_NOT_APPROVED = "trade_quote_not_approved"
    CUSTOMER_QUOTE_BINDING_MISMATCH = "trade_customer_quote_binding_mismatch"
    CONTRACT_QUOTE_BINDING_MISMATCH = "trade_contract_quote_binding_mismatch"
    CONTRACT_NOT_CURRENT = "trade_contract_not_current"
    CONTRACT_ARTIFACT_MISMATCH = "trade_contract_artifact_mismatch"
    CONTRACT_NOT_APPROVED = "trade_contract_not_approved"
    RECIPIENT_NOT_VERIFIED = "trade_recipient_not_verified"
    DELIVERY_OUTCOME_UNKNOWN = "trade_delivery_outcome_unknown"
    SIGNED_ARTIFACT_MISMATCH = "trade_signed_artifact_mismatch"


TRADE_ERROR_HTTP_STATUS: Final[dict[TradeErrorCode, int]] = {
    TradeErrorCode.CONTRACT_VERSION_UNSUPPORTED: 400,
    TradeErrorCode.INVALID_REQUEST: 400,
    TradeErrorCode.FORBIDDEN: 403,
    TradeErrorCode.CASE_NOT_FOUND: 404,
    TradeErrorCode.INVALID_TRANSITION: 409,
    TradeErrorCode.PRECONDITION_REQUIRED: 428,
    TradeErrorCode.VERSION_CONFLICT: 412,
    TradeErrorCode.IDEMPOTENCY_CONFLICT: 409,
    TradeErrorCode.QUOTE_FIELDS_PENDING_CONFIRMATION: 409,
    TradeErrorCode.QUOTE_NOT_CURRENT: 409,
    TradeErrorCode.QUOTE_HASH_MISMATCH: 409,
    TradeErrorCode.QUOTE_SNAPSHOT_STALE: 409,
    TradeErrorCode.QUOTE_NOT_APPROVED: 409,
    TradeErrorCode.CUSTOMER_QUOTE_BINDING_MISMATCH: 409,
    TradeErrorCode.CONTRACT_QUOTE_BINDING_MISMATCH: 409,
    TradeErrorCode.CONTRACT_NOT_CURRENT: 409,
    TradeErrorCode.CONTRACT_ARTIFACT_MISMATCH: 409,
    TradeErrorCode.CONTRACT_NOT_APPROVED: 409,
    TradeErrorCode.RECIPIENT_NOT_VERIFIED: 409,
    TradeErrorCode.DELIVERY_OUTCOME_UNKNOWN: 409,
    TradeErrorCode.SIGNED_ARTIFACT_MISMATCH: 409,
}


class TradeContractError(ValueError):
    def __init__(self, code: TradeErrorCode):
        super().__init__(code.value)
        self.code = code
        self.status_code = TRADE_ERROR_HTTP_STATUS[code]


@dataclass(frozen=True)
class TradeActor:
    actor_id: str
    tenant_id: str
    roles: frozenset[ActorRole] = field(default_factory=frozenset)
    customer_account_id: str | None = None
    is_ai: bool = False


@dataclass(frozen=True)
class TradeCaseState:
    inquiry: InquiryStatus = InquiryStatus.DRAFT
    quote: QuoteStatus = QuoteStatus.NOT_STARTED
    contract: ContractStatus = ContractStatus.NOT_STARTED
    delivery: DeliveryStatus = DeliveryStatus.NOT_QUEUED
    process_node: ProcessNode = ProcessNode.INQUIRY_RECEIVED
    process_status: ProcessNodeStatus = ProcessNodeStatus.WAITING_CUSTOMER


@dataclass(frozen=True)
class TransitionContext:
    tenant_id: str
    customer_account_id: str
    pending_confirmations: tuple[str, ...] = ()
    quote_version_matches: bool = True
    quote_hashes_match: bool = True
    quote_snapshot_fresh: bool = True
    quote_approval_recorded: bool = False
    customer_quote_binding_matches: bool = True
    contract_quote_binding_matches: bool = True
    contract_version_matches: bool = True
    artifact_hash_matches: bool = True
    contract_approval_recorded: bool = False
    recipient_verified: bool = False
    signed_artifact_hash_matches: bool = True


@dataclass(frozen=True)
class TransitionRule:
    action: TransitionAction
    conditions: tuple[tuple[str, StrEnum], ...]
    updates: tuple[tuple[str, StrEnum], ...]
    allowed_roles: frozenset[ActorRole]


def _rule(
    action: TransitionAction,
    conditions: tuple[tuple[str, StrEnum], ...],
    updates: tuple[tuple[str, StrEnum], ...],
    *roles: ActorRole,
) -> TransitionRule:
    return TransitionRule(action, conditions, updates, frozenset(roles))


TRANSITION_RULES: Final[tuple[TransitionRule, ...]] = (
    _rule(TransitionAction.SUBMIT_INQUIRY,
          (("inquiry", InquiryStatus.DRAFT),),
          (("inquiry", InquiryStatus.AI_PROCESSING),
           ("process_node", ProcessNode.REQUIREMENTS_REVIEWING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.CUSTOMER),
    _rule(TransitionAction.REQUEST_CUSTOMER_INPUT,
          (("inquiry", InquiryStatus.AI_PROCESSING),),
          (("inquiry", InquiryStatus.CUSTOMER_ACTION_REQUIRED),
           ("process_status", ProcessNodeStatus.WAITING_CUSTOMER)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RESUBMIT_CUSTOMER_INPUT,
          (("inquiry", InquiryStatus.CUSTOMER_ACTION_REQUIRED),),
          (("inquiry", InquiryStatus.AI_PROCESSING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.CUSTOMER),
    _rule(TransitionAction.CREATE_INTERNAL_QUOTE,
          (("inquiry", InquiryStatus.AI_PROCESSING), ("quote", QuoteStatus.NOT_STARTED)),
          (("inquiry", InquiryStatus.READY_FOR_QUOTE),
           ("quote", QuoteStatus.INTERNAL_DRAFT),
           ("process_node", ProcessNode.QUOTE_DRAFTING),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.SUBMIT_QUOTE_REVIEW,
          (("quote", QuoteStatus.INTERNAL_DRAFT),),
          (("quote", QuoteStatus.PENDING_APPROVAL),
           ("process_node", ProcessNode.WAITING_QUOTE_APPROVAL),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECORD_QUOTE_REMINDER,
          (("quote", QuoteStatus.PENDING_APPROVAL),),
          (("quote", QuoteStatus.PENDING_APPROVAL),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)), ActorRole.TRADE_REVIEWER),
    _rule(TransitionAction.RECORD_QUOTE_SLA_BREACH,
          (("quote", QuoteStatus.PENDING_APPROVAL),),
          (("quote", QuoteStatus.PENDING_APPROVAL),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.REQUEST_QUOTE_REVISION,
          (("quote", QuoteStatus.PENDING_APPROVAL),),
          (("quote", QuoteStatus.REVISION_REQUIRED),
           ("process_node", ProcessNode.QUOTE_DRAFTING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.TRADE_REVIEWER),
    _rule(TransitionAction.START_QUOTE_REVISION,
          (("quote", QuoteStatus.REVISION_REQUIRED),),
          (("quote", QuoteStatus.INTERNAL_DRAFT),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.APPROVE_QUOTE,
          (("quote", QuoteStatus.PENDING_APPROVAL),),
          (("quote", QuoteStatus.APPROVED_PENDING_PUBLICATION),
           ("process_node", ProcessNode.QUOTE_PUBLISHING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.TRADE_REVIEWER),
    _rule(TransitionAction.PUBLISH_QUOTE,
          (("quote", QuoteStatus.APPROVED_PENDING_PUBLICATION),),
          (("quote", QuoteStatus.PUBLISHED),
           ("process_node", ProcessNode.WAITING_CUSTOMER_QUOTE_DECISION),
           ("process_status", ProcessNodeStatus.WAITING_CUSTOMER)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.CUSTOMER_REQUEST_QUOTE_REVISION,
          (("quote", QuoteStatus.PUBLISHED),),
          (("quote", QuoteStatus.CUSTOMER_REVISION_REQUESTED),
           ("process_node", ProcessNode.QUOTE_DRAFTING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.CUSTOMER),
    _rule(TransitionAction.START_CUSTOMER_QUOTE_REVISION,
          (("quote", QuoteStatus.CUSTOMER_REVISION_REQUESTED),),
          (("quote", QuoteStatus.INTERNAL_DRAFT),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.CUSTOMER_ACCEPT_QUOTE,
          (("quote", QuoteStatus.PUBLISHED),),
          (("quote", QuoteStatus.ACCEPTED),
           ("process_node", ProcessNode.CONTRACT_RENDERING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.CUSTOMER),
    _rule(TransitionAction.START_CONTRACT_RENDER,
          (("quote", QuoteStatus.ACCEPTED), ("contract", ContractStatus.NOT_STARTED)),
          (("contract", ContractStatus.RENDERING),
           ("process_node", ProcessNode.CONTRACT_RENDERING),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.SUBMIT_CONTRACT_REVIEW,
          (("contract", ContractStatus.RENDERING),),
          (("contract", ContractStatus.PENDING_APPROVAL),
           ("process_node", ProcessNode.WAITING_CONTRACT_APPROVAL),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.REQUEST_CONTRACT_REVISION,
          (("contract", ContractStatus.PENDING_APPROVAL),),
          (("contract", ContractStatus.REVISION_REQUIRED),
           ("process_node", ProcessNode.CONTRACT_RENDERING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.CONTRACT_APPROVER),
    _rule(TransitionAction.START_CONTRACT_REVISION,
          (("contract", ContractStatus.REVISION_REQUIRED),),
          (("contract", ContractStatus.RENDERING),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.APPROVE_CONTRACT,
          (("contract", ContractStatus.PENDING_APPROVAL),),
          (("contract", ContractStatus.APPROVED_WAITING_EMAIL),
           ("process_node", ProcessNode.WAITING_CONTRACT_EMAIL),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)), ActorRole.CONTRACT_APPROVER),
    _rule(TransitionAction.QUEUE_CONTRACT_EMAIL,
          (("contract", ContractStatus.APPROVED_WAITING_EMAIL),
           ("delivery", DeliveryStatus.NOT_QUEUED)),
          (("delivery", DeliveryStatus.PENDING),
           ("process_node", ProcessNode.EMAIL_SENDING),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.CONTRACT_APPROVER),
    _rule(TransitionAction.CLAIM_CONTRACT_EMAIL,
          (("delivery", DeliveryStatus.PENDING),),
          (("delivery", DeliveryStatus.SENDING),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.CLAIM_CONTRACT_EMAIL,
          (("delivery", DeliveryStatus.RETRY_WAIT),),
          (("delivery", DeliveryStatus.SENDING),
           ("process_status", ProcessNodeStatus.RUNNING)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECORD_EMAIL_RETRY,
          (("delivery", DeliveryStatus.SENDING),),
          (("delivery", DeliveryStatus.RETRY_WAIT),
           ("process_status", ProcessNodeStatus.QUEUED)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECORD_EMAIL_DEAD_LETTER,
          (("delivery", DeliveryStatus.SENDING),),
          (("delivery", DeliveryStatus.DEAD_LETTER),
           ("process_status", ProcessNodeStatus.FAILED)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECORD_EMAIL_STALE,
          (("delivery", DeliveryStatus.SENDING),),
          (("delivery", DeliveryStatus.STALE),
           ("process_status", ProcessNodeStatus.FAILED)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECORD_EMAIL_OUTCOME_UNKNOWN,
          (("delivery", DeliveryStatus.SENDING),),
          (("delivery", DeliveryStatus.OUTCOME_UNKNOWN),
           ("process_status", ProcessNodeStatus.FAILED)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECORD_SMTP_ACCEPTED,
          (("delivery", DeliveryStatus.SENDING),
           ("contract", ContractStatus.APPROVED_WAITING_EMAIL)),
          (("delivery", DeliveryStatus.ACCEPTED),
           ("contract", ContractStatus.ISSUED_UNSIGNED),
           ("process_node", ProcessNode.WAITING_SIGNATURE),
           ("process_status", ProcessNodeStatus.WAITING_CUSTOMER)), ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.RECEIVE_SIGNED_COPY,
          (("contract", ContractStatus.ISSUED_UNSIGNED),),
          (("contract", ContractStatus.SIGNED_COPY_RECEIVED),
           ("process_node", ProcessNode.SIGNATURE_REVIEW),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)),
          ActorRole.CUSTOMER, ActorRole.SYSTEM_WORKER),
    _rule(TransitionAction.REJECT_SIGNED_COPY,
          (("contract", ContractStatus.SIGNED_COPY_RECEIVED),),
          (("contract", ContractStatus.ISSUED_UNSIGNED),
           ("process_node", ProcessNode.WAITING_SIGNATURE),
           ("process_status", ProcessNodeStatus.WAITING_CUSTOMER)), ActorRole.CONTRACT_APPROVER),
    _rule(TransitionAction.CONFIRM_SIGNATURE,
          (("contract", ContractStatus.SIGNED_COPY_RECEIVED),),
          (("contract", ContractStatus.SIGNATURE_CONFIRMED),
           ("process_status", ProcessNodeStatus.WAITING_STAFF)), ActorRole.CONTRACT_APPROVER),
    _rule(TransitionAction.MARK_EFFECTIVE,
          (("contract", ContractStatus.SIGNATURE_CONFIRMED),),
          (("contract", ContractStatus.EFFECTIVE),
           ("process_node", ProcessNode.COMPLETED),
           ("process_status", ProcessNodeStatus.COMPLETED)), ActorRole.CONTRACT_APPROVER),
)


def _state_matches(state: TradeCaseState, conditions: tuple[tuple[str, StrEnum], ...]) -> bool:
    return all(getattr(state, field_name) == expected for field_name, expected in conditions)


def _require_scope(actor: TradeActor, context: TransitionContext, role: ActorRole) -> None:
    if actor.tenant_id != context.tenant_id:
        raise TradeContractError(TradeErrorCode.CASE_NOT_FOUND)
    if role == ActorRole.CUSTOMER and actor.customer_account_id != context.customer_account_id:
        raise TradeContractError(TradeErrorCode.CASE_NOT_FOUND)


def _require_action_context(action: TransitionAction, context: TransitionContext) -> None:
    if action == TransitionAction.SUBMIT_QUOTE_REVIEW and context.pending_confirmations:
        raise TradeContractError(TradeErrorCode.QUOTE_FIELDS_PENDING_CONFIRMATION)
    if action in {TransitionAction.APPROVE_QUOTE, TransitionAction.PUBLISH_QUOTE}:
        if not context.quote_version_matches:
            raise TradeContractError(TradeErrorCode.QUOTE_NOT_CURRENT)
        if not context.quote_hashes_match:
            raise TradeContractError(TradeErrorCode.QUOTE_HASH_MISMATCH)
        if not context.quote_snapshot_fresh:
            raise TradeContractError(TradeErrorCode.QUOTE_SNAPSHOT_STALE)
    if action == TransitionAction.PUBLISH_QUOTE and not context.quote_approval_recorded:
        raise TradeContractError(TradeErrorCode.QUOTE_NOT_APPROVED)
    if action in {
        TransitionAction.CUSTOMER_ACCEPT_QUOTE,
        TransitionAction.CUSTOMER_REQUEST_QUOTE_REVISION,
    } and not context.customer_quote_binding_matches:
        raise TradeContractError(TradeErrorCode.CUSTOMER_QUOTE_BINDING_MISMATCH)
    if action == TransitionAction.START_CONTRACT_RENDER and not context.contract_quote_binding_matches:
        raise TradeContractError(TradeErrorCode.CONTRACT_QUOTE_BINDING_MISMATCH)
    if action in {TransitionAction.APPROVE_CONTRACT, TransitionAction.QUEUE_CONTRACT_EMAIL}:
        if not context.contract_version_matches:
            raise TradeContractError(TradeErrorCode.CONTRACT_NOT_CURRENT)
        if not context.artifact_hash_matches:
            raise TradeContractError(TradeErrorCode.CONTRACT_ARTIFACT_MISMATCH)
    if action == TransitionAction.QUEUE_CONTRACT_EMAIL:
        if not context.contract_approval_recorded:
            raise TradeContractError(TradeErrorCode.CONTRACT_NOT_APPROVED)
        if not context.recipient_verified:
            raise TradeContractError(TradeErrorCode.RECIPIENT_NOT_VERIFIED)
    if action in {TransitionAction.RECEIVE_SIGNED_COPY, TransitionAction.CONFIRM_SIGNATURE}:
        if not context.signed_artifact_hash_matches:
            raise TradeContractError(TradeErrorCode.SIGNED_ARTIFACT_MISMATCH)


def apply_transition(
    state: TradeCaseState,
    action: TransitionAction,
    actor: TradeActor,
    context: TransitionContext,
) -> TradeCaseState:
    """Authorize and apply one pure transition; no persistence is performed."""

    matches = [rule for rule in TRANSITION_RULES
               if rule.action == action and _state_matches(state, rule.conditions)]
    if not matches:
        if state.delivery == DeliveryStatus.OUTCOME_UNKNOWN and action in {
            TransitionAction.CLAIM_CONTRACT_EMAIL,
            TransitionAction.RECORD_EMAIL_RETRY,
        }:
            raise TradeContractError(TradeErrorCode.DELIVERY_OUTCOME_UNKNOWN)
        raise TradeContractError(TradeErrorCode.INVALID_TRANSITION)
    if actor.is_ai:
        raise TradeContractError(TradeErrorCode.FORBIDDEN)

    rule = next((candidate for candidate in matches if candidate.allowed_roles & actor.roles), None)
    if rule is None:
        raise TradeContractError(TradeErrorCode.FORBIDDEN)
    matched_roles = rule.allowed_roles & actor.roles
    matched_role = next(
        (role for role in sorted(matched_roles, key=lambda item: item.value)
         if role != ActorRole.CUSTOMER),
        ActorRole.CUSTOMER,
    )
    _require_scope(actor, context, matched_role)
    _require_action_context(action, context)
    return replace(state, **{field_name: value for field_name, value in rule.updates})


def _normalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _normalize_json(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def quote_etag(quote_id: str, version: int, content_hash: str, calculation_hash: str) -> str:
    return f'"quote:{quote_id}:{version}:{content_hash}:{calculation_hash}"'


def contract_etag(contract_id: str, version: int, artifact_sha256: str) -> str:
    return f'"contract:{contract_id}:{version}:{artifact_sha256}"'


@dataclass(frozen=True)
class IdempotencyReceipt:
    key: str
    payload_hash: str
    result_ref: str


def check_idempotency(
    receipt: IdempotencyReceipt | None,
    key: str,
    payload: Mapping[str, Any],
) -> bool:
    """Return True for an exact replay and False for a new command."""

    if not key.strip():
        raise TradeContractError(TradeErrorCode.PRECONDITION_REQUIRED)
    if receipt is None:
        return False
    if receipt.key != key or receipt.payload_hash != canonical_sha256(payload):
        raise TradeContractError(TradeErrorCode.IDEMPOTENCY_CONFLICT)
    return True


@dataclass(frozen=True)
class RouteContract:
    method: str
    path: str
    audience: str
    required_role: ActorRole
    idempotency_required: bool = False
    if_match_required: bool = False


TRADE_API_ROUTE_CONTRACT: Final[tuple[RouteContract, ...]] = (
    RouteContract("GET", "/api/customer/trade-cases", "customer", ActorRole.CUSTOMER),
    RouteContract("POST", "/api/customer/trade-cases", "customer", ActorRole.CUSTOMER, True),
    RouteContract("GET", "/api/customer/trade-cases/{trade_case_id}", "customer", ActorRole.CUSTOMER),
    RouteContract("PATCH", "/api/customer/trade-cases/{trade_case_id}/draft", "customer", ActorRole.CUSTOMER, True, True),
    RouteContract("POST", "/api/customer/trade-cases/{trade_case_id}/submit", "customer", ActorRole.CUSTOMER, True, True),
    RouteContract("GET", "/api/customer/trade-cases/{trade_case_id}/events", "customer", ActorRole.CUSTOMER),
    RouteContract("GET", "/api/customer/quotes/{quote_id}", "customer", ActorRole.CUSTOMER),
    RouteContract("POST", "/api/customer/quotes/{quote_id}/decisions", "customer", ActorRole.CUSTOMER, True, True),
    RouteContract("GET", "/api/customer/contracts", "customer", ActorRole.CUSTOMER),
    RouteContract("GET", "/api/customer/contracts/{contract_id}", "customer", ActorRole.CUSTOMER),
    RouteContract("GET", "/api/customer/contracts/{contract_id}/download", "customer", ActorRole.CUSTOMER),
    RouteContract("POST", "/api/customer/contracts/{contract_id}/signed-artifacts", "customer", ActorRole.CUSTOMER, True, True),
    RouteContract("GET", "/api/ops/trade-cases", "workspace", ActorRole.TRADE_VIEWER),
    RouteContract("GET", "/api/ops/trade-cases/{trade_case_id}", "workspace", ActorRole.TRADE_VIEWER),
    RouteContract("GET", "/api/ops/trade-cases/{trade_case_id}/events", "workspace", ActorRole.TRADE_VIEWER),
    RouteContract("POST", "/api/ops/quotes/{quote_id}/revisions", "workspace", ActorRole.TRADE_REVIEWER, True, True),
    RouteContract("POST", "/api/ops/quotes/{quote_id}/approval-decisions", "workspace", ActorRole.TRADE_REVIEWER, True, True),
    RouteContract("POST", "/api/ops/contracts", "workspace", ActorRole.CONTRACT_APPROVER, True, True),
    RouteContract("POST", "/api/ops/contracts/{contract_id}/approval-decisions", "workspace", ActorRole.CONTRACT_APPROVER, True, True),
    RouteContract("POST", "/api/ops/contracts/{contract_id}/email-commands", "workspace", ActorRole.CONTRACT_APPROVER, True, True),
    RouteContract("POST", "/api/ops/contracts/{contract_id}/signature-confirmations", "workspace", ActorRole.CONTRACT_APPROVER, True, True),
    RouteContract("POST", "/api/ops/contracts/{contract_id}/effective-decisions", "workspace", ActorRole.CONTRACT_APPROVER, True, True),
    RouteContract("GET", "/api/ops/contract-deliveries", "workspace", ActorRole.TRADE_VIEWER),
)


CUSTOMER_CASE_FIELDS: Final[frozenset[str]] = frozenset({
    "schema_version", "trade_case_id", "reference", "inquiry_summary",
    "current_node", "node_status", "waiting_on", "waiting_since",
    "updated_at", "quote", "contract", "delivery", "events",
})
CUSTOMER_QUOTE_FIELDS: Final[frozenset[str]] = frozenset({
    "quote_id", "quote_version", "content_hash", "calculation_hash", "currency",
    "items", "subtotal", "discount", "freight", "total", "valid_until",
    "payment_terms", "delivery_term", "published_at", "customer_decision",
})
CUSTOMER_CONTRACT_FIELDS: Final[frozenset[str]] = frozenset({
    "contract_id", "contract_number", "contract_version", "status", "locale",
    "filename", "mime_type", "size_bytes", "page_count", "published_at",
    "download_url", "signature_status",
})
CUSTOMER_DELIVERY_FIELDS: Final[frozenset[str]] = frozenset({
    "status", "masked_recipient", "queued_at", "accepted_at", "last_updated_at",
})
INTERNAL_ONLY_FIELDS: Final[frozenset[str]] = frozenset({
    "internal_quote_draft", "cost_price", "exact_inventory", "reviewer_id",
    "review_comment", "risk_notes", "model_name", "prompt", "tool_calls",
    "private_knowledge", "storage_path", "raw_error", "recipient_email",
})


def _whitelist(payload: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key in allowed}


def customer_projection(payload: Mapping[str, Any], quote_status: QuoteStatus) -> dict[str, Any]:
    """Build a fail-closed customer DTO from an internal aggregate payload."""

    result = _whitelist(payload, CUSTOMER_CASE_FIELDS)
    quote = payload.get("quote")
    if quote_status in {QuoteStatus.PUBLISHED, QuoteStatus.ACCEPTED} and isinstance(quote, Mapping):
        result["quote"] = _whitelist(quote, CUSTOMER_QUOTE_FIELDS)
    else:
        result.pop("quote", None)
    contract = payload.get("contract")
    if isinstance(contract, Mapping):
        result["contract"] = _whitelist(contract, CUSTOMER_CONTRACT_FIELDS)
    delivery = payload.get("delivery")
    if isinstance(delivery, Mapping):
        result["delivery"] = _whitelist(delivery, CUSTOMER_DELIVERY_FIELDS)
    return result
