"""Explicit check/apply command for the task-runtime MySQL migration."""
from __future__ import annotations

import argparse
from pathlib import Path

from agent.business.mysql_database import create_connection
from agent.business.mysql_task_runtime_repository import MySQLTaskRuntimeRepository


def check() -> dict:
    repository = MySQLTaskRuntimeRepository()
    repository.close()
    return {"backend": "mysql", "status": "ready", "migration": "007_task_runtime"}


def apply() -> dict:
    connection = create_connection()
    script = (Path(__file__).with_name("migrations") / "007_task_runtime.mysql.sql").read_text("utf-8")
    statements = [statement.strip() for statement in script.split(";") if statement.strip()]
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return check()


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(check() if args.check else apply())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
