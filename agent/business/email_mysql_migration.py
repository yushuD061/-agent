"""Explicit migration/check/rollback utility for M6 email delivery tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.business.config import load_business_config
from agent.business.mysql_database import get_connection


_MIGRATION_DIR = Path(__file__).with_name("migrations")
_UP = _MIGRATION_DIR / "004_email_delivery.mysql.sql"
_DOWN = _MIGRATION_DIR / "004_email_delivery.rollback.mysql.sql"


def _statements(path: Path) -> list[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return [item.strip() for item in "\n".join(lines).split(";") if item.strip()]


def _execute(path: Path) -> None:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        for statement in _statements(path):
            cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def check() -> dict:
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("""SELECT table_name FROM information_schema.tables
          WHERE table_schema=DATABASE() AND table_name IN
          ('ops_email_account','ops_email_account_audit','ops_email_delivery','ops_email_delivery_audit')""")
        actual = {row["table_name"] for row in cursor.fetchall()}
        required = {"ops_email_account", "ops_email_account_audit", "ops_email_delivery", "ops_email_delivery_audit"}
        cursor.execute("""SELECT column_name FROM information_schema.columns
          WHERE table_schema=DATABASE() AND table_name='ops_email_delivery'""")
        columns = {row["column_name"] for row in cursor.fetchall()}
        required_columns = {"idempotency_key", "lease_owner", "lease_until", "snapshot_hash", "content_redacted_at"}
        return {
            "database": load_business_config().mysql_database,
            "missing_tables": sorted(required - actual),
            "missing_columns": sorted(required_columns - columns),
            "ready": not (required - actual) and not (required_columns - columns),
        }
    finally:
        cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage M6 MySQL email delivery migration")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--apply-004", action="store_true")
    actions.add_argument("--rollback-004", action="store_true")
    parser.add_argument("--confirm-drop-email-delivery", action="store_true",
                        help="required acknowledgement for destructive rollback")
    args = parser.parse_args()
    if load_business_config().database_backend != "mysql":
        parser.error("BUSINESS_DATABASE_BACKEND=mysql is required")
    if args.apply_004:
        _execute(_UP)
    elif args.rollback_004:
        if not args.confirm_drop_email_delivery:
            parser.error("--confirm-drop-email-delivery is required for rollback")
        _execute(_DOWN)
    result = check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if (result["ready"] if not args.rollback_004 else not result["ready"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
