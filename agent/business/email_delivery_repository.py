"""SQLite M4 SMTP outbox with approval, idempotency, and lease gates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from agent.business.config import load_business_config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_quote_hash(version_payload: dict) -> str:
    encoded = json.dumps(
        version_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EmailDeliveryRepositoryError(RuntimeError):
    pass


class EmailDeliveryRepository:
    """Owns the local outbox transaction boundary.

    The legacy local quote tables remain the business truth. This repository
    snapshots only a server-rendered email after rechecking the approved quote
    version and its hash in the same SQLite transaction.
    """

    def __init__(self, connection: sqlite3.Connection | None = None):
        if connection is None:
            connection = sqlite3.connect(
                load_business_config().database_path, check_same_thread=False
            )
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.init_schema()

    @contextmanager
    def tx(self, *, immediate: bool = False):
        with self._lock:
            cursor = self.connection.cursor()
            try:
                if immediate:
                    cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def init_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS ops_email_delivery(
          delivery_id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL UNIQUE,
          account_id TEXT NOT NULL,
          quote_id INTEGER NOT NULL,
          quote_version INTEGER NOT NULL,
          approval_key INTEGER NOT NULL,
          recipient TEXT NOT NULL,
          subject_snapshot TEXT NOT NULL,
          body_snapshot TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          snapshot_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempt_count INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 5,
          next_attempt_at TEXT,
          lease_owner TEXT,
          lease_until TEXT,
          smtp_message_id TEXT NOT NULL,
          smtp_accepted_at TEXT,
          last_error_code TEXT,
          internet_message_id TEXT,
          in_reply_to TEXT,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          content_redacted_at TEXT,
          CHECK(status IN ('pending','sending','retry_wait','accepted','dead_letter','stale','outcome_unknown')),
          CHECK(attempt_count >= 0),
          CHECK(max_attempts > 0)
        );
        CREATE INDEX IF NOT EXISTS ix_email_delivery_due
          ON ops_email_delivery(status,next_attempt_at,lease_until);
        CREATE TABLE IF NOT EXISTS ops_email_delivery_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          delivery_id TEXT NOT NULL,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          error_code TEXT,
          created_at TEXT NOT NULL
        );
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(ops_email_delivery)")}
        if "content_redacted_at" not in columns:
            self.connection.execute("ALTER TABLE ops_email_delivery ADD COLUMN content_redacted_at TEXT")
        self.connection.commit()

    @staticmethod
    def _version_payload(row: sqlite3.Row, version: int) -> dict | None:
        try:
            versions = json.loads(row["version_data"] or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        return next((item for item in versions if int(item.get("version", 0)) == version), None)

    def _gate(self, cursor: sqlite3.Cursor, *, account_id: str, quote_id: int,
              quote_version: int, approval_key: int, content_hash: str) -> tuple[dict, dict]:
        account = cursor.execute(
            "SELECT * FROM ops_email_account WHERE account_id=? AND deleted_at IS NULL",
            (account_id,),
        ).fetchone()
        if account is None:
            raise EmailDeliveryRepositoryError("email_account_not_found")
        if not bool(account["outbound_enabled"]) or account["status"] != "healthy":
            raise EmailDeliveryRepositoryError("email_outbound_account_disabled")
        quote = cursor.execute(
            "SELECT * FROM quotes WHERE id=?", (quote_id,)
        ).fetchone()
        if quote is None:
            raise EmailDeliveryRepositoryError("email_quote_not_found")
        if int(quote["current_version"]) != quote_version or quote["status"] not in {"approved", "sent"}:
            raise EmailDeliveryRepositoryError("email_quote_version_stale")
        approval = cursor.execute(
            "SELECT * FROM approval_records WHERE id=? AND quote_id=? AND version=? AND status='approved'",
            (approval_key, quote_id, quote_version),
        ).fetchone()
        if approval is None:
            raise EmailDeliveryRepositoryError("email_quote_not_approved")
        payload = self._version_payload(quote, quote_version)
        if payload is None:
            raise EmailDeliveryRepositoryError("email_quote_version_not_found")
        actual_hash = canonical_quote_hash(payload)
        approval_hash = approval["content_hash"] if "content_hash" in approval.keys() else None
        if not approval_hash or approval_hash != actual_hash or content_hash != actual_hash:
            raise EmailDeliveryRepositoryError("email_quote_content_hash_mismatch")
        return dict(account), payload

    def approved_quote_snapshot(self, *, account_id: str, quote_id: int, quote_version: int,
                                approval_key: int) -> tuple[dict, dict, str]:
        with self.tx() as cursor:
            quote = cursor.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
            if quote is None:
                raise EmailDeliveryRepositoryError("email_quote_not_found")
            payload = self._version_payload(quote, quote_version)
            if payload is None:
                raise EmailDeliveryRepositoryError("email_quote_version_not_found")
            content_hash = canonical_quote_hash(payload)
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
            f"{account_id}:{quote_id}:{quote_version}:{approval_key}:{recipient}:{content_hash}".encode("utf-8")
        ).hexdigest()
        delivery_id = str(uuid.uuid4())
        message_id = f"<nanoclaw.{idempotency_key[:32]}@outbox.local>"
        now = utc_now()
        with self.tx(immediate=True) as cursor:
            self._gate(
                cursor, account_id=account_id, quote_id=quote_id, quote_version=quote_version,
                approval_key=approval_key, content_hash=content_hash,
            )
            cursor.execute("""INSERT OR IGNORE INTO ops_email_delivery(
              delivery_id,idempotency_key,account_id,quote_id,quote_version,approval_key,recipient,
              subject_snapshot,body_snapshot,content_hash,snapshot_hash,status,attempt_count,max_attempts,
              smtp_message_id,created_by,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,?,?,?,?)""", (
                delivery_id, idempotency_key, account_id, quote_id, quote_version, approval_key,
                recipient, subject, body, content_hash, snapshot_hash, max_attempts, message_id,
                created_by, now, now,
            ))
            inserted = cursor.rowcount == 1
            row = cursor.execute(
                "SELECT * FROM ops_email_delivery WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if inserted:
                self._audit(cursor, row["delivery_id"], created_by, "queued")
            return dict(row)

    def list_sendable_quotes(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute("""SELECT a.id AS approval_key,a.quote_id,a.version AS quote_version,
          a.content_hash,q.created_at FROM approval_records a JOIN quotes q ON q.id=a.quote_id
          WHERE a.status='approved' AND q.status IN ('approved','sent') AND q.current_version=a.version
          ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
        result = []
        for row in rows:
            quote = self.connection.execute("SELECT * FROM quotes WHERE id=?", (row["quote_id"],)).fetchone()
            payload = self._version_payload(quote, int(row["quote_version"])) if quote else None
            if payload and row["content_hash"] == canonical_quote_hash(payload):
                result.append(dict(row))
        return result

    def list_deliveries(self, limit: int = 100) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ops_email_delivery ORDER BY created_at DESC,delivery_id DESC LIMIT ?", (limit,)
        ).fetchall()]

    def get(self, delivery_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM ops_email_delivery WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        return dict(row) if row else None

    def claim_delivery(self, worker_id: str, *, lease_seconds: int = 60) -> dict | None:
        now = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        with self.tx(immediate=True) as cursor:
            row = cursor.execute("""SELECT * FROM ops_email_delivery
              WHERE status='pending' OR (status='retry_wait' AND next_attempt_at<=?)
              ORDER BY created_at,delivery_id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            cursor.execute("""UPDATE ops_email_delivery SET status='sending',attempt_count=attempt_count+1,
              lease_owner=?,lease_until=?,updated_at=? WHERE delivery_id=? AND status=?""",
                           (worker_id, lease_until, now, row["delivery_id"], row["status"]))
            if cursor.rowcount != 1:
                return None
            self._audit(cursor, row["delivery_id"], worker_id, "claimed")
            return dict(cursor.execute(
                "SELECT * FROM ops_email_delivery WHERE delivery_id=?", (row["delivery_id"],)
            ).fetchone())

    def revalidate_claim(self, delivery_id: str, worker_id: str) -> tuple[dict, dict]:
        with self.tx() as cursor:
            row = cursor.execute(
                "SELECT * FROM ops_email_delivery WHERE delivery_id=? AND status='sending' AND lease_owner=?",
                (delivery_id, worker_id),
            ).fetchone()
            if row is None:
                raise EmailDeliveryRepositoryError("email_delivery_lease_lost")
            account, _ = self._gate(
                cursor, account_id=row["account_id"], quote_id=int(row["quote_id"]),
                quote_version=int(row["quote_version"]), approval_key=int(row["approval_key"]),
                content_hash=row["content_hash"],
            )
            expected = hashlib.sha256(
                (row["subject_snapshot"] + "\n" + row["body_snapshot"]).encode("utf-8")
            ).hexdigest()
            if expected != row["snapshot_hash"]:
                raise EmailDeliveryRepositoryError("email_delivery_snapshot_tampered")
            allowed = json.loads(account["allowed_recipients_json"] or "[]")
            if row["recipient"].lower() not in {item.lower() for item in allowed}:
                raise EmailDeliveryRepositoryError("email_recipient_not_allowed")
            return dict(row), account

    def mark_smtp_accepted(self, delivery_id: str, worker_id: str,
                           internet_message_id: str | None = None) -> dict:
        now = utc_now()
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_delivery SET status='accepted',smtp_accepted_at=?,
              internet_message_id=?,lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=?
              WHERE delivery_id=? AND status='sending' AND lease_owner=?""",
                           (now, internet_message_id, now, delivery_id, worker_id))
            if cursor.rowcount != 1:
                raise EmailDeliveryRepositoryError("email_delivery_lease_lost")
            self._audit(cursor, delivery_id, worker_id, "smtp_accepted")
        return self.get(delivery_id)

    def fail_delivery(self, delivery_id: str, worker_id: str, error_code: str, *,
                      permanent: bool = False, outcome_unknown: bool = False) -> dict:
        with self.tx() as cursor:
            row = cursor.execute(
                "SELECT * FROM ops_email_delivery WHERE delivery_id=? AND status='sending' AND lease_owner=?",
                (delivery_id, worker_id),
            ).fetchone()
            if row is None:
                raise EmailDeliveryRepositoryError("email_delivery_lease_lost")
            if outcome_unknown:
                status, next_at = "outcome_unknown", None
            elif permanent or int(row["attempt_count"]) >= int(row["max_attempts"]):
                status, next_at = "dead_letter", None
            else:
                delay = min(3600, 30 * (2 ** max(0, int(row["attempt_count"]) - 1)))
                status = "retry_wait"
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
            cursor.execute("""UPDATE ops_email_delivery SET status=?,next_attempt_at=?,lease_owner=NULL,
              lease_until=NULL,last_error_code=?,updated_at=? WHERE delivery_id=?""",
                           (status, next_at, error_code, utc_now(), delivery_id))
            self._audit(cursor, delivery_id, worker_id, status, error_code)
        return self.get(delivery_id)

    def mark_stale(self, delivery_id: str, actor: str, error_code: str) -> dict:
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_delivery SET status='stale',lease_owner=NULL,lease_until=NULL,
              last_error_code=?,updated_at=? WHERE delivery_id=? AND status IN ('pending','sending','retry_wait','dead_letter')""",
                           (error_code, utc_now(), delivery_id))
            if cursor.rowcount:
                self._audit(cursor, delivery_id, actor, "stale", error_code)
        return self.get(delivery_id)

    def requeue_expired_leases(self, actor: str = "email_delivery_recovery") -> int:
        now = utc_now()
        with self.tx(immediate=True) as cursor:
            rows = cursor.execute(
                "SELECT delivery_id FROM ops_email_delivery WHERE status='sending' AND lease_until<?", (now,)
            ).fetchall()
            for row in rows:
                cursor.execute("""UPDATE ops_email_delivery SET status='retry_wait',next_attempt_at=?,
                  lease_owner=NULL,lease_until=NULL,last_error_code='email_delivery_lease_expired',updated_at=?
                  WHERE delivery_id=?""", (now, now, row["delivery_id"]))
                self._audit(cursor, row["delivery_id"], actor, "lease_recovered", "email_delivery_lease_expired")
            return len(rows)

    def retry_dead_letter(self, delivery_id: str, *, actor: str) -> dict:
        row = self.get(delivery_id)
        if row is None:
            raise EmailDeliveryRepositoryError("email_delivery_not_found")
        if row["status"] not in {"dead_letter", "stale"}:
            raise EmailDeliveryRepositoryError("email_delivery_not_retryable")
        try:
            with self.tx(immediate=True) as cursor:
                self._gate(cursor, account_id=row["account_id"], quote_id=int(row["quote_id"]),
                           quote_version=int(row["quote_version"]), approval_key=int(row["approval_key"]),
                           content_hash=row["content_hash"])
                cursor.execute("""UPDATE ops_email_delivery SET status='pending',attempt_count=0,
                  next_attempt_at=NULL,last_error_code=NULL,updated_at=?
                  WHERE delivery_id=? AND status IN ('dead_letter','stale')""",
                               (utc_now(), delivery_id))
                self._audit(cursor, delivery_id, actor, "manual_requeue")
        except EmailDeliveryRepositoryError as exc:
            self.mark_stale(delivery_id, actor, str(exc))
            raise
        return self.get(delivery_id)

    def metrics(self) -> dict:
        statuses = {
            row["status"]: int(row["count"])
            for row in self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM ops_email_delivery GROUP BY status"
            ).fetchall()
        }
        unredacted = int(self.connection.execute("""SELECT COUNT(*) FROM ops_email_delivery
          WHERE status IN ('accepted','dead_letter','stale','outcome_unknown')
            AND content_redacted_at IS NULL""").fetchone()[0])
        latency = self.connection.execute("""SELECT AVG(
          (julianday(smtp_accepted_at)-julianday(created_at))*86400.0)
          FROM ops_email_delivery WHERE status='accepted' AND smtp_accepted_at IS NOT NULL""").fetchone()[0]
        return {
            "status_counts": statuses,
            "terminal_unredacted": unredacted,
            "accepted_latency_seconds_avg": float(latency) if latency is not None else None,
        }

    def redact_terminal_content(self, before: str, *, actor: str, limit: int = 500,
                                apply: bool = False) -> int:
        with self.tx(immediate=apply) as cursor:
            rows = cursor.execute("""SELECT delivery_id FROM ops_email_delivery
              WHERE status IN ('accepted','dead_letter','stale','outcome_unknown')
                AND content_redacted_at IS NULL AND updated_at<?
              ORDER BY updated_at LIMIT ?""", (before, limit)).fetchall()
            if apply:
                now = utc_now()
                for row in rows:
                    cursor.execute("""UPDATE ops_email_delivery SET recipient='[redacted]',
                      subject_snapshot='[redacted]',body_snapshot='[redacted]',content_redacted_at=?,updated_at=?
                      WHERE delivery_id=? AND content_redacted_at IS NULL""",
                                   (now, now, row["delivery_id"]))
                    self._audit(cursor, row["delivery_id"], actor, "content_redacted")
            return len(rows)

    @staticmethod
    def _audit(cursor: sqlite3.Cursor, delivery_id: str, actor: str, action: str,
               error_code: str | None = None) -> None:
        cursor.execute("""INSERT INTO ops_email_delivery_audit(
          delivery_id,actor,action,error_code,created_at) VALUES(?,?,?,?,?)""",
                       (delivery_id, actor, action, error_code, utc_now()))
