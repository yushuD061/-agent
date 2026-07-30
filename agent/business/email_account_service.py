"""Validated mailbox account management and credential isolation."""

from __future__ import annotations

import argparse
import imaplib
import re
import smtplib
import socket
import ssl
import threading
import uuid
from dataclasses import asdict
from typing import Protocol

from agent.business.config import load_business_config
from agent.business.email_account_repository import EmailAccountRepository, EmailAccountRepositoryError
from agent.business.email_config import EmailIngestionConfig, load_email_config
from agent.business.email_secret_store import (
    DpapiFileEmailSecretStore, create_default_email_secret_store,
    EmailSecretStore,
    EmailSecretStoreError,
)
from channels.email.admin_contracts import (
    EMAIL_PROVIDER_PRESETS,
    EmailAccountResponse,
    EmailAccountStatus,
    EmailProviderId,
)
from channels.email.imap_source import ImapEmailSource, ImapOperationError, ImapSettings


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PROVIDERS = {item.provider.value: item for item in EMAIL_PROVIDER_PRESETS}


class EmailAccountServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class EmailConnectionTester(Protocol):
    def test(self, account: dict, auth_code: str) -> None: ...


class DefaultEmailConnectionTester:
    """Connection/authentication probe only; it never sends a message."""

    def test(self, account: dict, auth_code: str) -> None:
        preset = _PROVIDERS[account["provider"]]
        source = ImapEmailSource(ImapSettings(
            provider=account["provider"], account_id=account["account_id"], address=account["address"],
            auth_code=auth_code, host=preset.imap.host, port=preset.imap.port,
            folder=account["folder"], timeout_seconds=15,
        ))
        source.test_connection()
        if account["outbound_enabled"]:
            client = smtplib.SMTP_SSL(preset.smtp.host, preset.smtp.port,
                                      timeout=15, context=ssl.create_default_context())
            try:
                client.login(account["address"], auth_code)
            finally:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    client.close()


def _stable_connection_error(exc: Exception) -> tuple[str, EmailAccountStatus]:
    if isinstance(exc, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)):
        return "email_connection_authentication_failed", EmailAccountStatus.AUTH_FAILED
    if isinstance(exc, ImapOperationError) and "authentication" in exc.code:
        return "email_connection_authentication_failed", EmailAccountStatus.AUTH_FAILED
    if isinstance(exc, ssl.SSLError):
        return "email_connection_tls_failed", EmailAccountStatus.DEGRADED
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "email_connection_timeout", EmailAccountStatus.DEGRADED
    return "email_connection_failed", EmailAccountStatus.DEGRADED


class EmailAccountService:
    def __init__(self, repository: EmailAccountRepository, secret_store: EmailSecretStore,
                 connection_tester: EmailConnectionTester | None = None):
        self.repository = repository
        self.secret_store = secret_store
        self.connection_tester = connection_tester or DefaultEmailConnectionTester()
        self._lock = threading.RLock()

    @staticmethod
    def _account_id(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise EmailAccountServiceError("email_invalid_account_id", 400) from exc

    @staticmethod
    def _list(value, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise EmailAccountServiceError("email_invalid_account_config")
        items: list[str] = []
        for raw in value:
            item = str(raw).strip().lower()
            if not _EMAIL_PATTERN.fullmatch(item):
                raise EmailAccountServiceError("email_invalid_allowlist")
            if item not in items:
                items.append(item)
        if len(items) > 200:
            raise EmailAccountServiceError("email_allowlist_too_large")
        return items

    @classmethod
    def _validate(cls, payload: dict, *, existing: dict | None = None) -> dict:
        if not isinstance(payload, dict):
            raise EmailAccountServiceError("email_invalid_account_config")
        merged = dict(existing or {})
        merged.update({key: value for key, value in payload.items() if key != "auth_code"})
        provider = str(merged.get("provider", ""))
        preset = _PROVIDERS.get(provider)
        if preset is None:
            raise EmailAccountServiceError("email_invalid_provider")
        address = str(merged.get("address", "")).strip().lower()
        if (not _EMAIL_PATTERN.fullmatch(address)
                or address.rsplit("@", 1)[-1] not in preset.address_domains):
            raise EmailAccountServiceError("email_invalid_address")
        display_name = str(merged.get("display_name", "")).strip()
        if not 1 <= len(display_name) <= 50:
            raise EmailAccountServiceError("email_invalid_display_name")
        sender_name = str(merged.get("sender_name", "NanoClaw Sales")).strip()
        if not sender_name or len(sender_name) > 80 or "\r" in sender_name or "\n" in sender_name:
            raise EmailAccountServiceError("email_invalid_sender_name")
        try:
            poll_seconds = int(merged.get("poll_seconds", 60))
        except (TypeError, ValueError) as exc:
            raise EmailAccountServiceError("email_invalid_poll_seconds") from exc
        if not 30 <= poll_seconds <= 3600:
            raise EmailAccountServiceError("email_invalid_poll_seconds")
        allowed_senders = cls._list(merged.get("allowed_senders", []), "allowed_senders")
        allowed_recipients = cls._list(merged.get("allowed_recipients", []), "allowed_recipients")
        outbound_enabled = bool(merged.get("outbound_enabled", False))
        if outbound_enabled and not allowed_recipients:
            raise EmailAccountServiceError("email_recipients_required")
        if str(merged.get("folder", "INBOX")) != "INBOX":
            raise EmailAccountServiceError("email_invalid_folder")
        return {
            "display_name": display_name, "provider": provider, "address": address, "folder": "INBOX",
            "inbound_enabled": bool(merged.get("inbound_enabled", True)),
            "outbound_enabled": outbound_enabled, "poll_seconds": poll_seconds, "sender_name": sender_name,
            "allowed_senders": allowed_senders, "allowed_recipients": allowed_recipients,
        }

    @staticmethod
    def _auth_code(payload: dict, *, required: bool) -> str | None:
        raw = payload.get("auth_code")
        if raw is None or raw == "":
            if required:
                raise EmailAccountServiceError("email_credential_required")
            return None
        if not isinstance(raw, str) or not 1 <= len(raw) <= 1024 or "\x00" in raw:
            raise EmailAccountServiceError("email_credential_invalid")
        return raw

    def _safe(self, item: dict) -> dict:
        response = EmailAccountResponse(
            account_id=item["account_id"], display_name=item["display_name"],
            provider=EmailProviderId(item["provider"]), address=item["address"],
            credential_configured=self.secret_store.contains(item["secret_ref"]), folder=item["folder"],
            inbound_enabled=item["inbound_enabled"], outbound_enabled=item["outbound_enabled"],
            poll_seconds=item["poll_seconds"], sender_name=item["sender_name"],
            allowed_senders=tuple(item["allowed_senders"]), allowed_recipients=tuple(item["allowed_recipients"]),
            status=EmailAccountStatus(item["status"]), last_checked_at=item["last_checked_at"],
            last_error_code=item["last_error_code"], config_version=item["config_version"],
            created_at=item["created_at"], updated_at=item["updated_at"],
        )
        payload = asdict(response)
        payload["provider"] = response.provider.value
        payload["status"] = response.status.value
        return payload

    def list_accounts(self) -> list[dict]:
        return [self._safe(item) for item in self.repository.list_active()]

    def public_contact_email(self) -> str | None:
        """Return only the current public mailbox address, without loading credentials."""
        accounts = self.repository.list_active()
        if not accounts:
            return None
        current = max(
            accounts,
            key=lambda item: (item.get("updated_at", ""), item.get("created_at", ""), item["account_id"]),
        )
        return current["address"]

    def create_account(self, payload: dict, *, actor: str = "email_admin") -> dict:
        values = self._validate(payload)
        auth_code = self._auth_code(payload, required=True)
        account_id = str(uuid.uuid4())
        secret_ref = f"email-account/{account_id}"
        item = {**values, "account_id": account_id, "secret_ref": secret_ref, "status": "disabled"}
        with self._lock:
            self.secret_store.set(secret_ref, auth_code)
            try:
                created = self.repository.create(item, actor=actor)
            except Exception:
                self.secret_store.delete(secret_ref)
                raise
        return self._safe(created)

    def update_account(self, account_id: str, payload: dict, *, actor: str = "email_admin") -> dict:
        account_id = self._account_id(account_id)
        if any(key in payload for key in {"provider", "address", "folder", "account_id", "secret_ref"}):
            raise EmailAccountServiceError("email_immutable_account_field")
        existing = self.repository.get(account_id)
        if existing is None:
            raise EmailAccountServiceError("email_account_not_found", 404)
        try:
            expected_version = int(payload.get("config_version"))
        except (TypeError, ValueError) as exc:
            raise EmailAccountServiceError("email_config_version_conflict", 409) from exc
        values = self._validate(payload, existing=existing)
        auth_code = self._auth_code(payload, required=False)
        editable = {key: values[key] for key in (
            "display_name", "inbound_enabled", "outbound_enabled", "poll_seconds", "sender_name",
            "allowed_senders", "allowed_recipients",
        )}
        if auth_code:
            editable["status"] = "validating" if existing["status"] != "disabled" else "disabled"
            editable["last_error_code"] = None
        previous_secret: str | None = None
        with self._lock:
            if auth_code:
                try:
                    previous_secret = self.secret_store.get(existing["secret_ref"])
                except EmailSecretStoreError:
                    # A newly supplied credential is allowed to replace a missing or
                    # unreadable local DPAPI blob; it is never inferred or recovered.
                    previous_secret = None
                self.secret_store.set(existing["secret_ref"], auth_code)
            try:
                updated = self.repository.update(account_id, editable, expected_version=expected_version,
                                                 actor=actor, action="credential_rotated" if auth_code else "updated")
            except Exception:
                if auth_code:
                    if previous_secret is not None:
                        self.secret_store.set(existing["secret_ref"], previous_secret)
                    else:
                        self.secret_store.delete(existing["secret_ref"])
                raise
        return self._safe(updated)

    def set_enabled(self, account_id: str, enabled: bool, *, actor: str = "email_admin") -> dict:
        account_id = self._account_id(account_id)
        existing = self.repository.get(account_id)
        if existing is None:
            raise EmailAccountServiceError("email_account_not_found", 404)
        if enabled and not self.secret_store.contains(existing["secret_ref"]):
            raise EmailAccountServiceError("email_credential_required")
        return self._safe(self.repository.set_enabled(account_id, enabled, actor=actor))

    def test_connection(self, account_id: str, *, actor: str = "email_admin") -> dict:
        account_id = self._account_id(account_id)
        existing = self.repository.get(account_id)
        if existing is None:
            raise EmailAccountServiceError("email_account_not_found", 404)
        try:
            auth_code = self.secret_store.get(existing["secret_ref"])
            self.connection_tester.test(existing, auth_code)
        except EmailSecretStoreError as exc:
            updated = self.repository.record_health(account_id, "auth_failed", "email_credential_required", actor=actor)
            raise EmailAccountServiceError("email_credential_required") from exc
        except Exception as exc:
            code, status = _stable_connection_error(exc)
            self.repository.record_health(account_id, status.value, code, actor=actor)
            raise EmailAccountServiceError(code, 422) from exc
        return self._safe(self.repository.record_health(account_id, "healthy", None, actor=actor))

    def migrate_legacy_config(self, config: EmailIngestionConfig, *, actor: str = "legacy_env_migration") -> dict | None:
        if not (config.address and config.auth_code and config.account_id):
            return None
        existing = self.repository.find_by_provider_address(config.provider, config.address.strip().lower())
        if existing:
            return self._safe(existing)
        payload = {
            "display_name": "Legacy environment mailbox", "provider": config.provider,
            "address": config.address, "auth_code": config.auth_code,
            "inbound_enabled": bool(config.enabled), "outbound_enabled": False,
            "poll_seconds": config.poll_seconds, "sender_name": "NanoClaw Sales",
            "allowed_senders": [], "allowed_recipients": [],
        }
        values = self._validate(payload)
        account_id = str(uuid.uuid4())
        secret_ref = f"email-account/{account_id}"
        with self._lock:
            self.secret_store.set(secret_ref, config.auth_code)
            try:
                created = self.repository.create(
                    {**values, "account_id": account_id, "secret_ref": secret_ref, "status": "disabled"},
                    actor=actor, action="legacy_migrated",
                )
            except Exception:
                self.secret_store.delete(secret_ref)
                raise
        return self._safe(created)


def create_default_email_account_service() -> EmailAccountService:
    config = load_business_config()
    if config.database_backend == "mysql":
        from agent.business.mysql_email_account_repository import MySQLEmailAccountRepository
        repository = MySQLEmailAccountRepository()
    else:
        repository = EmailAccountRepository()
    return EmailAccountService(repository, create_default_email_secret_store())


def migrate_legacy_email_account() -> dict | None:
    """Explicit one-time migration entry; callers must invoke it deliberately."""
    return create_default_email_account_service().migrate_legacy_config(load_email_config())


def _map_repository_error(exc: EmailAccountRepositoryError) -> EmailAccountServiceError:
    mapping = {
        "email_account_not_found": ("email_account_not_found", 404),
        "email_config_version_conflict": ("email_config_version_conflict", 409),
        "email_account_already_exists": ("email_account_already_exists", 409),
    }
    code, status = mapping.get(str(exc), ("email_account_persistence_failed", 500))
    return EmailAccountServiceError(code, status)


def main() -> int:
    parser = argparse.ArgumentParser(description="NanoClaw mailbox account maintenance")
    parser.add_argument("--migrate-legacy", action="store_true",
                        help="explicitly migrate the legacy NANOCLAW_EMAIL_* account into M1 storage")
    args = parser.parse_args()
    if not args.migrate_legacy:
        parser.error("no operation selected")
    result = migrate_legacy_email_account()
    if result is None:
        print("legacy_email_account=not_configured")
    else:
        print(f"legacy_email_account=migrated account_id={result['account_id']} status={result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
