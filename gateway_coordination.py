"""Durable, body-free request coordination for concurrent Gateway workers.

The default coordinator is process-local.  M6 can explicitly select MySQL so
multiple Gateway instances share request claims and conversation leases.  The
ledger stores hashes and message references only; message bodies remain in the
authorized conversation repository.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class RequestScope:
    tenant_id: str
    account_id: str
    channel: str
    conversation_id: str
    request_id: str

    @property
    def key(self) -> str:
        raw = "\x1f".join((self.tenant_id, self.account_id, self.channel,
                            self.conversation_id, self.request_id))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def conversation_key(self) -> str:
        raw = "\x1f".join((self.tenant_id, self.account_id, self.channel,
                            self.conversation_id))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClaimResult:
    decision: str  # accepted / running / completed / conflict
    response_message_id: str | None = None


class RuntimeCoordinator(Protocol):
    async def claim(self, scope: RequestScope, payload_hash: str, owner: str,
                    lease_seconds: int) -> ClaimResult: ...
    async def acquire_conversation(self, scope: RequestScope, owner: str,
                                   lease_seconds: int) -> bool: ...
    async def renew_conversation(self, scope: RequestScope, owner: str,
                                 lease_seconds: int) -> bool: ...
    async def complete(self, scope: RequestScope, owner: str,
                       response_message_id: str | None = None) -> bool: ...
    async def fail(self, scope: RequestScope, owner: str) -> None: ...
    async def release_conversation(self, scope: RequestScope, owner: str) -> None: ...


class InMemoryRuntimeCoordinator:
    """Contract-compatible coordinator used by the unchanged default runtime."""

    def __init__(self) -> None:
        self.shared_across_instances = False
        self._guard = asyncio.Lock()
        self._claims: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, tuple[str, float]] = {}

    async def claim(self, scope: RequestScope, payload_hash: str, owner: str,
                    lease_seconds: int) -> ClaimResult:
        async with self._guard:
            now = time.monotonic()
            row = self._claims.get(scope.key)
            if row is None or (row["status"] == "retryable_failed" and row["expires"] <= now):
                self._claims[scope.key] = {"hash": payload_hash, "status": "running",
                                            "owner": owner, "expires": now + lease_seconds,
                                            "response": None}
                return ClaimResult("accepted")
            if row["hash"] != payload_hash:
                return ClaimResult("conflict")
            return ClaimResult(row["status"] if row["status"] == "completed" else "running",
                               row.get("response"))

    async def acquire_conversation(self, scope: RequestScope, owner: str,
                                   lease_seconds: int) -> bool:
        async with self._guard:
            now = time.monotonic()
            current = self._leases.get(scope.conversation_key)
            if current is not None and current[0] != owner and current[1] > now:
                return False
            self._leases[scope.conversation_key] = (owner, now + lease_seconds)
            return True

    async def renew_conversation(self, scope: RequestScope, owner: str,
                                 lease_seconds: int) -> bool:
        async with self._guard:
            current = self._leases.get(scope.conversation_key)
            if current is None or current[0] != owner:
                return False
            self._leases[scope.conversation_key] = (owner, time.monotonic() + lease_seconds)
            return True

    async def complete(self, scope: RequestScope, owner: str,
                       response_message_id: str | None = None) -> bool:
        async with self._guard:
            row = self._claims.get(scope.key)
            if row is None or row["owner"] != owner:
                return False
            row.update(status="completed", response=response_message_id, expires=float("inf"))
            return True

    async def fail(self, scope: RequestScope, owner: str) -> None:
        async with self._guard:
            row = self._claims.get(scope.key)
            if row is not None and row["owner"] == owner:
                row.update(status="retryable_failed", expires=time.monotonic())

    async def release_conversation(self, scope: RequestScope, owner: str) -> None:
        async with self._guard:
            current = self._leases.get(scope.conversation_key)
            if current is not None and current[0] == owner:
                self._leases.pop(scope.conversation_key, None)


class MySQLRuntimeCoordinator:
    """MySQL implementation backed by migration 005; schema creation is never implicit."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.shared_across_instances = True
        self._connection_factory = connection_factory

    async def _call(self, method: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(method, *args)

    def _transaction(self, callback: Callable[[Any], Any]) -> Any:
        connection = self._connection_factory()
        try:
            with connection.cursor() as cursor:
                value = callback(cursor)
            connection.commit()
            return value
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def claim(self, scope: RequestScope, payload_hash: str, owner: str,
                    lease_seconds: int) -> ClaimResult:
        def operation(cur: Any) -> ClaimResult:
            cur.execute(
                """INSERT IGNORE INTO runtime_request_ledger
                   (scope_key,tenant_id,account_id,channel,conversation_id,request_id,
                    payload_hash,status,lease_owner,lease_expires_at,expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'running',%s,
                           TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),
                           TIMESTAMPADD(DAY,7,UTC_TIMESTAMP(6)))""",
                (scope.key, scope.tenant_id, scope.account_id, scope.channel,
                 scope.conversation_id, scope.request_id, payload_hash, owner, lease_seconds),
            )
            inserted = cur.rowcount == 1
            cur.execute(
                """SELECT *, lease_expires_at<=UTC_TIMESTAMP(6) AS lease_expired
                   FROM runtime_request_ledger WHERE scope_key=%s FOR UPDATE""",
                        (scope.key,))
            row = cur.fetchone()
            if row["payload_hash"] != payload_hash:
                return ClaimResult("conflict")
            if inserted:
                return ClaimResult("accepted")
            if row["status"] == "retryable_failed" or (
                    row["status"] == "running" and row["lease_expires_at"] is not None
                    and bool(row["lease_expired"])):
                cur.execute(
                    """UPDATE runtime_request_ledger SET status='running',lease_owner=%s,
                       lease_expires_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),last_error_code=NULL
                       WHERE scope_key=%s""", (owner, lease_seconds, scope.key))
                return ClaimResult("accepted")
            return ClaimResult("completed" if row["status"] == "completed" else "running",
                               row.get("response_message_id"))
        return await self._call(self._transaction, operation)

    async def acquire_conversation(self, scope: RequestScope, owner: str,
                                   lease_seconds: int) -> bool:
        def operation(cur: Any) -> bool:
            cur.execute(
                """INSERT INTO runtime_conversation_lease
                   (conversation_scope_key,tenant_id,account_id,channel,conversation_id,
                    lease_owner,lease_expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)))
                   ON DUPLICATE KEY UPDATE
                     lease_owner=IF(lease_owner=VALUES(lease_owner) OR lease_expires_at<=UTC_TIMESTAMP(6),VALUES(lease_owner),lease_owner),
                     lease_expires_at=IF(lease_owner=VALUES(lease_owner) OR lease_expires_at<=UTC_TIMESTAMP(6),VALUES(lease_expires_at),lease_expires_at)""",
                (scope.conversation_key, scope.tenant_id, scope.account_id, scope.channel,
                 scope.conversation_id, owner, lease_seconds),
            )
            cur.execute("SELECT lease_owner FROM runtime_conversation_lease WHERE conversation_scope_key=%s",
                        (scope.conversation_key,))
            return cur.fetchone()["lease_owner"] == owner
        return await self._call(self._transaction, operation)

    async def renew_conversation(self, scope: RequestScope, owner: str,
                                 lease_seconds: int) -> bool:
        def operation(cur: Any) -> bool:
            cur.execute(
                """UPDATE runtime_conversation_lease
                   SET lease_expires_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6))
                   WHERE conversation_scope_key=%s AND lease_owner=%s""",
                (lease_seconds, scope.conversation_key, owner))
            return cur.rowcount == 1
        return await self._call(self._transaction, operation)

    async def complete(self, scope: RequestScope, owner: str,
                       response_message_id: str | None = None) -> bool:
        def operation(cur: Any) -> bool:
            cur.execute(
                """UPDATE runtime_request_ledger SET status='completed',response_message_id=%s,
                   lease_expires_at=NULL,completed_at=UTC_TIMESTAMP(6)
                   WHERE scope_key=%s AND lease_owner=%s AND status='running'""",
                (response_message_id, scope.key, owner))
            return cur.rowcount == 1
        return await self._call(self._transaction, operation)

    async def fail(self, scope: RequestScope, owner: str) -> None:
        def operation(cur: Any) -> None:
            cur.execute(
                """UPDATE runtime_request_ledger SET status='retryable_failed',
                   lease_expires_at=UTC_TIMESTAMP(6),last_error_code='gateway_processing_failed'
                   WHERE scope_key=%s AND lease_owner=%s AND status='running'""",
                (scope.key, owner))
        await self._call(self._transaction, operation)

    async def release_conversation(self, scope: RequestScope, owner: str) -> None:
        def operation(cur: Any) -> None:
            cur.execute("DELETE FROM runtime_conversation_lease WHERE conversation_scope_key=%s AND lease_owner=%s",
                        (scope.conversation_key, owner))
        await self._call(self._transaction, operation)


def payload_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def coordinator_from_environment() -> RuntimeCoordinator:
    backend = os.environ.get("NANOCLAW_RUNTIME_COORDINATOR_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return InMemoryRuntimeCoordinator()
    if backend != "mysql":
        raise ValueError("NANOCLAW_RUNTIME_COORDINATOR_BACKEND must be memory or mysql")
    from agent.business.mysql_database import create_connection
    return MySQLRuntimeCoordinator(create_connection)


def new_worker_id() -> str:
    return f"gateway-{uuid.uuid4().hex}"
