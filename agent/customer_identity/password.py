"""Password hashing boundary; production local passwords require Argon2id."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class PasswordHasherUnavailable(RuntimeError):
    pass


@runtime_checkable
class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, encoded: str, password: str) -> bool: ...


class Argon2PasswordHasher:
    """Lazy adapter so disabled M1 does not require an unapproved dependency."""

    def __init__(self) -> None:
        try:
            from argon2 import PasswordHasher as _Argon2Hasher
            from argon2.low_level import Type
        except ImportError as exc:
            raise PasswordHasherUnavailable(
                "customer_password_hasher_not_configured"
            ) from exc
        self._hasher = _Argon2Hasher(type=Type.ID)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return bool(self._hasher.verify(encoded, password))
        except Exception:
            return False

