"""SQLite persistence for non-sensitive mailbox account configuration."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from agent.business.config import load_business_config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EmailAccountRepositoryError(RuntimeError):
    pass


class EmailAccountRepository:
    def __init__(self, connection: sqlite3.Connection | None = None):
        if connection is None:
            connection = sqlite3.connect(load_business_config().database_path, check_same_thread=False)
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.init_schema()

    @contextmanager
    def tx(self):
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def init_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS ops_email_account(
          account_id TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          provider TEXT NOT NULL,
          address TEXT NOT NULL,
          secret_ref TEXT NOT NULL UNIQUE,
          folder TEXT NOT NULL DEFAULT 'INBOX',
          inbound_enabled INTEGER NOT NULL DEFAULT 1,
          outbound_enabled INTEGER NOT NULL DEFAULT 0,
          poll_seconds INTEGER NOT NULL DEFAULT 60,
          sender_name TEXT NOT NULL DEFAULT 'NanoClaw Sales',
          allowed_senders_json TEXT NOT NULL DEFAULT '[]',
          allowed_recipients_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'disabled',
          last_checked_at TEXT,
          last_error_code TEXT,
          config_version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          deleted_at TEXT,
          UNIQUE(provider,address)
        );
        CREATE TABLE IF NOT EXISTS ops_email_account_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id TEXT NOT NULL,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          changed_fields_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY(account_id) REFERENCES ops_email_account(account_id)
        );
        """)
        self.connection.commit()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["inbound_enabled"] = bool(item["inbound_enabled"])
        item["outbound_enabled"] = bool(item["outbound_enabled"])
        item["allowed_senders"] = json.loads(item.pop("allowed_senders_json"))
        item["allowed_recipients"] = json.loads(item.pop("allowed_recipients_json"))
        return item

    def list_active(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM ops_email_account WHERE deleted_at IS NULL ORDER BY created_at,account_id"
        ).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, account_id: str) -> dict | None:
        return self._decode(self.connection.execute(
            "SELECT * FROM ops_email_account WHERE account_id=? AND deleted_at IS NULL", (account_id,)
        ).fetchone())

    def find_by_provider_address(self, provider: str, address: str) -> dict | None:
        return self._decode(self.connection.execute(
            "SELECT * FROM ops_email_account WHERE provider=? AND address=? AND deleted_at IS NULL",
            (provider, address),
        ).fetchone())

    def create(self, item: dict, *, actor: str, action: str = "created") -> dict:
        now = utc_now()
        with self.tx() as cursor:
            try:
                cursor.execute("""INSERT INTO ops_email_account(
                  account_id,display_name,provider,address,secret_ref,folder,inbound_enabled,outbound_enabled,
                  poll_seconds,sender_name,allowed_senders_json,allowed_recipients_json,status,
                  config_version,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""", (
                    item["account_id"], item["display_name"], item["provider"], item["address"], item["secret_ref"],
                    item.get("folder", "INBOX"), int(item["inbound_enabled"]), int(item["outbound_enabled"]),
                    item["poll_seconds"], item["sender_name"], json.dumps(item["allowed_senders"]),
                    json.dumps(item["allowed_recipients"]), item.get("status", "disabled"), now, now,
                ))
            except sqlite3.IntegrityError as exc:
                raise EmailAccountRepositoryError("email_account_already_exists") from exc
            self._audit(cursor, item["account_id"], actor, action,
                        [key for key in item if key not in {"secret_ref"}])
        return self.get(item["account_id"])

    def update(self, account_id: str, values: dict, *, expected_version: int,
               actor: str, action: str = "updated") -> dict:
        allowed = {
            "display_name", "inbound_enabled", "outbound_enabled", "poll_seconds", "sender_name",
            "allowed_senders", "allowed_recipients", "status", "last_error_code",
        }
        unknown = set(values) - allowed
        if unknown:
            raise EmailAccountRepositoryError("email_account_invalid_update")
        columns: list[str] = []
        parameters: list[object] = []
        for key, value in values.items():
            if key in {"allowed_senders", "allowed_recipients"}:
                columns.append(f"{key}_json=?")
                parameters.append(json.dumps(value))
            else:
                columns.append(f"{key}=?")
                parameters.append(int(value) if key in {"inbound_enabled", "outbound_enabled"} else value)
        columns.extend(["config_version=config_version+1", "updated_at=?"])
        parameters.extend([utc_now(), account_id, expected_version])
        with self.tx() as cursor:
            cursor.execute(
                f"UPDATE ops_email_account SET {','.join(columns)} "
                "WHERE account_id=? AND config_version=? AND deleted_at IS NULL", parameters,
            )
            if cursor.rowcount != 1:
                if self.get(account_id) is None:
                    raise EmailAccountRepositoryError("email_account_not_found")
                raise EmailAccountRepositoryError("email_config_version_conflict")
            self._audit(cursor, account_id, actor, action, sorted(values))
        return self.get(account_id)

    def set_enabled(self, account_id: str, enabled: bool, *, actor: str) -> dict:
        status = "validating" if enabled else "disabled"
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_account SET status=?,config_version=config_version+1,updated_at=?
              WHERE account_id=? AND deleted_at IS NULL""", (status, utc_now(), account_id))
            if cursor.rowcount != 1:
                raise EmailAccountRepositoryError("email_account_not_found")
            self._audit(cursor, account_id, actor, "enabled" if enabled else "disabled", ["status"])
        return self.get(account_id)

    def record_health(self, account_id: str, status: str, error_code: str | None, *, actor: str) -> dict:
        now = utc_now()
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_account SET status=?,last_checked_at=?,last_error_code=?,updated_at=?
              WHERE account_id=? AND deleted_at IS NULL""", (status, now, error_code, now, account_id))
            if cursor.rowcount != 1:
                raise EmailAccountRepositoryError("email_account_not_found")
            self._audit(cursor, account_id, actor, "connection_test", ["status", "last_checked_at", "last_error_code"])
        return self.get(account_id)

    @staticmethod
    def _audit(cursor: sqlite3.Cursor, account_id: str, actor: str,
               action: str, changed_fields: list[str]) -> None:
        cursor.execute("""INSERT INTO ops_email_account_audit(account_id,actor,action,changed_fields_json,created_at)
          VALUES(?,?,?,?,?)""", (account_id, actor, action, json.dumps(changed_fields), utc_now()))

    def audit_rows(self, account_id: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ops_email_account_audit WHERE account_id=? ORDER BY audit_id", (account_id,)
        ).fetchall()]
