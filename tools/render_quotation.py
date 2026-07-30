from __future__ import annotations
import argparse,json
from pathlib import Path
from agent.business.trade_workbench_files import render_quotation
def main():
    p=argparse.ArgumentParser();p.add_argument("quotation_json");p.add_argument("--output-dir",required=True);p.add_argument("--formats",nargs="*",default=["html","excel"]);a=p.parse_args();payload=json.loads(Path(a.quotation_json).read_text(encoding="utf-8"));result=render_quotation(payload,a.output_dir);print(json.dumps({k:v for k,v in result.items() if k in {"json",*a.formats}},ensure_ascii=False));return 0
if __name__=="__main__":raise SystemExit(main())
