"""Email ingestion configuration; credentials are process-environment only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from channels.email.imap_source import ImapSettings
from channels.email.mime_parser import EmailParseLimits


_PROJECT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _load_project_env() -> None:
    """Load root .env without overriding values injected by the process."""
    if not _PROJECT_ENV_PATH.is_file():
        return
    original_keys = set(os.environ)
    try:
        for raw_line in _PROJECT_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in original_keys:
                os.environ[key] = value
    except OSError as exc:
        raise ValueError("unable to read project .env") from exc


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be a boolean")
    return value in {"true", "1", "yes"}


@dataclass(frozen=True)
class EmailIngestionConfig:
    enabled: bool = False
    provider: str = "qq"
    account_id: str = ""
    address: str = ""
    auth_code: str = ""
    host: str = ""
    port: int = 993
    folder: str = "INBOX"
    poll_seconds: int = 60
    max_message_bytes: int = 10 * 1024 * 1024
    max_attachments: int = 10
    extraction_timeout_seconds: int = 60
    qq_notify_enabled: bool = False
    qq_target_id: str = ""
    qq_target_type: str = "c2c"
    managed_accounts_enabled: bool = False
    managed_scan_seconds: int = 5
    remote_extraction_approved: bool = False

    def validate_for_imap(self) -> None:
        missing = [name for name, value in {"account_id": self.account_id, "address": self.address,
                                             "auth_code": self.auth_code}.items() if not value]
        if missing:
            raise ValueError("missing email configuration: " + ", ".join(missing))

    def imap_settings(self) -> ImapSettings:
        self.validate_for_imap()
        return ImapSettings(self.provider, self.account_id, self.address, self.auth_code,
                            self.host, self.port, self.folder)

    def limits(self) -> EmailParseLimits:
        return EmailParseLimits(max_message_bytes=self.max_message_bytes, max_attachments=self.max_attachments)

    def validate_qq_notification(self) -> None:
        if not self.qq_notify_enabled:
            return
        if not self.enabled:
            raise ValueError("NANOCLAW_EMAIL_ENABLED must be true when QQ notification is enabled")
        if not self.qq_target_id:
            raise ValueError("NANOCLAW_EMAIL_QQ_TARGET_ID is required when QQ notification is enabled")
        if self.qq_target_type not in {"c2c", "group"}:
            raise ValueError("NANOCLAW_EMAIL_QQ_TARGET_TYPE must be 'c2c' or 'group'")


def load_email_config() -> EmailIngestionConfig:
    _load_project_env()
    return EmailIngestionConfig(
        enabled=_bool("NANOCLAW_EMAIL_ENABLED"), provider=os.environ.get("NANOCLAW_EMAIL_PROVIDER", "qq"),
        account_id=os.environ.get("NANOCLAW_EMAIL_ACCOUNT_ID", ""), address=os.environ.get("NANOCLAW_EMAIL_ADDRESS", ""),
        auth_code=os.environ.get("NANOCLAW_EMAIL_AUTH_CODE", ""), host=os.environ.get("NANOCLAW_EMAIL_IMAP_HOST", ""),
        port=int(os.environ.get("NANOCLAW_EMAIL_IMAP_PORT", "993")), folder=os.environ.get("NANOCLAW_EMAIL_FOLDER", "INBOX"),
        poll_seconds=int(os.environ.get("NANOCLAW_EMAIL_POLL_SECONDS", "60")),
        max_message_bytes=int(os.environ.get("NANOCLAW_EMAIL_MAX_MESSAGE_BYTES", str(10 * 1024 * 1024))),
        max_attachments=int(os.environ.get("NANOCLAW_EMAIL_MAX_ATTACHMENTS", "10")),
        extraction_timeout_seconds=int(os.environ.get("NANOCLAW_EMAIL_EXTRACTION_TIMEOUT_SECONDS", "60")),
        qq_notify_enabled=_bool("NANOCLAW_EMAIL_QQ_NOTIFY_ENABLED"),
        qq_target_id=os.environ.get("NANOCLAW_EMAIL_QQ_TARGET_ID", ""),
        qq_target_type=os.environ.get("NANOCLAW_EMAIL_QQ_TARGET_TYPE", "c2c").strip().lower(),
        managed_accounts_enabled=_bool("NANOCLAW_EMAIL_MANAGED_ACCOUNTS_ENABLED"),
        managed_scan_seconds=int(os.environ.get("NANOCLAW_EMAIL_MANAGED_SCAN_SECONDS", "5")),
        remote_extraction_approved=_bool("NANOCLAW_EMAIL_REMOTE_EXTRACTION_APPROVED"),
    )
