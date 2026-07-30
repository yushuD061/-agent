"""Evidence-first services for the internal foreign-trade workbench."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from agent.business.trade_workbench_files import read_records, read_records_bytes, render_quotation, safe_filename
from agent.business.trade_workbench_repository import (
    STAGES, StageEnvelope, TradeWorkbenchError,
    canonical_json, content_hash, new_id, now_utc,
)


NEXT_STAGE = {stage: STAGES[index + 1] if index + 1 < len(STAGES) else None
              for index, stage in enumerate(STAGES)}
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)")
ROLE_RE = re.compile(r"(?i)\b(owner|founder|purchasing manager|sourcing manager|procurement manager|category manager|buyer)\b")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TradeWorkbenchError(f"trade_invalid_{field}") from exc
    if not result.is_finite() or result < 0:
        raise TradeWorkbenchError(f"trade_invalid_{field}")
    return result


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_domain(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


class TradeWorkbenchService:
    def __init__(self, repository, *, config_dir: str | Path = "data/config",
                 artifact_root: str | Path = "outputs/trade-workbench"):
        self.repository = repository
        self.config_dir = Path(config_dir)
        self.artifact_root = Path(artifact_root).resolve()

    def input_status(self) -> dict[str, Any]:
        configs: dict[str, Any] = {}
        for name in ("PRODUCT", "PRICING", "MARKET", "DISCOVERY", "TONE"):
            path = self.config_dir / f"{name}.yaml"
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
            except (OSError, yaml.YAMLError):
                payload = None
            configs[name.lower()] = {"present": bool(payload), "status": (payload or {}).get("status", "待补充")}
        try: count = self.repository.database_product_count()
        except Exception: count = 0
        missing = [
            "正式公司主体、品牌、地址、联系人签名", "真实产品尺寸、包装尺寸/箱规、重量、HS Code、认证",
            "正式阶梯价、币种、价格有效期、Incoterm、付款条款、样品规则",
            "目标国家、客户类型、语言、排除行业及当地外联合规规则",
            "获准的公开来源 URL、采集 API，或 CSV/XLSX 潜客名单",
            "发件语气、退订文本、联系人验证规则",
            "具体公司网址、买家回复、询价 SKU、数量、目的地及运费",
        ]
        return {"database_product_count": count, "database_product_source": "demo_only",
                "safe_to_quote": False, "configs": configs, "missing_inputs": missing}

    def create_campaign(self, name: str) -> dict[str, Any]:
        return self.repository.create_campaign(name)

    def import_input(self, campaign_id: str, input_type: str, path: str | Path) -> dict[str, Any]:
        records = read_records(path)
        snapshot = self.repository.save_input(campaign_id, input_type, {"items": records},
                                              source_name=Path(path).name)
        return {**snapshot, "input_rows": len(records), "original_unchanged": True}

    def import_input_bytes(self, campaign_id: str, input_type: str, filename: str,
                           raw: bytes) -> dict[str, Any]:
        self.repository.get_campaign(campaign_id)
        records = read_records_bytes(filename, raw)
        digest = hashlib.sha256(raw).hexdigest()
        suffix = Path(filename).suffix.lower()
        storage = self.artifact_root / campaign_id / "inputs" / f"{digest}{suffix}"
        storage.parent.mkdir(parents=True, exist_ok=True)
        if storage.exists() and storage.read_bytes() != raw:
            raise TradeWorkbenchError("trade_import_hash_collision", 409)
        if not storage.exists():
            storage.write_bytes(raw)
        snapshot = self.repository.save_input(
            campaign_id, input_type,
            {"items": records, "original_sha256": digest, "original_storage": str(storage)},
            source_name=safe_filename(filename),
        )
        return {**snapshot,"input_rows":len(records),"original_unchanged":True,
                "original_sha256":digest,"source_name":safe_filename(filename)}

    def run_stage(self, campaign_id: str, stage: str, payload: Mapping[str, Any] | None = None) -> StageEnvelope:
        if stage not in STAGES:
            raise TradeWorkbenchError("trade_stage_invalid")
        campaign = self.repository.get_campaign(campaign_id)
        if campaign["paused"]:
            raise TradeWorkbenchError("trade_campaign_paused", 409)
        handler = getattr(self, f"_{stage}")
        return handler(campaign_id, dict(payload or {}))

    def _save(self, campaign_id: str, stage: str, payload: dict[str, Any], result: dict[str, Any], *,
              status: str = "completed", evidence: list[dict[str, Any]] | None = None,
              risks: list[str] | None = None, missing: list[str] | None = None,
              next_required: list[str] | None = None, review: bool = False) -> StageEnvelope:
        return self.repository.save_stage_result(
            campaign_id, stage=stage, status=status, result=result, evidence=evidence,
            risks=risks, missing_inputs=missing, next_stage=NEXT_STAGE[stage],
            next_required_inputs=next_required, human_review_required=review,
            input_payload=payload,
        )

    def _product_loader(self, campaign_id: str, payload: dict[str, Any]) -> StageEnvelope:
        sku = str(payload.get("sku") or "").strip()
        quantity = payload.get("quantity")
        products: list[dict[str, Any]] = []
        rows = self.repository.load_product_context(sku)
        for row in rows:
            item = dict(row)
            raw_certifications = item.pop("certifications_json", "[]") or "[]"
            item["certifications"] = (json.loads(raw_certifications)
                                      if isinstance(raw_certifications,str) else raw_certifications)
            item["source_kind"] = item.get("source_kind") or "demo_only"
            products.append(item)
        rules = self.repository.load_price_rules(sku) if sku else []
        missing=[]
        if not products: missing.append("sku")
        for field in ("quantity",):
            if payload.get(field) in (None, ""): missing.append(field)
        if products:
            product=products[0]
            for field in ("product_size","packing_size"):
                if not product.get(field): missing.append(field)
            if product.get("source_kind") == "demo_only": missing.append("authoritative_product_source")
        if not rules: missing.extend(["approved_unit_price","currency","incoterm","payment_terms","validity"])
        safe=not missing
        evidence=[{"source_type":"product_database","source_ref":f"sku:{item['sku']}","excerpt":item.get("specification","")} for item in products]
        return self._save(campaign_id,"product_loader",payload,{"company":{},"products":products,"pricing_rules":rules,"missing_fields":missing,"safe_to_quote":safe,"source_kind":"database"},
                          status="completed" if products else "blocked_missing_input", evidence=evidence,
                          risks=["现有种子产品为演示数据，不可用于正式报价"] if any(p.get("source_kind")=="demo_only" for p in products) else [],
                          missing=missing,next_required=missing)

    def _prospect_discovery(self, campaign_id: str, payload: dict[str, Any]) -> StageEnvelope:
        sources = [str(value).strip() for value in payload.get("allowed_sources", []) if str(value).strip()]
        regions = [str(value).strip() for value in payload.get("target_regions", []) if str(value).strip()]
        keywords = [str(value).strip() for value in payload.get("product_keywords", []) if str(value).strip()]
        search_tasks=[{"query":" ".join(filter(None,(keyword,region,"distributor importer wholesaler"))),"status":"task_only"}
                      for keyword in (keywords or ["待补充产品"]) for region in (regions or ["待补充市场"])]
        source_status="approved_sources_configured" if sources else "search_tasks_only"
        missing=[] if sources else ["approved_source_url_or_collection_api_or_prospect_list"]
        result={"collection_api_status":payload.get("collection_api_status","none"),"keyword_strategy":keywords,
                "target_regions":regions,"exclude_terms":["jobs","consumer reviews","unrelated retail"],
                "allowed_sources":sources,"source_status":source_status,"search_tasks":search_tasks,
                "candidate_fields":["company_name","website","country","source_url","evidence_summary"]}
        return self._save(campaign_id,"prospect_discovery",payload,result,
                          status="completed" if sources else "blocked_missing_input",
                          risks=["搜索任务不是已验证潜客"] if not sources else [],missing=missing,
                          next_required=["prospect_rows"] if sources else missing)

    def _prospect_list_enrichment(self, campaign_id: str, payload: dict[str, Any]) -> StageEnvelope:
        rows = payload.get("prospects") or ((self.repository.latest_input(campaign_id,"prospects") or {}).get("payload") or {}).get("items") or []
        if not isinstance(rows,list): raise TradeWorkbenchError("trade_prospect_input_invalid")
        unique: dict[str,dict[str,Any]]={}; needs=[]; duplicates=[]
        for index,raw in enumerate(rows,1):
            if not isinstance(raw,dict): continue
            company=str(raw.get("company_name") or raw.get("company") or raw.get("name") or "").strip()
            website=str(raw.get("website") or raw.get("url") or "").strip(); domain=normalize_domain(website)
            source=str(raw.get("source_url") or raw.get("source") or website).strip()
            if not company or not website or not source:
                needs.append({"row":index,"reason":"missing_company_website_or_source"}); continue
            key=domain or f"{company.casefold()}|{str(raw.get('country','')).casefold()}"
            if key in unique:
                duplicates.append(index); unique[key]["source_urls"] = sorted(set(unique[key]["source_urls"]+[source])); continue
            unique[key]={"company_name":company,"website":website,"normalized_domain":domain,
                "country":str(raw.get("country") or ""),"business_type":str(raw.get("business_type") or ""),
                "source_urls":[source],"source_notes":[str(raw.get("source_note") or "")],
                "contact_email":str(raw.get("contact_email") or "没有"),"contact_phone":str(raw.get("contact_phone") or "没有")}
        saved=self.repository.upsert_prospects(campaign_id,list(unique.values())) if unique else []
        result={"input_rows":len(rows),"unique_companies":len(unique),"duplicate_rows":duplicates,"ready_for_research":saved or list(unique.values()),"needs_review":needs,"excluded_rows":[],"output_columns":["company_name","website","country","source_urls","contact_email","contact_phone"]}
        return self._save(campaign_id,"prospect_list_enrichment",payload,result,
                          status="completed" if unique else "blocked_missing_input",
                          evidence=[{"source_type":"user_upload","source_ref":url,"excerpt":"prospect source"} for item in unique.values() for url in item["source_urls"]],
                          missing=[] if unique else ["prospect_rows"],next_required=["fetched_company_pages"])

    def _company_research(self, campaign_id: str, payload: dict[str, Any]) -> StageEnvelope:
        pages=payload.get("pages") or []
        if not pages:
            return self._save(campaign_id,"company_research",payload,{"evidence_status":"fetch_failed","company_summary":""},
                status="blocked_missing_input",risks=["未提供已抓取的白名单官网页面"],missing=["fetched_company_pages"],next_required=["fetched_company_pages"])
        combined="\n".join(str(page.get("content") or "") for page in pages if isinstance(page,dict))
        urls=[str(page.get("url") or "") for page in pages if isinstance(page,dict)]
        emails=sorted(set(EMAIL_RE.findall(combined))); phones=sorted(set(match.strip() for match in PHONE_RE.findall(combined)))
        roles=sorted(set(match.group(0) for match in ROLE_RE.finditer(combined)))
        keywords=[str(value).casefold() for value in payload.get("product_keywords",[]) if str(value).strip()]
        overlaps=[value for value in keywords if value in combined.casefold()]
        business_type="unverified"
        for candidate in ("importer","distributor","wholesaler","retailer","manufacturer","contractor"):
            if candidate in combined.casefold(): business_type=candidate; break
        evidence=[]
        for page in pages:
            if not isinstance(page,dict): continue
            text=str(page.get("content") or ""); supplied_hash=str(page.get("content_hash") or "")
            fetched=bool(page.get("fetched_at")) and supplied_hash==content_hash(text)
            evidence.append({"source_type":"official_website_fetch" if fetched else "user_supplied_page",
                "source_ref":str(page.get("url") or ""),"fetched_at":str(page.get("fetched_at") or now_utc()),
                "content_hash":content_hash(text),"excerpt":text[:300]})
        status="verified" if overlaps or business_type!="unverified" else "no_evidence"
        result={"company_summary":str(payload.get("company_name") or ""),"business_type":business_type,
                "main_products":overlaps,"target_customers":"","countries_served":[],"evidence":evidence,
                "evidence_status":status,"possible_needs":overlaps,"personalization_points":overlaps[:2],
                "decision_maker_clues":roles,"contact_email":emails[0] if emails else "没有","contact_phone":phones[0] if phones else "没有",
                "email_result":"found" if emails else "没有","phone_result":"found" if phones else "没有",
                "red_flags":([] if status=="verified" else ["no_product_or_channel_evidence"])+
                    ([] if all(item["source_type"]=="official_website_fetch" for item in evidence) else ["page_provenance_user_supplied"]),
                "confidence":"medium" if status=="verified" else "low"}
        return self._save(campaign_id,"company_research",payload,result,evidence=evidence,
                          risks=result["red_flags"],next_required=["market_rules"])

    def _prospect_scoring(self,campaign_id:str,payload:dict[str,Any])->StageEnvelope:
        research=payload.get("research") or (asdict(self.repository.latest_stage_result(campaign_id,"company_research"))["result"] if self.repository.latest_stage_result(campaign_id,"company_research") else {})
        verified=research.get("evidence_status")=="verified"; product=min(40,20*len(research.get("main_products") or [])) if verified else 0
        channel=20 if research.get("business_type") in {"importer","distributor","wholesaler","retailer","brand owner"} else 0
        access=15 if research.get("decision_maker_clues") else (5 if research.get("contact_email") not in {None,"","没有"} else 0)
        market=10 if payload.get("market_match") else 0; website=10 if verified else 0
        risk=-10 if research.get("red_flags") else 0; score=max(0,min(100,product+channel+access+market+website+risk))
        priority="A" if score>=80 else "B" if score>=60 else "C" if score>=40 else "D"
        if not verified: score=min(score,39); priority="D"
        result={"score":score,"priority":priority,"score_breakdown":{"product_fit":product,"channel_value":channel,"decision_maker_access":access,"target_market":market,"website_quality":website,"risk_adjustment":risk},"recommended_action":"contact_first" if priority=="A" else "manual_review" if priority in {"B","D"} else "monitor","evidence_status":research.get("evidence_status","no_evidence"),"reason":"development priority, not purchase intent","evidence":research.get("evidence",[]),"risk_notes":research.get("red_flags",[])}
        return self._save(campaign_id,"prospect_scoring",payload,result,evidence=result["evidence"],risks=result["risk_notes"],next_required=["approved_contact_evidence"])

    def _decision_maker_finder(self,campaign_id:str,payload:dict[str,Any])->StageEnvelope:
        pages=payload.get("pages") or []
        combined="\n".join(str(page.get("content") or "") for page in pages if isinstance(page,dict)); urls=[str(page.get("url") or "") for page in pages if isinstance(page,dict)]
        emails=sorted(set(EMAIL_RE.findall(combined))); phones=sorted(set(match.strip() for match in PHONE_RE.findall(combined))); roles=sorted(set(m.group(0) for m in ROLE_RE.finditer(combined)))
        website=payload.get("website") or (urls[0] if urls else ""); domain=normalize_domain(str(website))
        candidates=[]
        for role in roles:
            email=emails[0] if emails else ""; email_domain=email.rsplit("@",1)[-1].lower() if email else ""
            domain_match=bool(domain and (email_domain==domain or email_domain.endswith("."+domain)))
            candidates.append({"name":"","role":role,"email":email,"email_status":"domain_match" if domain_match else "format_valid" if email else "missing","phone":phones[0] if phones else "","phone_status":"found" if phones else "missing","confidence":"medium","source_url":urls[0] if urls else "","evidence":role})
        result={"website":website,"pages_checked":urls,"contact_search":{"email_result":"found" if emails else "没有","phone_result":"found" if phones else "没有","emails":emails,"phones":phones},"candidates":candidates,"review_notes":[] if candidates else ["未发现公开采购岗位线索"]}
        evidence=[{"source_type":"official_website","source_ref":url,"excerpt":"contact search"} for url in urls]
        return self._save(campaign_id,"decision_maker_finder",payload,result,evidence=evidence,risks=result["review_notes"],next_required=["sender_signature","tone_rules"])

    def _email_crafting(self,campaign_id:str,payload:dict[str,Any])->StageEnvelope:
        score=payload.get("score") or (self.repository.latest_stage_result(campaign_id,"prospect_scoring").result if self.repository.latest_stage_result(campaign_id,"prospect_scoring") else {})
        priority=score.get("priority","D"); personalization=payload.get("personalization_evidence") or []
        valid_evidence=[item for item in personalization if isinstance(item,dict) and
                        str(item.get("source_ref") or item.get("url") or "").strip() and
                        str(item.get("excerpt") or "").strip()]
        draft_missing=[field for field in ("sender_signature","product_value","opt_out_text") if not str(payload.get(field) or "").strip()]
        if priority=="D" or (priority=="C" and not payload.get("c_override")) or not valid_evidence or draft_missing:
            result={"subject":"","body":"","draft_status":"blocked_no_evidence","cta":"","personalization_evidence":personalization,"review_notes":["需要 A/B 优先级及官网个性化证据"]}
            missing=["eligible_priority_and_personalization_evidence"] if not valid_evidence or priority=="D" else []
            missing.extend(draft_missing)
            return self._save(campaign_id,"email_crafting",payload,result,status="blocked_missing_input",risks=result["review_notes"],missing=missing,next_required=missing,review=True)
        signature=str(payload.get("sender_signature") or "待补充"); product=str(payload.get("product_value") or "待补充产品价值")
        fact=str(personalization[0].get("excerpt") if isinstance(personalization[0],dict) else personalization[0])
        subject=f"A product idea for {str(payload.get('company_name') or 'your team')}"
        body=(f"Hello,\n\nI noticed {fact}. We support {product}, and I thought this may be relevant to your sourcing team. "
              "The product details and any commercial terms will be shared only after internal review. Would a short specification sheet be useful for your evaluation?\n\n"
              f"{signature}\n\n{str(payload.get('opt_out_text') or 'If this is not relevant, please let me know and I will not follow up.')}" )
        result={"subject":subject,"body":body,"draft_status":"draft_ready","cta":"Would a short specification sheet be useful?","personalization_evidence":personalization,"review_notes":["人工审核后仍需独立排队发送"]}
        prospect_id=str(payload.get("prospect_id") or "")
        if prospect_id:
            draft_id,version,digest=self._store_outreach_draft(campaign_id,prospect_id,result)
            result.update({"draft_id":draft_id,"draft_version":version,"content_hash":digest})
        return self._save(campaign_id,"email_crafting",payload,result,status="waiting_review",evidence=personalization,risks=result["review_notes"],next_required=["human_email_approval"],review=True)

    def _reply_classification(self,campaign_id:str,payload:dict[str,Any])->StageEnvelope:
        text=str(payload.get("reply_text") or "").strip(); lower=text.casefold()
        if not text:
            return self._save(campaign_id,"reply_classification",payload,{"classification":"unclear","confidence":"low"},status="blocked_missing_input",missing=["reply_text"],next_required=["reply_text"],review=True)
        patterns=[("unsubscribe",("unsubscribe","remove me","do not contact","stop emailing")),("quotation_request",("quote","quotation","price")),("sample_request",("sample",)),("catalog_request",("catalog","brochure")),("meeting_request",("meeting","call","teams","zoom")),("not_interested",("not interested",)),("out_of_office",("out of office","automatic reply")),("objection",("too expensive","already have supplier"))]
        classification=next((name for name,terms in patterns if any(term in lower for term in terms)),"inquiry" if "?" in text else "unclear")
        missing=[]
        if classification=="quotation_request":
            for field,pattern in (("sku",r"\b[A-Z]{2,}-\d+\b"),("quantity",r"\b\d+\s*(?:pcs|units|sets)\b"),("destination",r"\b(?:to|deliver(?:y)? to)\s+[A-Z][A-Za-z -]+")):
                if not re.search(pattern,text,re.I): missing.append(field)
        result={"classification":classification,"confidence":"high" if classification!="unclear" else "low","buyer_signals":[text[:300]],"requested_items":re.findall(r"\b[A-Z]{2,}-\d+\b",text),"missing_information":missing,"recommended_action":"pause_follow_up" if classification in {"unsubscribe","not_interested"} else "prepare_quotation_draft" if classification=="quotation_request" else "human_review","reply_draft_needed":classification not in {"unsubscribe","not_interested","out_of_office"},"human_review_required":True}
        if classification=="unsubscribe" and payload.get("prospect_id"):
            self.repository.mark_prospect_do_not_contact(str(payload["prospect_id"]))
        return self._save(campaign_id,"reply_classification",payload,result,risks=["买家意图不得超出原文解释"],next_required=missing or ["human_review"],review=True)

    def _follow_up_planner(self,campaign_id:str,payload:dict[str,Any])->StageEnvelope:
        priority=str(payload.get("priority") or "D"); classification=str(payload.get("classification") or "")
        prospect_id=str(payload.get("prospect_id") or "")
        persisted_dnc=self.repository.prospect_do_not_contact(prospect_id) if prospect_id else False
        paused=priority=="D" or classification in {"unsubscribe","not_interested"} or bool(payload.get("paused")) or persisted_dnc
        try: base=date.fromisoformat(str(payload.get("last_contact_date")))
        except ValueError: base=date.today()
        tasks=[] if paused else [{"due_date":str(base+timedelta(days=offset)),"task_type":"follow_up","reason":"unanswered priority prospect","draft_requested":True,"human_review_required":True} for offset in (3,7,14) if priority in {"A","B"}]
        result={"prospect_status":"paused" if paused else "active","last_contact_date":str(base),"tasks":tasks,"pause_reason":"do_not_contact_or_low_priority" if paused else "","review_notes":["提醒不会自动发送"]}
        return self._save(campaign_id,"follow_up_planner",payload,result,risks=result["review_notes"],next_required=["confirmed_quote_inputs"],review=True)

    def _quotation_generator(self,campaign_id:str,payload:dict[str,Any])->StageEnvelope:
        buyer=str(payload.get("buyer") or "").strip(); sku=str(payload.get("sku") or "").strip(); quantity=_decimal(payload.get("quantity",0),"quantity")
        missing=[]
        if not buyer: missing.append("buyer")
        if not sku: missing.append("sku")
        if payload.get("quantity") in (None,"") or quantity <= 0: missing.append("quantity")
        products=self.repository.load_product_context(sku) if sku else []
        product=products[0] if products else None
        rules=self.repository.load_price_rules(sku,str(quantity)) if sku and quantity > 0 else []
        if not product: missing.append("exact_sku")
        else:
            if quantity < Decimal(str(product["moq"])) and not payload.get("sample_price_approved"): missing.append("quantity_below_moq")
            for field in ("product_size","packing_size"):
                if not product[field]: missing.append(field)
            if (product["source_kind"] or "demo_only")=="demo_only": missing.append("authoritative_product_source")
        requested_incoterm=str(payload.get("incoterm") or "").upper()
        if requested_incoterm:
            rules=[rule for rule in rules if str(rule["incoterm"]).upper()==requested_incoterm]
        if not rules: missing.extend(["approved_unit_price","currency","incoterm","payment_terms","validity"])
        if not requested_incoterm and rules: requested_incoterm=str(rules[0]["incoterm"]).upper()
        if requested_incoterm in {"CIF","DDP"} and payload.get("freight_amount") in (None,""): missing.append("confirmed_freight_and_insurance")
        number=str(payload.get("quotation_number") or f"Q-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{new_id()[:8].upper()}")
        if missing:
            quote={"quotation_status":"blocked","quotation_number":number,"buyer":buyer,"items":[],"terms":{},"total_amount":"0.00","human_review_required":True,"content_hash_scope":"stored_commercial_payload","missing_fields":sorted(set(missing)),"review_notes":["待补充字段齐全后重新生成新版本"]}
            qid,version,digest=self._store_quote(campaign_id,payload,quote)
            quote.update({"quote_draft_id":qid,"draft_version":version,"content_hash":digest})
            return self._save(campaign_id,"quotation_generator",payload,quote,status="blocked_missing_input",risks=quote["review_notes"],missing=quote["missing_fields"],next_required=quote["missing_fields"],review=True)
        rule=rules[0]; unit=_decimal(rule["unit_price"],"unit_price"); amount=unit*quantity; freight=_decimal(payload.get("freight_amount",0),"freight_amount"); total=amount+freight
        quote={"quotation_status":"draft","quotation_number":number,"buyer":buyer,"buyer_country":str(payload.get("buyer_country") or ""),"seller":str(payload.get("seller") or "待补充"),"quotation_date":str(date.today()),"items":[{"sku":sku,"product":product["name_en"],"specification":product["specification"],"packing":product["packing_size"],"quantity":str(quantity),"unit_price":_money(unit),"amount":_money(amount)}],"terms":{"currency":rule["currency"],"incoterm":requested_incoterm,"payment_terms":rule["payment_terms"],"validity":str(rule["valid_until"]),"lead_time_days":product["lead_time_days"],"freight_amount":_money(freight)},"total_amount":_money(total),"human_review_required":True,"content_hash_scope":"stored_commercial_payload","missing_fields":[],"review_notes":["报价批准仅代表内部批准，不代表发送或发布"]}
        qid,version,digest=self._store_quote(campaign_id,payload,quote)
        quote.update({"quote_draft_id":qid,"draft_version":version,"content_hash":digest})
        out=self.artifact_root/campaign_id/qid/f"v{version}"; outputs=render_quotation(quote,out)
        artifacts=self.repository.save_artifacts(campaign_id,"quote",qid,version,outputs)
        quote["export_outputs"]=outputs
        quote["artifacts"]=[{"kind":item["artifact_kind"],"sha256":item["artifact_sha256"],
                             "byte_size":item["byte_size"]} for item in artifacts]
        return self._save(campaign_id,"quotation_generator",payload,quote,status="waiting_review",evidence=[{"source_type":"price_rule","source_ref":rule["source_ref"],"excerpt":f"{sku} {rule['unit_price']} {rule['currency']}"}],risks=quote["review_notes"],next_required=["human_quote_approval"],review=True)

    def _store_outreach_draft(self,campaign_id:str,prospect_id:str,result:dict[str,Any])->tuple[str,int,str]:
        return self.repository.create_outreach_draft(campaign_id,prospect_id,result["subject"],result["body"])

    def approve_outreach(self,draft_id:str,*,expected_hash:str,actor:str="local_operator")->dict[str,Any]:
        return self.repository.approve_outreach_draft(draft_id,expected_hash,actor)

    def queue_outreach(self,draft_id:str,*,account_id:str,recipient:str,expected_hash:str)->dict[str,Any]:
        if not EMAIL_RE.fullmatch(recipient): raise TradeWorkbenchError("trade_recipient_invalid")
        if not account_id.strip(): raise TradeWorkbenchError("trade_email_account_required")
        return self.repository.queue_outreach_draft(draft_id,account_id,recipient,expected_hash)

    def _store_quote(self,campaign_id:str,payload:dict[str,Any],quote:dict[str,Any])->tuple[str,int,str]:
        return self.repository.create_quote_draft(campaign_id,payload.get("prospect_id"),quote)

    def approve_quote(self,quote_draft_id:str,*,expected_hash:str,actor:str="local_operator")->dict[str,Any]:
        return self.repository.approve_quote_draft(quote_draft_id,expected_hash,actor)


class TradeStageDrainer:
    """Bounded one-shot drainer.  It never sends email or publishes a quote."""
    def __init__(self,repository,service:TradeWorkbenchService,*,worker_id:str="trade-local-worker"):
        self.repository,self.service,self.worker_id=repository,service,worker_id

    def drain_once(self,max_jobs:int=10)->list[dict[str,Any]]:
        outcomes=[]
        for _ in range(max(1,min(max_jobs,100))):
            job=self.repository.claim_job(self.worker_id)
            if not job: break
            try:
                result=self.service.run_stage(job.campaign_id,job.stage,job.input)
                self.repository.finish_job(job.job_id,self.worker_id,status="completed")
                outcomes.append({"job_id":job.job_id,"stage":job.stage,"status":result.status})
                if result.status=="completed" and result.next_stage and not result.human_review_required:
                    self.repository.enqueue_job(job.campaign_id,result.next_stage,{})
            except Exception as exc:
                self.repository.finish_job(job.job_id,self.worker_id,status="retry_wait",error_code=getattr(exc,"code","trade_stage_failed"))
                outcomes.append({"job_id":job.job_id,"stage":job.stage,"status":"retry_wait"})
        return outcomes
