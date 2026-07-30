"""Privacy-safe text handling for console output, logs, and persisted data."""

from __future__ import annotations

import builtins
import logging
import re
import traceback
from collections.abc import Mapping, Sequence
from typing import Any


_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-])([\w.+-]*)(@[^\s@]+)", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})(\d{5,8})(\d{3}[\dXx])(?!\d)")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|app[_-]?secret|access[_-]?token|refresh[_-]?token|password)"
    r"(\s*[=:]\s*|[\"']\s*:\s*[\"'])([^\s,;\"']+)"
)
_CONTRACT_PARTY_RE = re.compile(
    r"(?i)(合同方(?:名称)?|甲方|乙方|供应商(?:名称)?|客户名称|"
    r"contract[_ -]?party|counterparty|company[_ -]?name)"
    r"(\s*[=:：]\s*|[\"']\s*:\s*[\"'])([^,;，；\n\r\"']+)"
)


def sanitize_text(value: Any) -> str:
    """Return a log-safe representation with credentials and PII masked."""
    text = str(value)
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
    text = _PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(3)}", text)
    text = _ID_CARD_RE.sub(lambda m: f"{m.group(1)}********{m.group(3)}", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _CONTRACT_PARTY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return text


def sanitize_data(value: Any) -> Any:
    """Recursively sanitize strings before writing project data to disk."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {key: sanitize_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(sanitize_data(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [sanitize_data(item) for item in value]
    return value


def safe_print(*values: Any, **kwargs: Any) -> None:
    """Drop-in ``print`` replacement that masks every dynamic value."""
    builtins.print(*(sanitize_text(value) for value in values), **kwargs)


def safe_print_exception(exc: BaseException) -> None:
    """Print a sanitized traceback without exposing message content or secrets."""
    safe_print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def _sanitize_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {key: _sanitize_log_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_sanitize_log_value(item) for item in value)
    return value


def configure_privacy_logging() -> None:
    """Sanitize records emitted by application and third-party loggers."""
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_nanoclaw_privacy_safe", False):
        return

    def privacy_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current_factory(*args, **kwargs)
        record.msg = _sanitize_log_value(record.msg)
        record.args = _sanitize_log_value(record.args)
        return record

    privacy_factory._nanoclaw_privacy_safe = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(privacy_factory)
