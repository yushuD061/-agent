from __future__ import annotations
import hashlib, html, json
from pathlib import Path
from .contracts import CanonicalDocument, DocumentStatus

def _hash(text: str) -> str: return hashlib.sha256(text.encode("utf-8")).hexdigest()

class DocumentLoader:
    """标准库加载器；PDF/DOCX 需在部署时通过可选适配器启用。"""
    supported = {".md": "text/markdown", ".markdown": "text/markdown", ".html": "text/html", ".htm": "text/html", ".json": "application/json", ".txt": "text/plain", ".csv": "text/csv"}
    def load(self, path: str | Path, *, document_id: str | None = None, version: int = 1, business_unit_id: str = "default", allowed_roles: set[str] | frozenset[str] = frozenset()) -> CanonicalDocument:
        p = Path(path); suffix = p.suffix.lower()
        if suffix not in self.supported: raise ValueError(f"unsupported document type: {suffix}")
        raw = p.read_text(encoding="utf-8")
        content = html.unescape(raw)
        if suffix in {".html", ".htm"}: content = _strip_html(content)
        if not content.strip(): raise ValueError("empty document")
        did = document_id or _hash(str(p.resolve()))[:16]
        return CanonicalDocument(did, version, str(p), p.stem, content, _hash(content), self.supported[suffix], str(p), "zh" if any("\u4e00" <= c <= "\u9fff" for c in content) else "en", business_unit_id, frozenset(allowed_roles), status=DocumentStatus.REVIEW_REQUIRED)

    def load_record(self, record: dict, *, document_id: str, version: int = 1) -> CanonicalDocument:
        content = str(record.get("content", ""));
        if not content.strip(): raise ValueError("empty record")
        return CanonicalDocument(document_id, version, str(record.get("source_uri", "record://" + document_id)), str(record.get("title", document_id)), content, _hash(content), "application/json", str(record.get("location", "")), str(record.get("language", "und")), str(record.get("business_unit_id", "default")), frozenset(record.get("allowed_roles", [])), status=DocumentStatus.REVIEW_REQUIRED, metadata={k:v for k,v in record.items() if k not in {"content"}})

def _strip_html(value: str) -> str:
    import re
    value = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>", " ", value)
    return re.sub(r"<[^>]+>", " ", value).replace("&nbsp;", " ")

class ApprovalWorkflow:
    allowed = {DocumentStatus.DRAFT: {DocumentStatus.REVIEW_REQUIRED}, DocumentStatus.REVIEW_REQUIRED: {DocumentStatus.APPROVED}, DocumentStatus.APPROVED: {DocumentStatus.PUBLISHED, DocumentStatus.REVOKED}, DocumentStatus.PUBLISHED: {DocumentStatus.REVOKED, DocumentStatus.EXPIRED}}
    def transition(self, document: CanonicalDocument, target: DocumentStatus) -> CanonicalDocument:
        if target not in self.allowed.get(document.status, set()): raise ValueError(f"invalid transition {document.status} -> {target}")
        document.status = target; return document
