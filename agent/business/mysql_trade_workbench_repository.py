"""MySQL 8 implementation of the trade workbench core repository."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent.business.config import load_business_config
from agent.business.mysql_database import create_connection
from agent.business.trade_workbench_repository import (
    STAGES, StageEnvelope, StageJob, TradeWorkbenchError, canonical_json,
    content_hash, new_id,
)


def _mysql_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


class MySQLTradeWorkbenchRepository:
    REQUIRED_TABLES = {
        "trade_campaign", "trade_input_snapshot", "trade_stage_run", "trade_stage_job",
        "trade_evidence", "trade_product_profile", "trade_price_rule", "trade_prospect",
        "trade_outreach_draft", "trade_outreach_outbox", "trade_quote_draft",
        "trade_artifact", "trade_ai_audit", "trade_audit_event", "trade_idempotency",
    }
    REQUIRED_COLUMNS = {
        "trade_campaign": {"campaign_id","status","current_stage","version","paused"},
        "trade_stage_run": {"run_id","campaign_id","stage","version","status","input_hash","output_hash"},
        "trade_stage_job": {"job_id","business_key","status","lease_owner","lease_until","attempt_count"},
        "trade_outreach_draft": {"draft_id","version","content_hash","status","approved_by"},
        "trade_outreach_outbox": {"command_id","draft_version","content_hash","recipient","status","lease_until",
                                  "max_attempts","next_attempt_at","smtp_message_id","accepted_at"},
        "trade_quote_draft": {"quote_draft_id","version","payload_json","content_hash","status","artifact_dir"},
        "trade_artifact": {"artifact_id","object_id","object_version","artifact_kind","artifact_sha256"},
        "trade_idempotency": {"scope","idempotency_key","payload_hash","response_json"},
    }

    def __init__(self, connection=None):
        self.connection = connection or create_connection()
        self.check_schema()

    @contextmanager
    def tx(self) -> Iterator[Any]:
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def close(self) -> None:
        self.connection.close()

    def check_schema(self) -> None:
        with self.tx() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE()")
            actual = {row["table_name"] for row in cur.fetchall()}
        missing = sorted(self.REQUIRED_TABLES - actual)
        if missing:
            raise TradeWorkbenchError("trade_workbench_mysql_migration_incomplete:" + ",".join(missing), 503)
        with self.tx() as cur:
            cur.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=DATABASE()")
            columns: dict[str,set[str]] = {}
            for row in cur.fetchall(): columns.setdefault(row["table_name"],set()).add(row["column_name"])
        missing_columns = sorted(
            f"{table}.{column}" for table, required in self.REQUIRED_COLUMNS.items()
            for column in required - columns.get(table,set())
        )
        if missing_columns:
            raise TradeWorkbenchError("trade_workbench_mysql_migration_incomplete:" + ",".join(missing_columns),503)

    @staticmethod
    def _campaign(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row); result["paused"] = bool(result["paused"])
        result["created_at"], result["updated_at"] = _text(result["created_at"]), _text(result["updated_at"])
        result["etag"] = f'"trade-campaign-{result["campaign_id"]}-{result["version"]}"'
        return result

    def create_campaign(self, name: str, *, created_by: str = "local_operator",
                        campaign_id: str | None = None) -> dict[str, Any]:
        name = str(name).strip()
        if not name or len(name) > 255:
            raise TradeWorkbenchError("trade_campaign_invalid_name")
        campaign_id, now = campaign_id or new_id(), _mysql_now()
        with self.tx() as cur:
            cur.execute("""INSERT INTO trade_campaign(campaign_id,name,status,current_stage,created_by,created_at,updated_at)
              VALUES(%s,%s,'active','product_loader',%s,%s,%s)""", (campaign_id, name, created_by, now, now))
            self._audit(cur,campaign_id,created_by,"campaign.create","campaign",campaign_id,"success",{})
        return self.get_campaign(campaign_id)

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_campaign WHERE campaign_id=%s", (campaign_id,)); row = cur.fetchone()
        if not row:
            raise TradeWorkbenchError("trade_campaign_not_found", 404)
        result = self._campaign(row); result["stages"] = [item.__dict__ for item in self.list_stage_results(campaign_id)]
        return result

    def list_campaigns(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_campaign ORDER BY updated_at DESC,campaign_id DESC LIMIT %s",
                        (max(1, min(int(limit), 100)),)); rows = cur.fetchall()
        return [self._campaign(row) for row in rows]

    @staticmethod
    def _require_etag(row: Mapping[str, Any], expected_etag: str | None) -> None:
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
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_campaign WHERE campaign_id=%s FOR UPDATE", (campaign_id,))
            row = cur.fetchone()
            if not row:
                raise TradeWorkbenchError("trade_campaign_not_found", 404)
            self._require_etag(row, expected_etag)
            cur.execute("""UPDATE trade_campaign SET paused=%s,status=%s,version=version+1,
              updated_at=%s WHERE campaign_id=%s""",
                        (int(paused), "paused" if paused else "active", _mysql_now(), campaign_id))
            self._audit(cur, campaign_id, actor, "campaign.pause" if paused else "campaign.resume",
                        "campaign", campaign_id, "success", {})
        return self.get_campaign(campaign_id)

    def save_input(self, campaign_id: str, input_type: str, payload: Mapping[str, Any],
                   *, source_name: str | None = None) -> dict[str, Any]:
        normalized, digest, snapshot_id = canonical_json(dict(payload)), content_hash(dict(payload)), new_id()
        with self.tx() as cur:
            cur.execute("SELECT campaign_id FROM trade_campaign WHERE campaign_id=%s FOR UPDATE", (campaign_id,))
            if not cur.fetchone(): raise TradeWorkbenchError("trade_campaign_not_found", 404)
            cur.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM trade_input_snapshot WHERE campaign_id=%s AND input_type=%s FOR UPDATE", (campaign_id,input_type))
            version = int(cur.fetchone()["version"])
            cur.execute("""INSERT INTO trade_input_snapshot(snapshot_id,campaign_id,input_type,version,payload_json,payload_hash,source_name,created_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (snapshot_id,campaign_id,input_type,version,normalized,digest,source_name,_mysql_now()))
        return {"snapshot_id":snapshot_id,"input_type":input_type,"version":version,"payload_hash":digest,"source_name":source_name}

    def latest_input(self, campaign_id: str, input_type: str) -> dict[str, Any] | None:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_input_snapshot WHERE campaign_id=%s AND input_type=%s ORDER BY version DESC LIMIT 1", (campaign_id,input_type)); row=cur.fetchone()
        if not row:return None
        result=dict(row); raw=result.pop("payload_json"); result["payload"]=json.loads(raw) if isinstance(raw,str) else raw; return result

    def database_product_count(self) -> int:
        with self.tx() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM ops_product WHERE sale_status='active'")
            return int(cur.fetchone()["count"])

    def load_product_context(self, sku: str = "") -> list[dict[str, Any]]:
        with self.tx() as cur:
            cur.execute("""SELECT p.sku,p.name_cn,p.name_en,p.category_code AS category,
              p.specification_text AS specification,p.quantity_unit AS unit,p.moq,
              NULL AS price_usd,0 AS inventory,p.lead_time_days,1 AS active,
              x.product_size,x.packing_size,x.weight_kg,x.hs_code,x.certifications_json,
              x.materials_json,x.applications_json,x.source_kind,x.source_ref,x.version AS profile_version
              FROM ops_product p LEFT JOIN trade_product_profile x ON x.sku=p.sku
              WHERE p.sale_status='active' AND (%s='' OR p.sku=%s) ORDER BY p.sku""", (sku,sku))
            return [dict(row) for row in cur.fetchall()]

    def load_price_rules(self, sku: str, quantity: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trade_price_rule WHERE sku=%s AND approval_status='approved'"
        params: list[Any] = [sku]
        if quantity is not None:
            sql += " AND min_qty<=%s AND (max_qty IS NULL OR max_qty>=%s)"
            params.extend([quantity,quantity])
        sql += " AND valid_from<=UTC_TIMESTAMP(6) AND valid_until>=UTC_TIMESTAMP(6) ORDER BY min_qty DESC,version DESC"
        with self.tx() as cur:
            cur.execute(sql,params); return [dict(row) for row in cur.fetchall()]

    def upsert_prospects(self, campaign_id: str, prospects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        with self.tx() as cur:
            for item in prospects:
                pid, now = new_id(), _mysql_now(); domain = str(item.get("normalized_domain") or "")
                email = str(item.get("contact_email") or "没有")
                email_domain = email.rsplit("@",1)[-1].lower() if "@" in email else ""
                email_result = "domain_match" if domain and email_domain == domain else "format_valid" if "@" in email else "没有"
                phone = str(item.get("contact_phone") or "没有")
                cur.execute("""INSERT INTO trade_prospect(prospect_id,campaign_id,company_name,normalized_domain,
                  website,country,business_type,source_urls_json,source_notes_json,contact_email,contact_phone,
                  email_result,phone_result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON DUPLICATE KEY UPDATE company_name=VALUES(company_name),website=VALUES(website),country=VALUES(country),
                  business_type=VALUES(business_type),source_urls_json=VALUES(source_urls_json),source_notes_json=VALUES(source_notes_json),
                  contact_email=VALUES(contact_email),contact_phone=VALUES(contact_phone),email_result=VALUES(email_result),
                  phone_result=VALUES(phone_result),updated_at=VALUES(updated_at)""",
                  (pid,campaign_id,item["company_name"],domain,item["website"],item.get("country",""),
                   item.get("business_type",""),canonical_json(item.get("source_urls",[])),
                   canonical_json(item.get("source_notes",[])),email,phone,email_result,
                   "found" if phone != "没有" else "没有",now,now))
                cur.execute("SELECT prospect_id FROM trade_prospect WHERE campaign_id=%s AND normalized_domain=%s",(campaign_id,domain))
                saved.append({**item,"prospect_id":cur.fetchone()["prospect_id"],"email_result":email_result})
        return saved

    def mark_prospect_do_not_contact(self, prospect_id: str) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE trade_prospect SET do_not_contact=1,status='unsubscribed',updated_at=%s WHERE prospect_id=%s",
                        (_mysql_now(),prospect_id))

    def prospect_do_not_contact(self, prospect_id: str) -> bool:
        with self.tx() as cur:
            cur.execute("SELECT do_not_contact FROM trade_prospect WHERE prospect_id=%s",(prospect_id,));row=cur.fetchone()
        return bool(row["do_not_contact"]) if row else False

    def create_outreach_draft(self, campaign_id: str, prospect_id: str,
                              subject: str, body: str) -> tuple[str,int,str]:
        with self.tx() as cur:
            cur.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM trade_outreach_draft WHERE prospect_id=%s FOR UPDATE",(prospect_id,))
            version=int(cur.fetchone()["version"]); draft_id=new_id(); digest=content_hash({"subject":subject,"body":body})
            cur.execute("""INSERT INTO trade_outreach_draft(draft_id,campaign_id,prospect_id,version,subject,
              body,content_hash,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'draft',%s)""",
                        (draft_id,campaign_id,prospect_id,version,subject,body,digest,_mysql_now()))
        return draft_id,version,digest

    def approve_outreach_draft(self, draft_id: str, expected_hash: str,
                               actor: str) -> dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_outreach_draft WHERE draft_id=%s FOR UPDATE",(draft_id,)); row=cur.fetchone()
            if not row: raise TradeWorkbenchError("trade_draft_not_found",404)
            if row["content_hash"] != expected_hash: raise TradeWorkbenchError("trade_draft_hash_mismatch",409)
            if row["status"] not in {"draft","approved"}: raise TradeWorkbenchError("trade_draft_not_approvable",409)
            cur.execute("UPDATE trade_outreach_draft SET status='approved',approved_by=%s,approved_at=%s WHERE draft_id=%s",
                        (actor,_mysql_now(),draft_id))
            self._audit(cur,row["campaign_id"],actor,"outreach.approve","outreach_draft",draft_id,"success",{"content_hash":expected_hash})
        return {"draft_id":draft_id,"status":"approved","content_hash":expected_hash,"queue_required":True}

    def queue_outreach_draft(self, draft_id: str, account_id: str, recipient: str,
                             expected_hash: str) -> dict[str, Any]:
        with self.tx() as cur:
            cur.execute("""SELECT outbound_enabled,status,allowed_recipients_json FROM ops_email_account
              WHERE account_id=%s AND deleted_at IS NULL FOR UPDATE""",(account_id,));account=cur.fetchone()
            if not account or not account["outbound_enabled"] or account["status"]!="healthy":
                raise TradeWorkbenchError("trade_email_account_not_sendable",409)
            raw_allowed=account["allowed_recipients_json"] or []
            allowed=json.loads(raw_allowed) if isinstance(raw_allowed,str) else raw_allowed
            if recipient.casefold() not in {str(value).casefold() for value in allowed}:
                raise TradeWorkbenchError("trade_recipient_not_allowed",409)
            cur.execute("""SELECT d.*,p.do_not_contact,p.email_result,p.contact_email FROM trade_outreach_draft d
              JOIN trade_prospect p ON p.prospect_id=d.prospect_id WHERE d.draft_id=%s FOR UPDATE""",(draft_id,)); row=cur.fetchone()
            if not row: raise TradeWorkbenchError("trade_draft_not_found",404)
            if row["status"] != "approved": raise TradeWorkbenchError("trade_draft_not_approved",409)
            if row["content_hash"] != expected_hash: raise TradeWorkbenchError("trade_draft_hash_mismatch",409)
            if row["do_not_contact"]: raise TradeWorkbenchError("trade_prospect_do_not_contact",409)
            if recipient.casefold() != str(row["contact_email"]).casefold() or row["email_result"] == "没有":
                raise TradeWorkbenchError("trade_recipient_not_evidenced",409)
            command_id,now=new_id(),_mysql_now()
            cur.execute("""INSERT INTO trade_outreach_outbox(command_id,draft_id,draft_version,content_hash,
              account_id,recipient,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,'pending',%s,%s)
              ON DUPLICATE KEY UPDATE draft_id=VALUES(draft_id)""",
                        (command_id,draft_id,row["version"],expected_hash,account_id,recipient,now,now))
            cur.execute("SELECT command_id,status FROM trade_outreach_outbox WHERE draft_id=%s AND draft_version=%s",
                        (draft_id,row["version"])); stored=cur.fetchone()
            self._audit(cur,row["campaign_id"],"local_operator","outreach.queue","outreach_draft",draft_id,"success",{"command_id":stored["command_id"],"content_hash":expected_hash})
        return {"command_id":stored["command_id"],"status":stored["status"],"smtp_worker_started":False}

    def claim_outreach(self, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
        now=_mysql_now();until=now+timedelta(seconds=max(1,lease_seconds))
        with self.tx() as cur:
            cur.execute("""SELECT * FROM trade_outreach_outbox WHERE
              ((status IN ('pending','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=%s))
              OR (status='sending' AND lease_until<%s)) ORDER BY created_at,command_id LIMIT 1
              FOR UPDATE SKIP LOCKED""",(now,now));row=cur.fetchone()
            if not row:return None
            message_id=row.get("smtp_message_id") or f"<{row['command_id']}@nanoclaw.local>"
            cur.execute("""UPDATE trade_outreach_outbox SET status='sending',lease_owner=%s,lease_until=%s,
              attempt_count=attempt_count+1,smtp_message_id=%s,updated_at=%s WHERE command_id=%s""",
                        (worker_id,until,message_id,now,row["command_id"]))
            cur.execute("SELECT * FROM trade_outreach_outbox WHERE command_id=%s",(row["command_id"],));row=cur.fetchone()
        result=dict(row)
        for key in ("lease_until","next_attempt_at","accepted_at","created_at","updated_at"):result[key]=_text(result.get(key))
        return result

    def revalidate_outreach(self, command_id: str, worker_id: str) -> tuple[dict[str, Any],dict[str, Any]]:
        with self.tx() as cur:
            cur.execute("""SELECT o.*,d.subject,d.body,d.status AS draft_status,d.content_hash AS draft_hash,
              d.version AS current_draft_version,p.do_not_contact,p.contact_email,p.email_result,
              a.provider,a.address,a.secret_ref,a.sender_name,a.outbound_enabled,a.status AS account_status,
              a.allowed_recipients_json FROM trade_outreach_outbox o JOIN trade_outreach_draft d ON d.draft_id=o.draft_id
              JOIN trade_prospect p ON p.prospect_id=d.prospect_id JOIN ops_email_account a ON a.account_id=o.account_id
              WHERE o.command_id=%s AND a.deleted_at IS NULL""",(command_id,));row=cur.fetchone()
        if not row:raise TradeWorkbenchError("trade_outreach_command_not_found",404)
        if row["status"]!="sending" or row["lease_owner"]!=worker_id:raise TradeWorkbenchError("trade_outreach_lease_lost",409)
        raw=row["allowed_recipients_json"] or [];allowed={str(value).casefold() for value in (json.loads(raw) if isinstance(raw,str) else raw)}
        if (row["draft_status"]!="approved" or row["draft_hash"]!=row["content_hash"] or
            int(row["current_draft_version"])!=int(row["draft_version"]) or row["do_not_contact"] or
            str(row["contact_email"]).casefold()!=str(row["recipient"]).casefold() or row["email_result"]=="没有" or
            not row["outbound_enabled"] or row["account_status"]!="healthy" or str(row["recipient"]).casefold() not in allowed):
            raise TradeWorkbenchError("trade_outreach_stale",409)
        delivery={"delivery_id":row["command_id"],"recipient":row["recipient"],"subject_snapshot":row["subject"],
                  "body_snapshot":row["body"],"smtp_message_id":row["smtp_message_id"],"in_reply_to":None}
        account={key:row[key] for key in ("provider","address","secret_ref","sender_name")}
        return delivery,account

    def finish_outreach(self, command_id: str, worker_id: str, *, status: str,
                        error_code: str | None = None) -> dict[str, Any]:
        if status not in {"accepted","retry_wait","dead_letter","outcome_unknown","stale"}:
            raise TradeWorkbenchError("trade_outreach_status_invalid")
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_outreach_outbox WHERE command_id=%s FOR UPDATE",(command_id,));row=cur.fetchone()
            if not row:raise TradeWorkbenchError("trade_outreach_command_not_found",404)
            if row["status"]=="accepted":return dict(row)
            if row["status"]!="sending" or row["lease_owner"]!=worker_id:raise TradeWorkbenchError("trade_outreach_lease_lost",409)
            next_attempt=_mysql_now()+timedelta(seconds=30) if status=="retry_wait" else None
            accepted=_mysql_now() if status=="accepted" else None
            cur.execute("""UPDATE trade_outreach_outbox SET status=%s,lease_owner=NULL,lease_until=NULL,
              next_attempt_at=%s,accepted_at=%s,error_code=%s,updated_at=%s WHERE command_id=%s""",
                        (status,next_attempt,accepted,error_code,_mysql_now(),command_id))
            cur.execute("SELECT * FROM trade_outreach_outbox WHERE command_id=%s",(command_id,));row=cur.fetchone()
        result=dict(row)
        for key in ("lease_until","next_attempt_at","accepted_at","created_at","updated_at"):result[key]=_text(result.get(key))
        return result

    def create_quote_draft(self, campaign_id: str, prospect_id: str | None,
                           quote: Mapping[str, Any]) -> tuple[str,int,str]:
        number=str(quote["quotation_number"])
        with self.tx() as cur:
            cur.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM trade_quote_draft WHERE campaign_id=%s AND quotation_number=%s FOR UPDATE",(campaign_id,number))
            version=int(cur.fetchone()["version"]);qid=new_id();digest=content_hash(dict(quote))
            cur.execute("""INSERT INTO trade_quote_draft(quote_draft_id,campaign_id,prospect_id,version,
              quotation_number,payload_json,content_hash,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (qid,campaign_id,prospect_id,version,number,canonical_json(dict(quote)),digest,
               "blocked" if quote.get("quotation_status")=="blocked" else "draft",_mysql_now()))
        return qid,version,digest

    def approve_quote_draft(self, quote_draft_id: str, expected_hash: str,
                            actor: str) -> dict[str, Any]:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_quote_draft WHERE quote_draft_id=%s FOR UPDATE",(quote_draft_id,));row=cur.fetchone()
            if not row: raise TradeWorkbenchError("trade_quote_draft_not_found",404)
            if row["status"]=="blocked": raise TradeWorkbenchError("trade_quotation_blocked",409)
            if row["content_hash"]!=expected_hash: raise TradeWorkbenchError("trade_quote_hash_mismatch",409)
            cur.execute("UPDATE trade_quote_draft SET status='approved',approved_by=%s,approved_at=%s WHERE quote_draft_id=%s",
                        (actor,_mysql_now(),quote_draft_id))
            self._audit(cur,row["campaign_id"],actor,"quote_draft.approve","quote_draft",quote_draft_id,"success",{"content_hash":expected_hash,"published":False,"sent":False})
        return {"quote_draft_id":quote_draft_id,"status":"approved","published":False,"sent":False}

    def save_stage_result(self, campaign_id: str, *, stage: str, status: str,
                          result: Mapping[str, Any], evidence: list[dict[str, Any]] | None = None,
                          risks: list[str] | None = None, missing_inputs: list[str] | None = None,
                          next_stage: str | None = None, next_required_inputs: list[str] | None = None,
                          human_review_required: bool = False, input_payload: Mapping[str, Any] | None = None,
                          error_code: str | None = None) -> StageEnvelope:
        evidence, risks, missing_inputs, next_required_inputs = evidence or [], risks or [], missing_inputs or [], next_required_inputs or []
        from agent.business.trade_workbench_repository import STAGE_STATUSES
        if stage not in STAGES or status not in STAGE_STATUSES: raise TradeWorkbenchError("trade_stage_invalid")
        run_id, started = new_id(), _mysql_now()
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_campaign WHERE campaign_id=%s FOR UPDATE", (campaign_id,)); campaign=cur.fetchone()
            if not campaign: raise TradeWorkbenchError("trade_campaign_not_found",404)
            if campaign["paused"] and status=="running": raise TradeWorkbenchError("trade_campaign_paused",409)
            cur.execute("SELECT COALESCE(MAX(version),0)+1 AS version FROM trade_stage_run WHERE campaign_id=%s AND stage=%s FOR UPDATE",(campaign_id,stage)); version=int(cur.fetchone()["version"])
            completed = None if status=="running" else _mysql_now()
            cur.execute("""INSERT INTO trade_stage_run(run_id,campaign_id,stage,version,status,result_json,evidence_json,risks_json,
              missing_inputs_json,next_stage,next_required_inputs_json,human_review_required,input_hash,output_hash,error_code,started_at,completed_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (run_id,campaign_id,stage,version,status,canonical_json(dict(result)),canonical_json(evidence),canonical_json(risks),canonical_json(missing_inputs),next_stage,canonical_json(next_required_inputs),int(human_review_required),content_hash(dict(input_payload or {})),content_hash(dict(result)),error_code,started,completed))
            for item in evidence:
                source_ref = str(item.get("source_ref") or item.get("url") or item.get("source_url") or "")
                excerpt = str(item.get("excerpt") or item.get("evidence") or "")[:2000]
                cur.execute("""INSERT INTO trade_evidence(evidence_id,campaign_id,stage,source_type,
                  source_ref,fetched_at,content_hash,excerpt,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (new_id(),campaign_id,stage,str(item.get("source_type") or "unknown"),source_ref,
                   _mysql_now(),content_hash(item),excerpt,_mysql_now()))
            current = next_stage if status == "completed" and not human_review_required else stage
            cur.execute("UPDATE trade_campaign SET current_stage=%s,version=version+1,updated_at=%s WHERE campaign_id=%s",(current or stage,_mysql_now(),campaign_id))
        return StageEnvelope(stage,status,version,dict(result),evidence,risks,missing_inputs,next_stage,next_required_inputs,human_review_required,run_id,_text(started) or "",_text(completed))

    def list_stage_results(self,campaign_id:str)->list[StageEnvelope]:
        with self.tx() as cur:
            cur.execute("""SELECT s.* FROM trade_stage_run s JOIN (SELECT stage,MAX(version) version FROM trade_stage_run WHERE campaign_id=%s GROUP BY stage) x ON x.stage=s.stage AND x.version=s.version WHERE s.campaign_id=%s ORDER BY s.started_at""",(campaign_id,campaign_id)); rows=cur.fetchall()
        return [self._stage(r) for r in rows]

    def latest_stage_result(self,campaign_id:str,stage:str)->StageEnvelope|None:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_stage_run WHERE campaign_id=%s AND stage=%s ORDER BY version DESC LIMIT 1",(campaign_id,stage)); row=cur.fetchone()
        return self._stage(row) if row else None

    @staticmethod
    def _stage(row:dict[str,Any])->StageEnvelope:
        load=lambda v: json.loads(v) if isinstance(v,str) else v
        return StageEnvelope(row["stage"],row["status"],int(row["version"]),load(row["result_json"]),load(row["evidence_json"]),load(row["risks_json"]),load(row["missing_inputs_json"]),row["next_stage"],load(row["next_required_inputs_json"]),bool(row["human_review_required"]),row["run_id"],_text(row["started_at"]) or "",_text(row["completed_at"]))

    def enqueue_job(self,campaign_id:str,stage:str,input_payload:Mapping[str,Any],*,business_key:str|None=None)->StageJob:
        payload=canonical_json(dict(input_payload)); key=business_key or f"{campaign_id}:{stage}:{content_hash(dict(input_payload))}"
        with self.tx() as cur:
            cur.execute("""INSERT INTO trade_stage_job(job_id,campaign_id,stage,input_json,business_key,status,created_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,'pending',%s,%s) ON DUPLICATE KEY UPDATE business_key=VALUES(business_key)""",(new_id(),campaign_id,stage,payload,key,_mysql_now(),_mysql_now()))
            cur.execute("SELECT * FROM trade_stage_job WHERE business_key=%s",(key,)); row=cur.fetchone()
        return self._job(row)

    def claim_job(self,worker_id:str,*,lease_seconds:int=60)->StageJob|None:
        now=_mysql_now(); until=now+timedelta(seconds=max(1,lease_seconds))
        with self.tx() as cur:
            cur.execute("""SELECT j.* FROM trade_stage_job j JOIN trade_campaign c ON c.campaign_id=j.campaign_id
              WHERE c.paused=0 AND ((j.status IN ('pending','retry_wait') AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=%s))
              OR (j.status='running' AND j.lease_until<%s)) ORDER BY j.created_at,j.job_id LIMIT 1 FOR UPDATE SKIP LOCKED""",(now,now)); row=cur.fetchone()
            if not row:return None
            cur.execute("UPDATE trade_stage_job SET status='running',lease_owner=%s,lease_until=%s,attempt_count=attempt_count+1,updated_at=%s WHERE job_id=%s",(worker_id,until,now,row["job_id"]))
            cur.execute("SELECT * FROM trade_stage_job WHERE job_id=%s",(row["job_id"],)); row=cur.fetchone()
        return self._job(row)

    def finish_job(self, job_id: str, worker_id: str, *, status: str = "completed",
                   error_code: str | None = None) -> StageJob:
        if status not in {"completed", "retry_wait", "failed", "cancelled"}:
            raise TradeWorkbenchError("trade_job_status_invalid")
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_stage_job WHERE job_id=%s FOR UPDATE", (job_id,))
            row = cur.fetchone()
            if not row:
                raise TradeWorkbenchError("trade_job_not_found", 404)
            if row["status"] == "completed":
                return self._job(row)
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise TradeWorkbenchError("trade_job_lease_lost", 409)
            next_attempt = _mysql_now() + timedelta(seconds=30) if status == "retry_wait" else None
            cur.execute("""UPDATE trade_stage_job SET status=%s,lease_owner=NULL,lease_until=NULL,
              next_attempt_at=%s,error_code=%s,updated_at=%s WHERE job_id=%s""",
                        (status, next_attempt, error_code, _mysql_now(), job_id))
            cur.execute("SELECT * FROM trade_stage_job WHERE job_id=%s", (job_id,)); row = cur.fetchone()
        return self._job(row)

    @staticmethod
    def _job(row:dict[str,Any])->StageJob:
        raw=row["input_json"]; return StageJob(row["job_id"],row["campaign_id"],row["stage"],json.loads(raw) if isinstance(raw,str) else raw,row["business_key"],row["status"],row["lease_owner"],_text(row["lease_until"]),int(row["attempt_count"]))

    def idempotent(self, scope: str, key: str, payload: Mapping[str, Any], action) -> dict[str, Any]:
        if not key or len(key) > 255:
            raise TradeWorkbenchError("trade_idempotency_key_required", 400)
        digest = content_hash(dict(payload))
        lock_name = "trade-idem-" + hashlib.sha256(f"{scope}:{key}".encode()).hexdigest()[:48]
        cursor = self.connection.cursor()
        try:
            cursor.execute("SELECT GET_LOCK(%s,5) AS acquired", (lock_name,))
            if int(cursor.fetchone()["acquired"] or 0) != 1:
                raise TradeWorkbenchError("trade_idempotency_busy", 409)
            cursor.execute("SELECT * FROM trade_idempotency WHERE scope=%s AND idempotency_key=%s", (scope,key))
            row = cursor.fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise TradeWorkbenchError("trade_idempotency_conflict", 409)
                raw = row["response_json"]
                return json.loads(raw) if isinstance(raw, str) else raw
            value = action()
            response = asdict(value) if hasattr(value, "__dataclass_fields__") else dict(value)
            with self.tx() as cur:
                cur.execute("INSERT INTO trade_idempotency VALUES(%s,%s,%s,%s,%s)",
                            (scope,key,digest,canonical_json(response),_mysql_now()))
            return response
        finally:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            finally:
                cursor.close()

    def audit_ai(self, campaign_id: str | None, stage: str, *, provider_type: str,
                 model: str, prompt_version: str, input_digest: str,
                 output_digest: str | None, duration_ms: int, status: str,
                 actor_id: str = "local_operator") -> None:
        with self.tx() as cur:
            cur.execute("""INSERT INTO trade_ai_audit VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (new_id(),campaign_id,stage,provider_type,model,prompt_version,input_digest,
                 output_digest,duration_ms,status,actor_id,_mysql_now()))

    @staticmethod
    def _audit(cur, campaign_id: str | None, actor: str, action: str,
               object_type: str, object_id: str, result: str,
               metadata: Mapping[str, Any]) -> None:
        cur.execute("INSERT INTO trade_audit_event VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (new_id(),campaign_id,actor,action,object_type,object_id,result,
                     canonical_json(dict(metadata)),_mysql_now()))

    def get_quote_draft(self, quote_draft_id: str) -> dict[str, Any] | None:
        with self.tx() as cur:
            cur.execute("SELECT * FROM trade_quote_draft WHERE quote_draft_id=%s", (quote_draft_id,))
            row = cur.fetchone()
        if not row:
            return None
        result = dict(row); raw = result.pop("payload_json")
        result["payload"] = json.loads(raw) if isinstance(raw, str) else raw
        return result

    def save_artifacts(self, campaign_id: str, object_type: str, object_id: str,
                       object_version: int, outputs: Mapping[str, str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with self.tx() as cur:
            for kind, raw_path in outputs.items():
                path = Path(raw_path).resolve()
                if not path.is_file():
                    raise TradeWorkbenchError("trade_artifact_not_found", 404)
                data = path.read_bytes(); artifact_id = new_id(); created = _mysql_now()
                record = {"artifact_id":artifact_id,"campaign_id":campaign_id,"object_type":object_type,
                          "object_id":object_id,"object_version":int(object_version),"artifact_kind":str(kind),
                          "file_name":path.name,"storage_path":str(path),"byte_size":len(data),
                          "artifact_sha256":hashlib.sha256(data).hexdigest(),"created_at":_text(created)}
                cur.execute("""INSERT INTO trade_artifact(artifact_id,campaign_id,object_type,object_id,
                  object_version,artifact_kind,file_name,storage_path,byte_size,artifact_sha256,created_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (artifact_id,campaign_id,object_type,object_id,object_version,kind,path.name,str(path),len(data),record["artifact_sha256"],created))
                records.append(record)
            if object_type == "quote":
                artifact_dir = str(Path(next(iter(outputs.values()))).resolve().parent) if outputs else None
                cur.execute("UPDATE trade_quote_draft SET artifact_dir=%s WHERE quote_draft_id=%s", (artifact_dir,object_id))
        return records

    def get_artifact(self, object_type: str, object_id: str,
                     artifact_kind: str) -> dict[str, Any] | None:
        with self.tx() as cur:
            cur.execute("""SELECT * FROM trade_artifact WHERE object_type=%s AND object_id=%s
              AND artifact_kind=%s ORDER BY object_version DESC LIMIT 1""",
                        (object_type,object_id,artifact_kind)); row = cur.fetchone()
        if not row:
            return None
        result = dict(row); result["created_at"] = _text(result["created_at"]); return result
