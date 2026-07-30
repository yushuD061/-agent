"""Read-only IMAP adapter shared by QQ and NetEase providers."""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import EmailEnvelope
from .mime_parser import EmailParseLimits, parse_email


IMAP_PRESETS = {"qq": "imap.qq.com", "netease_163": "imap.163.com", "netease_126": "imap.126.com"}
NETEASE_PROVIDERS = {"netease_163", "netease_126"}
NETEASE_CLIENT_ID = '("name" "NanoClaw" "version" "0.1.0" "vendor" "NanoClaw")'


class ImapOperationError(RuntimeError):
    """A safe, stable IMAP error code suitable for logs and retry state."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _imap_error_code(operation: str, data) -> str:
    text = " ".join(
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        for value in (data or [])
    ).lower()
    if "unsafe login" in text:
        return f"imap_{operation}_unsafe_login"
    if "authentication" in text or "authenticate" in text:
        return f"imap_{operation}_authentication_failed"
    return f"imap_{operation}_failed"


@dataclass(frozen=True)
class ImapSettings:
    provider: str
    account_id: str
    address: str
    auth_code: str
    host: str = ""
    port: int = 993
    folder: str = "INBOX"
    timeout_seconds: float = 30.0

    def resolved_host(self) -> str:
        host = self.host or IMAP_PRESETS.get(self.provider, "")
        if not host:
            raise ValueError("custom_imap requires NANOCLAW_EMAIL_IMAP_HOST")
        if self.provider not in {*IMAP_PRESETS, "custom_imap"}:
            raise ValueError("unsupported email provider")
        return host


class ImapEmailSource:
    """Fetches bytes with PEEK and never STOREs, MOVEs, COPYs or deletes."""

    def __init__(self, settings: ImapSettings, limits: EmailParseLimits | None = None):
        self.settings = settings
        self.limits = limits
        self.uidvalidity = 0

    def _connect(self) -> imaplib.IMAP4_SSL:
        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.settings.timeout_seconds)
        try:
            client = imaplib.IMAP4_SSL(self.settings.resolved_host(), self.settings.port,
                                       ssl_context=ssl.create_default_context())
            client.login(self.settings.address, self.settings.auth_code)
            if self.settings.provider in NETEASE_PROVIDERS:
                self._send_netease_client_id(client)
            return client
        finally:
            socket.setdefaulttimeout(previous)

    def test_connection(self) -> None:
        """Authenticate and EXAMINE the configured folder without reading mail."""
        client = self._connect()
        try:
            status, data = client.select(self.settings.folder, readonly=True)
            if status != "OK":
                raise ImapOperationError(_imap_error_code("select", data))
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass

    @staticmethod
    def _send_netease_client_id(client: imaplib.IMAP4_SSL) -> None:
        """Identify the client as required by NetEase IMAP anti-abuse controls."""
        imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
        status, data = client._simple_command("ID", NETEASE_CLIENT_ID)
        if status != "OK":
            raise ImapOperationError(_imap_error_code("id", data))

    def fetch_after(self, last_uid: int, limit: int = 50) -> list[EmailEnvelope]:
        client = self._connect()
        try:
            status, data = client.select(self.settings.folder, readonly=True)
            if status != "OK":
                raise ImapOperationError(_imap_error_code("select", data))
            response = client.response("UIDVALIDITY")[1]
            if not response:
                raise ImapOperationError("imap_uidvalidity_missing")
            self.uidvalidity = int(response[0])
            status, data = client.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise ImapOperationError(_imap_error_code("search", data))
            uids = [int(value) for value in (data[0] or b"").split()][:limit]
            result = []
            for uid in uids:
                status, rows = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise ImapOperationError(_imap_error_code("fetch", rows))
                raw = next((row[1] for row in rows if isinstance(row, tuple) and len(row) > 1), None)
                if raw is None:
                    raise ImapOperationError("imap_message_missing")
                result.append(parse_email(raw, account_id=self.settings.account_id, provider=self.settings.provider,
                                          folder=self.settings.folder, uidvalidity=self.uidvalidity, uid=uid,
                                          received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                          limits=self.limits))
            return result
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass
