"""Customer identity and server-side authentication session contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomerAccount:
    account_id: str
    tenant_id: str
    status: str
    preferred_locale: str
    created_at: str
    updated_at: str
    last_login_at: str | None = None
    deleted_at: str | None = None
    version: int = 1


@dataclass(frozen=True)
class AuthRecord:
    identity_id: str
    account: CustomerAccount
    identifier_type: str
    identifier_normalized: str
    credential_hash: str
    failed_attempts: int
    locked_until: str | None


@dataclass(frozen=True)
class CustomerAuthSession:
    session_id: str
    account_id: str
    tenant_id: str
    token_hash: str
    csrf_hash: str
    created_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str
    revoked_at: str | None = None


@dataclass(frozen=True)
class AuthenticatedCustomer:
    session_id: str
    account_id: str
    tenant_id: str
    preferred_locale: str
    csrf_hash: str


@dataclass(frozen=True)
class IssuedCustomerSession:
    customer: AuthenticatedCustomer
    token: str
    csrf_token: str
    idle_expires_at: str
    absolute_expires_at: str

