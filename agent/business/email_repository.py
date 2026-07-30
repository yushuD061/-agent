"""SQLite repository for durable email ingestion, leases, cursors and reviews."""

from __future__ import annotations

import json
import base64
import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from agent.business.config import load_business_config
from channels.email.contracts import EmailEnvelope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EmailReviewRepositoryError(RuntimeError):
    pass


class EmailRepository:
    def __init__(self, connection: sqlite3.Connection | None = None):
        if connection is None:
            connection = sqlite3.connect(load_business_config().database_path, check_same_thread=False)
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.init_schema()

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

    def init_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS ops_inbound_email(
          email_id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, provider TEXT NOT NULL,
          folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
          internet_message_id TEXT NOT NULL DEFAULT '', raw_sha256 TEXT NOT NULL,
          from_address TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
          text_body TEXT NOT NULL, envelope_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'persisted',
          extraction_json TEXT, extraction_mode TEXT, extractor_version TEXT, ingestion_classification_code TEXT,
          rfq_id INTEGER,
          lease_owner TEXT, lease_until TEXT, next_retry_at TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(account_id,folder,uidvalidity,uid));
        CREATE UNIQUE INDEX IF NOT EXISTS uk_email_fallback ON ops_inbound_email(account_id,internet_message_id,raw_sha256)
          WHERE internet_message_id <> '';
        CREATE TABLE IF NOT EXISTS ops_inbound_email_attachment(
          attachment_id INTEGER PRIMARY KEY AUTOINCREMENT, email_id INTEGER NOT NULL, filename TEXT NOT NULL,
          mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, scan_status TEXT NOT NULL,
          FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id));
        CREATE TABLE IF NOT EXISTS ops_email_sync_cursor(
          account_id TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, PRIMARY KEY(account_id,folder));
        CREATE TABLE IF NOT EXISTS ops_email_processing_attempt(
          attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, email_id INTEGER NOT NULL, attempt_no INTEGER NOT NULL,
          stage TEXT NOT NULL, error_code TEXT, started_at TEXT NOT NULL, ended_at TEXT,
          FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id));
        CREATE TABLE IF NOT EXISTS ops_email_ingestion_skip(
          skip_id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
          raw_sha256 TEXT NOT NULL, classification_code TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(account_id,folder,uidvalidity,uid));
        CREATE TABLE IF NOT EXISTS ops_email_review_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT, email_id INTEGER NOT NULL, reviewer TEXT NOT NULL,
          action TEXT NOT NULL, changes_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id));
        CREATE TABLE IF NOT EXISTS ops_email_review_revision(
          review_id TEXT PRIMARY KEY,
          email_id INTEGER NOT NULL,
          review_version INTEGER NOT NULL,
          base_extraction_hash TEXT NOT NULL,
          reviewed_json TEXT NOT NULL,
          review_hash TEXT NOT NULL,
          change_meta_json TEXT NOT NULL,
          reviewer_id TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          request_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'confirmed',
          created_at TEXT NOT NULL,
          UNIQUE(email_id,review_version),
          UNIQUE(email_id,idempotency_key),
          FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id));
        CREATE TABLE IF NOT EXISTS ops_email_notification_outbox(
          notification_id INTEGER PRIMARY KEY AUTOINCREMENT, email_id INTEGER NOT NULL,
          channel TEXT NOT NULL, target_id TEXT NOT NULL, target_type TEXT NOT NULL,
          notification_version INTEGER NOT NULL DEFAULT 1, content TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
          next_retry_at TEXT, last_error_code TEXT, created_at TEXT NOT NULL, sent_at TEXT,
          UNIQUE(email_id,channel,target_id,target_type,notification_version),
          FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id));
        """)
        outbox_columns = {row[1] for row in self.connection.execute(
            "PRAGMA table_info(ops_email_notification_outbox)")}
        if "notification_version" not in outbox_columns:
            # Preserve existing sent rows while upgrading the old one-row-per-email outbox.
            self.connection.executescript("""
            ALTER TABLE ops_email_notification_outbox RENAME TO ops_email_notification_outbox_legacy;
            CREATE TABLE ops_email_notification_outbox(
              notification_id INTEGER PRIMARY KEY AUTOINCREMENT, email_id INTEGER NOT NULL,
              channel TEXT NOT NULL, target_id TEXT NOT NULL, target_type TEXT NOT NULL,
              notification_version INTEGER NOT NULL DEFAULT 1, content TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
              next_retry_at TEXT, last_error_code TEXT, created_at TEXT NOT NULL, sent_at TEXT,
              UNIQUE(email_id,channel,target_id,target_type,notification_version),
              FOREIGN KEY(email_id) REFERENCES ops_inbound_email(email_id));
            INSERT INTO ops_email_notification_outbox(
              notification_id,email_id,channel,target_id,target_type,notification_version,content,status,
              attempt_count,next_retry_at,last_error_code,created_at,sent_at)
            SELECT notification_id,email_id,channel,target_id,target_type,1,content,status,
              attempt_count,next_retry_at,last_error_code,created_at,sent_at
            FROM ops_email_notification_outbox_legacy;
            DROP TABLE ops_email_notification_outbox_legacy;
            """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(ops_inbound_email)")}
        if "extraction_mode" not in columns:
            self.connection.execute("ALTER TABLE ops_inbound_email ADD COLUMN extraction_mode TEXT")
        if "extractor_version" not in columns:
            self.connection.execute("ALTER TABLE ops_inbound_email ADD COLUMN extractor_version TEXT")
        if "ingestion_classification_code" not in columns:
            self.connection.execute("ALTER TABLE ops_inbound_email ADD COLUMN ingestion_classification_code TEXT")
        for name, definition in (
            ("review_version", "INTEGER NOT NULL DEFAULT 0"),
            ("confirmed_review_id", "TEXT"),
            ("confirmed_review_hash", "TEXT"),
            ("confirmed_at", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE ops_inbound_email ADD COLUMN {name} {definition}")
        audit_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(ops_email_review_audit)")}
        for name, definition in (
            ("idempotency_key", "TEXT"), ("base_version", "INTEGER"),
            ("result_version", "INTEGER"), ("result_hash", "TEXT"),
            ("change_meta_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in audit_columns:
                self.connection.execute(f"ALTER TABLE ops_email_review_audit ADD COLUMN {name} {definition}")
        self.connection.commit()

    def persist(self, envelope: EmailEnvelope, *, classification_code: str | None = None,
                status: str = "persisted") -> tuple[int, bool]:
        if status not in {"persisted", "received"}:
            raise ValueError("invalid inbound email status")
        now = _now()
        with self.tx() as cursor:
            try:
                cursor.execute("""INSERT INTO ops_inbound_email(account_id,provider,folder,uidvalidity,uid,internet_message_id,
                  raw_sha256,from_address,from_name,subject,text_body,envelope_json,status,ingestion_classification_code,
                  created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (envelope.account_id, envelope.provider, envelope.folder, envelope.uidvalidity, envelope.uid,
                   envelope.internet_message_id, envelope.raw_sha256, envelope.from_address, envelope.from_name,
                   envelope.subject, envelope.text_body, json.dumps(envelope.to_dict(), ensure_ascii=False), status,
                   classification_code, now, now))
                email_id, created = int(cursor.lastrowid), True
                for item in envelope.attachments:
                    cursor.execute("""INSERT INTO ops_inbound_email_attachment(email_id,filename,mime_type,size_bytes,sha256,scan_status)
                                      VALUES(?,?,?,?,?,?)""", (email_id, item.filename, item.content_type, item.size_bytes,
                                                               item.sha256, item.processing_status))
            except sqlite3.IntegrityError:
                cursor.execute("""SELECT email_id FROM ops_inbound_email WHERE account_id=? AND
                  ((folder=? AND uidvalidity=? AND uid=?) OR (internet_message_id<>'' AND internet_message_id=? AND raw_sha256=?))
                  LIMIT 1""", (envelope.account_id, envelope.folder, envelope.uidvalidity, envelope.uid,
                                envelope.internet_message_id, envelope.raw_sha256))
                row = cursor.fetchone()
                if not row:
                    raise
                email_id, created = int(row["email_id"]), False
        return email_id, created

    def get_cursor(self, account_id: str, folder: str) -> tuple[int, int]:
        row = self.connection.execute("SELECT uidvalidity,last_uid FROM ops_email_sync_cursor WHERE account_id=? AND folder=?",
                                      (account_id, folder)).fetchone()
        return (int(row["uidvalidity"]), int(row["last_uid"])) if row else (0, 0)

    def advance_cursor(self, account_id: str, folder: str, uidvalidity: int, uid: int) -> None:
        existing_validity, last_uid = self.get_cursor(account_id, folder)
        if existing_validity not in (0, uidvalidity):
            raise RuntimeError("uidvalidity_changed")
        if uid < last_uid:
            return
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO ops_email_sync_cursor(account_id,folder,uidvalidity,last_uid,updated_at)
              VALUES(?,?,?,?,?) ON CONFLICT(account_id,folder) DO UPDATE SET uidvalidity=excluded.uidvalidity,
              last_uid=excluded.last_uid,version=version+1,updated_at=excluded.updated_at""",
              (account_id, folder, uidvalidity, uid, _now()))

    def record_skipped(self, envelope: EmailEnvelope, classification_code: str) -> int:
        """Retain protected header metadata, a body-free skip receipt, and advance the cursor."""
        if not classification_code.startswith("email_trade_"):
            raise ValueError("invalid email trade classification code")
        now = _now()
        metadata = envelope.to_dict()
        metadata["text_body"] = ""
        with self.tx() as cursor:
            existing = cursor.execute(
                "SELECT uidvalidity,last_uid FROM ops_email_sync_cursor WHERE account_id=? AND folder=?",
                (envelope.account_id, envelope.folder),
            ).fetchone()
            if existing and int(existing["uidvalidity"]) != envelope.uidvalidity:
                raise RuntimeError("uidvalidity_changed")
            cursor.execute("""INSERT OR IGNORE INTO ops_inbound_email(
              account_id,provider,folder,uidvalidity,uid,internet_message_id,raw_sha256,
              from_address,from_name,subject,text_body,envelope_json,status,extraction_json,
              ingestion_classification_code,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'ignored_non_trade','{}',?,?,?)""", (
                envelope.account_id, envelope.provider, envelope.folder, envelope.uidvalidity, envelope.uid,
                envelope.internet_message_id, envelope.raw_sha256, envelope.from_address, envelope.from_name,
                envelope.subject, "", json.dumps(metadata, ensure_ascii=False), classification_code, now, now,
            ))
            row = cursor.execute("""SELECT email_id FROM ops_inbound_email WHERE account_id=? AND
              ((folder=? AND uidvalidity=? AND uid=?) OR
               (internet_message_id<>'' AND internet_message_id=? AND raw_sha256=?)) LIMIT 1""", (
                envelope.account_id, envelope.folder, envelope.uidvalidity, envelope.uid,
                envelope.internet_message_id, envelope.raw_sha256,
            )).fetchone()
            if row is None:
                raise RuntimeError("email_skip_persistence_failed")
            cursor.execute("""INSERT OR IGNORE INTO ops_email_ingestion_skip(
              account_id,folder,uidvalidity,uid,raw_sha256,classification_code,created_at)
              VALUES(?,?,?,?,?,?,?)""", (
                envelope.account_id, envelope.folder, envelope.uidvalidity, envelope.uid,
                envelope.raw_sha256, classification_code, now,
            ))
            last_uid = max(int(existing["last_uid"]) if existing else 0, envelope.uid)
            cursor.execute("""INSERT INTO ops_email_sync_cursor(account_id,folder,uidvalidity,last_uid,updated_at)
              VALUES(?,?,?,?,?) ON CONFLICT(account_id,folder) DO UPDATE SET uidvalidity=excluded.uidvalidity,
              last_uid=excluded.last_uid,version=version+1,updated_at=excluded.updated_at""",
              (envelope.account_id, envelope.folder, envelope.uidvalidity, last_uid, now))
        return int(row["email_id"])

    def list_skip_receipts(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute("""SELECT account_id,folder,uidvalidity,uid,raw_sha256,
          classification_code,created_at FROM ops_email_ingestion_skip ORDER BY skip_id DESC LIMIT ?""",
                                       (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(row) for row in rows]

    def acquire(self, email_id: int, owner: str, lease_seconds: int = 120) -> bool:
        now = _now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_inbound_email SET status='extracting',lease_owner=?,lease_until=?,
              attempt_count=attempt_count+1,updated_at=? WHERE email_id=? AND status IN ('persisted','retry_wait','extracting')
              AND (lease_until IS NULL OR lease_until<?)""", (owner, lease_until, now, email_id, now))
            if cursor.rowcount != 1:
                return False
            cursor.execute("INSERT INTO ops_email_processing_attempt(email_id,attempt_no,stage,started_at) SELECT email_id,attempt_count,'extract',? FROM ops_inbound_email WHERE email_id=?", (now, email_id))
            return True

    def complete_extraction(self, email_id: int, result: dict, *, extraction_mode: str = "unknown",
                            extractor_version: str = "unknown") -> None:
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_inbound_email SET status='needs_review',extraction_json=?,extraction_mode=?,
              extractor_version=?,lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=? WHERE email_id=?""",
                           (json.dumps(result, ensure_ascii=False), extraction_mode, extractor_version, _now(), email_id))
            cursor.execute("UPDATE ops_email_processing_attempt SET ended_at=? WHERE attempt_id=(SELECT MAX(attempt_id) FROM ops_email_processing_attempt WHERE email_id=?)", (_now(), email_id))

    def complete_extraction_with_notification(self, email_id: int, result: dict, *, target_id: str,
                                              target_type: str, content: str, extraction_mode: str = "unknown",
                                              extractor_version: str = "unknown") -> int:
        """Atomically persist extraction and its durable QQ notification intent."""
        now = _now()
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_inbound_email SET status='needs_review',extraction_json=?,extraction_mode=?,
              extractor_version=?,lease_owner=NULL,lease_until=NULL,last_error_code=NULL,updated_at=? WHERE email_id=?""",
                           (json.dumps(result, ensure_ascii=False), extraction_mode, extractor_version, now, email_id))
            cursor.execute("UPDATE ops_email_processing_attempt SET ended_at=? WHERE attempt_id=(SELECT MAX(attempt_id) FROM ops_email_processing_attempt WHERE email_id=?)", (now, email_id))
            previous = cursor.execute("""SELECT notification_id,notification_version,content,status
              FROM ops_email_notification_outbox WHERE email_id=? AND channel='qq' AND target_id=? AND target_type=?
              ORDER BY notification_version DESC LIMIT 1""", (email_id, target_id, target_type)).fetchone()
            if previous and previous["content"] == content:
                return int(previous["notification_id"])
            if previous and previous["status"] != "sent":
                cursor.execute("""UPDATE ops_email_notification_outbox SET content=?,status='pending',
                  next_retry_at=NULL,last_error_code=NULL,sent_at=NULL WHERE notification_id=?""",
                               (content, previous["notification_id"]))
                return int(previous["notification_id"])
            version = int(previous["notification_version"]) + 1 if previous else 1
            cursor.execute("""INSERT INTO ops_email_notification_outbox(
              email_id,channel,target_id,target_type,notification_version,content,created_at)
              VALUES(?,'qq',?,?,?,?,?)""", (email_id, target_id, target_type, version, content, now))
            row = cursor.execute("""SELECT notification_id FROM ops_email_notification_outbox
              WHERE email_id=? AND channel='qq' AND target_id=? AND target_type=? AND notification_version=?""",
                                 (email_id, target_id, target_type, version)).fetchone()
            return int(row["notification_id"])

    def pending_notifications(self, limit: int = 20) -> list[dict]:
        rows = self.connection.execute("""SELECT * FROM ops_email_notification_outbox
          WHERE status IN ('pending','retry_wait') AND (next_retry_at IS NULL OR next_retry_at<=?)
          ORDER BY notification_id LIMIT ?""", (_now(), limit)).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_notification_outbox SET status='sent',attempt_count=attempt_count+1,
              sent_at=?,last_error_code=NULL,next_retry_at=NULL WHERE notification_id=?""", (_now(), notification_id))

    def mark_notification_failed(self, notification_id: int, error_code: str) -> None:
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_email_notification_outbox SET status='retry_wait',attempt_count=attempt_count+1,
              next_retry_at=?,last_error_code=? WHERE notification_id=?""",
                           (retry_at, error_code[:100], notification_id))

    def fail(self, email_id: int, error_code: str, retryable: bool = True, max_attempts: int = 3) -> None:
        row = self.get(email_id)
        status = "retry_wait" if retryable and row and row["attempt_count"] < max_attempts else "failed"
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z") if status == "retry_wait" else None
        with self.tx() as cursor:
            cursor.execute("UPDATE ops_inbound_email SET status=?,next_retry_at=?,last_error_code=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE email_id=?",
                           (status, retry_at, error_code[:100], _now(), email_id))
            cursor.execute("UPDATE ops_email_processing_attempt SET error_code=?,ended_at=? WHERE attempt_id=(SELECT MAX(attempt_id) FROM ops_email_processing_attempt WHERE email_id=?)", (error_code[:100], _now(), email_id))

    def enqueue_notification(self, email_id: int, *, target_id: str, target_type: str, content: str) -> int:
        with self.tx() as cursor:
            row = cursor.execute("""SELECT notification_id,notification_version,status,content FROM ops_email_notification_outbox
              WHERE email_id=? AND channel='qq' AND target_id=? AND target_type=?
              ORDER BY notification_version DESC LIMIT 1""", (email_id, target_id, target_type)).fetchone()
            if row and row["content"] == content:
                return int(row["notification_id"])
            if row and row["status"] != "sent":
                cursor.execute("""UPDATE ops_email_notification_outbox SET content=?,status='pending',
                  next_retry_at=NULL,last_error_code=NULL WHERE notification_id=?""",
                               (content, row["notification_id"]))
            else:
                version = int(row["notification_version"]) + 1 if row else 1
                cursor.execute("""INSERT INTO ops_email_notification_outbox(
                  email_id,channel,target_id,target_type,notification_version,content,created_at)
                  VALUES(?,'qq',?,?,?,?,?)""", (email_id, target_id, target_type, version, content, _now()))
                row = cursor.execute("SELECT notification_id FROM ops_email_notification_outbox WHERE rowid=last_insert_rowid()").fetchone()
            return int(row["notification_id"])

    def get(self, email_id: int):
        return self.connection.execute("SELECT * FROM ops_inbound_email WHERE email_id=?", (email_id,)).fetchone()

    def recoverable_extractions(self, limit: int = 20, *,
                                classification_code: str | None = None) -> list[int]:
        now = _now()
        classification_sql = " AND ingestion_classification_code=?" if classification_code else ""
        params: list[object] = [now, now]
        if classification_code:
            params.append(classification_code)
        params.append(limit)
        rows = self.connection.execute(f"""SELECT email_id FROM ops_inbound_email
          WHERE (status='persisted'
             OR (status='retry_wait' AND (next_retry_at IS NULL OR next_retry_at<=?))
             OR (status='extracting' AND (lease_until IS NULL OR lease_until<?)))
          {classification_sql} ORDER BY email_id LIMIT ?""", params).fetchall()
        return [int(row["email_id"]) for row in rows]

    def release_extraction_for_retry(self, email_id: int, error_code: str = "WorkerRestartRecovery") -> bool:
        """Release exactly one interrupted extraction without touching persisted email content."""
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_inbound_email SET status='retry_wait',lease_owner=NULL,lease_until=NULL,
              next_retry_at=NULL,last_error_code=?,updated_at=? WHERE email_id=? AND status='extracting'""",
                           (error_code[:100], _now(), email_id))
            return cursor.rowcount == 1

    def reopen_failed_extraction(self, email_id: int, reason: str = "ModelChangedRetry") -> bool:
        """Explicit operator action after configuration/model remediation."""
        with self.tx() as cursor:
            cursor.execute("""UPDATE ops_inbound_email SET status='retry_wait',attempt_count=0,lease_owner=NULL,
              lease_until=NULL,next_retry_at=NULL,last_error_code=?,updated_at=? WHERE email_id=? AND status='failed'""",
                           (reason[:100], _now(), email_id))
            return cursor.rowcount == 1

    def reopen_review_for_extraction(self, email_id: int, reason: str = "ValidationChangedRetry") -> bool:
        """Explicitly re-extract a needs_review record after validator remediation."""
        with self.tx() as cursor:
            row = cursor.execute(
                "SELECT status FROM ops_inbound_email WHERE email_id=?", (email_id,)
            ).fetchone()
            if row is None or row["status"] not in {"needs_review", "confirmed"}:
                return False
            cursor.execute("""UPDATE ops_inbound_email SET status='retry_wait',attempt_count=0,lease_owner=NULL,
              lease_until=NULL,next_retry_at=NULL,last_error_code=?,confirmed_review_id=NULL,
              confirmed_review_hash=NULL,confirmed_at=NULL,review_version=review_version+1,updated_at=?
              WHERE email_id=? AND status IN ('needs_review','confirmed')""",
                           (reason[:100], _now(), email_id))
            updated = cursor.rowcount == 1
            if updated:
                cursor.execute("""UPDATE ops_email_review_revision SET status='superseded'
                  WHERE email_id=? AND status='confirmed'""", (email_id,))
            return updated

    @staticmethod
    def _encode_cursor(created_at: str, email_id: int) -> str:
        value = json.dumps({"created_at": created_at, "email_id": email_id}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[str, int]:
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded).decode())
            created_at, email_id = data["created_at"], int(data["email_id"])
            if not isinstance(created_at, str) or email_id <= 0:
                raise ValueError
            return created_at, email_id
        except Exception as exc:
            raise EmailReviewRepositoryError("email_review_invalid_request") from exc

    def list_inbound_reviews(self, *, status: str = "needs_review", account_id: str | None = None,
                             cursor: str | None = None, limit: int = 20) -> tuple[list[dict], str | None]:
        if status not in {"all", "received", "ignored_non_trade", "needs_review", "confirmed", "failed"} or not 1 <= limit <= 100:
            raise EmailReviewRepositoryError("email_review_invalid_request")
        conditions, params = ([] if status == "all" else ["status=?"]), ([] if status == "all" else [status])
        if account_id:
            conditions.append("account_id=?")
            params.append(account_id)
        if cursor:
            created_at, email_id = self._decode_cursor(cursor)
            conditions.append("(created_at<? OR (created_at=? AND email_id<?))")
            params.extend([created_at, created_at, email_id])
        rows = self.connection.execute(f"""SELECT email_id,account_id,provider,from_address,subject,status,
          extraction_json,extraction_mode,extractor_version,review_version,envelope_json,created_at,
          ingestion_classification_code
          FROM ops_inbound_email WHERE {' AND '.join(conditions) if conditions else '1=1'}
          ORDER BY created_at DESC,email_id DESC LIMIT ?""", (*params, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = self._encode_cursor(rows[-1]["created_at"], int(rows[-1]["email_id"])) if has_more else None
        return [dict(row) for row in rows], next_cursor

    def get_inbound_review_detail(self, email_id: int) -> dict | None:
        row = self.connection.execute("""SELECT email_id,account_id,provider,folder,uidvalidity,uid,
          internet_message_id,from_address,from_name,subject,text_body,envelope_json,status,extraction_json,
          extraction_mode,extractor_version,review_version,confirmed_review_id,confirmed_review_hash,
          confirmed_at,created_at,updated_at,ingestion_classification_code
          FROM ops_inbound_email WHERE email_id=?""", (email_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["attachments"] = [dict(value) for value in self.connection.execute("""SELECT
          attachment_id,filename,mime_type,size_bytes,sha256,scan_status
          FROM ops_inbound_email_attachment WHERE email_id=? ORDER BY attachment_id""", (email_id,)).fetchall()]
        return item

    def confirm_review(self, *, email_id: int, base_version: int, idempotency_key: str,
                       request_hash: str, base_extraction_hash: str, snapshot: dict,
                       review_hash: str, change_meta: list[dict], reviewer: str) -> dict:
        now, review_id = _now(), str(uuid.uuid4())
        with self.tx() as cursor:
            existing = cursor.execute("""SELECT * FROM ops_email_review_revision
              WHERE email_id=? AND idempotency_key=?""", (email_id, idempotency_key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise EmailReviewRepositoryError("email_review_idempotency_conflict")
                return dict(existing)
            row = cursor.execute("SELECT * FROM ops_inbound_email WHERE email_id=?", (email_id,)).fetchone()
            if row is None:
                raise EmailReviewRepositoryError("email_inbound_not_found")
            if row["status"] != "needs_review" or int(row["review_version"] or 0) != base_version:
                raise EmailReviewRepositoryError("email_review_version_conflict")
            current_hash = hashlib.sha256((row["extraction_json"] or "{}").encode("utf-8")).hexdigest()
            if current_hash != base_extraction_hash:
                raise EmailReviewRepositoryError("email_review_superseded")
            result_version = base_version + 1
            cursor.execute("""INSERT INTO ops_email_review_revision(
              review_id,email_id,review_version,base_extraction_hash,reviewed_json,review_hash,
              change_meta_json,reviewer_id,idempotency_key,request_hash,status,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,'confirmed',?)""", (
                review_id, email_id, result_version, base_extraction_hash,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                review_hash, json.dumps(change_meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                reviewer, idempotency_key, request_hash, now,
            ))
            cursor.execute("""UPDATE ops_inbound_email SET status='confirmed',review_version=?,
              confirmed_review_id=?,confirmed_review_hash=?,confirmed_at=?,updated_at=?
              WHERE email_id=? AND status='needs_review' AND review_version=?""",
                           (result_version, review_id, review_hash, now, now, email_id, base_version))
            if cursor.rowcount != 1:
                raise EmailReviewRepositoryError("email_review_version_conflict")
            cursor.execute("""INSERT INTO ops_email_review_audit(
              email_id,reviewer,action,changes_json,created_at,idempotency_key,base_version,
              result_version,result_hash,change_meta_json)
              VALUES(?,?,'confirm','[]',?,?,?,?,?,?)""", (
                email_id, reviewer, now, idempotency_key, base_version, result_version,
                review_hash, json.dumps(change_meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
            return dict(cursor.execute(
                "SELECT * FROM ops_email_review_revision WHERE review_id=?", (review_id,)
            ).fetchone())

    def get_confirmed_review(self, email_id: int, review_id: str, review_hash: str) -> dict | None:
        row = self.connection.execute("""SELECT r.* FROM ops_email_review_revision r
          JOIN ops_inbound_email e ON e.email_id=r.email_id
          JOIN ops_email_account a ON a.account_id=e.account_id AND a.deleted_at IS NULL
          WHERE e.email_id=? AND e.status='confirmed' AND e.confirmed_review_id=?
            AND e.confirmed_review_hash=? AND r.review_id=? AND r.review_hash=? AND r.status='confirmed'""",
                                      (email_id, review_id, review_hash, review_id, review_hash)).fetchone()
        return dict(row) if row else None

    def get_review_by_idempotency(self, email_id: int, idempotency_key: str) -> dict | None:
        row = self.connection.execute("""SELECT * FROM ops_email_review_revision
          WHERE email_id=? AND idempotency_key=?""", (email_id, idempotency_key)).fetchone()
        return dict(row) if row else None

    def supersede_review(self, email_id: int, reason: str, *, actor: str = "email_review_system") -> bool:
        with self.tx() as cursor:
            row = cursor.execute("SELECT review_version FROM ops_inbound_email WHERE email_id=?", (email_id,)).fetchone()
            if row is None:
                raise EmailReviewRepositoryError("email_inbound_not_found")
            cursor.execute("UPDATE ops_email_review_revision SET status='superseded' WHERE email_id=? AND status='confirmed'", (email_id,))
            cursor.execute("""UPDATE ops_inbound_email SET status='needs_review',review_version=review_version+1,
              confirmed_review_id=NULL,confirmed_review_hash=NULL,confirmed_at=NULL,updated_at=? WHERE email_id=?""",
                           (_now(), email_id))
            updated = cursor.rowcount == 1
            cursor.execute("""INSERT INTO ops_email_review_audit(
              email_id,reviewer,action,changes_json,created_at,base_version,result_version,change_meta_json)
              VALUES(?,?,'supersede','[]',?,?,?,'[]')""",
                           (email_id, actor, _now(), int(row["review_version"]), int(row["review_version"]) + 1))
            return updated

    def list_reviews(self, limit: int = 20) -> list[dict]:
        rows = self.connection.execute("SELECT email_id,subject,status,extraction_json,created_at FROM ops_inbound_email WHERE status='needs_review' ORDER BY email_id LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "extraction": json.loads(row["extraction_json"] or "{}")} for row in rows]

    def confirm(self, email_id: int, reviewer: str, reviewed_result: dict | None = None) -> dict:
        row = self.get(email_id)
        if not row or row["status"] != "needs_review":
            raise ValueError("email is not awaiting review")
        result = json.loads(row["extraction_json"] or "{}")
        if reviewed_result:
            result = reviewed_result
        def has_pending(value) -> bool:
            if isinstance(value, dict):
                return value.get("status") == "pending_confirmation" or any(has_pending(item) for item in value.values())
            if isinstance(value, list):
                return any(has_pending(item) for item in value)
            return False
        if has_pending(result):
            raise ValueError("pending fields must be resolved before confirmation")
        with self.tx() as cursor:
            cursor.execute("UPDATE ops_inbound_email SET status='confirmed',extraction_json=?,updated_at=? WHERE email_id=?",
                           (json.dumps(result, ensure_ascii=False), _now(), email_id))
            cursor.execute("INSERT INTO ops_email_review_audit(email_id,reviewer,action,changes_json,created_at) VALUES(?,?,?,?,?)",
                           (email_id, reviewer, "confirm", json.dumps(reviewed_result or {}, ensure_ascii=False), _now()))
        return result
