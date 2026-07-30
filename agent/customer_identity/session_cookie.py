"""Opaque customer-session and CSRF token utilities."""

import hashlib
import secrets


AUTH_COOKIE = "nanoclaw_customer_session"
CSRF_COOKIE = "nanoclaw_customer_csrf"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

