"""Persistent core for the internal foreign-trade growth workbench.

This module has no HTTP or SMTP side effects.  SQLite is the local default;
MySQL uses the same record shapes and requires an explicitly applied migration.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import threading
import uuid
from typing import Any, Callable, Iterator, Mapping, Protocol, runtime_checkable

from agent.business.config import load_business_config


STAGES = (
    "product_loader", "prospect_discovery", "prospect_list_enrichment",
    "company_research", "prospect_scoring", "decision_maker_finder",
    "email_crafting", "reply_classification", "follow_up_planner",
    "quotation_generator",
)
STAGE_STATUSES = frozenset({
    "pending", "running", "blocked_missing_input", "waiting_review",
    "completed", "failed", "cancelled",
})


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id() -> str:
    return str(uuid.uuid4())


class TradeWorkbenchError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code, self.status_code = code, status_code


@dataclass(frozen=True)
class StageEnvelope:
    stage: str
    status: str
    version: int
    result: dict[str, Any]
    evidence: list[dict[str, Any]]
    risks: list[str]
    missing_inputs: list[str]
    next_stage: str | None
    next_required_inputs: list[str]
    human_review_required: bool
    run_id: str = ""
    started_at: str = ""
    completed_at: str | None = None


@dataclass(frozen=True)
class StageJob:
    job_id: str
    campaign_id: str
    stage: str
    input: dict[str, Any]
    business_key: str
    status: str
    lease_owner: str | None
    lease_until: str | None
    attempt_count: int


@runtime_checkable
class TradeWorkbenchRepository(Protocol):
    """Backend-neutral persistence contract used by the workbench service."""

    def create_campaign(self, name: str, *, created_by: str = "local_operator",
                        campaign_id: str | None = None) -> dict[str, Any]: ...
    def get_campaign(self, campaign_id: str) -> dict[str, Any]: ...
    def list_campaigns(self, limit: int = 100) -> list[dict[str, Any]]: ...
    def pause(self, campaign_id: str, *, expected_etag: str,
              actor: str = "local_operator") -> dict[str, Any]: ...
    def resume(self, campaign_id: str, *, expected_etag: str,
               actor: str = "local_operator") -> dict[str, Any]: ...
    def save_input(self, campaign_id: str, input_type: str, payload: Mapping[str, Any],
                   *, source_name: str | None = None) -> dict[str, Any]: ...
    def latest_input(self, campaign_id: str, input_type: str) -> dict[str, Any] | None: ...
    def save_stage_result(self, campaign_id: str, **kwargs: Any) -> StageEnvelope: ...
    def list_stage_results(self, campaign_id: str) -> list[StageEnvelope]: ...
    def latest_stage_result(self, campaign_id: str, stage: str) -> StageEnvelope | None: ...
    def enqueue_job(self, campaign_id: str, stage: str, input_payload: Mapping[str, Any],
                    *, business_key: str | None = None) -> StageJob: ...
    def claim_job(self, worker_id: str, *, lease_seconds: int = 60) -> StageJob | None: ...
    def finish_job(self, job_id: str, worker_id: str, *, status: str = "completed",
                   error_code: str | None = None) -> StageJob: ...
    def idempotent(self, scope: str, key: str, payload: Mapping[str, Any],
                   action: Callable[[], Any]) -> dict[str, Any]: ...
    def audit_ai(self, campaign_id: str | None, stage: str, **kwargs: Any) -> None: ...
    def get_quote_draft(self, quote_draft_id: str) -> dict[str, Any] | None: ...
    def save_artifacts(self, campaign_id: str, object_type: str, object_id: str,
                       object_version: int, outputs: Mapping[str, str]) -> list[dict[str, Any]]: ...
    def get_artifact(self, object_type: str, object_id: str,
                     artifact_kind: str) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


class SQLiteTradeWorkbenchRepository:
    def __init__(self, database_path: str | Path | None = None):
        configured = database_path or load_business_config().database_path
        self.database_path = str(Path(configured).resolve())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, check_same_thread=False,
                                          isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._idempotency_lock = threading.RLock()
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def tx(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def migrate(self) -> None:
        path = Path(__file__).with_name("migrations") / "006_trade_workbench.sqlite.sql"
        script = path.read_text(encoding="utf-8")
        try:
            with self._lock:
                self.connection.executescript(script)
                columns = {row[1] for row in self.connection.execute("PRAGMA table_info(trade_outreach_outbox)")}
                additions = {
                    "max_attempts": "INTEGER NOT NULL DEFAULT 5",
                    "next_attempt_at": "TEXT", "smtp_message_id": "TEXT", "accepted_at": "TEXT",
                }
                for name, ddl in additions.items():
                    if name not in columns:
                        self.connection.execute(f"ALTER TABLE trade_outreach_outbox ADD COLUMN {name} {ddl}")
        except sqlite3.DatabaseError as exc:
            raise TradeWorkbenchError("trade_workbench_migration_failed", 503) from exc

    @staticmethod
    def _campaign(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["paused"] = bool(result["paused"])
        result["etag"] = f'"trade-campaign-{result["campaign_id"]}-{result["version"]}"'
        return result

    def create_campaign(self, name: str, *, created_by: str = "local_operator",
                        campaign_id: str | None = None) -> dict[str, Any]:
        name = str(name).strip()
        if not name or len(name) > 255:
            raise TradeWorkbenchError("trade_campaign_invalid_name")
        campaign_id, now = campaign_id or new_id(), now_utc()
        with self.tx(immediate=True) as db:
            db.execute("""INSERT INTO trade_campaign(campaign_id,name,status,current_stage,created_by,created_at,updated_at)
              VALUES(?,?,'active','product_loader',?,?,?)""",
                       (campaign_id, name, created_by, now, now))
            self._audit(db, campaign_id, created_by, "campaign.create", "campaign",
                        campaign_id, "success", {})
        return self.get_campaign(campaign_id)

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM trade_campaign WHERE campaign_id=?",
                                      (campaign_id,)).fetchone()
        if not row:
            raise TradeWorkbenchError("trade_campaign_not_found", 404)
        result = self._campaign(row)
        result["stages"] = [asdict(item) for item in self.list_stage_results(campaign_id)]
        return result

    def list_campaigns(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        rows = self.connection.execute(
            "SELECT * FROM trade_campaign ORDER BY updated_at DESC,campaign_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._campaign(row) for row in rows]

    def _require_etag(self, row: sqlite3.Row, expected_etag: str | None) -> None:
        if not expected_etag:
            raise TradeWorkbenchError("trade_precondition_required", 428)
        actual = f'"trade-campaign-{row["campaign_id"]}-{row["version"]}"'
        if expected_etag != actual:
            raise TradeWorkbenchError("trade_version_conflict", 412)

    def pause(self, campaign_id: str, *, expected_etag: str,
              actor: str = "local_operator") -> dict[str, Any]:
        return self._set_paused(campaign_id, True, expected_etag, actor)

    def resume(self, campaign_id: str, *, expected_etag: str,
               actor: str = "local_operator") -> dict[str, Any]:
        return self._set_paused(campaign_id, False, expected_etag, actor)

    def _set_paused(self, campaign_id: str, paused: bool, expected_etag: str,
                    actor: str) -> dict[str, Any]:
        with self.tx(immediate=True) as db:
            row = db.execute("SELECT * FROM trade_campaign WHERE campaign_id=?", (campaign_id,)).fetchone()
            if not row:
                raise TradeWorkbenchError("trade_campaign_not_found", 404)
            self._require_etag(row, expected_etag)
            db.execute("UPDATE trade_campaign SET paused=?,status=?,version=version+1,updated_at=? WHERE campaign_id=?",
                       (int(paused), "paused" if paused else "active", now_utc(), campaign_id))
            self._audit(db, campaign_id, actor, "campaign.pause" if paused else "campaign.resume",
                        "campaign", campaign_id, "success", {})
        return self.get_campaign(campaign_id)

    def save_input(self, campaign_id: str, input_type: str, payload: Mapping[str, Any],
                   *, source_name: str | None = None) -> dict[str, Any]:
        self.get_campaign(campaign_id)
        normalized, digest = canonical_json(dict(payload)), content_hash(dict(payload))
        with self.tx(immediate=True) as db:
            version = int(db.execute("SELECT COALESCE(MAX(version),0)+1 FROM trade_input_snapshot WHERE campaign_id=? AND input_type=?",
                                     (campaign_id, input_type)).fetchone()[0])
            snapshot_id = new_id()
            db.execute("""INSERT INTO trade_input_snapshot(snapshot_id,campaign_id,input_type,version,payload_json,payload_hash,source_name,created_at)
              VALUES(?,?,?,?,?,?,?,?)""", (snapshot_id, campaign_id, input_type, version,
                                             normalized, digest, source_name, now_utc()))
        return {"snapshot_id": snapshot_id, "input_type": input_type, "version": version,
                "payload_hash": digest, "source_name": source_name}

    def latest_input(self, campaign_id: str, input_type: str) -> dict[str, Any] | None:
        row = self.connection.execute("""SELECT * FROM trade_input_snapshot WHERE campaign_id=? AND input_type=?
          ORDER BY version DESC LIMIT 1""", (campaign_id, input_type)).fetchone()
        if not row:
            return None
        result = dict(row); result["payload"] = json.loads(result.pop("payload_json")); return result

    def database_product_count(self) -> int:
        try:
            return int(self.connection.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0])
        except sqlite3.DatabaseError:
            return 0

    def load_product_context(self, sku: str = "") -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute("""SELECT p.*,x.product_size,x.packing_size,x.weight_kg,
              x.hs_code,x.certifications_json,x.materials_json,x.applications_json,x.source_kind,
              x.source_ref,x.version AS profile_version FROM products p
              LEFT JOIN trade_product_profile x ON x.sku=p.sku
              WHERE p.active=1 AND (?='' OR p.sku=?) ORDER BY p.sku""", (sku,sku)).fetchall()
        except sqlite3.DatabaseError:
            return []
        return [dict(row) for row in rows]

    def load_price_rules(self, sku: str, quantity: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [sku]
        quantity_clause = ""
        if quantity is not None:
            quantity_clause = " AND CAST(min_qty AS REAL)<=? AND (max_qty IS NULL OR CAST(max_qty AS REAL)>=?)"
            params.extend([float(quantity),float(quantity)])
        params.extend([now_utc(),now_utc()])
        rows = self.connection.execute("""SELECT * FROM trade_price_rule WHERE sku=?
          AND approval_status='approved'""" + quantity_clause +
          " AND valid_from<=? AND valid_until>=? ORDER BY CAST(min_qty AS REAL) DESC,version DESC", params).fetchall()
        return [dict(row) for row in rows]

    def upsert_prospects(self, campaign_id: str, prospects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        with self.tx(immediate=True) as db:
            for item in prospects:
                pid, now = new_id(), now_utc()
                email = str(item.get("contact_email") or "没有")
                domain = str(item.get("normalized_domain") or "")
                email_domain = email.rsplit("@",1)[-1].lower() if "@" in email else ""
                email_result = ("domain_match" if domain and email_domain == domain else
                                "format_valid" if "@" in email else "没有")
                phone = str(item.get("contact_phone") or "没有")
                db.execute("""INSERT INTO trade_prospect(prospect_id,campaign_id,company_name,normalized_domain,website,country,business_type,
                  source_urls_json,source_notes_json,contact_email,contact_phone,email_result,phone_result,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(campaign_id,normalized_domain) DO UPDATE SET
                  company_name=excluded.company_name,website=excluded.website,country=excluded.country,
                  business_type=excluded.business_type,source_urls_json=excluded.source_urls_json,
                  source_notes_json=excluded.source_notes_json,contact_email=excluded.contact_email,
                  contact_phone=excluded.contact_phone,email_result=excluded.email_result,
                  phone_result=excluded.phone_result,updated_at=excluded.updated_at""",
                  (pid,campaign_id,item["company_name"],domain,item["website"],item.get("country",""),
                   item.get("business_type",""),canonical_json(item.get("source_urls",[])),
                   canonical_json(item.get("source_notes",[])),email,phone,email_result,
                   "found" if phone != "没有" else "没有",now,now))
                row = db.execute("SELECT prospect_id FROM trade_prospect WHERE campaign_id=? AND normalized_domain=?",
                                 (campaign_id,domain)).fetchone()
                saved.append({**item,"prospect_id":row[0],"email_result":email_result})
        return saved

    def mark_prospect_do_not_contact(self, prospect_id: str) -> None:
        with self.tx(immediate=True) as db:
            db.execute("UPDATE trade_prospect SET do_not_contact=1,status='unsubscribed',updated_at=? WHERE prospect_id=?",
                       (now_utc(),prospect_id))

    def prospect_do_not_contact(self, prospect_id: str) -> bool:
        row = self.connection.execute("SELECT do_not_contact FROM trade_prospect WHERE prospect_id=?",
                                      (prospect_id,)).fetchone()
        return bool(row[0]) if row else False

    def create_outreach_draft(self, campaign_id: str, prospect_id: str,
                              subject: str, body: str) -> tuple[str,int,str]:
        with self.tx(immediate=True) as db:
            version = int(db.execute("SELECT COALESCE(MAX(version),0)+1 FROM trade_outreach_draft WHERE prospect_id=?",
                                     (prospect_id,)).fetchone()[0])
            draft_id, digest = new_id(), content_hash({"subject":subject,"body":body})
            db.execute("""INSERT INTO trade_outreach_draft(draft_id,campaign_id,prospect_id,version,
              subject,body,content_hash,status,created_at) VALUES(?,?,?,?,?,?,?,'draft',?)""",
                       (draft_id,campaign_id,prospect_id,version,subject,body,digest,now_utc()))
        return draft_id,version,digest

    def approve_outreach_draft(self, draft_id: str, expected_hash: str,
                               actor: str) -> dict[str, Any]:
        with self.tx(immediate=True) as db:
            row = db.execute("SELECT * FROM trade_outreach_draft WHERE draft_id=?",(draft_id,)).fetchone()
            if not row: raise TradeWorkbenchError("trade_draft_not_found",404)
            if row["content_hash"] != expected_hash: raise TradeWorkbenchError("trade_draft_hash_mismatch",409)
            if row["status"] not in {"draft","approved"}: raise TradeWorkbenchError("trade_draft_not_approvable",409)
            db.execute("UPDATE trade_outreach_draft SET status='approved',approved_by=?,approved_at=? WHERE draft_id=?",
                       (actor,now_utc(),draft_id))
            self._audit(db,row["campaign_id"],actor,"outreach.approve","outreach_draft",draft_id,"success",{"content_hash":expected_hash})
        return {"draft_id":draft_id,"status":"approved","content_hash":expected_hash,"queue_required":True}

    def queue_outreach_draft(self, draft_id: str, account_id: str, recipient: str,
                             expected_hash: str) -> dict[str, Any]:
        from agent.business.email_account_repository import EmailAccountRepository
        account = EmailAccountRepository(self.connection).get(account_id)
        if not account or not account["outbound_enabled"] or account["status"] != "healthy":
            raise TradeWorkbenchError("trade_email_account_not_sendable",409)
        if recipient.casefold() not in {value.casefold() for value in account["allowed_recipients"]}:
            raise TradeWorkbenchError("trade_recipient_not_allowed",409)
        with self.tx(immediate=True) as db:
            row = db.execute("""SELECT d.*,p.do_not_contact,p.email_result,p.contact_email
              FROM trade_outreach_draft d JOIN trade_prospect p ON p.prospect_id=d.prospect_id
              WHERE d.draft_id=?""",(draft_id,)).fetchone()
            if not row: raise TradeWorkbenchError("trade_draft_not_found",404)
            if row["status"] != "approved": raise TradeWorkbenchError("trade_draft_not_approved",409)
            if row["content_hash"] != expected_hash: raise TradeWorkbenchError("trade_draft_hash_mismatch",409)
            if row["do_not_contact"]: raise TradeWorkbenchError("trade_prospect_do_not_contact",409)
            if recipient.casefold() != str(row["contact_email"]).casefold() or row["email_result"] == "没有":
                raise TradeWorkbenchError("trade_recipient_not_evidenced",409)
            command_id, now = new_id(), now_utc()
            db.execute("""INSERT OR IGNORE INTO trade_outreach_outbox(command_id,draft_id,draft_version,
              content_hash,account_id,recipient,status,created_at,updated_at)
              VALUES(?,?,?,?,?,?,'pending',?,?)""",
                       (command_id,draft_id,row["version"],expected_hash,account_id,recipient,now,now))
            stored = db.execute("SELECT command_id,status FROM trade_outreach_outbox WHERE draft_id=? AND draft_version=?",
                                (draft_id,row["version"])).fetchone()
            self._audit(db,row["campaign_id"],"local_operator","outreach.queue","outreach_draft",draft_id,"success",
                        {"command_id":stored["command_id"],"content_hash":expected_hash})
        return {"command_id":stored["command_id"],"status":stored["status"],"smtp_worker_started":False}

    def claim_outreach(self, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
        now=now_utc();until=(datetime.now(timezone.utc)+timedelta(seconds=max(1,lease_seconds))).isoformat().replace("+00:00","Z")
        with self.tx(immediate=True) as db:
            row=db.execute("""SELECT * FROM trade_outreach_outbox WHERE
              ((status IN ('pending','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=?))
              OR (status='sending' AND lease_until<?)) ORDER BY created_at,command_id LIMIT 1""",(now,now)).fetchone()
            if not row:return None
            message_id=row["smtp_message_id"] or f"<{row['command_id']}@nanoclaw.local>"
            db.execute("""UPDATE trade_outreach_outbox SET status='sending',lease_owner=?,lease_until=?,
              attempt_count=attempt_count+1,smtp_message_id=?,updated_at=? WHERE command_id=?""",
                       (worker_id,until,message_id,now,row["command_id"]))
            row=db.execute("SELECT * FROM trade_outreach_outbox WHERE command_id=?",(row["command_id"],)).fetchone()
        return dict(row)

    def revalidate_outreach(self, command_id: str, worker_id: str) -> tuple[dict[str, Any],dict[str, Any]]:
        row=self.connection.execute("""SELECT o.*,d.subject,d.body,d.status AS draft_status,d.content_hash AS draft_hash,
          d.version AS current_draft_version,p.do_not_contact,p.contact_email,p.email_result,
          a.provider,a.address,a.secret_ref,a.sender_name,a.outbound_enabled,a.status AS account_status,
          a.allowed_recipients_json FROM trade_outreach_outbox o JOIN trade_outreach_draft d ON d.draft_id=o.draft_id
          JOIN trade_prospect p ON p.prospect_id=d.prospect_id JOIN ops_email_account a ON a.account_id=o.account_id
          WHERE o.command_id=? AND a.deleted_at IS NULL""",(command_id,)).fetchone()
        if not row:raise TradeWorkbenchError("trade_outreach_command_not_found",404)
        if row["status"]!="sending" or row["lease_owner"]!=worker_id:raise TradeWorkbenchError("trade_outreach_lease_lost",409)
        allowed={str(value).casefold() for value in json.loads(row["allowed_recipients_json"] or "[]")}
        if (row["draft_status"]!="approved" or row["draft_hash"]!=row["content_hash"] or
            int(row["current_draft_version"])!=int(row["draft_version"]) or row["do_not_contact"] or
            str(row["contact_email"]).casefold()!=str(row["recipient"]).casefold() or
            row["email_result"]=="没有" or not row["outbound_enabled"] or row["account_status"]!="healthy" or
            str(row["recipient"]).casefold() not in allowed):
            raise TradeWorkbenchError("trade_outreach_stale",409)
        delivery={"delivery_id":row["command_id"],"recipient":row["recipient"],
                  "subject_snapshot":row["subject"],"body_snapshot":row["body"],
                  "smtp_message_id":row["smtp_message_id"],"in_reply_to":None}
        account={key:row[key] for key in ("provider","address","secret_ref","sender_name")}
        return delivery,account

    def finish_outreach(self, command_id: str, worker_id: str, *, status: str,
                        error_code: str | None = None) -> dict[str, Any]:
        if status not in {"accepted","retry_wait","dead_letter","outcome_unknown","stale"}:
            raise TradeWorkbenchError("trade_outreach_status_invalid")
        with self.tx(immediate=True) as db:
            row=db.execute("SELECT * FROM trade_outreach_outbox WHERE command_id=?",(command_id,)).fetchone()
            if not row:raise TradeWorkbenchError("trade_outreach_command_not_found",404)
            if row["status"]=="accepted":return dict(row)
            if row["status"]!="sending" or row["lease_owner"]!=worker_id:raise TradeWorkbenchError("trade_outreach_lease_lost",409)
            next_attempt=(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat().replace("+00:00","Z") if status=="retry_wait" else None
            accepted=now_utc() if status=="accepted" else None
            db.execute("""UPDATE trade_outreach_outbox SET status=?,lease_owner=NULL,lease_until=NULL,
              next_attempt_at=?,accepted_at=?,error_code=?,updated_at=? WHERE command_id=?""",
                       (status,next_attempt,accepted,error_code,now_utc(),command_id))
            row=db.execute("SELECT * FROM trade_outreach_outbox WHERE command_id=?",(command_id,)).fetchone()
        return dict(row)

    def create_quote_draft(self, campaign_id: str, prospect_id: str | None,
                           quote: Mapping[str, Any]) -> tuple[str,int,str]:
        number = str(quote["quotation_number"])
        with self.tx(immediate=True) as db:
            version = int(db.execute("SELECT COALESCE(MAX(version),0)+1 FROM trade_quote_draft WHERE campaign_id=? AND quotation_number=?",
                                     (campaign_id,number)).fetchone()[0])
            qid, digest = new_id(), content_hash(dict(quote))
            db.execute("""INSERT INTO trade_quote_draft(quote_draft_id,campaign_id,prospect_id,version,
              quotation_number,payload_json,content_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
              (qid,campaign_id,prospect_id,version,number,canonical_json(dict(quote)),digest,
               "blocked" if quote.get("quotation_status") == "blocked" else "draft",now_utc()))
        return qid,version,digest

    def approve_quote_draft(self, quote_draft_id: str, expected_hash: str,
                            actor: str) -> dict[str, Any]:
        with self.tx(immediate=True) as db:
            row = db.execute("SELECT * FROM trade_quote_draft WHERE quote_draft_id=?",(quote_draft_id,)).fetchone()
            if not row: raise TradeWorkbenchError("trade_quote_draft_not_found",404)
            if row["status"] == "blocked": raise TradeWorkbenchError("trade_quotation_blocked",409)
            if row["content_hash"] != expected_hash: raise TradeWorkbenchError("trade_quote_hash_mismatch",409)
            db.execute("UPDATE trade_quote_draft SET status='approved',approved_by=?,approved_at=? WHERE quote_draft_id=?",
                       (actor,now_utc(),quote_draft_id))
            self._audit(db,row["campaign_id"],actor,"quote_draft.approve","quote_draft",quote_draft_id,"success",{"content_hash":expected_hash,"published":False,"sent":False})
        return {"quote_draft_id":quote_draft_id,"status":"approved","published":False,"sent":False}

    def save_stage_result(self, campaign_id: str, *, stage: str, status: str,
                          result: Mapping[str, Any], evidence: list[dict[str, Any]] | None = None,
                          risks: list[str] | None = None, missing_inputs: list[str] | None = None,
                          next_stage: str | None = None, next_required_inputs: list[str] | None = None,
                          human_review_required: bool = False, input_payload: Mapping[str, Any] | None = None,
                          error_code: str | None = None) -> StageEnvelope:
        if stage not in STAGES or status not in STAGE_STATUSES:
            raise TradeWorkbenchError("trade_stage_invalid")
        evidence, risks = evidence or [], risks or []
        missing_inputs, next_required_inputs = missing_inputs or [], next_required_inputs or []
        started, completed = now_utc(), None if status == "running" else now_utc()
        serialized = canonical_json(dict(result))
        with self.tx(immediate=True) as db:
            campaign = db.execute("SELECT * FROM trade_campaign WHERE campaign_id=?", (campaign_id,)).fetchone()
            if not campaign:
                raise TradeWorkbenchError("trade_campaign_not_found", 404)
            if campaign["paused"] and status == "running":
                raise TradeWorkbenchError("trade_campaign_paused", 409)
            version = int(db.execute("SELECT COALESCE(MAX(version),0)+1 FROM trade_stage_run WHERE campaign_id=? AND stage=?",
                                     (campaign_id, stage)).fetchone()[0])
            run_id = new_id()
            db.execute("""INSERT INTO trade_stage_run(run_id,campaign_id,stage,version,status,result_json,evidence_json,
              risks_json,missing_inputs_json,next_stage,next_required_inputs_json,human_review_required,input_hash,
              output_hash,error_code,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (run_id, campaign_id, stage, version, status, serialized, canonical_json(evidence), canonical_json(risks),
               canonical_json(missing_inputs), next_stage, canonical_json(next_required_inputs), int(human_review_required),
               content_hash(dict(input_payload or {})), content_hash(dict(result)), error_code, started, completed))
            for item in evidence:
                source_ref = str(item.get("source_ref") or item.get("url") or item.get("source_url") or "")
                excerpt = str(item.get("excerpt") or item.get("evidence") or "")[:2000]
                db.execute("""INSERT INTO trade_evidence(evidence_id,campaign_id,stage,source_type,source_ref,
                  fetched_at,content_hash,excerpt,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                  (new_id(), campaign_id, stage, str(item.get("source_type") or "unknown"), source_ref,
                   str(item.get("fetched_at") or now_utc()), content_hash(item), excerpt, now_utc()))
            current = next_stage if status == "completed" and not human_review_required else stage
            db.execute("""UPDATE trade_campaign SET current_stage=?,version=version+1,updated_at=? WHERE campaign_id=?""",
                       (current or stage, now_utc(), campaign_id))
        return StageEnvelope(stage, status, version, dict(result), evidence, risks, missing_inputs,
                             next_stage, next_required_inputs, human_review_required, run_id, started, completed)

    def list_stage_results(self, campaign_id: str) -> list[StageEnvelope]:
        rows = self.connection.execute("""SELECT s.* FROM trade_stage_run s JOIN
          (SELECT stage,MAX(version) version FROM trade_stage_run WHERE campaign_id=? GROUP BY stage) x
          ON x.stage=s.stage AND x.version=s.version WHERE s.campaign_id=? ORDER BY s.started_at""",
          (campaign_id, campaign_id)).fetchall()
        return [self._stage(row) for row in rows]

    def latest_stage_result(self, campaign_id: str, stage: str) -> StageEnvelope | None:
        row = self.connection.execute("""SELECT * FROM trade_stage_run WHERE campaign_id=? AND stage=?
          ORDER BY version DESC LIMIT 1""", (campaign_id, stage)).fetchone()
        return self._stage(row) if row else None

    @staticmethod
    def _stage(row: sqlite3.Row) -> StageEnvelope:
        return StageEnvelope(row["stage"], row["status"], int(row["version"]), json.loads(row["result_json"]),
            json.loads(row["evidence_json"]), json.loads(row["risks_json"]), json.loads(row["missing_inputs_json"]),
            row["next_stage"], json.loads(row["next_required_inputs_json"]), bool(row["human_review_required"]),
            row["run_id"], row["started_at"], row["completed_at"])

    def enqueue_job(self, campaign_id: str, stage: str, input_payload: Mapping[str, Any],
                    *, business_key: str | None = None) -> StageJob:
        self.get_campaign(campaign_id)
        if stage not in STAGES:
            raise TradeWorkbenchError("trade_stage_invalid")
        payload = canonical_json(dict(input_payload))
        key = business_key or f"{campaign_id}:{stage}:{content_hash(dict(input_payload))}"
        now, job_id = now_utc(), new_id()
        with self.tx(immediate=True) as db:
            db.execute("""INSERT OR IGNORE INTO trade_stage_job(job_id,campaign_id,stage,input_json,business_key,status,created_at,updated_at)
              VALUES(?,?,?,?,?,'pending',?,?)""", (job_id, campaign_id, stage, payload, key, now, now))
            row = db.execute("SELECT * FROM trade_stage_job WHERE business_key=?", (key,)).fetchone()
        return self._job(row)

    def claim_job(self, worker_id: str, *, lease_seconds: int = 60) -> StageJob | None:
        now = now_utc(); until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat().replace("+00:00", "Z")
        with self.tx(immediate=True) as db:
            row = db.execute("""SELECT j.* FROM trade_stage_job j JOIN trade_campaign c ON c.campaign_id=j.campaign_id
              WHERE c.paused=0 AND ((j.status IN ('pending','retry_wait') AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?))
              OR (j.status='running' AND j.lease_until<?)) ORDER BY j.created_at,j.job_id LIMIT 1""", (now, now)).fetchone()
            if not row:
                return None
            db.execute("""UPDATE trade_stage_job SET status='running',lease_owner=?,lease_until=?,attempt_count=attempt_count+1,
              updated_at=? WHERE job_id=?""", (worker_id, until, now, row["job_id"]))
            row = db.execute("SELECT * FROM trade_stage_job WHERE job_id=?", (row["job_id"],)).fetchone()
        return self._job(row)

    def finish_job(self, job_id: str, worker_id: str, *, status: str = "completed",
                   error_code: str | None = None) -> StageJob:
        if status not in {"completed", "retry_wait", "failed", "cancelled"}:
            raise TradeWorkbenchError("trade_job_status_invalid")
        with self.tx(immediate=True) as db:
            row = db.execute("SELECT * FROM trade_stage_job WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise TradeWorkbenchError("trade_job_not_found", 404)
            if row["status"] == "completed":
                return self._job(row)
            if row["lease_owner"] != worker_id or row["status"] != "running":
                raise TradeWorkbenchError("trade_job_lease_lost", 409)
            next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z") if status == "retry_wait" else None
            db.execute("""UPDATE trade_stage_job SET status=?,lease_owner=NULL,lease_until=NULL,next_attempt_at=?,error_code=?,updated_at=?
              WHERE job_id=?""", (status, next_attempt, error_code, now_utc(), job_id))
            row = db.execute("SELECT * FROM trade_stage_job WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row: sqlite3.Row) -> StageJob:
        return StageJob(row["job_id"], row["campaign_id"], row["stage"], json.loads(row["input_json"]),
                        row["business_key"], row["status"], row["lease_owner"], row["lease_until"],
                        int(row["attempt_count"]))

    def idempotent(self, scope: str, key: str, payload: Mapping[str, Any], action) -> dict[str, Any]:
        if not key or len(key) > 255:
            raise TradeWorkbenchError("trade_idempotency_key_required", 400)
        digest = content_hash(dict(payload))
        # The workbench is loopback/single-process in this release.  Holding this
        # lock across the command serializes duplicate browser retries without
        # nesting the command's own database transaction.
        with self._idempotency_lock:
            row = self.connection.execute(
                "SELECT * FROM trade_idempotency WHERE scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise TradeWorkbenchError("trade_idempotency_conflict", 409)
                return json.loads(row["response_json"])
            value = action()
            response = asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)
            with self.tx(immediate=True) as db:
                db.execute("INSERT INTO trade_idempotency VALUES(?,?,?,?,?)",
                           (scope, key, digest, canonical_json(response), now_utc()))
            return response

    def get_quote_draft(self, quote_draft_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM trade_quote_draft WHERE quote_draft_id=?", (quote_draft_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def save_artifacts(self, campaign_id: str, object_type: str, object_id: str,
                       object_version: int, outputs: Mapping[str, str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with self.tx(immediate=True) as db:
            for kind, raw_path in outputs.items():
                path = Path(raw_path).resolve()
                if not path.is_file():
                    raise TradeWorkbenchError("trade_artifact_not_found", 404)
                data = path.read_bytes()
                record = {
                    "artifact_id": new_id(), "campaign_id": campaign_id,
                    "object_type": object_type, "object_id": object_id,
                    "object_version": int(object_version), "artifact_kind": str(kind),
                    "file_name": path.name, "storage_path": str(path),
                    "byte_size": len(data), "artifact_sha256": hashlib.sha256(data).hexdigest(),
                    "created_at": now_utc(),
                }
                db.execute("""INSERT INTO trade_artifact(artifact_id,campaign_id,object_type,object_id,
                  object_version,artifact_kind,file_name,storage_path,byte_size,artifact_sha256,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""", tuple(record.values()))
                records.append(record)
            if object_type == "quote":
                artifact_dir = str(Path(next(iter(outputs.values()))).resolve().parent) if outputs else None
                db.execute("UPDATE trade_quote_draft SET artifact_dir=? WHERE quote_draft_id=?",
                           (artifact_dir, object_id))
        return records

    def get_artifact(self, object_type: str, object_id: str,
                     artifact_kind: str) -> dict[str, Any] | None:
        row = self.connection.execute("""SELECT * FROM trade_artifact
          WHERE object_type=? AND object_id=? AND artifact_kind=?
          ORDER BY object_version DESC LIMIT 1""", (object_type, object_id, artifact_kind)).fetchone()
        return dict(row) if row else None

    def audit_ai(self, campaign_id: str | None, stage: str, *, provider_type: str,
                 model: str, prompt_version: str, input_digest: str,
                 output_digest: str | None, duration_ms: int, status: str,
                 actor_id: str = "local_operator") -> None:
        with self.tx() as db:
            db.execute("""INSERT INTO trade_ai_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), campaign_id, stage, provider_type, model, prompt_version, input_digest,
                 output_digest, duration_ms, status, actor_id, now_utc()))

    @staticmethod
    def _audit(db, campaign_id: str | None, actor: str, action: str, object_type: str,
               object_id: str, result: str, metadata: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO trade_audit_event VALUES(?,?,?,?,?,?,?,?,?)",
                   (new_id(), campaign_id, actor, action, object_type, object_id,
                    result, canonical_json(dict(metadata)), now_utc()))


def create_trade_workbench_repository(database_path: str | Path | None = None):
    cfg = load_business_config()
    if cfg.database_backend == "mysql" and database_path is None:
        from agent.business.mysql_trade_workbench_repository import MySQLTradeWorkbenchRepository
        return MySQLTradeWorkbenchRepository()
    return SQLiteTradeWorkbenchRepository(database_path)
