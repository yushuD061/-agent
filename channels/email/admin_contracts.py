"""Stable M0 contracts for the internal email administration API.

This module deliberately contains no mailbox credentials, persistence, IMAP
connections, or SMTP behavior. Later milestones implement those capabilities
behind these provider, account, route, and error-code contracts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final, TypedDict


EMAIL_ADMIN_CONTRACT_VERSION: Final = "email-admin.v1"


class EmailProviderId(StrEnum):
    QQ = "qq"
    NETEASE_163 = "netease_163"
    NETEASE_126 = "netease_126"


class EmailAccountStatus(StrEnum):
    DISABLED = "disabled"
    VALIDATING = "validating"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    AUTH_FAILED = "auth_failed"


class EmailAdminErrorCode(StrEnum):
    AUTH_NOT_CONFIGURED = "email_admin_auth_not_configured"
    UNAUTHORIZED = "email_admin_unauthorized"
    ORIGIN_FORBIDDEN = "email_admin_origin_forbidden"
    FEATURE_NOT_IMPLEMENTED = "email_feature_not_implemented"
    INVALID_PROVIDER = "email_invalid_provider"
    INVALID_ACCOUNT_ID = "email_invalid_account_id"
    ACCOUNT_NOT_FOUND = "email_account_not_found"
    CONFIG_VERSION_CONFLICT = "email_config_version_conflict"
    CREDENTIAL_REQUIRED = "email_credential_required"
    CONNECTION_FAILED = "email_connection_failed"


@dataclass(frozen=True)
class EndpointPreset:
    host: str
    port: int
    security: str = "ssl_tls"


@dataclass(frozen=True)
class EmailProviderPreset:
    provider: EmailProviderId
    label: str
    address_domains: tuple[str, ...]
    imap: EndpointPreset
    smtp: EndpointPreset
    imap_client_id_required: bool = False

    def public_payload(self) -> dict:
        payload = asdict(self)
        payload["provider"] = self.provider.value
        return payload


EMAIL_PROVIDER_PRESETS: Final = (
    EmailProviderPreset(
        provider=EmailProviderId.QQ,
        label="QQ 邮箱",
        address_domains=("qq.com",),
        imap=EndpointPreset("imap.qq.com", 993),
        smtp=EndpointPreset("smtp.qq.com", 465),
    ),
    EmailProviderPreset(
        provider=EmailProviderId.NETEASE_163,
        label="网易 163 邮箱",
        address_domains=("163.com",),
        imap=EndpointPreset("imap.163.com", 993),
        smtp=EndpointPreset("smtp.163.com", 465),
        imap_client_id_required=True,
    ),
    EmailProviderPreset(
        provider=EmailProviderId.NETEASE_126,
        label="网易 126 邮箱",
        address_domains=("126.com",),
        imap=EndpointPreset("imap.126.com", 993),
        smtp=EndpointPreset("smtp.126.com", 465),
        imap_client_id_required=True,
    ),
)


class EmailAccountCreate(TypedDict):
    display_name: str
    provider: str
    address: str
    auth_code: str
    inbound_enabled: bool
    outbound_enabled: bool
    poll_seconds: int
    sender_name: str
    allowed_senders: list[str]
    allowed_recipients: list[str]


class EmailAccountPatch(TypedDict, total=False):
    display_name: str
    auth_code: str
    inbound_enabled: bool
    outbound_enabled: bool
    poll_seconds: int
    sender_name: str
    allowed_senders: list[str]
    allowed_recipients: list[str]
    config_version: int


@dataclass(frozen=True)
class EmailAccountResponse:
    """The safe account shape returned to browsers; secrets never appear."""

    account_id: str
    display_name: str
    provider: EmailProviderId
    address: str
    credential_configured: bool
    folder: str
    inbound_enabled: bool
    outbound_enabled: bool
    poll_seconds: int
    sender_name: str
    allowed_senders: tuple[str, ...]
    allowed_recipients: tuple[str, ...]
    status: EmailAccountStatus
    last_checked_at: str | None
    last_error_code: str | None
    config_version: int
    created_at: str
    updated_at: str


EMAIL_ADMIN_ROUTE_CONTRACT: Final = (
    ("GET", "/api/email/providers", "public_provider_presets"),
    ("GET", "/api/email/accounts", "protected_m1"),
    ("POST", "/api/email/accounts", "protected_m1"),
    ("PATCH", "/api/email/accounts/{account_id}", "protected_m1"),
    ("POST", "/api/email/accounts/{account_id}/test", "protected_m1"),
    ("POST", "/api/email/accounts/{account_id}/enable", "protected_m1"),
    ("POST", "/api/email/accounts/{account_id}/disable", "protected_m1"),
    ("GET", "/api/email/inbound", "protected_m3"),
    ("GET", "/api/email/inbound/{email_id}", "protected_m3"),
    ("POST", "/api/email/inbound/{email_id}/review-preview", "protected_m3"),
    ("POST", "/api/email/inbound/{email_id}/confirm", "protected_m3"),
    ("GET", "/api/email/sendable-quotes", "protected_m4"),
    ("POST", "/api/email/approvals/{approval_key}/decision", "protected_m4"),
    ("POST", "/api/email/deliveries", "protected_m4"),
    ("GET", "/api/email/deliveries", "protected_m4"),
    ("GET", "/api/email/metrics", "protected_m6"),
    ("POST", "/api/email/deliveries/{delivery_id}/retry", "protected_m4"),
    ("GET", "/api/email/runtime", "protected_m4"),
)


def public_provider_contract() -> dict:
    return {
        "contract_version": EMAIL_ADMIN_CONTRACT_VERSION,
        "providers": [preset.public_payload() for preset in EMAIL_PROVIDER_PRESETS],
    }
