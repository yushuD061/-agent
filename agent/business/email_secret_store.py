"""Secret storage for mailbox authorization codes.

The production-local implementation uses Windows DPAPI.  SQLite stores only
the opaque ``secret_ref``; plaintext authorization codes never enter the
database or the JSON file managed here.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Protocol


class EmailSecretStoreError(RuntimeError):
    """Stable, non-secret failure raised by a mailbox secret store."""


class EmailSecretStore(Protocol):
    def set(self, secret_ref: str, secret: str) -> None: ...
    def get(self, secret_ref: str) -> str: ...
    def delete(self, secret_ref: str) -> None: ...
    def contains(self, secret_ref: str) -> bool: ...


class MemoryEmailSecretStore:
    """Process-local test store; never selected as the runtime fallback."""

    def __init__(self):
        self._items: dict[str, str] = {}

    def set(self, secret_ref: str, secret: str) -> None:
        if not secret:
            raise EmailSecretStoreError("email_secret_empty")
        self._items[secret_ref] = secret

    def get(self, secret_ref: str) -> str:
        try:
            return self._items[secret_ref]
        except KeyError as exc:
            raise EmailSecretStoreError("email_secret_not_found") from exc

    def delete(self, secret_ref: str) -> None:
        self._items.pop(secret_ref, None)

    def contains(self, secret_ref: str) -> bool:
        return secret_ref in self._items


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi_protect(value: bytes) -> bytes:
    if os.name != "nt":
        raise EmailSecretStoreError("email_secret_store_windows_required")
    source, keepalive = _blob(value)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(ctypes.byref(source), "NanoClaw mailbox secret", None, None, None,
                                    0x01, ctypes.byref(output)):
        raise EmailSecretStoreError("email_secret_protect_failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del keepalive


def _dpapi_unprotect(value: bytes) -> bytes:
    if os.name != "nt":
        raise EmailSecretStoreError("email_secret_store_windows_required")
    source, keepalive = _blob(value)
    output = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                                   0x01, ctypes.byref(output)):
        raise EmailSecretStoreError("email_secret_unprotect_failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del keepalive


class DpapiFileEmailSecretStore:
    """Current-Windows-user DPAPI blobs stored in an atomic local JSON file."""

    def __init__(self, path: Path, *, protect: Callable[[bytes], bytes] | None = None,
                 unprotect: Callable[[bytes], bytes] | None = None):
        self.path = Path(path)
        self._protect = protect or _dpapi_protect
        self._unprotect = unprotect or _dpapi_unprotect
        self._lock = threading.RLock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("items"), dict):
                raise ValueError
            return {str(key): str(value) for key, value in payload["items"].items()}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise EmailSecretStoreError("email_secret_store_corrupt") from exc

    def _write(self, items: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"version": 1, "items": items}, sort_keys=True, separators=(",", ":"))
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, self.path)
        except OSError as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise EmailSecretStoreError("email_secret_store_write_failed") from exc

    def set(self, secret_ref: str, secret: str) -> None:
        if not secret_ref or not secret:
            raise EmailSecretStoreError("email_secret_empty")
        encrypted = self._protect(secret.encode("utf-8"))
        encoded = base64.b64encode(encrypted).decode("ascii")
        with self._lock:
            items = self._read()
            items[secret_ref] = encoded
            self._write(items)

    def get(self, secret_ref: str) -> str:
        with self._lock:
            encoded = self._read().get(secret_ref)
        if encoded is None:
            raise EmailSecretStoreError("email_secret_not_found")
        try:
            encrypted = base64.b64decode(encoded, validate=True)
            return self._unprotect(encrypted).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise EmailSecretStoreError("email_secret_unprotect_failed") from exc

    def delete(self, secret_ref: str) -> None:
        with self._lock:
            items = self._read()
            if secret_ref in items:
                del items[secret_ref]
                self._write(items)

    def contains(self, secret_ref: str) -> bool:
        with self._lock:
            return secret_ref in self._read()


def create_default_email_secret_store() -> DpapiFileEmailSecretStore:
    """Return the single runtime secret store used by account and SMTP services."""
    path = Path(__file__).resolve().parents[2] / "data" / "email_secrets.dpapi.json"
    return DpapiFileEmailSecretStore(path)
