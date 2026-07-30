"""Bounded MIME normalization using only the Python standard library."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from .contracts import AttachmentMeta, EmailEnvelope


class EmailSecurityError(ValueError):
    """The message exceeded a deterministic safety limit."""


@dataclass(frozen=True)
class EmailParseLimits:
    max_message_bytes: int = 10 * 1024 * 1024
    max_attachments: int = 10
    max_mime_parts: int = 100
    max_mime_depth: int = 12
    max_body_chars: int = 100_000


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_BREAK_RE = re.compile(r"<(br|/p|/div|/li|/tr)\b[^>]*>", re.I)


def _header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _addresses(values: list[str]) -> tuple[str, ...]:
    return tuple(address for _, address in getaddresses(values) if address)


def _date(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return dt.isoformat()
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return ""


def _html_to_text(value: str) -> str:
    value = _SCRIPT_RE.sub(" ", value)
    value = _BREAK_RE.sub("\n", value)
    value = _TAG_RE.sub(" ", value)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in html.unescape(value).splitlines())
    return "\n".join(line for line in lines if line)


def _depth(part, current: int = 1) -> int:
    children = list(part.iter_parts()) if part.is_multipart() else []
    return max([current, *(_depth(child, current + 1) for child in children)])


def parse_email(raw: bytes, *, account_id: str, provider: str, folder: str,
                uidvalidity: int, uid: int, received_at: str = "",
                limits: EmailParseLimits | None = None) -> EmailEnvelope:
    limits = limits or EmailParseLimits()
    if len(raw) > limits.max_message_bytes:
        raise EmailSecurityError("message_too_large")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = list(message.walk())
    if len(parts) > limits.max_mime_parts:
        raise EmailSecurityError("too_many_mime_parts")
    if _depth(message) > limits.max_mime_depth:
        raise EmailSecurityError("mime_nesting_too_deep")

    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentMeta] = []
    for part in parts:
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        filename = _header(part.get_filename())
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            attachments.append(AttachmentMeta(filename=filename or "unnamed", content_type=part.get_content_type(),
                                              size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            content = payload.decode("utf-8", errors="replace")
        (plain if part.get_content_type() == "text/plain" else html_parts).append(str(content))
    if len(attachments) > limits.max_attachments:
        raise EmailSecurityError("too_many_attachments")
    html_source = "\n".join(html_parts)
    body = "\n".join(plain).strip() if plain else _html_to_text(html_source)
    if len(body) > limits.max_body_chars:
        raise EmailSecurityError("body_too_large")
    from_pairs = getaddresses(message.get_all("from", []))
    from_name, from_address = from_pairs[0] if from_pairs else ("", "")
    return EmailEnvelope(
        account_id=account_id, provider=provider,
        provider_message_id=f"{folder}:{uidvalidity}:{uid}", folder=folder,
        uidvalidity=uidvalidity, uid=uid,
        internet_message_id=_header(message.get("Message-ID")).strip(),
        thread_id=_header(message.get("In-Reply-To")).strip(),
        from_name=_header(from_name), from_address=from_address,
        reply_to=_addresses(message.get_all("reply-to", [])),
        to=_addresses(message.get_all("to", [])), cc=_addresses(message.get_all("cc", [])),
        subject=_header(message.get("subject")), sent_at=_date(message.get("date")), received_at=received_at,
        text_body=body, html_body_hash=hashlib.sha256(html_source.encode()).hexdigest() if html_source else "",
        attachments=tuple(attachments), raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
