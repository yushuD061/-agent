from __future__ import annotations
import argparse, asyncio, json, re
from pathlib import Path
from agent.business.trade_workbench_service import EMAIL_RE,PHONE_RE,ROLE_RE,normalize_domain
from agent.business.trade_workbench_web import fetch_public_page

async def run(url):
    domain=normalize_domain(url); page=await fetch_public_page(url,allowed_domains={domain}); text=page["content"]
    emails=sorted(set(EMAIL_RE.findall(text))); phones=sorted(set(x.strip() for x in PHONE_RE.findall(text))); roles=sorted(set(m.group(0) for m in ROLE_RE.finditer(text)))
    candidates=[]
    for role in roles:
        email=emails[0] if emails else ""; candidates.append({"name":"","role":role,"email":email,"email_status":"domain_match" if email.lower().endswith("@"+domain) else "format_valid" if email else "missing","phone":phones[0] if phones else "","phone_status":"found" if phones else "missing","confidence":"medium","source_url":page["url"],"evidence":role})
    return {"website":url,"pages_checked":[page["url"]],"contact_search":{"email_result":"found" if emails else "没有","phone_result":"found" if phones else "没有","emails":emails,"phones":phones},"candidates":candidates,"review_notes":[] if candidates else ["未发现公开采购岗位线索"]}

def main():
    p=argparse.ArgumentParser();p.add_argument("--website",required=True);p.add_argument("--output",required=True);a=p.parse_args(); result=asyncio.run(run(a.website));Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8");print(a.output);return 0
if __name__=="__main__":raise SystemExit(main())
