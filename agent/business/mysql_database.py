"""MySQL ``trade_ops`` repository for the foreign-trade workflow.

The repository exposes the existing fixed business methods while persisting data
in the normalized ``ops_*`` tables defined by
``agent/business/migrations/001_trade_ops_core.mysql.sql``. It intentionally exposes
no arbitrary-SQL MCP tool.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Optional

from agent.business.config import load_business_config
from agent.models.schemas import Product, Quote, QuoteItem, QuoteVersion, RfqFieldExtraction, RfqRequest, FollowupTask

_connection: Any = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20].upper()}"


def _require_driver():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError("MySQL driver is missing; install the pinned requirements before starting quote_business") from exc
    return pymysql, DictCursor


def get_connection():
    global _connection
    if _connection is None or not getattr(_connection, "open", False):
        _connection = create_connection()
    return _connection


def create_connection():
    """Create an independent PyMySQL connection for a repository/worker."""
    pymysql, dict_cursor = _require_driver()
    cfg = load_business_config()
    if cfg.database_backend != "mysql":
        raise RuntimeError("trade_ops requires database.backend=mysql")
    if not cfg.mysql_user or not cfg.mysql_password:
        raise RuntimeError("TRADE_OPS_MYSQL_USER and TRADE_OPS_MYSQL_PASSWORD are required")
    return pymysql.connect(
        host=cfg.mysql_host,
        port=cfg.mysql_port,
        user=cfg.mysql_user,
        password=cfg.mysql_password,
        database=cfg.mysql_database,
        charset="utf8mb4",
        cursorclass=dict_cursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )


@contextmanager
def tx() -> Iterator[Any]:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def close_all() -> None:
    global _connection
    if _connection is not None:
        try:
            _connection.close()
        finally:
            _connection = None


def init_db() -> None:
    """Verify the deployed migration; schema creation is an explicit deploy step."""
    required = {
        "ops_product", "ops_product_price_rule", "ops_inventory_snapshot",
        "ops_rfq_request", "ops_rfq_extraction_version", "ops_rfq_field_value",
        "ops_rfq_item", "ops_quote", "ops_quote_version", "ops_quote_line",
        "ops_approval_record", "ops_followup_task", "ops_fx_rate_snapshot",
    }
    cfg = load_business_config()
    with tx() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
            (cfg.mysql_database,),
        )
        actual = {row["table_name"] for row in cur.fetchall()}
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError("trade_ops migration is incomplete; missing tables: " + ", ".join(missing))


def _as_float(value: Any) -> float:
    return float(value or 0)


def _row_to_product(row: dict[str, Any]) -> Product:
    return Product(
        id=int(row["product_key"]), sku=row["sku"], name_cn=row["name_cn"], name_en=row["name_en"],
        category=row["category_code"], specification=row.get("specification_text") or "",
        unit=row["quantity_unit"], moq=int(row["moq"]), price_usd=_as_float(row.get("unit_price")),
        weight_kg=0, package_info="", inventory=int(row.get("available_qty") or 0),
        lead_time_days=int(row["lead_time_days"]), active=row["sale_status"] == "active",
    )


_PRODUCT_SELECT = """
SELECT p.*, pr.unit_price, inv.available_qty
FROM ops_product p
LEFT JOIN ops_product_price_rule pr ON pr.price_rule_key=(
  SELECT pr2.price_rule_key FROM ops_product_price_rule pr2
  WHERE pr2.sku=p.sku AND pr2.currency_code='USD'
    AND pr2.valid_from<=UTC_TIMESTAMP(6) AND (pr2.valid_to IS NULL OR pr2.valid_to>UTC_TIMESTAMP(6))
  ORDER BY pr2.min_qty ASC, pr2.valid_from DESC LIMIT 1)
LEFT JOIN ops_inventory_snapshot inv ON inv.inventory_snapshot_key=(
  SELECT i2.inventory_snapshot_key FROM ops_inventory_snapshot i2
  WHERE i2.sku=p.sku ORDER BY i2.snapshot_at DESC LIMIT 1)
"""


def list_products(category: str = "", keyword: str = "", limit: int = 20, offset: int = 0) -> list[Product]:
    conditions = ["p.sale_status='active'"]
    params: list[Any] = []
    if category:
        conditions.append("p.category_code=%s")
        params.append(category)
    if keyword:
        conditions.append("(p.name_en LIKE %s OR p.name_cn LIKE %s OR p.sku LIKE %s OR p.specification_text LIKE %s)")
        params.extend([f"%{keyword}%"] * 4)
    with tx() as cur:
        cur.execute(_PRODUCT_SELECT + " WHERE " + " AND ".join(conditions) + " ORDER BY p.sku LIMIT %s OFFSET %s", params + [limit, offset])
        return [_row_to_product(row) for row in cur.fetchall()]


def get_product_by_sku(sku: str) -> Optional[Product]:
    with tx() as cur:
        cur.execute(_PRODUCT_SELECT + " WHERE p.sku=%s", (sku,))
        row = cur.fetchone()
    return _row_to_product(row) if row else None


def get_product_by_id(pid: int) -> Optional[Product]:
    with tx() as cur:
        cur.execute(_PRODUCT_SELECT + " WHERE p.product_key=%s", (pid,))
        row = cur.fetchone()
    return _row_to_product(row) if row else None


def check_inventory(sku: str, quantity: int) -> dict[str, Any]:
    product = get_product_by_sku(sku)
    if not product:
        return {"available": False, "reason": f"SKU '{sku}' 不存在", "inventory": 0, "moq": 0}
    if Decimal(str(quantity)) < Decimal(str(product.moq)):
        return {"available": False, "reason": f"数量 {quantity} 小于最小起订量 {product.moq}", "inventory": product.inventory, "moq": product.moq}
    available = product.inventory >= quantity
    return {"available": available, "reason": "库存充足" if available else f"库存不足（需求 {quantity}，可用 {product.inventory}）", "inventory": product.inventory, "moq": product.moq}


def create_rfq(session_key: str, raw_text: str) -> int:
    source_hash = hashlib.sha256(raw_text.strip().encode("utf-8")).hexdigest()
    rfq_id = _new_id("RFQ")
    with tx() as cur:
        try:
            cur.execute(
                "INSERT INTO ops_rfq_request (rfq_id,source_channel,source_message_id,source_hash,received_at,status,current_extraction_version) VALUES (%s,%s,%s,%s,%s,'received',0)",
                (rfq_id, session_key.split(":", 1)[0] or "unknown", session_key, source_hash, _utc_now()),
            )
            return int(cur.lastrowid)
        except Exception as exc:
            if exc.__class__.__name__ != "IntegrityError":
                raise
            cur.execute("SELECT rfq_key FROM ops_rfq_request WHERE source_channel=%s AND source_hash=%s", (session_key.split(":", 1)[0] or "unknown", source_hash))
            row = cur.fetchone()
            if not row:
                raise
            return int(row["rfq_key"])


def update_rfq_extraction(rfq_id: int, extracted: RfqFieldExtraction) -> None:
    with tx() as cur:
        cur.execute("SELECT rfq_id,current_extraction_version FROM ops_rfq_request WHERE rfq_key=%s FOR UPDATE", (rfq_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"RFQ #{rfq_id} does not exist")
        version = int(row["current_extraction_version"]) + 1
        cur.execute("INSERT INTO ops_rfq_extraction_version (rfq_id,version_no,extractor_type,model_name,created_by,created_at,change_reason) VALUES (%s,%s,'llm',%s,'agent',%s,'initial extraction')", (row["rfq_id"], version, load_business_config().llm_model, _utc_now()))
        values = extracted.__dict__.copy()
        missing = set(values.pop("missing_fields", []))
        for name, value in values.items():
            status = "pending_confirmation" if name in missing or value in ("", 0, None) else "extracted"
            number = Decimal(str(value)) if isinstance(value, (int, float)) and value else None
            text = None if number is not None or value in ("", None) else str(value)
            cur.execute("INSERT INTO ops_rfq_field_value (rfq_id,version_no,field_name,value_text,value_number,field_status) VALUES (%s,%s,%s,%s,%s,%s)", (row["rfq_id"], version, name, text, number, status))
        cur.execute("INSERT INTO ops_rfq_item (rfq_id,version_no,item_no,raw_product_text,quantity,quantity_unit) VALUES (%s,%s,1,%s,%s,%s)", (row["rfq_id"], version, extracted.product_description or "待确认", extracted.quantity or None, extracted.unit or None))
        next_status = "needs_clarification" if missing else "extracted"
        cur.execute("UPDATE ops_rfq_request SET current_extraction_version=%s,status=%s WHERE rfq_key=%s", (version, next_status, rfq_id))


def get_rfq(rfq_id: int) -> Optional[RfqRequest]:
    with tx() as cur:
        cur.execute("SELECT * FROM ops_rfq_request WHERE rfq_key=%s", (rfq_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT field_name,value_text,value_number,field_status FROM ops_rfq_field_value WHERE rfq_id=%s AND version_no=%s", (row["rfq_id"], row["current_extraction_version"]))
        fields = cur.fetchall()
    data: dict[str, Any] = {"missing_fields": []}
    for field in fields:
        data[field["field_name"]] = field["value_number"] if field["value_number"] is not None else (field["value_text"] or "")
        if field["field_status"] == "pending_confirmation":
            data["missing_fields"].append(field["field_name"])
    extracted = RfqFieldExtraction(**{k: data[k] for k in RfqFieldExtraction.__dataclass_fields__ if k in data})
    return RfqRequest(id=int(row["rfq_key"]), session_key=row.get("source_message_id") or "", raw_text="[encrypted source retained outside repository response]", extracted=extracted, status=row["status"], created_at=str(row["received_at"]))


def create_quote(rfq_id: int, customer_name: str, customer_company: str, country: str, rfq_preview: str) -> int:
    del customer_name, customer_company, country, rfq_preview
    quote_id = _new_id("Q")
    with tx() as cur:
        cur.execute("SELECT rfq_id,customer_id FROM ops_rfq_request WHERE rfq_key=%s", (rfq_id,))
        rfq = cur.fetchone()
        if not rfq:
            raise ValueError(f"RFQ #{rfq_id} does not exist")
        cur.execute("INSERT INTO ops_quote (quote_id,rfq_id,customer_id,current_version_no,status,created_at) VALUES (%s,%s,%s,0,'draft',%s)", (quote_id, rfq["rfq_id"], rfq["customer_id"], _utc_now()))
        return int(cur.lastrowid)


def add_quote_version(quote_id: int, version: QuoteVersion) -> int:
    payload = json.dumps(version.__dict__, ensure_ascii=False, default=lambda obj: obj.__dict__, sort_keys=True)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    calculation_hash = hashlib.sha256((payload + "|calculation").encode("utf-8")).hexdigest()
    calculation_id = _new_id("CALC")
    with tx() as cur:
        cur.execute("SELECT quote_id,current_version_no FROM ops_quote WHERE quote_key=%s FOR UPDATE", (quote_id,))
        quote = cur.fetchone()
        if not quote:
            return 0
        new_version = int(quote["current_version_no"]) + 1
        cur.execute("INSERT INTO ops_quote_version (quote_id,version_no,calculation_id,subtotal_amount,discount_amount,packaging_amount,freight_amount,total_amount,currency_code,valid_until,content_hash,calculation_hash,created_by,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'USD',%s,%s,%s,%s,%s)", (quote["quote_id"], new_version, calculation_id, Decimal(str(version.subtotal_usd)), Decimal(str(version.discount_amount)), Decimal(str(version.packaging_cost_usd)), Decimal(str(version.freight_cost_usd)), Decimal(str(version.total_usd)), version.valid_until, content_hash, calculation_hash, version.created_by, _utc_now()))
        for line_no, item in enumerate(version.items, 1):
            product = get_product_by_sku(item.product_sku)
            cur.execute("INSERT INTO ops_quote_line (quote_id,version_no,line_no,sku,quantity,unit_price,discount_rate,line_amount,lead_time_days) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (quote["quote_id"], new_version, line_no, item.product_sku, Decimal(str(item.quantity)), Decimal(str(item.unit_price_usd)), Decimal(str(version.discount_percent)), Decimal(str(item.total_price_usd)), product.lead_time_days if product else 0))
        cur.execute("UPDATE ops_quote SET current_version_no=%s,status='draft' WHERE quote_key=%s", (new_version, quote_id))
    version.version = new_version
    return new_version


def update_quote_status(quote_id: int, status: str) -> None:
    with tx() as cur:
        cur.execute("UPDATE ops_quote SET status=%s WHERE quote_key=%s", (status, quote_id))


def get_quote(quote_id: int) -> Optional[Quote]:
    with tx() as cur:
        cur.execute("SELECT * FROM ops_quote WHERE quote_key=%s", (quote_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT * FROM ops_quote_version WHERE quote_id=%s ORDER BY version_no", (row["quote_id"],))
        versions = [QuoteVersion(version=int(v["version_no"]), subtotal_usd=_as_float(v["subtotal_amount"]), discount_amount=_as_float(v["discount_amount"]), packaging_cost_usd=_as_float(v["packaging_amount"]), freight_cost_usd=_as_float(v["freight_amount"]), total_usd=_as_float(v["total_amount"]), valid_until=str(v["valid_until"]), created_by=v["created_by"], created_at=str(v["created_at"])) for v in cur.fetchall()]
    return Quote(id=int(row["quote_key"]), rfq_id=0, status=row["status"], current_version=int(row["current_version_no"]), versions=versions, created_at=str(row["created_at"]))


def list_quotes_by_session(session_key: str, limit: int = 10) -> list[Quote]:
    with tx() as cur:
        cur.execute("SELECT q.quote_key FROM ops_quote q JOIN ops_rfq_request r ON r.rfq_id=q.rfq_id WHERE r.source_message_id=%s ORDER BY q.created_at DESC LIMIT %s", (session_key, limit))
        ids = [int(row["quote_key"]) for row in cur.fetchall()]
    return [quote for quote_id in ids if (quote := get_quote(quote_id))]


def create_followup(rfq_id: int, quote_id: int, task_type: str, title: str, description: str, due_at: str) -> int:
    del rfq_id, title, description
    with tx() as cur:
        cur.execute("SELECT q.rfq_id,q.quote_id,q.current_version_no FROM ops_quote q WHERE q.quote_key=%s", (quote_id,))
        quote = cur.fetchone()
        if not quote:
            raise ValueError(f"Quote #{quote_id} does not exist")
        cur.execute("INSERT INTO ops_followup_task (rfq_id,quote_id,quote_version_no,task_type,assignee_user_id,due_at,priority,status) VALUES (%s,%s,%s,%s,'unassigned',%s,'normal','pending')", (quote["rfq_id"], quote["quote_id"], quote["current_version_no"], task_type, due_at))
        return int(cur.lastrowid)


def list_pending_followups(limit: int = 20) -> list[FollowupTask]:
    with tx() as cur:
        cur.execute("SELECT * FROM ops_followup_task WHERE status='pending' ORDER BY due_at LIMIT %s", (limit,))
        rows = cur.fetchall()
    return [FollowupTask(id=int(r["followup_key"]), quote_id=0, task_type=r["task_type"], due_at=str(r["due_at"]), status=r["status"]) for r in rows]


def create_approval(quote_id: int, version: int) -> int:
    with tx() as cur:
        cur.execute("SELECT q.quote_id,v.content_hash,v.calculation_hash FROM ops_quote q JOIN ops_quote_version v ON v.quote_id=q.quote_id AND v.version_no=%s WHERE q.quote_key=%s AND q.current_version_no=%s FOR UPDATE", (version, quote_id, version))
        row = cur.fetchone()
        if not row:
            raise ValueError("approval must target the current immutable quote version")
        cur.execute("INSERT INTO ops_approval_record (quote_id,version_no,action,approval_status,required_role,content_hash,calculation_hash,acted_at) VALUES (%s,%s,'request','pending','quote_reviewer',%s,%s,%s)", (row["quote_id"], version, row["content_hash"], row["calculation_hash"], _utc_now()))
        return int(cur.lastrowid)


def approve(approval_id: int, reviewer: str, comment: str = "", approved: bool = True) -> None:
    status = "approved" if approved else "rejected"
    action = "approve" if approved else "reject"
    with tx() as cur:
        cur.execute("SELECT a.*,q.quote_key,q.current_version_no,v.content_hash AS current_content_hash,v.calculation_hash AS current_calculation_hash FROM ops_approval_record a JOIN ops_quote q ON q.quote_id=a.quote_id JOIN ops_quote_version v ON v.quote_id=a.quote_id AND v.version_no=a.version_no WHERE a.approval_key=%s FOR UPDATE", (approval_id,))
        row = cur.fetchone()
        if not row or row["approval_status"] != "pending":
            raise ValueError("pending approval not found")
        if int(row["version_no"]) != int(row["current_version_no"]) or row["content_hash"] != row["current_content_hash"] or row["calculation_hash"] != row["current_calculation_hash"]:
            raise ValueError("quote version or hash changed; submit a new approval")
        cur.execute("UPDATE ops_approval_record SET action=%s,approval_status=%s,reviewer_user_id=%s,comment=%s,acted_at=%s WHERE approval_key=%s", (action, status, reviewer, comment, _utc_now(), approval_id))
        cur.execute("UPDATE ops_quote SET status=%s WHERE quote_key=%s", (status, row["quote_key"]))


def upsert_exchange_rate(from_cur: str, to_cur: str, rate: float) -> None:
    with tx() as cur:
        cur.execute("INSERT INTO ops_fx_rate_snapshot (from_currency,to_currency,rate,source_code,quoted_at) VALUES (%s,%s,%s,'seed',%s)", (from_cur.upper(), to_cur.upper(), Decimal(str(rate)), _utc_now()))


def get_exchange_rate(from_cur: str, to_cur: str) -> Optional[float]:
    if from_cur.upper() == to_cur.upper():
        return 1.0
    with tx() as cur:
        cur.execute("SELECT rate FROM ops_fx_rate_snapshot WHERE from_currency=%s AND to_currency=%s AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP(6)) ORDER BY quoted_at DESC LIMIT 1", (from_cur.upper(), to_cur.upper()))
        row = cur.fetchone()
    return _as_float(row["rate"]) if row else None
