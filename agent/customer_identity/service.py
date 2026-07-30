"""Customer registration, login, logout and session resolution service."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from agent.memory_runtime.errors import (
    CUSTOMER_ACCOUNT_LOCKED,
    CUSTOMER_AUTH_FAILED,
    CUSTOMER_AUTH_REQUIRED,
    CUSTOMER_CSRF_INVALID,
)

from .models import CustomerAuthSession, IssuedCustomerSession
from .password import PasswordHasher
from .repository import CustomerIdentityRepository, utcnow
from .session_cookie import new_token, token_hash


class CustomerIdentityError(Exception):
    STATUS = {
        CUSTOMER_AUTH_REQUIRED: 401,
        CUSTOMER_AUTH_FAILED: 401,
        CUSTOMER_ACCOUNT_LOCKED: 423,
        CUSTOMER_CSRF_INVALID: 403,
        "customer_registration_disabled": 403,
        "customer_identity_conflict": 409,
        "customer_input_invalid": 422,
    }

    def __init__(self, code: str):
        self.code = code
        self.status_code = self.STATUS.get(code, 400)
        super().__init__(code)


class CustomerIdentityService:
    def __init__(
        self, repository: CustomerIdentityRepository, password_hasher: PasswordHasher,
        *, tenant_id: str = "default", registration_enabled: bool = False,
        idle_minutes: int = 30, absolute_hours: int = 12,
        lock_after: int = 5, lock_minutes: int = 15,
    ) -> None:
        self.repository = repository
        self.password_hasher = password_hasher
        self.tenant_id = tenant_id
        self.registration_enabled = registration_enabled
        self.idle_minutes = idle_minutes
        self.absolute_hours = absolute_hours
        self.lock_after = lock_after
        self.lock_minutes = lock_minutes

    @staticmethod
    def normalize_identifier(kind: str, value: str) -> str:
        normalized = value.strip().casefold()
        if kind != "email" or "@" not in normalized or len(normalized) > 254:
            raise CustomerIdentityError("customer_input_invalid")
        return normalized

    def register(self, identifier: str, password: str, locale: str = "en"):
        if not self.registration_enabled:
            raise CustomerIdentityError("customer_registration_disabled")
        if len(password) < 12 or len(password) > 256 or locale not in {"zh", "en", "de"}:
            raise CustomerIdentityError("customer_input_invalid")
        normalized = self.normalize_identifier("email", identifier)
        try:
            return self.repository.create_account(
                tenant_id=self.tenant_id, identifier_type="email",
                identifier_normalized=normalized,
                credential_hash=self.password_hasher.hash(password),
                preferred_locale=locale,
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise CustomerIdentityError("customer_identity_conflict") from None
            raise

    def login(self, identifier: str, password: str) -> IssuedCustomerSession:
        normalized = self.normalize_identifier("email", identifier)
        record = self.repository.get_auth_record(self.tenant_id, "email", normalized)
        now_dt = datetime.now(timezone.utc)
        if record is None or record.account.status != "active":
            raise CustomerIdentityError(CUSTOMER_AUTH_FAILED)
        if record.locked_until and record.locked_until > utcnow():
            raise CustomerIdentityError(CUSTOMER_ACCOUNT_LOCKED)
        if not self.password_hasher.verify(record.credential_hash, password):
            locked_until = (now_dt + timedelta(minutes=self.lock_minutes)).isoformat().replace("+00:00", "Z")
            self.repository.record_login_failure(
                record.identity_id, lock_after=self.lock_after, locked_until=locked_until
            )
            raise CustomerIdentityError(CUSTOMER_AUTH_FAILED)
        self.repository.record_login_success(record.account.account_id, record.identity_id)
        return self._issue(record.account.account_id, record.account.preferred_locale, now_dt)

    def _issue(self, account_id: str, locale: str, now_dt: datetime) -> IssuedCustomerSession:
        token, csrf = new_token(), new_token()
        idle = (now_dt + timedelta(minutes=self.idle_minutes)).isoformat().replace("+00:00", "Z")
        absolute = (now_dt + timedelta(hours=self.absolute_hours)).isoformat().replace("+00:00", "Z")
        session = CustomerAuthSession(
            secrets.token_hex(16), account_id, self.tenant_id, token_hash(token),
            token_hash(csrf), utcnow(), utcnow(), idle, absolute,
        )
        self.repository.create_session(session)
        customer = self.repository.resolve_active(session.token_hash, utcnow())
        assert customer is not None
        return IssuedCustomerSession(customer, token, csrf, idle, absolute)

    def resolve(self, token: str | None):
        if not token:
            return None
        return self.repository.resolve_active(token_hash(token), utcnow())

    def require(self, token: str | None):
        customer = self.resolve(token)
        if customer is None:
            raise CustomerIdentityError(CUSTOMER_AUTH_REQUIRED)
        return customer

    def require_csrf(self, customer, supplied: str | None) -> None:
        if not supplied or not secrets.compare_digest(token_hash(supplied), customer.csrf_hash):
            raise CustomerIdentityError(CUSTOMER_CSRF_INVALID)

    def logout(self, customer) -> None:
        self.repository.revoke(customer.session_id, utcnow())

