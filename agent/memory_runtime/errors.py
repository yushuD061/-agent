"""Stable error contracts for the staged memory runtime."""

from __future__ import annotations


class MemoryRuntimeError(Exception):
    """Base exception carrying a stable, non-enumerating error code."""

    default_code = "memory_runtime_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.default_code
        super().__init__(self.code)


class MemoryAccessDenied(MemoryRuntimeError):
    """Raised when an actor cannot access a memory scope."""

    default_code = "memory_scope_denied"


class MemoryScopeInvalid(MemoryRuntimeError, ValueError):
    """Raised when a scope violates a deterministic realm invariant."""

    default_code = "memory_scope_invalid"


class MemoryVersionConflict(MemoryRuntimeError):
    default_code = "customer_version_conflict"


class MemoryDeletionPending(MemoryRuntimeError):
    default_code = "memory_deletion_pending"


CUSTOMER_AUTH_REQUIRED = "customer_auth_required"
CUSTOMER_AUTH_FAILED = "customer_auth_failed"
CUSTOMER_ACCOUNT_LOCKED = "customer_account_locked"
CUSTOMER_CSRF_INVALID = "customer_csrf_invalid"
CUSTOMER_RESOURCE_NOT_FOUND = "customer_resource_not_found"
CUSTOMER_VERSION_CONFLICT = "customer_version_conflict"
CUSTOMER_CLAIM_CONFLICT = "customer_claim_conflict"
MEMORY_SCOPE_DENIED = "memory_scope_denied"
MEMORY_DELETION_PENDING = "memory_deletion_pending"

