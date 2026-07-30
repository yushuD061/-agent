from __future__ import annotations
import argparse, asyncio, csv, json
from pathlib import Path
import yaml
from agent.business.trade_workbench_service import normalize_domain
from agent.business.trade_workbench_web import fetch_public_page

async def run(args):
    cfg=yaml.safe_load(Path(args.discovery).read_text(encoding="utf-8")) or {}
    configured=cfg.get("allowed_sources") or []; urls=list(dict.fromkeys([*configured,*args.source_url]))
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    if not urls:
        rows=[{"query":"待补充产品 待补充市场 distributor importer","status":"task_only"}]
        with (out/"prospect_search_tasks.csv").open("w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=rows[0]); w.writeheader(); w.writerows(rows)
        return {"source_status":"search_tasks_only","outputs":[str(out/"prospect_search_tasks.csv")]}
    allowed={normalize_domain(url) for url in urls}; rows=[]; report=[]
    for url in urls:
        try:
            page=await fetch_public_page(url,allowed_domains=allowed); domain=normalize_domain(page["url"])
            rows.append({"company_name":domain,"website":f"https://{domain}","country":"","business_type":"","source_url":page["url"],"evidence_summary":page["content"][:300],"risk_notes":"needs_company_research","contact_email":"没有","contact_phone":"没有","email_result":"没有","phone_result":"没有"}); report.append({"url":url,"status":"fetched","content_hash":page["content_hash"]})
        except Exception as exc: report.append({"url":url,"status":"failed","error_code":getattr(exc,"code","trade_source_fetch_failed")})
    fields=["company_name","website","country","business_type","source_url","evidence_summary","risk_notes","contact_email","contact_phone","email_result","phone_result"]
    with (out/"prospects.raw.csv").open("w",encoding="utf-8-sig",newline="") as f: w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    (out/"prospects.raw.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
    (out/"crawl_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"source_status":"verified" if rows else "source_unavailable","count":len(rows),"outputs":[str(out/"prospects.raw.csv"),str(out/"prospects.raw.json"),str(out/"crawl_report.json")]}

def main():
    p=argparse.ArgumentParser(); p.add_argument("--discovery",default="data/config/DISCOVERY.yaml"); p.add_argument("--product",default="data/config/PRODUCT.yaml"); p.add_argument("--output-dir",required=True); p.add_argument("--source-url",action="append",default=[]); p.add_argument("--formats",nargs="*",default=["csv","json"]); args=p.parse_args(); print(json.dumps(asyncio.run(run(args)),ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
