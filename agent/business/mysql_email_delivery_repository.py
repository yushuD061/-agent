"""MySQL implementation of the controlled SMTP delivery repository contract."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from agent.business.email_delivery_repository import EmailDeliveryRepositoryError
from agent.business.mysql_database import create_connection


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_number(value):
    if value is None:
        return ""
    return float(value) if hasattr(value, "as_tuple") else value


class MySQLEmailDeliveryRepository:
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
        required = {"ops_email_delivery", "ops_email_delivery_audit"}
        with self.tx() as cursor:
            cursor.execute("""SELECT table_name FROM information_schema.tables
              WHERE table_schema=DATABASE() AND table_name IN
              ('ops_email_delivery','ops_email_delivery_audit')""")
            actual = {row["table_name"] for row in cursor.fetchall()}
        if required - actual:
            raise EmailDeliveryRepositoryError(
                "email_delivery_migration_incomplete:" + ",".join(sorted(required - actual))
            )

    @staticmethod
    def _row(row: dict | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        for key, value in list(item.items()):
            if isinstance(value, datetime):
                item[key] = value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return item

    @staticmethod
    def _decode_json(value) -> list:
        return json.loads(value) if isinstance(value, str) else list(value or [])

    def _gate(self, cursor, *, account_id: str, quote_id: int, quote_version: int,
              approval_key: int, content_hash: str, lock: bool = False) -> tuple[dict, dict]:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(
            "SELECT * FROM ops_email_account WHERE account_id=%s AND deleted_at IS NULL" + suffix,
            (account_id,),
        )
        account = cursor.fetchone()
        if account is None:
            raise EmailDeliveryRepositoryError("email_account_not_found")
        if not bool(account["outbound_enabled"]) or account["status"] != "healthy":
            raise EmailDeliveryRepositoryError("email_outbound_account_disabled")
        cursor.execute("""SELECT q.quote_key,q.quote_id,q.current_version_no,q.status AS quote_status,
          v.*,a.approval_status,a.content_hash AS approval_content_hash
          FROM ops_quote q
          JOIN ops_quote_version v ON v.quote_id=q.quote_id AND v.version_no=%s
          JOIN ops_approval_record a ON a.approval_key=%s AND a.quote_id=q.quote_id AND a.version_no=v.version_no
          WHERE q.quote_key=%s""" + suffix, (quote_version, approval_key, quote_id))
        quote = cursor.fetchone()
        if quote is None:
            raise EmailDeliveryRepositoryError("email_quote_not_found")
        if int(quote["current_version_no"]) != quote_version or quote["quote_status"] not in {"approved", "sent"}:
            raise EmailDeliveryRepositoryError("email_quote_version_stale")
        if quote["approval_status"] != "approved":
            raise EmailDeliveryRepositoryError("email_quote_not_approved")
        if (quote["approval_content_hash"] != quote["content_hash"]
                or content_hash != quote["content_hash"]):
            raise EmailDeliveryRepositoryError("email_quote_content_hash_mismatch")
        cursor.execute("""SELECT l.*,p.name_en FROM ops_quote_line l
          LEFT JOIN ops_product p ON p.sku=l.sku
          WHERE l.quote_id=%s AND l.version_no=%s ORDER BY l.line_no""",
                       (quote["quote_id"], quote_version))
        items = [{
            "product_sku": row["sku"],
            "product_name_en": row.get("name_en") or row["sku"],
            "quantity": _as_number(row["quantity"]),
            "unit_price_usd": _as_number(row["unit_price"]),
            "total_price_usd": _as_number(row["line_amount"]),
        } for row in cursor.fetchall()]
        payload = {
            "version": quote_version,
            "items": items,
            "subtotal_usd": _as_number(quote["subtotal_amount"]),
            "discount_amount": _as_number(quote["discount_amount"]),
            "packaging_cost_usd": _as_number(quote["packaging_amount"]),
            "freight_cost_usd": _as_number(quote["freight_amount"]),
            "total_usd": _as_number(quote["total_amount"]),
            "valid_until": str(quote["valid_until"]),
            "payment_terms": "",
            "delivery_term": "",
            "remarks_en": "",
        }
        account = dict(account)
        account["allowed_recipients_json"] = self._decode_json(account["allowed_recipients_json"])
        return account, payload

    def approved_quote_snapshot(self, *, account_id: str, quote_id: int, quote_version: int,
                                approval_key: int) -> tuple[dict, dict, str]:
        with self.tx() as cursor:
            cursor.execute("""SELECT v.content_hash FROM ops_quote q JOIN ops_quote_version v
              ON v.quote_id=q.quote_id AND v.version_no=%s WHERE q.quote_key=%s""",
                           (quote_version, quote_id))
            row = cursor.fetchone()
            if row is None:
                raise EmailDeliveryRepositoryError("email_quote_version_not_found")
            content_hash = row["content_hash"]
            account, payload = self._gate(
                cursor, account_id=account_id, quote_id=quote_id, quote_version=quote_version,
                approval_key=approval_key, content_hash=content_hash,
            )
            return account, payload, content_hash

    def queue_approved_email(self, *, account_id: str, quote_id: int, quote_version: int,
                             approval_key: int, recipient: str, subject: str, body: str,
                             content_hash: str, created_by: str, max_attempts: int = 5) -> dict:
        snapshot_hash = hashlib.sha256((subject + "\n" + body).encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{account_id}:{quote_id}:{quote_version}:{approval_key}:{recipient}:{content_hash}".encode()
        ).hexdigest()
        delivery_id = str(uuid.uuid4())
        message_id = f"<nanoclaw.{idempotency_key[:32]}@outbox.local>"
        now = _now()
        with self.tx() as cursor:
            self._gate(cursor, account_id=account_id, quote_id=quote_id, quote_version=quote_version,
                       approval_key=approval_key, content_hash=content_hash, lock=True)
            cursor.execute("""INSERT IGNORE INTO ops_email_delivery(
              delivery_id,idempotency_key,account_id,quote_id,quote_version,approval_key,recipient,
              subject_snapshot,body_snapshot,content_hash,snapshot_hash,status,attempt_count,max_attempts,
              smtp_message_id,created_by,created_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',0,%s,%s,%s,%s,%s)""", (
                delivery_id, idempotency_key, account_id, quote_id, quote_version, approval_key,
                recipient, subject, body, content_hash, snapshot_hash, max_attempts, message_id,
                created_by, now, now,
            ))
            inserted = cursor.rowcount == 1
            cursor.execute("SELECT * FROM ops_email_delivery WHERE idempotency_key=%s", (idempotency_key,))
            row = cursor.fetchone()
            if inserted:
                self._audit(cursor, row["delivery_id"], created_by, "queued")
            return self._row(row)

    def list_sendable_quotes(self, limit: int = 100) -> list[dict]:
        with self.tx() as cursor:
            cursor.execute("""SELECT a.approval_key,q.quote_key AS quote_id,a.version_no AS quote_version,
              a.content_hash,q.created_at FROM ops_approval_record a
              JOIN ops_quote q ON q.quote_id=a.quote_id
              JOIN ops_quote_version v ON v.quote_id=q.quote_id AND v.version_no=a.version_no
              WHERE a.approval_status='approved' AND q.status IN ('approved','sent')
                AND q.current_version_no=a.version_no AND a.content_hash=v.content_hash
              ORDER BY a.approval_key DESC LIMIT %s""", (limit,))
            return [self._row(row) for row in cursor.fetchall()]

    def list_deliveries(self, limit: int = 100) -> list[dict]:
        with self.tx() as cursor:
            cursor.execute("SELECT * FROM ops_email_delivery ORDER BY created_at DESC,delivery_id DESC LIMIT %s", (limit,))
            return [self._row(row) for row in cursor.fetchall()]

    def get(self, delivery_id: str) -> dict | None:
        with self.tx() as cursor:
            cursor.execute("SELECT * FROM ops_email_delivery WHERE delivery_id=%s", (delivery_id,))
            return self._row(cursor.fetchone())

    def claim_delivery(self, worker_id: str, *, lease_seconds: int = 60) -> dict | None:
        now, lease_until = _now(), _now() + timedelta(seconds=lease_seconds)
        with self.tx() as cursor:
            cursor.execute("""SELECT * FROM ops_email_delivery
              WHERE status='pending' OR (status='retry_wait' AND next_attempt_at<=%s)
              ORDER BY created_at,delivery_id LIMIT 1 FOR UPDATE SKIP LOCKED""", (now,))
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute("""UPDATE ops_email_delivery SET status='sending',attempt_count=attempt_count+1,
              lease_owner=%s,lease_until=%s,updated_at=%s WHERE delivery_id=%s AND status=%s""",
                           (worker_id, lease_until, now, row["delivery_id"], row["status"]))
            if cursor.rowcount != 1:
                return None
            self._audit(cursor, row["delivery_id"], worker_id, "claimed")
            cursor.execute("SELECT * FROM ops_email_delivery WHERE delivery_id=%s", (row["delivery_id"],))
            return self._row(cursor.fetchone())

    def revalidate_claim(self, delivery_id: str, worker_id: str) -> tuple[dict, dict]:
        with self.tx() as cursor:
            cursor.execute("""SELECT * FROM ops_email_delivery
              WHERE delivery_id=%s AND status='sending' AND lease_owner=%s FOR UPDATE""",
                           (delivery_id, worker_id))
            row = cursor.fetchone()
            if row is None:
                raise EmailDeliveryRepositoryError("email_delivery_lease_lost")
            account, _ = self._gate(cursor, account_id=row["account_id"], quote_id=int(row["quote_id"]),
                                    quote_version=int(row["quote_version"]), approval_key=int(row["approval_key"]),
                                    content_hash=row["content_hash"], lock=True)
            expected = hashlib.sha256((row["subject_snapshot"] + "\n" + row["body_snapshot"]).encode()).hexdigest()
            if expected != row["snapshot_hash"]:
                raise EmailDeliveryRepositoryError("email_delivery_snapshot_tampered")
            if row["recipient"].lower() not in {item.lower() for item in account["allowed_recipients_json"]}:
                raise EmailDeliveryRepositoryError("email_recipient_not_allowed")
            return self._row(row), account

    def mark_smtp_accepted(self, delivery_id: str, worker_id: str,
                           internet_message_id: str | None = None) -> dict:
        now = _now()
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_delivery SET status='accepted',smtp_accepted_at=%s,
              internet_message_id=%s,lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=%s
              WHERE delivery_id=%s AND status='sending' AND lease_owner=%s""",
                           (now, internet_message_id, now, delivery_id, worker_id))
            if cursor.rowcount != 1:
                raise EmailDeliveryRepositoryError("email_delivery_lease_lost")
            self._audit(cursor, delivery_id, worker_id, "smtp_accepted")
        return self.get(delivery_id)

    def fail_delivery(self, delivery_id: str, worker_id: str, error_code: str, *,
                      permanent: bool = False, outcome_unknown: bool = False) -> dict:
        with self.tx() as cursor:
            cursor.execute("""SELECT * FROM ops_email_delivery
              WHERE delivery_id=%s AND status='sending' AND lease_owner=%s FOR UPDATE""",
                           (delivery_id, worker_id))
            row = cursor.fetchone()
            if row is None:
                raise EmailDeliveryRepositoryError("email_delivery_lease_lost")
            if outcome_unknown:
                status, next_at = "outcome_unknown", None
            elif permanent or int(row["attempt_count"]) >= int(row["max_attempts"]):
                status, next_at = "dead_letter", None
            else:
                delay = min(3600, 30 * (2 ** max(0, int(row["attempt_count"]) - 1)))
                status, next_at = "retry_wait", _now() + timedelta(seconds=delay)
            cursor.execute("""UPDATE ops_email_delivery SET status=%s,next_attempt_at=%s,lease_owner=NULL,
              lease_until=NULL,last_error_code=%s,updated_at=%s WHERE delivery_id=%s""",
                           (status, next_at, error_code, _now(), delivery_id))
            self._audit(cursor, delivery_id, worker_id, status, error_code)
        return self.get(delivery_id)

    def mark_stale(self, delivery_id: str, actor: str, error_code: str) -> dict:
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_delivery SET status='stale',lease_owner=NULL,lease_until=NULL,
              last_error_code=%s,updated_at=%s WHERE delivery_id=%s
              AND status IN ('pending','sending','retry_wait','dead_letter')""",
                           (error_code, _now(), delivery_id))
            if cursor.rowcount:
                self._audit(cursor, delivery_id, actor, "stale", error_code)
        return self.get(delivery_id)

    def requeue_expired_leases(self, actor: str = "email_delivery_recovery") -> int:
        now = _now()
        with self.tx() as cursor:
            cursor.execute("""SELECT delivery_id FROM ops_email_delivery
              WHERE status='sending' AND lease_until<%s FOR UPDATE SKIP LOCKED""", (now,))
            rows = cursor.fetchall()
            for row in rows:
                cursor.execute("""UPDATE ops_email_delivery SET status='retry_wait',next_attempt_at=%s,
                  lease_owner=NULL,lease_until=NULL,last_error_code='email_delivery_lease_expired',updated_at=%s
                  WHERE delivery_id=%s""", (now, now, row["delivery_id"]))
                self._audit(cursor, row["delivery_id"], actor, "lease_recovered", "email_delivery_lease_expired")
            return len(rows)

    def retry_dead_letter(self, delivery_id: str, *, actor: str) -> dict:
        row = self.get(delivery_id)
        if row is None:
            raise EmailDeliveryRepositoryError("email_delivery_not_found")
        if row["status"] != "dead_letter":
            raise EmailDeliveryRepositoryError("email_delivery_not_retryable")
        try:
            with self.tx() as cursor:
                self._gate(cursor, account_id=row["account_id"], quote_id=int(row["quote_id"]),
                           quote_version=int(row["quote_version"]), approval_key=int(row["approval_key"]),
                           content_hash=row["content_hash"], lock=True)
                cursor.execute("""UPDATE ops_email_delivery SET status='pending',attempt_count=0,
                  next_attempt_at=NULL,last_error_code=NULL,updated_at=%s
                  WHERE delivery_id=%s AND status='dead_letter'""", (_now(), delivery_id))
                self._audit(cursor, delivery_id, actor, "manual_requeue")
        except EmailDeliveryRepositoryError as exc:
            self.mark_stale(delivery_id, actor, str(exc))
            raise
        return self.get(delivery_id)

    def metrics(self) -> dict:
        with self.tx() as cursor:
            cursor.execute("SELECT status,COUNT(*) AS count FROM ops_email_delivery GROUP BY status")
            statuses = {row["status"]: int(row["count"]) for row in cursor.fetchall()}
            cursor.execute("""SELECT COUNT(*) AS terminal_unredacted FROM ops_email_delivery
              WHERE status IN ('accepted','dead_letter','stale','outcome_unknown') AND content_redacted_at IS NULL""")
            unredacted = int(cursor.fetchone()["terminal_unredacted"])
            cursor.execute("""SELECT AVG(TIMESTAMPDIFF(MICROSECOND,created_at,smtp_accepted_at))/1000000 AS seconds
              FROM ops_email_delivery WHERE status='accepted' AND smtp_accepted_at IS NOT NULL""")
            latency = cursor.fetchone()["seconds"]
        return {"status_counts": statuses, "terminal_unredacted": unredacted,
                "accepted_latency_seconds_avg": float(latency) if latency is not None else None}

    def redact_terminal_content(self, before, *, actor: str, limit: int = 500,
                                apply: bool = False) -> int:
        with self.tx() as cursor:
            cursor.execute("""SELECT delivery_id FROM ops_email_delivery
              WHERE status IN ('accepted','dead_letter','stale','outcome_unknown')
                AND content_redacted_at IS NULL AND updated_at<%s
              ORDER BY updated_at LIMIT %s FOR UPDATE""", (before, limit))
            rows = cursor.fetchall()
            if apply:
                for row in rows:
                    cursor.execute("""UPDATE ops_email_delivery SET recipient='[redacted]',
                      subject_snapshot='[redacted]',body_snapshot='[redacted]',content_redacted_at=%s,updated_at=%s
                      WHERE delivery_id=%s AND content_redacted_at IS NULL""",
                                   (_now(), _now(), row["delivery_id"]))
                    self._audit(cursor, row["delivery_id"], actor, "content_redacted")
            return len(rows)

    @staticmethod
    def _audit(cursor, delivery_id: str, actor: str, action: str,
               error_code: str | None = None) -> None:
        cursor.execute("""INSERT INTO ops_email_delivery_audit(
          delivery_id,actor,action,error_code,created_at) VALUES(%s,%s,%s,%s,%s)""",
                       (delivery_id, actor, action, error_code, _now()))
