"""Allowlisted, parameterized analytics over the current operational demo store.

This is the M4 MVP. It deliberately exposes no arbitrary SQL or NL2SQL surface;
the future trade_dw path remains disabled until its schema and metadata contracts
are deployed and approved.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.business.config import load_business_config


MAX_ROWS = 50
MAX_RESULT_BYTES = 64 * 1024
_AUDIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class QuerySpec:
    code: str
    description: str
    sqlite_sql: str
    mysql_sql: str
    allowed_filters: tuple[str, ...]
    columns: tuple[str, ...]


SPECS = {
    "inventory_top": QuerySpec(
        "inventory_top", "Active products ordered by available inventory",
        "SELECT sku,name_en,category,inventory,moq FROM products WHERE active=1 AND (?='' OR category=?) ORDER BY inventory DESC,sku LIMIT ?",
        "SELECT p.sku,p.name_en,p.category_code AS category,COALESCE(inv.available_qty,0) AS inventory,p.moq FROM ops_product p LEFT JOIN ops_inventory_snapshot inv ON inv.inventory_snapshot_key=(SELECT i2.inventory_snapshot_key FROM ops_inventory_snapshot i2 WHERE i2.sku=p.sku ORDER BY i2.snapshot_at DESC LIMIT 1) WHERE p.sale_status='active' AND (%s='' OR p.category_code=%s) ORDER BY inventory DESC,p.sku LIMIT %s",
        ("category", "limit"), ("sku", "name_en", "category", "inventory", "moq"),
    ),
    "quote_status_summary": QuerySpec(
        "quote_status_summary", "Quote counts grouped by status",
        "SELECT status,COUNT(*) AS quote_count FROM quotes GROUP BY status ORDER BY quote_count DESC,status LIMIT ?",
        "SELECT status,COUNT(*) AS quote_count FROM ops_quote GROUP BY status ORDER BY quote_count DESC,status LIMIT %s",
        ("limit",), ("status", "quote_count"),
    ),
    "rfq_status_summary": QuerySpec(
        "rfq_status_summary", "RFQ counts grouped by status",
        "SELECT status,COUNT(*) AS rfq_count FROM rfq_requests GROUP BY status ORDER BY rfq_count DESC,status LIMIT ?",
        "SELECT status,COUNT(*) AS rfq_count FROM ops_rfq_request GROUP BY status ORDER BY rfq_count DESC,status LIMIT %s",
        ("limit",), ("status", "rfq_count"),
    ),
    "pending_followups": QuerySpec(
        "pending_followups", "Pending follow-up counts grouped by type",
        "SELECT task_type,COUNT(*) AS task_count FROM followup_tasks WHERE status='pending' GROUP BY task_type ORDER BY task_count DESC,task_type LIMIT ?",
        "SELECT task_type,COUNT(*) AS task_count FROM ops_followup_task WHERE status='pending' GROUP BY task_type ORDER BY task_count DESC,task_type LIMIT %s",
        ("limit",), ("task_type", "task_count"),
    ),
}


class AnalyticsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def list_query_contracts() -> list[dict[str, Any]]:
    return [{"query_code": spec.code, "description": spec.description,
             "allowed_filters": list(spec.allowed_filters), "columns": list(spec.columns)}
            for spec in SPECS.values()]


def _validate(query_code: str, filters: dict[str, Any]) -> tuple[QuerySpec, int]:
    spec = SPECS.get(query_code)
    if spec is None:
        raise AnalyticsError("query_not_allowlisted")
    unknown = set(filters) - set(spec.allowed_filters)
    if unknown:
        raise AnalyticsError("filter_not_allowlisted")
    try:
        limit = int(filters.get("limit", 20))
    except (TypeError, ValueError):
        raise AnalyticsError("invalid_limit")
    if limit < 1 or limit > MAX_ROWS:
        raise AnalyticsError("invalid_limit")
    category = filters.get("category", "")
    if not isinstance(category, str) or len(category) > 64:
        raise AnalyticsError("invalid_category")
    return spec, limit


def _query(spec: QuerySpec, filters: dict[str, Any], limit: int) -> tuple[list[dict[str, Any]], str, str]:
    cfg = load_business_config()
    if cfg.database_backend == "mysql":
        from agent.business import mysql_database as backend
        sql = spec.mysql_sql
        params = ([filters.get("category", "")] * 2 + [limit]) if spec.code == "inventory_top" else [limit]
    else:
        from agent.business import sqlite_database as backend
        sql = spec.sqlite_sql
        params = ([filters.get("category", "")] * 2 + [limit]) if spec.code == "inventory_top" else [limit]
    with backend.tx() as cursor:
        cursor.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
    return rows, sql, cfg.database_backend


def _audit(query_code: str, sql_hash: str, backend: str, row_count: int,
           audit_dir: str | Path = "workspace/analytics") -> str:
    query_id = str(uuid.uuid4())
    record = {"query_id": query_id, "query_code": query_code, "sql_hash": sql_hash,
              "backend": backend, "row_count": row_count,
              "executed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    root = Path(audit_dir)
    root.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK, (root / "query_audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    return query_id


def execute_fixed_query(query_code: str, filters: dict[str, Any] | None = None,
                        *, audit_dir: str | Path = "workspace/analytics") -> dict[str, Any]:
    filters = dict(filters or {})
    spec, limit = _validate(query_code, filters)
    rows, sql, backend = _query(spec, filters, limit)
    sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    payload = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        raise AnalyticsError("result_too_large")
    query_id = _audit(query_code, sql_hash, backend, len(rows), audit_dir)
    return {"query_id": query_id, "query_code": query_code, "backend": backend,
            "source": "trade_ops_demo", "sql_hash": sql_hash, "row_count": len(rows),
            "columns": list(spec.columns), "rows": rows,
            "limitations": ["not_trade_dw", "fixed_query_only"]}


def execute_arbitrary_sql(_: str) -> None:
    """Explicit guard used by tests and callers tempted to bypass the contract."""
    raise AnalyticsError("arbitrary_sql_disabled")
