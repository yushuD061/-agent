"""Safe CSV/YAML/JSON/XLSX import and quotation artifact helpers."""
from __future__ import annotations

import csv
from html import escape
import io
import json
from pathlib import Path
import re
import shutil
import zipfile
from xml.etree import ElementTree as ET
from typing import Any

import yaml

from agent.business.trade_workbench_repository import TradeWorkbenchError, canonical_json

MAX_IMPORT_BYTES = 10 * 1024 * 1024


def _safe_read(path: str | Path) -> bytes:
    target = Path(path).resolve()
    if not target.is_file():
        raise TradeWorkbenchError("trade_import_not_found", 404)
    if target.stat().st_size > MAX_IMPORT_BYTES:
        raise TradeWorkbenchError("trade_import_too_large", 413)
    return target.read_bytes()


def read_records(path: str | Path) -> list[dict[str, Any]]:
    target, raw = Path(path), _safe_read(path)
    return read_records_bytes(target.name, raw)


def read_records_bytes(filename: str, raw: bytes) -> list[dict[str, Any]]:
    if len(raw) > MAX_IMPORT_BYTES:
        raise TradeWorkbenchError("trade_import_too_large", 413)
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return [dict(row) for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))]
    if suffix in {".json", ".yaml", ".yml"}:
        value = json.loads(raw.decode("utf-8")) if suffix == ".json" else yaml.safe_load(raw.decode("utf-8"))
        if isinstance(value, dict): value = value.get("items") or value.get("products") or value.get("prospects") or [value]
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise TradeWorkbenchError("trade_import_invalid_shape")
        return [dict(item) for item in value]
    if suffix == ".xlsx":
        return _read_xlsx(raw)
    raise TradeWorkbenchError("trade_import_type_unsupported", 415)


def _read_xlsx(raw: bytes) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in root]
            sheet_name = next(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
            root = ET.fromstring(archive.read(sheet_name))
    except (zipfile.BadZipFile, StopIteration, ET.ParseError, KeyError) as exc:
        raise TradeWorkbenchError("trade_import_invalid_xlsx") from exc
    rows: list[list[str]] = []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for row in root.iter(ns + "row"):
        values: list[str] = []
        for cell in row.findall(ns + "c"):
            ref = cell.attrib.get("r", "A1"); col = 0
            for char in re.match(r"[A-Z]+", ref).group(0): col = col * 26 + ord(char) - 64
            while len(values) < col: values.append("")
            inline = cell.find(ns + "is")
            value_node = cell.find(ns + "v")
            value = "".join(inline.itertext()) if inline is not None else (value_node.text or "" if value_node is not None else "")
            if cell.attrib.get("t") == "s" and value: value = shared[int(value)]
            values[col - 1] = value
        rows.append(values)
    if not rows: return []
    headers = [str(item).strip() for item in rows[0]]
    return [{headers[index]: value for index, value in enumerate(row) if index < len(headers) and headers[index]}
            for row in rows[1:] if any(str(value).strip() for value in row)]


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return cleaned[:80] or "draft"


def render_quotation(payload: dict[str, Any], output_dir: str | Path,
                     *, template_path: str | Path | None = None) -> dict[str, str]:
    if payload.get("quotation_status") == "blocked":
        raise TradeWorkbenchError("trade_quotation_blocked", 409)
    out = Path(output_dir).resolve(); out.mkdir(parents=True, exist_ok=True)
    number = safe_filename(str(payload["quotation_number"])); buyer = safe_filename(str(payload.get("buyer", "buyer")))
    base = f"quotation-{number}-{buyer}-draft"
    json_path = out / f"{base}.json"; json_path.write_text(canonical_json(payload), encoding="utf-8")
    template = Path(template_path) if template_path else Path(__file__).parents[2] / "skills" / "quotation-generator" / "templates" / "quotation.html"
    html = template.read_text(encoding="utf-8")
    item_rows = "".join("<tr>" + "".join(f"<td>{escape(str(item.get(key, '')))}</td>" for key in
        ("sku", "product", "specification", "packing", "quantity", "unit_price", "amount")) + "</tr>" for item in payload.get("items", []))
    terms = payload.get("terms") or {}
    replacements = {"quotation_number": payload.get("quotation_number", ""), "buyer_name": payload.get("buyer", ""),
        "buyer_country": payload.get("buyer_country", ""), "seller_name": payload.get("seller", "待补充"),
        "quotation_date": payload.get("quotation_date", ""), "validity": terms.get("validity", ""),
        "incoterm": terms.get("incoterm", ""), "payment_terms": terms.get("payment_terms", ""),
        "items": item_rows, "total_amount": payload.get("total_amount", "")}
    for key, value in replacements.items(): html = html.replace("{{" + key + "}}", str(value) if key == "items" else escape(str(value)))
    html_path = out / f"{base}.html"; html_path.write_text(html, encoding="utf-8")
    xlsx_path = out / f"{base}.xlsx"; _write_quote_xlsx(payload, xlsx_path)
    return {"json": str(json_path), "html": str(html_path), "excel": str(xlsx_path)}


def _xml(value: Any) -> str:
    return escape(str(value), quote=False)


def _sheet(rows: list[list[Any]]) -> str:
    body=[]
    for rindex,row in enumerate(rows,1):
        cells=[]
        for cindex,value in enumerate(row,1):
            n=cindex; letters=""
            while n: n,rem=divmod(n-1,26); letters=chr(65+rem)+letters
            cells.append(f'<c r="{letters}{rindex}" t="inlineStr"><is><t>{_xml(value)}</t></is></c>')
        body.append(f'<row r="{rindex}">{"".join(cells)}</row>')
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+"".join(body)+"</sheetData></worksheet>"


def _write_quote_xlsx(payload: dict[str, Any], path: Path) -> None:
    terms=payload.get("terms") or {}
    sheets={
      "Quotation":[["Quotation","待确认 / DRAFT"],["Number",payload.get("quotation_number","")],["Buyer",payload.get("buyer","")],["Total",payload.get("total_amount","")],["Human review required","true"]],
      "Items":[["SKU","Product","Specification","Packing","Quantity","Unit Price","Amount"]]+[[item.get(k,"") for k in ("sku","product","specification","packing","quantity","unit_price","amount")] for item in payload.get("items",[])],
      "Terms":[["Term","Value"]]+[[key,value] for key,value in terms.items()],
      "Review Notes":[["Missing Fields"]]+[[item] for item in payload.get("missing_fields",[])]+[["Review Notes"]]+[[item] for item in payload.get("review_notes",[])],
    }
    types='<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,5))+'</Types>'
    rels='<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook='<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i,name in enumerate(sheets,1))+'</sheets></workbook>'
    wb_rels='<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,5))+'</Relationships>'
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",types); archive.writestr("_rels/.rels",rels)
        archive.writestr("xl/workbook.xml",workbook); archive.writestr("xl/_rels/workbook.xml.rels",wb_rels)
        for i,rows in enumerate(sheets.values(),1): archive.writestr(f"xl/worksheets/sheet{i}.xml",_sheet(rows))
