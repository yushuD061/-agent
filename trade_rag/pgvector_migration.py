from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_rag_vector_config


_MIGRATION = Path(__file__).with_name("migrations") / "001_pgvector.sql"


def _connect(config):
    import psycopg
    return psycopg.connect(**config.connection_kwargs)


def _assert_m2_target(config) -> None:
    config.validate()
    if config.backend != "pgvector":
        raise ValueError("RAG_VECTOR_BACKEND=pgvector is required")
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("M2 migration only permits a loopback PostgreSQL host")
    if config.database != "nanoclaw_vector_docker":
        raise ValueError("M2 migration only permits nanoclaw_vector_docker")


def check(config) -> dict[str, object]:
    _assert_m2_target(config)
    with _connect(config) as connection:
        extension = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        column = connection.execute(
            """SELECT format_type(a.atttypid, a.atttypmod)
                 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'rag_child_vector' AND a.attname = 'embedding'
                  AND NOT a.attisdropped"""
        ).fetchone()
        version = connection.execute(
            """SELECT version FROM rag_schema_migration WHERE version = 1"""
        ).fetchone() if column else None
    ready = bool(extension and column and column[0] == "vector(64)" and version)
    return {
        "database": config.database,
        "extension_version": extension[0] if extension else None,
        "vector_type": column[0] if column else None,
        "migration_version": version[0] if version else None,
        "ready": ready,
    }


def apply(config) -> dict[str, object]:
    _assert_m2_target(config)
    with _connect(config) as connection:
        connection.execute(_MIGRATION.read_text(encoding="utf-8"))
    return check(config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the isolated M2 pgvector schema")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load_rag_vector_config()
    result = apply(config) if args.apply else check(config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
