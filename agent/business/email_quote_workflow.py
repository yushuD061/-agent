"""Deterministic bridge from confirmed inbound RFQs to quote approval and delivery."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from agent.business.config import load_business_config
from agent.business.email_delivery_repository import canonical_quote_hash
from agent.business.email_repository import EmailRepository
from agent.business.email_review_service import EmailReviewGate
from agent.tools.calculate_quote import _calc_discount


class EmailQuoteWorkflowError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mask_email(value: str) -> str:
    local, separator, domain = str(value).partition("@")
    return f"{local[:1]}***@{domain}" if separator and domain else "***"


def _words(value: str) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9]+", value.casefold()) if len(item) > 1}


class EmailQuoteWorkflowService:
    """Creates one immutable quote approval request per confirmed email review."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.email_repository = EmailRepository(connection)
        self.init_schema()

    def init_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT,sku TEXT UNIQUE,name_cn TEXT,name_en TEXT,
          category TEXT,specification TEXT,unit TEXT,moq INTEGER,price_usd REAL,
          inventory INTEGER,lead_time_days INTEGER,active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS rfq_requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,session_key TEXT,raw_text TEXT,
          extracted_json TEXT DEFAULT '{}',status TEXT DEFAULT 'pending',created_at TEXT,updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS quotes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,rfq_id INTEGER,status TEXT DEFAULT 'draft',
          current_version INTEGER DEFAULT 0,version_data TEXT DEFAULT '[]',created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS approval_records(
          id INTEGER PRIMARY KEY AUTOINCREMENT,quote_id INTEGER,version INTEGER,status TEXT DEFAULT 'pending',
          reviewer TEXT,comment TEXT,content_hash TEXT,created_at TEXT,decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ops_email_quote_workflow(
          email_id INTEGER PRIMARY KEY,review_id TEXT,review_hash TEXT,rfq_id INTEGER,quote_id INTEGER,
          quote_version INTEGER,approval_key INTEGER,status TEXT NOT NULL,error_code TEXT,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uk_email_quote_workflow_quote
          ON ops_email_quote_workflow(quote_id) WHERE quote_id IS NOT NULL;
        """)
        self.connection.commit()

    @staticmethod
    def _node_value(node: dict | None, key: str = "value"):
        return (node or {}).get(key)

    def _match_product(self, product_text: str, specification: str) -> sqlite3.Row:
        products = self.connection.execute(
            "SELECT * FROM products WHERE active=1 ORDER BY sku"
        ).fetchall()
        query = f"{product_text} {specification}".strip()
        query_words = _words(query)
        ranked: list[tuple[int, sqlite3.Row]] = []
        for product in products:
            sku = str(product["sku"] or "")
            name = str(product["name_en"] or "")
            haystack = " ".join(str(product[key] or "") for key in (
                "sku", "name_en", "name_cn", "specification"
            ))
            score = len(query_words & _words(haystack))
            if sku and sku.casefold() in query.casefold():
                score += 100
            if name and name.casefold() in query.casefold():
                score += 20
            ranked.append((score, product))
        ranked.sort(key=lambda item: (item[0], str(item[1]["sku"])), reverse=True)
        if not ranked or ranked[0][0] < 2 or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            raise EmailQuoteWorkflowError("email_quote_product_match_required", 422)
        return ranked[0][1]

    def _build_quote(self, snapshot: dict) -> tuple[dict, dict]:
        if snapshot.get("missing_fields"):
            raise EmailQuoteWorkflowError("email_quote_pending_fields", 422)
        raw_items = snapshot.get("items") or []
        if not raw_items:
            raise EmailQuoteWorkflowError("email_quote_items_required", 422)
        term = snapshot.get("trade_term") or {}
        incoterm = str(term.get("incoterm") or "").upper()
        place = str(term.get("named_place") or "").strip()
        if incoterm not in {"EXW", "FOB"}:
            raise EmailQuoteWorkflowError("email_quote_freight_required", 422)
        cfg = load_business_config()
        quote_items = []
        subtotal = 0.0
        total_discount = 0.0
        extraction_items = []
        for item in raw_items:
            product_text = str(self._node_value(item.get("product")) or "").strip()
            specification = str(self._node_value(item.get("specification")) or "").strip()
            quantity_node = item.get("quantity") or {}
            try:
                quantity = int(quantity_node.get("value"))
            except (TypeError, ValueError) as exc:
                raise EmailQuoteWorkflowError("email_quote_quantity_invalid", 422) from exc
            product = self._match_product(product_text, specification)
            if quantity < int(product["moq"] or 1):
                raise EmailQuoteWorkflowError("email_quote_below_moq", 422)
            if quantity > int(product["inventory"] or 0):
                raise EmailQuoteWorkflowError("email_quote_inventory_insufficient", 422)
            base_price = float(product["price_usd"] or 0)
            if base_price <= 0:
                raise EmailQuoteWorkflowError("email_quote_price_missing", 422)
            unit_price = round(base_price * (1 + cfg.default_markup_percent / 100), 2)
            line_subtotal = round(unit_price * quantity, 2)
            discount_percent, discount_amount = _calc_discount(quantity, unit_price)
            subtotal = round(subtotal + line_subtotal, 2)
            total_discount = round(total_discount + discount_amount, 2)
            quote_items.append({
                "product_sku": product["sku"], "product_name_en": product["name_en"],
                "quantity": quantity, "unit_price_usd": unit_price,
                "total_price_usd": line_subtotal, "moq_note": f"MOQ: {int(product['moq'] or 1)}",
            })
            extraction_items.append({
                "product_description": product_text, "specification": specification,
                "quantity": quantity, "unit": str(quantity_node.get("unit") or ""),
                "matched_sku": product["sku"], "discount_percent": discount_percent,
            })
        total = round(subtotal - total_discount, 2)
        valid_until = (datetime.now() + timedelta(days=cfg.default_validity_days)).strftime("%Y-%m-%d")
        version = {
            "version": 1, "items": quote_items, "subtotal_usd": subtotal,
            "discount_percent": round(total_discount / subtotal * 100, 4) if subtotal else 0.0,
            "discount_amount": total_discount, "packaging_cost_usd": 0.0,
            "freight_cost_usd": 0.0, "total_usd": total, "exchange_rate_note": "",
            "validity_days": cfg.default_validity_days, "valid_until": valid_until,
            "payment_terms": "T/T", "delivery_term": " ".join(filter(None, (incoterm, place))),
            "remarks_cn": "邮件询盘自动生成的报价草案，须经业务人员审批。",
            "remarks_en": "Draft quotation generated from the confirmed inquiry; subject to approval.",
            "created_by": "email_quote_workflow", "created_at": _now(),
        }
        customer = snapshot.get("customer") or {}
        extracted = {
            "customer_name": self._node_value(customer.get("name")) or "",
            "customer_company": self._node_value(customer.get("company")) or "",
            "customer_country": self._node_value(snapshot.get("country")) or "",
            "items": extraction_items, "delivery_term": version["delivery_term"],
            "delivery_deadline": self._node_value(snapshot.get("delivery_deadline"), "raw") or "",
            "missing_fields": [],
        }
        return extracted, version

    def _set_blocked(self, email_id: int, review_id: str, review_hash: str, code: str) -> dict:
        now = _now()
        self.connection.execute("""INSERT INTO ops_email_quote_workflow(
          email_id,review_id,review_hash,status,error_code,created_at,updated_at)
          VALUES(?,?,?,'quote_blocked',?,?,?)
          ON CONFLICT(email_id) DO UPDATE SET review_id=excluded.review_id,review_hash=excluded.review_hash,
          status='quote_blocked',error_code=excluded.error_code,updated_at=excluded.updated_at""",
          (email_id, review_id, review_hash, code, now, now))
        self.connection.commit()
        return {"email_id": email_id, "work_status": "quote_blocked", "error_code": code}

    def materialize_confirmed(self, *, email_id: int, review_id: str, review_hash: str) -> dict:
        existing = self.connection.execute(
            "SELECT * FROM ops_email_quote_workflow WHERE email_id=?", (email_id,)
        ).fetchone()
        if existing and existing["quote_id"] and existing["review_hash"] == review_hash:
            return self._work_item(dict(existing))
        snapshot = EmailReviewGate(self.email_repository).require_confirmed(
            email_id=email_id, review_id=review_id, review_hash=review_hash
        )
        try:
            extracted, version = self._build_quote(snapshot)
        except EmailQuoteWorkflowError as exc:
            return self._set_blocked(email_id, review_id, review_hash, exc.code)
        now = _now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.connection.execute(
                "SELECT * FROM ops_email_quote_workflow WHERE email_id=?", (email_id,)
            ).fetchone()
            if current and current["quote_id"] and current["review_hash"] == review_hash:
                self.connection.commit()
                return self._work_item(dict(current))
            cursor = self.connection.execute("""INSERT INTO rfq_requests(
              session_key,raw_text,extracted_json,status,created_at,updated_at)
              VALUES(?, '', ?, 'quoted', ?, ?)""",
              (f"email:{email_id}", json.dumps(extracted, ensure_ascii=False), now, now))
            rfq_id = int(cursor.lastrowid)
            cursor = self.connection.execute("""INSERT INTO quotes(
              rfq_id,status,current_version,version_data,created_at)
              VALUES(?,'pending_approval',1,?,?)""",
              (rfq_id, json.dumps([version], ensure_ascii=False), now))
            quote_id = int(cursor.lastrowid)
            content_hash = canonical_quote_hash(version)
            cursor = self.connection.execute("""INSERT INTO approval_records(
              quote_id,version,status,content_hash,created_at)
              VALUES(?,1,'pending',?,?)""", (quote_id, content_hash, now))
            approval_key = int(cursor.lastrowid)
            self.connection.execute("""INSERT INTO ops_email_quote_workflow(
              email_id,review_id,review_hash,rfq_id,quote_id,quote_version,approval_key,
              status,error_code,created_at,updated_at)
              VALUES(?,?,?,?,?,1,?,'pending_approval',NULL,?,?)
              ON CONFLICT(email_id) DO UPDATE SET review_id=excluded.review_id,
              review_hash=excluded.review_hash,rfq_id=excluded.rfq_id,quote_id=excluded.quote_id,
              quote_version=1,approval_key=excluded.approval_key,status='pending_approval',
              error_code=NULL,updated_at=excluded.updated_at""",
              (email_id, review_id, review_hash, rfq_id, quote_id, approval_key, now, now))
            self.connection.execute(
                "UPDATE ops_inbound_email SET rfq_id=?,updated_at=? WHERE email_id=?",
                (rfq_id, now, email_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        row = self.connection.execute(
            "SELECT * FROM ops_email_quote_workflow WHERE email_id=?", (email_id,)
        ).fetchone()
        return self._work_item(dict(row))

    def decide(self, approval_key: int, *, action: str, reviewer: str, comment: str = "") -> dict:
        if action not in {"approve", "reject"}:
            raise EmailQuoteWorkflowError("email_approval_invalid_action")
        if not reviewer.strip():
            raise EmailQuoteWorkflowError("email_approver_not_configured", 503)
        if len(comment) > 500:
            raise EmailQuoteWorkflowError("email_approval_comment_too_long")
        row = self.connection.execute("""SELECT a.*,q.current_version,q.version_data,q.status AS quote_status
          FROM approval_records a JOIN quotes q ON q.id=a.quote_id WHERE a.id=?""",
          (int(approval_key),)).fetchone()
        if row is None:
            raise EmailQuoteWorkflowError("email_approval_not_found", 404)
        if row["status"] != "pending":
            raise EmailQuoteWorkflowError("email_approval_already_decided", 409)
        versions = json.loads(row["version_data"] or "[]")
        version = next((item for item in versions if int(item.get("version", 0)) == int(row["version"])), None)
        if (version is None or int(row["current_version"]) != int(row["version"])
                or row["content_hash"] != canonical_quote_hash(version)):
            raise EmailQuoteWorkflowError("email_approval_quote_stale", 409)
        status = "approved" if action == "approve" else "rejected"
        now = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        cursor = self.connection.execute("""UPDATE approval_records SET status=?,reviewer=?,comment=?,decided_at=?
          WHERE id=? AND status='pending'""", (status, reviewer, comment.strip(), now, approval_key))
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise EmailQuoteWorkflowError("email_approval_already_decided", 409)
        self.connection.execute("UPDATE quotes SET status=? WHERE id=?", (status, row["quote_id"]))
        self.connection.execute("""UPDATE ops_email_quote_workflow SET status=?,updated_at=?
          WHERE approval_key=?""", (status, now, approval_key))
        self.connection.commit()
        workflow = self.connection.execute(
            "SELECT * FROM ops_email_quote_workflow WHERE approval_key=?", (approval_key,)
        ).fetchone()
        return self._work_item(dict(workflow)) if workflow else {
            "approval_key": approval_key, "quote_id": row["quote_id"], "work_status": status
        }

    def sync_confirmed(self) -> None:
        rows = self.connection.execute("""SELECT email_id,confirmed_review_id,confirmed_review_hash
          FROM ops_inbound_email WHERE status='confirmed' AND confirmed_review_id IS NOT NULL""").fetchall()
        for row in rows:
            self.materialize_confirmed(
                email_id=int(row["email_id"]), review_id=row["confirmed_review_id"],
                review_hash=row["confirmed_review_hash"],
            )

    def _work_item(self, workflow: dict) -> dict:
        email = self.connection.execute(
            "SELECT provider,from_address,subject,created_at,status FROM ops_inbound_email WHERE email_id=?",
            (workflow["email_id"],),
        ).fetchone()
        result = {
            "email_id": int(workflow["email_id"]), "quote_id": workflow.get("quote_id"),
            "quote_version": workflow.get("quote_version"), "approval_key": workflow.get("approval_key"),
            "work_status": workflow.get("status"), "error_code": workflow.get("error_code"),
            "created_at": workflow.get("created_at"), "content_hash": None,
        }
        if email:
            result.update({
                "provider": email["provider"], "sender_masked": _mask_email(email["from_address"]),
                "subject_preview": str(email["subject"] or "")[:160],
            })
        if workflow.get("quote_id"):
            quote = self.connection.execute("SELECT * FROM quotes WHERE id=?", (workflow["quote_id"],)).fetchone()
            approval = self.connection.execute(
                "SELECT * FROM approval_records WHERE id=?", (workflow.get("approval_key"),)
            ).fetchone()
            versions = json.loads(quote["version_data"] or "[]") if quote else []
            version = next((item for item in versions if int(item.get("version", 0)) == int(workflow.get("quote_version") or 0)), {})
            approval_binding_valid = bool(
                quote and approval and version
                and approval["content_hash"]
                and int(quote["current_version"]) == int(workflow.get("quote_version") or 0)
                and approval["content_hash"] == canonical_quote_hash(version)
            )
            if result["work_status"] == "pending_approval" and not approval_binding_valid:
                result["work_status"] = "quote_blocked"
                result["error_code"] = "email_approval_regeneration_required"
            result.update({
                "total_usd": version.get("total_usd"), "item_count": len(version.get("items") or []),
                "delivery_term": version.get("delivery_term"),
                "content_hash": approval["content_hash"] if approval else None,
                "reviewer": approval["reviewer"] if approval else None,
                "decided_at": approval["decided_at"] if approval else None,
            })
            delivery = self.connection.execute("""SELECT status,delivery_id,updated_at FROM ops_email_delivery
              WHERE quote_id=? AND quote_version=? AND approval_key=?
              ORDER BY created_at DESC LIMIT 1""", (
                workflow["quote_id"], workflow.get("quote_version"), workflow.get("approval_key")
            )).fetchone() if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ops_email_delivery'"
            ).fetchone() else None
            if delivery:
                result.update({"delivery_id": delivery["delivery_id"], "delivery_status": delivery["status"]})
        return result

    def list_work_items(self, limit: int = 100) -> list[dict]:
        self.sync_confirmed()
        limit = max(1, min(int(limit), 100))
        result = []
        reviews = self.connection.execute("""SELECT email_id,provider,from_address,subject,extraction_json,created_at
          FROM ops_inbound_email WHERE status='needs_review' ORDER BY created_at DESC,email_id DESC LIMIT ?""",
          (limit,)).fetchall()
        for row in reviews:
            try:
                extraction = json.loads(row["extraction_json"] or "{}")
            except json.JSONDecodeError:
                extraction = {}
            result.append({
                "email_id": int(row["email_id"]), "quote_id": None, "quote_version": None,
                "approval_key": None, "work_status": "awaiting_field_review",
                "error_code": None, "provider": row["provider"],
                "sender_masked": _mask_email(row["from_address"]),
                "subject_preview": str(row["subject"] or "")[:160],
                "missing_count": len(extraction.get("missing_fields") or []),
                "item_count": len(extraction.get("items") or []), "created_at": row["created_at"],
                "content_hash": None,
            })
        workflows = self.connection.execute(
            "SELECT * FROM ops_email_quote_workflow ORDER BY updated_at DESC,email_id DESC LIMIT ?", (limit,)
        ).fetchall()
        result.extend(self._work_item(dict(row)) for row in workflows)
        linked = {int(item["approval_key"]) for item in result if item.get("approval_key")}
        legacy = self.connection.execute("""SELECT a.*,q.status AS quote_status,q.current_version,q.version_data
          FROM approval_records a JOIN quotes q ON q.id=a.quote_id
          WHERE q.current_version=a.version ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
        for row in legacy:
            if int(row["id"]) in linked:
                continue
            versions = json.loads(row["version_data"] or "[]")
            version = next((item for item in versions if int(item.get("version", 0)) == int(row["version"])), {})
            approval_binding_valid = bool(
                version and row["content_hash"]
                and row["content_hash"] == canonical_quote_hash(version)
            )
            work_status = "pending_approval" if row["status"] == "pending" else row["status"]
            error_code = None
            if work_status == "pending_approval" and not approval_binding_valid:
                work_status = "quote_blocked"
                error_code = "email_approval_regeneration_required"
            result.append({
                "email_id": None, "quote_id": int(row["quote_id"]),
                "quote_version": int(row["version"]), "approval_key": int(row["id"]),
                "work_status": work_status,
                "error_code": error_code, "total_usd": version.get("total_usd"),
                "item_count": len(version.get("items") or []),
                "delivery_term": version.get("delivery_term"), "content_hash": row["content_hash"],
                "created_at": row["created_at"], "reviewer": row["reviewer"],
                "decided_at": row["decided_at"],
            })
        return result[:limit]
