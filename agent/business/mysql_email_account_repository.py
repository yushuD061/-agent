"""MySQL implementation of the M1 mailbox-account repository contract."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from agent.business.email_account_repository import EmailAccountRepositoryError
from agent.business.mysql_database import create_connection


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySQLEmailAccountRepository:
    def __init__(self, connection=None, *, verify_schema: bool = True):
        self.connection = connection or create_connection()
        self._lock = threading.RLock()
        if verify_schema:
            self.verify_schema()

    @contextmanager
    def tx(self):
        with self._lock:
            cursor = self.connection.cursor()
            try:
                yield cursor
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def verify_schema(self) -> None:
        with self.tx() as cursor:
            cursor.execute("""SELECT table_name FROM information_schema.tables
              WHERE table_schema=DATABASE() AND table_name IN
              ('ops_email_account','ops_email_account_audit')""")
            actual = {row["table_name"] for row in cursor.fetchall()}
        missing = {"ops_email_account", "ops_email_account_audit"} - actual
        if missing:
            raise EmailAccountRepositoryError(
                "email_account_migration_incomplete:" + ",".join(sorted(missing))
            )

    @staticmethod
    def _decode(row: dict | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["inbound_enabled"] = bool(item["inbound_enabled"])
        item["outbound_enabled"] = bool(item["outbound_enabled"])
        for key in ("allowed_senders_json", "allowed_recipients_json"):
            value = item.pop(key)
            item[key.removesuffix("_json")] = json.loads(value) if isinstance(value, str) else list(value or [])
        for key in ("last_checked_at", "created_at", "updated_at", "deleted_at"):
            if isinstance(item.get(key), datetime):
                item[key] = item[key].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return item

    def list_active(self) -> list[dict]:
        with self.tx() as cursor:
            cursor.execute("SELECT * FROM ops_email_account WHERE deleted_at IS NULL ORDER BY created_at,account_id")
            return [self._decode(row) for row in cursor.fetchall()]

    def get(self, account_id: str) -> dict | None:
        with self.tx() as cursor:
            cursor.execute("SELECT * FROM ops_email_account WHERE account_id=%s AND deleted_at IS NULL", (account_id,))
            return self._decode(cursor.fetchone())

    def find_by_provider_address(self, provider: str, address: str) -> dict | None:
        with self.tx() as cursor:
            cursor.execute("""SELECT * FROM ops_email_account
              WHERE provider=%s AND address=%s AND deleted_at IS NULL""", (provider, address))
            return self._decode(cursor.fetchone())

    def create(self, item: dict, *, actor: str, action: str = "created") -> dict:
        now = _utc_now()
        with self.tx() as cursor:
            try:
                cursor.execute("""INSERT INTO ops_email_account(
                  account_id,display_name,provider,address,secret_ref,folder,inbound_enabled,outbound_enabled,
                  poll_seconds,sender_name,allowed_senders_json,allowed_recipients_json,status,
                  config_version,created_at,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s)""", (
                    item["account_id"], item["display_name"], item["provider"], item["address"],
                    item["secret_ref"], item.get("folder", "INBOX"), bool(item["inbound_enabled"]),
                    bool(item["outbound_enabled"]), item["poll_seconds"], item["sender_name"],
                    json.dumps(item["allowed_senders"]), json.dumps(item["allowed_recipients"]),
                    item.get("status", "disabled"), now, now,
                ))
            except Exception as exc:
                if getattr(exc, "args", [None])[0] == 1062:
                    raise EmailAccountRepositoryError("email_account_already_exists") from exc
                raise
            self._audit(cursor, item["account_id"], actor, action,
                        [key for key in item if key != "secret_ref"])
        return self.get(item["account_id"])

    def update(self, account_id: str, values: dict, *, expected_version: int,
               actor: str, action: str = "updated") -> dict:
        allowed = {
            "display_name", "inbound_enabled", "outbound_enabled", "poll_seconds", "sender_name",
            "allowed_senders", "allowed_recipients", "status", "last_error_code",
        }
        if set(values) - allowed:
            raise EmailAccountRepositoryError("email_account_invalid_update")
        columns, parameters = [], []
        for key, value in values.items():
            if key in {"allowed_senders", "allowed_recipients"}:
                columns.append(f"{key}_json=%s")
                parameters.append(json.dumps(value))
            else:
                columns.append(f"{key}=%s")
                parameters.append(bool(value) if key in {"inbound_enabled", "outbound_enabled"} else value)
        columns.extend(["config_version=config_version+1", "updated_at=%s"])
        parameters.extend([_utc_now(), account_id, expected_version])
        with self.tx() as cursor:
            cursor.execute(
                f"UPDATE ops_email_account SET {','.join(columns)} "
                "WHERE account_id=%s AND config_version=%s AND deleted_at IS NULL", parameters,
            )
            if cursor.rowcount != 1:
                cursor.execute("SELECT account_id FROM ops_email_account WHERE account_id=%s AND deleted_at IS NULL", (account_id,))
                if cursor.fetchone() is None:
                    raise EmailAccountRepositoryError("email_account_not_found")
                raise EmailAccountRepositoryError("email_config_version_conflict")
            self._audit(cursor, account_id, actor, action, sorted(values))
        return self.get(account_id)

    def set_enabled(self, account_id: str, enabled: bool, *, actor: str) -> dict:
        status = "validating" if enabled else "disabled"
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_account SET status=%s,config_version=config_version+1,updated_at=%s
              WHERE account_id=%s AND deleted_at IS NULL""", (status, _utc_now(), account_id))
            if cursor.rowcount != 1:
                raise EmailAccountRepositoryError("email_account_not_found")
            self._audit(cursor, account_id, actor, "enabled" if enabled else "disabled", ["status"])
        return self.get(account_id)

    def record_health(self, account_id: str, status: str, error_code: str | None, *, actor: str) -> dict:
        now = _utc_now()
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_account SET status=%s,last_checked_at=%s,last_error_code=%s,updated_at=%s
              WHERE account_id=%s AND deleted_at IS NULL""", (status, now, error_code, now, account_id))
            if cursor.rowcount != 1:
                raise EmailAccountRepositoryError("email_account_not_found")
            self._audit(cursor, account_id, actor, "connection_test",
                        ["status", "last_checked_at", "last_error_code"])
        return self.get(account_id)

    @staticmethod
    def _audit(cursor, account_id: str, actor: str, action: str, changed_fields: list[str]) -> None:
        cursor.execute("""INSERT INTO ops_email_account_audit(
          account_id,actor,action,changed_fields_json,created_at) VALUES(%s,%s,%s,%s,%s)""",
                       (account_id, actor, action, json.dumps(changed_fields), _utc_now()))

    def audit_rows(self, account_id: str) -> list[dict]:
        with self.tx() as cursor:
            cursor.execute("SELECT * FROM ops_email_account_audit WHERE account_id=%s ORDER BY audit_id", (account_id,))
            return [dict(row) for row in cursor.fetchall()]
