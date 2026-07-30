"""SQLite customer-account and authentication-session repository."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AuthRecord,
    AuthenticatedCustomer,
    CustomerAccount,
    CustomerAuthSession,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CustomerIdentityRepository:
    def __init__(self, database: str | Path | sqlite3.Connection) -> None:
        if isinstance(database, sqlite3.Connection):
            self.connection = database
        else:
            path = Path(database)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self.connection:
            self.connection.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS customer_account (
                  account_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','locked','disabled','deleted')),
                  preferred_locale TEXT NOT NULL DEFAULT 'en', created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, last_login_at TEXT, deleted_at TEXT,
                  version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS customer_auth_identity (
                  identity_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                  identifier_type TEXT NOT NULL, identifier_normalized TEXT NOT NULL,
                  credential_hash TEXT NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0,
                  locked_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(tenant_id, identifier_type, identifier_normalized),
                  FOREIGN KEY(account_id) REFERENCES customer_account(account_id)
                );
                CREATE TABLE IF NOT EXISTS customer_auth_session (
                  session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                  token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL, idle_expires_at TEXT NOT NULL,
                  absolute_expires_at TEXT NOT NULL, revoked_at TEXT,
                  FOREIGN KEY(account_id) REFERENCES customer_account(account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_customer_auth_session_account
                  ON customer_auth_session(tenant_id, account_id, revoked_at);
            """)

    @staticmethod
    def _account(row: sqlite3.Row) -> CustomerAccount:
        return CustomerAccount(**{
            name: row[name] for name in CustomerAccount.__dataclass_fields__
        })

    def create_account(
        self, *, tenant_id: str, identifier_type: str, identifier_normalized: str,
        credential_hash: str, preferred_locale: str,
    ) -> CustomerAccount:
        now, account_id = utcnow(), str(uuid.uuid4())
        account = CustomerAccount(
            account_id, tenant_id, "active", preferred_locale, now, now
        )
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO customer_account VALUES (?,?,?,?,?,?,?,?,?)",
                (*account.__dict__.values(),),
            )
            self.connection.execute(
                """INSERT INTO customer_auth_identity
                   (identity_id,account_id,tenant_id,identifier_type,identifier_normalized,
                    credential_hash,failed_attempts,locked_until,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,0,NULL,?,?)""",
                (str(uuid.uuid4()), account_id, tenant_id, identifier_type,
                 identifier_normalized, credential_hash, now, now),
            )
        return account

    def get_auth_record(
        self, tenant_id: str, identifier_type: str, identifier_normalized: str
    ) -> AuthRecord | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT i.*, a.status AS account_status, a.preferred_locale,
                          a.created_at AS account_created_at, a.updated_at AS account_updated_at,
                          a.last_login_at, a.deleted_at, a.version
                   FROM customer_auth_identity i JOIN customer_account a
                     ON a.account_id=i.account_id AND a.tenant_id=i.tenant_id
                   WHERE i.tenant_id=? AND i.identifier_type=? AND i.identifier_normalized=?""",
                (tenant_id, identifier_type, identifier_normalized),
            ).fetchone()
        if row is None:
            return None
        account = CustomerAccount(
            row["account_id"], row["tenant_id"], row["account_status"],
            row["preferred_locale"], row["account_created_at"],
            row["account_updated_at"], row["last_login_at"], row["deleted_at"],
            row["version"],
        )
        return AuthRecord(
            row["identity_id"], account, row["identifier_type"],
            row["identifier_normalized"], row["credential_hash"],
            row["failed_attempts"], row["locked_until"],
        )

    def record_login_failure(
        self, identity_id: str, *, lock_after: int, locked_until: str
    ) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """UPDATE customer_auth_identity SET failed_attempts=failed_attempts+1,
                   locked_until=CASE WHEN failed_attempts+1>=? THEN ? ELSE locked_until END,
                   updated_at=? WHERE identity_id=?""",
                (lock_after, locked_until, utcnow(), identity_id),
            )

    def record_login_success(self, account_id: str, identity_id: str) -> None:
        now = utcnow()
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE customer_auth_identity SET failed_attempts=0,locked_until=NULL,updated_at=? WHERE identity_id=?",
                (now, identity_id),
            )
            self.connection.execute(
                "UPDATE customer_account SET last_login_at=?,updated_at=? WHERE account_id=?",
                (now, now, account_id),
            )

    def create_session(self, session: CustomerAuthSession) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO customer_auth_session VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(session.__dict__.values()),
            )

    def resolve_active(self, hashed_token: str, now: str) -> AuthenticatedCustomer | None:
        with self._lock, self.connection:
            row = self.connection.execute(
                """SELECT s.*,a.preferred_locale,a.status FROM customer_auth_session s
                   JOIN customer_account a ON a.account_id=s.account_id AND a.tenant_id=s.tenant_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.idle_expires_at>?
                     AND s.absolute_expires_at>? AND a.status='active'""",
                (hashed_token, now, now),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE customer_auth_session SET last_seen_at=? WHERE session_id=?",
                (now, row["session_id"]),
            )
        return AuthenticatedCustomer(
            row["session_id"], row["account_id"], row["tenant_id"],
            row["preferred_locale"], row["csrf_hash"],
        )

    def revoke(self, session_id: str, now: str) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE customer_auth_session SET revoked_at=COALESCE(revoked_at,?) WHERE session_id=?",
                (now, session_id),
            )

    def revoke_account(self, tenant_id: str, account_id: str, now: str) -> int:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE customer_auth_session SET revoked_at=COALESCE(revoked_at,?)
                   WHERE tenant_id=? AND account_id=? AND revoked_at IS NULL""",
                (now, tenant_id, account_id),
            )
        return cursor.rowcount
