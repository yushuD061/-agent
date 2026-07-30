from __future__ import annotations
import argparse,csv,json
from pathlib import Path
from agent.business.trade_workbench_files import read_records,_write_quote_xlsx
from agent.business.trade_workbench_service import normalize_domain

def main():
    p=argparse.ArgumentParser();p.add_argument("--input",required=True);p.add_argument("--product");p.add_argument("--market");p.add_argument("--tone");p.add_argument("--discovery");p.add_argument("--output-dir",required=True);a=p.parse_args(); rows=read_records(a.input); unique={}
    for raw in rows:
        company=str(raw.get("company_name") or raw.get("company") or raw.get("name") or "").strip();website=str(raw.get("website") or raw.get("url") or "").strip();domain=normalize_domain(website);key=domain or company.casefold()
        if not company or not website:continue
        item=unique.setdefault(key,{"company_name":company,"website":website,"country":raw.get("country","") ,"source_url":raw.get("source_url") or website,"contact_email":raw.get("contact_email") or "没有","contact_phone":raw.get("contact_phone") or "没有","email_result":"found" if raw.get("contact_email") else "没有","phone_result":"found" if raw.get("contact_phone") else "没有"})
    out=Path(a.output_dir);out.mkdir(parents=True,exist_ok=True); enriched=list(unique.values())
    # The minimal workbook is standards-compliant and keeps all rows; research remains explicitly unfetched.
    payload={"quotation_number":"prospects","buyer":"internal","quotation_status":"draft","items":[{"sku":x["company_name"],"product":x["website"],"specification":x["country"],"packing":x["source_url"],"quantity":"","unit_price":"","amount":""} for x in enriched],"terms":{},"total_amount":"","missing_fields":[],"review_notes":["Internal prospect enrichment output"]}
    _write_quote_xlsx(payload,out/"prospects.enriched.xlsx")
    reports=[{"company_name":x["company_name"],"website":x["website"],"evidence_status":"fetch_failed","review_notes":["Run approved company research"]} for x in enriched]
    (out/"research_reports.json").write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding="utf-8")
    _write_quote_xlsx({**payload,"quotation_number":"scores","items":[]},out/"scores.xlsx");_write_quote_xlsx({**payload,"quotation_number":"email-drafts","items":[]},out/"email_drafts.xlsx")
    print(json.dumps({"input_rows":len(rows),"unique_companies":len(enriched),"outputs":[str(out/name) for name in ("prospects.enriched.xlsx","research_reports.json","scores.xlsx","email_drafts.xlsx")]},ensure_ascii=False));return 0
if __name__=="__main__":raise SystemExit(main())
