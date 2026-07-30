"""Durable, local-only document imports shared by the Web UI and RAG MCP process."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .chunking import PDF_SPLITTER_VERSION, ParentChildSplitter, PdfAwareParentChildSplitter
from .config import OcrConfig, PdfIngestionConfig, load_ocr_config, load_pdf_ingestion_config
from .contracts import (
    CanonicalDocument,
    DocumentStatus,
    ImageArtifact,
    ImageSourceKind,
    ParseResult,
    ParsedBlock,
    ParsedBlockType,
    PdfErrorCode,
    PdfIngestionRoute,
    PdfWarningCode,
    ProcessingStatus,
    SourceLocation,
)
from .loaders import ApprovalWorkflow, DocumentLoader, _strip_html
from .parsers import PdfProcessingError, parse_document_bytes
from .ocr import (PaddleOcrProvider, OcrOutput, OcrProcessingError,
                  SUPPORTED_IMAGE_TYPES, extract_pdf_images, make_image_artifact,
                  validate_image_bytes)


MANIFEST_SCHEMA = "knowledge-import-v4"
PARSED_ARTIFACT_SCHEMA = "knowledge-pdf-parse-v1"
SUPPORTED_MANIFEST_SCHEMAS = {
    "knowledge-import-v1", "knowledge-import-v2", "knowledge-import-v3", MANIFEST_SCHEMA,
}
SUPPORTED_TEXT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".csv": "text/csv",
}
PRIVATE_RECORD_FIELDS = {"stored_name", "source_stored_name", "parsed_stored_name", "parsed_hash"}


def _image_public_payload(image: ImageArtifact) -> dict:
    return {
        "image_id": image.image_id, "image_index": image.image_index,
        "source_kind": image.source_kind.value, "page_number": image.page_number,
        "page_image_index": image.page_image_index, "source_hash": image.source_hash,
        "mime_type": image.mime_type, "width": image.width, "height": image.height,
        "ocr_text": image.ocr_text, "ocr_confidence": image.ocr_confidence,
        "ocr_status": image.ocr_status,
    }


def safe_upload_name(value: str) -> str:
    """Discard client paths/control characters while retaining a readable filename."""
    name = Path(value.replace("\\", "/")).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        raise ValueError("invalid_file_name")
    return name[:180]


def decode_text_document(filename: str, payload: bytes, *, max_bytes: int) -> tuple[str, str, str]:
    """Validate and decode a bounded UTF-8 text document without executing its content."""
    name = safe_upload_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_TEXT_TYPES:
        raise ValueError("unsupported_file_type")
    if not payload:
        raise ValueError("empty_file")
    if len(payload) > max_bytes:
        raise ValueError("file_too_large")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("file_must_be_utf8") from exc
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_json") from exc
    if suffix in {".html", ".htm"}:
        text = _strip_html(html.unescape(text))
    if not text.strip():
        raise ValueError("empty_file")
    return name, text, SUPPORTED_TEXT_TYPES[suffix]


def public_document_record(item: dict) -> dict:
    record = {key: value for key, value in item.items() if key not in PRIVATE_RECORD_FIELDS}
    record["review_approval_eligible"] = _review_approval_eligible(item)
    record["review_type"] = _review_type(item)
    return record


def _complex_layout_review_safe(item: dict) -> bool:
    warnings = {str(value) for value in item.get("parse_warnings", [])}
    return (
        item.get("content_type") == "application/pdf"
        and item.get("parse_status") == "ready"
        and not bool(item.get("needs_ocr"))
        and float(item.get("text_page_ratio") or 0) == 1.0
        and "pdf_complex_layout_review" in warnings
        and "ocr_low_confidence" not in warnings
        and "pdf_partial_text_coverage" not in warnings
        and "pdf_empty_page" not in warnings
    )


def _ocr_review_safe(item: dict) -> bool:
    images = list(item.get("images") or [])
    warnings = {str(value) for value in item.get("parse_warnings", [])}
    if not images or not any(image.get("ocr_status") == "low_confidence" for image in images):
        return False
    if item.get("parse_status") != "ready" or bool(item.get("needs_ocr")):
        return False
    if any(image.get("ocr_status") in {"failed", "pending"} for image in images):
        return False
    if str(item.get("content_type", "")).startswith("image/"):
        return bool(images[0].get("ocr_text"))
    return (
        item.get("content_type") == "application/pdf"
        and float(item.get("text_page_ratio") or 0) == 1.0
        and "pdf_partial_text_coverage" not in warnings
        and "pdf_empty_page" not in warnings
    )


def _review_type(item: dict) -> str | None:
    if item.get("status") != "review_required":
        return None
    if _ocr_review_safe(item):
        return "ocr_low_confidence"
    if _complex_layout_review_safe(item):
        return "complex_layout"
    return None


def _review_approval_eligible(item: dict) -> bool:
    return _review_type(item) is not None


def _approved_review_safe(item: dict) -> bool:
    if not item.get("review_approved_at"):
        return False
    review_type = item.get("review_type")
    if review_type == "ocr_low_confidence":
        copy = dict(item); copy["status"] = "review_required"
        return _ocr_review_safe(copy)
    if review_type == "complex_layout" or review_type is None:
        return _complex_layout_review_safe(item)
    return False


def _parse_result_payload(result: ParseResult, *, source_hash: str, content_hash: str) -> dict:
    return {
        "schema_version": PARSED_ARTIFACT_SCHEMA,
        "source_hash": source_hash,
        "content_hash": content_hash,
        "parser": result.parser,
        "parser_version": result.parser_version,
        "pages": result.pages,
        "text_chars": result.text_chars,
        "text_page_ratio": result.text_page_ratio,
        "needs_ocr": result.needs_ocr,
        "status": result.status.value,
        "route": result.route.value,
        "warnings": [warning.value for warning in result.warnings],
        "page_char_counts": list(result.page_char_counts),
        "blank_pages": list(result.blank_pages),
        "images": [
            {
                "image_id": image.image_id, "image_index": image.image_index,
                "source_kind": image.source_kind.value, "page_number": image.page_number,
                "page_image_index": image.page_image_index, "source_hash": image.source_hash,
                "mime_type": image.mime_type, "width": image.width, "height": image.height,
                "ocr_text": image.ocr_text, "ocr_confidence": image.ocr_confidence,
                "ocr_status": image.ocr_status,
            } for image in result.images
        ],
        "blocks": [
            {
                "text": block.text,
                "ordinal": block.ordinal,
                "metadata": block.metadata,
                "location": {
                    "page_start": block.location.page_start,
                    "page_end": block.location.page_end,
                    "section_path": list(block.location.section_path),
                    "block_type": block.location.block_type.value,
                    "bbox": list(block.location.bbox) if block.location.bbox is not None else None,
                },
            }
            for block in result.blocks
        ],
    }


def _parse_result_from_payload(payload: dict) -> ParseResult:
    if payload.get("schema_version") != PARSED_ARTIFACT_SCHEMA:
        raise ValueError("parsed artifact schema is invalid")
    blocks = []
    for raw in payload.get("blocks", []):
        location = raw["location"]
        blocks.append(ParsedBlock(
            text=str(raw["text"]),
            location=SourceLocation(
                page_start=int(location["page_start"]),
                page_end=int(location["page_end"]),
                section_path=tuple(str(value) for value in location.get("section_path", [])),
                block_type=ParsedBlockType(str(location.get("block_type", "paragraph"))),
                bbox=tuple(float(value) for value in location["bbox"]) if location.get("bbox") is not None else None,
            ),
            ordinal=int(raw["ordinal"]),
            metadata=dict(raw.get("metadata", {})),
        ))
    result = ParseResult(
        parser=str(payload["parser"]),
        parser_version=str(payload["parser_version"]),
        pages=int(payload["pages"]),
        text_chars=int(payload["text_chars"]),
        text_page_ratio=float(payload["text_page_ratio"]),
        needs_ocr=bool(payload["needs_ocr"]),
        status=ProcessingStatus(str(payload["status"])),
        warnings=tuple(PdfWarningCode(str(value)) for value in payload.get("warnings", [])),
        blocks=tuple(blocks),
        page_char_counts=tuple(int(value) for value in payload.get("page_char_counts", [])),
        blank_pages=tuple(int(value) for value in payload.get("blank_pages", [])),
        images=tuple(ImageArtifact(
            image_id=str(raw["image_id"]), image_index=int(raw["image_index"]),
            source_kind=ImageSourceKind(str(raw["source_kind"])),
            page_number=int(raw["page_number"]) if raw.get("page_number") is not None else None,
            page_image_index=int(raw["page_image_index"]), source_hash=str(raw["source_hash"]),
            mime_type=str(raw["mime_type"]), width=int(raw["width"]), height=int(raw["height"]),
            ocr_text=str(raw.get("ocr_text", "")),
            ocr_confidence=float(raw["ocr_confidence"]) if raw.get("ocr_confidence") is not None else None,
            ocr_status=str(raw.get("ocr_status", "pending")),
        ) for raw in payload.get("images", [])),
    )
    if payload.get("route") != result.route.value:
        raise ValueError("parsed artifact route is invalid")
    return result


class KnowledgeRepository:
    """Content-addressed local knowledge documents plus an atomic manifest."""

    def __init__(self, root: str | Path | None = None, *, max_bytes: int = 5 * 1024 * 1024,
                 pdf_config: PdfIngestionConfig | None = None,
                 ocr_config: OcrConfig | None = None, ocr_provider=None):
        project_root = Path(__file__).resolve().parent.parent
        self.root = Path(root) if root else project_root / "workspace" / "knowledge_base"
        self.documents_dir = self.root / "documents"
        self.parsed_dir = self.root / "parsed"
        self.manifest_path = self.root / "manifest.json"
        self.max_text_bytes = max_bytes
        self.pdf_config = pdf_config or load_pdf_ingestion_config()
        self.ocr_config = ocr_config or load_ocr_config()
        self.ocr_provider = ocr_provider or PaddleOcrProvider(self.ocr_config)
        self.max_bytes = max(max_bytes, self.pdf_config.max_bytes, self.ocr_config.max_image_bytes)
        self.loader = DocumentLoader()
        self.splitter = ParentChildSplitter()
        self.pdf_splitter = PdfAwareParentChildSplitter()
        self.workflow = ApprovalWorkflow()
        self._lock = threading.RLock()
        self.trash_dir = self.root / "trash"

    def _read_manifest(self) -> dict:
        if not self.manifest_path.is_file():
            return {"schema_version": MANIFEST_SCHEMA, "documents": []}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("knowledge_manifest_invalid") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("documents"), list)
                or data.get("schema_version") not in SUPPORTED_MANIFEST_SCHEMAS):
            raise RuntimeError("knowledge_manifest_invalid")
        return data

    def _write_manifest(self, manifest: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest["schema_version"] = MANIFEST_SCHEMA
        temporary = self.root / f".manifest-{os.getpid()}-{threading.get_ident()}.json.tmp"
        try:
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_stem(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-._") or "document"

    @staticmethod
    def _source_name(item: dict) -> str:
        return safe_upload_name(str(item.get("source_stored_name") or item.get("stored_name") or ""))

    def _source_path(self, item: dict) -> Path:
        return self.documents_dir / self._source_name(item)

    def _parsed_path(self, item: dict) -> Path | None:
        value = item.get("parsed_stored_name")
        return self.parsed_dir / safe_upload_name(str(value)) if value else None

    @staticmethod
    def _write_bytes_atomic(directory: Path, name: str, payload: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_upload_name(name)
        temporary = directory / f".{target.name}.{os.getpid()}-{threading.get_ident()}.tmp"
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def _load_entry_document(self, item: dict) -> CanonicalDocument:
        if str(item.get("content_type", "")).lower() == "application/pdf":
            raise ValueError("pdf_chunking_not_ready")
        if str(item.get("content_type", "")).lower().startswith("image/"):
            images = item.get("images") or []
            content = "\n\n".join(str(image.get("ocr_text", "")).strip()
                                    for image in images if str(image.get("ocr_text", "")).strip())
            if not content or hashlib.sha256(content.encode("utf-8")).hexdigest() != item.get("content_hash"):
                raise RuntimeError("knowledge_content_integrity_failed")
            return CanonicalDocument(
                document_id=str(item["document_id"]), version=int(item.get("version", 1)),
                source_uri=f"knowledge://{item['document_id']}", title=str(item.get("title")),
                content=content, content_hash=str(item["content_hash"]),
                content_type=str(item["content_type"]), location="image:1", language="und",
                business_unit_id=str(item.get("business_unit_id", "default")),
                allowed_roles=frozenset(str(role) for role in item.get("allowed_roles", [])),
                classification=str(item.get("classification", "internal")),
                status=DocumentStatus.PUBLISHED, parser_version=str(item.get("parser_version")),
                metadata={"source_hash": str(item.get("source_hash", "")), "images": images},
            )
        path = self._source_path(item)
        if not path.is_file():
            raise FileNotFoundError("knowledge_source_missing")
        document = self.loader.load(
            path,
            document_id=str(item["document_id"]),
            version=int(item.get("version", 1)),
            business_unit_id=str(item.get("business_unit_id", "default")),
            allowed_roles=frozenset(str(role) for role in item.get("allowed_roles", [])),
        )
        self.workflow.transition(document, DocumentStatus.APPROVED)
        self.workflow.transition(document, DocumentStatus.PUBLISHED)
        return replace(document, classification=str(item.get("classification", "internal")))

    def _load_pdf_document(self, item: dict, result: ParseResult, *,
                           reviewed_complex_layout: bool = False) -> CanonicalDocument:
        if not result.indexable and not reviewed_complex_layout:
            raise ValueError("pdf_parse_result_not_indexable")
        content = result.content
        expected_hash = str(item.get("content_hash", ""))
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash:
            raise RuntimeError("knowledge_content_integrity_failed")
        language = "zh" if any("\u4e00" <= char <= "\u9fff" for char in content) else "en"
        return CanonicalDocument(
            document_id=str(item["document_id"]),
            version=int(item.get("version", 1)),
            source_uri=f"knowledge://{item['document_id']}",
            title=str(item.get("title") or item["document_id"]),
            content=content,
            content_hash=expected_hash,
            content_type="application/pdf",
            location=f"pages:1-{result.pages}",
            language=language,
            business_unit_id=str(item.get("business_unit_id", "default")),
            allowed_roles=frozenset(str(role) for role in item.get("allowed_roles", [])),
            classification=str(item.get("classification", "internal")),
            status=DocumentStatus.PUBLISHED,
            parser_version=result.parser_version,
            metadata={
                "source_hash": str(item.get("source_hash", "")),
                "pages": result.pages,
                "parser": result.parser,
                "splitter_version": PDF_SPLITTER_VERSION,
                "images": [_image_public_payload(image) for image in result.images],
            },
        )

    def _upgrade_entry(self, item: dict) -> bool:
        original_entry_schema = item.get("schema_version")
        is_current_entry = original_entry_schema == MANIFEST_SCHEMA
        trusted_integrity_entry = original_entry_schema in {
            "knowledge-import-v3", MANIFEST_SCHEMA,
        }
        changed = not is_current_entry
        now = self._now()
        defaults = {
            "size_bytes": None,
            "source_hash": None,
            "source_stored_name": item.get("stored_name"),
            "parsed_stored_name": None,
            "parsed_hash": None,
            "parse_status": "pending",
            "chunk_status": "pending",
            "index_status": "pending",
            "indexed_count": None,
            "parent_count": None,
            "child_count": None,
            "splitter_version": "parent-child-v1",
            "parser": "stdlib",
            "parser_version": "stdlib-1",
            "pages": None,
            "text_chars": None,
            "text_page_ratio": None,
            "needs_ocr": False,
            "image_count": 0,
            "ocr_image_count": 0,
            "ocr_no_text_count": 0,
            "images": [],
            "review_type": None,
            "review_approved_at": None,
            "parse_warnings": [],
            "ingestion_route": "index",
            "index_generation": None,
            "embedding_model_id": None,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "last_error_code": None,
            "classification": "internal",
            "processing_attempt_count": 0,
            "processing_started_at": None,
            "processing_completed_at": None,
        }
        for key, value in defaults.items():
            if key not in item:
                item[key] = value
                changed = True
        if "stored_name" not in item and item.get("source_stored_name"):
            item["stored_name"] = item["source_stored_name"]
            changed = True

        path = None
        try:
            path = self._source_path(item)
        except ValueError:
            pass
        if path is None or not path.is_file():
            if item.get("status") != "revoked":
                item.update(parse_status="failed", chunk_status="failed", index_status="failed",
                            last_error_code="knowledge_source_missing", updated_at=now)
                changed = True
            item["schema_version"] = MANIFEST_SCHEMA
            return changed

        raw = path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        if not item.get("source_hash"):
            item["source_hash"] = source_hash
            changed = True
        elif item.get("source_hash") != source_hash and trusted_integrity_entry:
            if (item.get("parse_status"), item.get("chunk_status"), item.get("index_status"),
                    item.get("last_error_code")) != (
                    "failed", "failed", "failed", "knowledge_source_integrity_failed"):
                item.update(parse_status="failed", chunk_status="failed", index_status="failed",
                            last_error_code="knowledge_source_integrity_failed", updated_at=now)
                changed = True
            item["schema_version"] = MANIFEST_SCHEMA
            return changed
        elif item.get("source_hash") != source_hash:
            item["source_hash"] = source_hash
            changed = True
        if item.get("size_bytes") is None:
            item["size_bytes"] = len(raw)
            changed = True
        elif item.get("size_bytes") != len(raw) and trusted_integrity_entry:
            item.update(parse_status="failed", chunk_status="failed", index_status="failed",
                        last_error_code="knowledge_source_integrity_failed", updated_at=now)
            item["schema_version"] = MANIFEST_SCHEMA
            return True
        elif item.get("size_bytes") != len(raw):
            item["size_bytes"] = len(raw)
            changed = True

        suffix = Path(str(item.get("original_name") or path.name)).suffix.lower()
        if suffix == ".pdf" or item.get("content_type") == "application/pdf":
            processing = item.get("parse_status") in {"pending", "running"}
            if (not item.get("parsed_stored_name") and item.get("status") != "revoked"
                    and not processing):
                item.update(parse_status="failed", chunk_status="pending", index_status="pending",
                            last_error_code="knowledge_parsed_artifact_missing", updated_at=now)
                changed = True
        elif suffix in SUPPORTED_IMAGE_TYPES or str(item.get("content_type", "")).startswith("image/"):
            try:
                expected_type = SUPPORTED_IMAGE_TYPES[suffix]
                validate_image_bytes(raw, expected_type, self.ocr_config)
                images = list(item.get("images") or [])
                if len(images) != 1 or int(images[0].get("image_index", 0)) != 1:
                    raise ValueError("knowledge_image_metadata_invalid")
                text = str(images[0].get("ocr_text", "")).strip()
                expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if item.get("content_hash") != expected_hash:
                    raise ValueError("knowledge_content_integrity_failed")
                if not text:
                    updates = {"parent_count": 0, "child_count": 0, "indexed_count": 0,
                               "chunk_status": "ready", "index_status": "indexed"}
                    for key, value in updates.items():
                        if item.get(key) != value:
                            item[key] = value; changed = True
            except Exception:
                item.update(parse_status="failed", chunk_status="failed", index_status="failed",
                            last_error_code="knowledge_statistics_failed", updated_at=now)
                changed = True
        else:
            try:
                name, text, content_type = decode_text_document(
                    str(item.get("original_name") or path.name), raw, max_bytes=self.max_text_bytes
                )
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if (trusted_integrity_entry and item.get("content_hash")
                        and item.get("content_hash") != content_hash):
                    item.update(parse_status="failed", chunk_status="failed", index_status="failed",
                                last_error_code="knowledge_content_integrity_failed", updated_at=now)
                    item["schema_version"] = MANIFEST_SCHEMA
                    return True
                document = self._load_entry_document(item)
                parents, children = self.splitter.split(document)
                updates = {
                    "original_name": name,
                    "content_type": content_type,
                    "content_hash": content_hash,
                    "parse_status": "ready",
                    "chunk_status": "ready",
                    "parent_count": len(parents),
                    "child_count": len(children),
                    "parser": "stdlib",
                    "parser_version": "stdlib-1",
                    "pages": 1,
                    "text_chars": len(text),
                    "text_page_ratio": 1.0,
                    "needs_ocr": False,
                    "parse_warnings": [],
                    "ingestion_route": "index",
                    "last_error_code": None,
                }
                for key, value in updates.items():
                    if item.get(key) != value:
                        item[key] = value
                        changed = True
            except Exception:
                item.update(parse_status="failed", chunk_status="failed", index_status="failed",
                            last_error_code="knowledge_statistics_failed", updated_at=now)
                changed = True
        item["schema_version"] = MANIFEST_SCHEMA
        return changed

    def _read_upgraded_manifest(self) -> dict:
        manifest = self._read_manifest()
        original_schema = str(manifest.get("schema_version"))
        changed = original_schema != MANIFEST_SCHEMA
        for item in manifest["documents"]:
            changed = self._upgrade_entry(item) or changed
        if not changed:
            return manifest

        backup = None
        if original_schema != MANIFEST_SCHEMA and self.manifest_path.is_file():
            version = original_schema.rsplit("-", 1)[-1]
            backup = self.root / f"manifest.{version}.backup.json"
            if not backup.exists():
                shutil.copy2(self.manifest_path, backup)
        try:
            self._write_manifest(manifest)
        except Exception:
            if backup is not None and backup.is_file():
                shutil.copy2(backup, self.manifest_path)
            raise
        return manifest

    def _find_duplicate(self, manifest: dict, source_hash: str) -> tuple[dict | None, list[dict]]:
        matches = [item for item in manifest["documents"] if item.get("source_hash") == source_hash]
        active = next((item for item in matches if item.get("status") != "revoked"), None)
        return active, matches

    def stage_pdf(self, filename: str, payload: bytes, *, classification: str = "internal",
                  content_type: str | None = None) -> dict:
        """Durably accept one PDF without running parsing, OCR, chunking, or indexing."""
        if classification not in {"internal", "public"}:
            raise ValueError("invalid_knowledge_classification")
        name = safe_upload_name(filename)
        if Path(name).suffix.lower() != ".pdf":
            raise ValueError("unsupported_file_type")
        if not payload:
            raise ValueError("empty_file")
        if len(payload) > self.pdf_config.max_bytes:
            raise ValueError(PdfErrorCode.FILE_TOO_LARGE.value)
        supplied_type = (content_type or "application/pdf").split(";", 1)[0].strip().lower()
        if supplied_type not in {"application/pdf", "application/x-pdf", "application/octet-stream",
                                 "binary/octet-stream", ""}:
            raise ValueError(PdfErrorCode.SIGNATURE_MISMATCH.value)

        source_hash = hashlib.sha256(payload).hexdigest()
        with self._lock:
            manifest = self._read_upgraded_manifest()
            existing, matches = self._find_duplicate(manifest, source_hash)
            if existing:
                return {**existing, "duplicate": True}
            generation = len(matches) + 1
            generation_suffix = f"-v{generation}" if generation > 1 else ""
            document_id = f"web-{source_hash[:24]}{generation_suffix}"
            safe_stem = self._safe_stem(name)
            stored_name = f"{safe_stem[:60]}-{source_hash[:12]}{generation_suffix}.pdf"
            parsed_name = f"{safe_stem[:60]}-{source_hash[:12]}{generation_suffix}.parsed.json"
            now = self._now()
            entry = {
                "schema_version": MANIFEST_SCHEMA,
                "document_id": document_id,
                "version": 1,
                "title": Path(name).stem,
                "original_name": name,
                "stored_name": stored_name,
                "source_stored_name": stored_name,
                "parsed_stored_name": parsed_name,
                "parsed_hash": None,
                "source_hash": source_hash,
                "content_hash": None,
                "content_type": "application/pdf",
                "business_unit_id": "default",
                "allowed_roles": [],
                "classification": classification,
                "status": "draft",
                "size_bytes": len(payload),
                "parse_status": "pending",
                "chunk_status": "pending",
                "index_status": "pending",
                "indexed_count": 0,
                "parent_count": None,
                "child_count": None,
                "splitter_version": "pdf-aware-pending",
                "parser": "pending",
                "parser_version": "pending",
                "pages": None,
                "text_chars": None,
                "text_page_ratio": None,
                "needs_ocr": False,
                "image_count": 0,
                "ocr_image_count": 0,
                "ocr_no_text_count": 0,
                "images": [],
                "parse_warnings": [],
                "ingestion_route": "pending",
                "review_type": None,
                "review_approved_at": None,
                "index_generation": None,
                "embedding_model_id": None,
                "processing_attempt_count": 0,
                "processing_started_at": None,
                "processing_completed_at": None,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
                "last_error_code": None,
            }
            target = self._write_bytes_atomic(self.documents_dir, stored_name, payload)
            try:
                manifest["documents"].append(entry)
                self._write_manifest(manifest)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return {**entry, "duplicate": False}

    def _mark_pdf_processing_failure(self, document_id: str, *, stage: str,
                                     error_code: str) -> dict:
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("status") in {"revoked", "revoke_pending"}:
                return dict(item)
            updates = {
                "status": "draft",
                "last_error_code": error_code,
                "processing_completed_at": self._now(),
                "updated_at": self._now(),
            }
            if stage == "parse":
                updates.update(parse_status="failed", chunk_status="failed", index_status="failed")
            elif stage == "chunk":
                updates.update(chunk_status="failed", index_status="failed")
            else:
                updates.update(index_status="failed")
            item.update(updates)
            self._write_manifest(manifest)
            return dict(item)

    def process_staged_pdf(self, document_id: str) -> dict:
        """Resume a staged PDF through durable parse/OCR and chunk preparation."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("content_type") != "application/pdf":
                raise ValueError("knowledge_document_is_not_pdf")
            if item.get("status") in {"revoked", "revoke_pending"}:
                return dict(item)
            parsed_ready = item.get("parse_status") == "ready"
            if not parsed_ready:
                now = self._now()
                item.update(
                    status="draft", parse_status="running", chunk_status="pending",
                    index_status="pending", last_error_code=None,
                    processing_attempt_count=int(item.get("processing_attempt_count") or 0) + 1,
                    processing_started_at=now, processing_completed_at=None, updated_at=now,
                )
                self._write_manifest(manifest)
            snapshot = dict(item)

        if parsed_ready:
            try:
                result = self.load_parsed_result(document_id)
            except Exception:
                parsed_ready = False

        if not parsed_ready:
            try:
                source = self._source_path(snapshot)
                payload = source.read_bytes()
                if (hashlib.sha256(payload).hexdigest() != snapshot.get("source_hash")
                        or len(payload) != int(snapshot.get("size_bytes") or -1)):
                    raise RuntimeError("knowledge_source_integrity_failed")
                result = parse_document_bytes(
                    str(snapshot["original_name"]), payload,
                    content_type="application/pdf", config=self.pdf_config,
                )
                result = self._augment_pdf_with_ocr(payload, result, str(snapshot["source_hash"]))
                content_hash = hashlib.sha256(result.content.encode("utf-8")).hexdigest()
                artifact_payload = _parse_result_payload(
                    result, source_hash=str(snapshot["source_hash"]), content_hash=content_hash,
                )
                artifact_bytes = (
                    json.dumps(artifact_payload, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8")
                parsed_hash = hashlib.sha256(artifact_bytes).hexdigest()
                with self._lock:
                    manifest = self._read_upgraded_manifest()
                    item = self._select_document_entry(manifest, document_id)
                    if item is None:
                        raise KeyError("knowledge_document_not_found")
                    if item.get("status") in {"revoked", "revoke_pending"}:
                        return dict(item)
                    parsed_name = str(item["parsed_stored_name"])
                    self._write_bytes_atomic(self.parsed_dir, parsed_name, artifact_bytes)
                    final_status = "draft" if result.indexable else "review_required"
                    item.update(
                        status=final_status,
                        content_hash=content_hash,
                        parsed_hash=parsed_hash,
                        parse_status=result.status.value,
                        chunk_status="pending",
                        index_status="pending",
                        indexed_count=0,
                        parent_count=None,
                        child_count=None,
                        splitter_version="pdf-aware-pending",
                        parser=result.parser,
                        parser_version=result.parser_version,
                        pages=result.pages,
                        text_chars=result.text_chars,
                        text_page_ratio=result.text_page_ratio,
                        needs_ocr=result.needs_ocr,
                        image_count=len(result.images),
                        ocr_image_count=sum(bool(image.ocr_text) for image in result.images),
                        ocr_no_text_count=sum(image.ocr_status == "no_text" for image in result.images),
                        images=[_image_public_payload(image) for image in result.images],
                        parse_warnings=[warning.value for warning in result.warnings],
                        ingestion_route=result.route.value,
                        review_type=(
                            "ocr_low_confidence"
                            if PdfWarningCode.OCR_LOW_CONFIDENCE in result.warnings
                            else "complex_layout"
                            if PdfWarningCode.COMPLEX_LAYOUT_REVIEW in result.warnings
                            else None
                        ),
                        last_error_code=(PdfErrorCode.NEEDS_OCR.value if result.needs_ocr else None),
                        processing_completed_at=(self._now() if not result.indexable else None),
                        updated_at=self._now(),
                    )
                    self._write_manifest(manifest)
                    snapshot = dict(item)
            except PdfProcessingError as exc:
                return self._mark_pdf_processing_failure(
                    document_id, stage="parse", error_code=exc.code.value,
                )
            except OcrProcessingError as exc:
                code = str(exc) if str(exc).startswith("ocr_") else "ocr_recognition_failed"
                return self._mark_pdf_processing_failure(document_id, stage="parse", error_code=code)
            except Exception as exc:
                code = str(exc) if str(exc) in {
                    "knowledge_source_integrity_failed", "knowledge_source_missing",
                } else PdfErrorCode.PARSE_FAILED.value
                return self._mark_pdf_processing_failure(document_id, stage="parse", error_code=code)

        if not result.indexable:
            return snapshot
        if snapshot.get("chunk_status") == "ready":
            return snapshot

        try:
            with self._lock:
                manifest = self._read_upgraded_manifest()
                item = self._select_document_entry(manifest, document_id)
                if item is None:
                    raise KeyError("knowledge_document_not_found")
                if item.get("status") in {"revoked", "revoke_pending"}:
                    return dict(item)
                item.update(chunk_status="running", updated_at=self._now())
                self._write_manifest(manifest)
                snapshot = dict(item)
            document = self._load_pdf_document(snapshot, result)
            parents, children = self.pdf_splitter.split(document, result)
            if not parents or not children:
                raise RuntimeError("knowledge_chunks_empty")
            with self._lock:
                manifest = self._read_upgraded_manifest()
                item = self._select_document_entry(manifest, document_id)
                if item is None:
                    raise KeyError("knowledge_document_not_found")
                if item.get("status") in {"revoked", "revoke_pending"}:
                    return dict(item)
                item.update(
                    chunk_status="ready", index_status="pending",
                    parent_count=len(parents), child_count=len(children), indexed_count=0,
                    splitter_version=self.pdf_splitter.version, last_error_code=None,
                    updated_at=self._now(),
                )
                self._write_manifest(manifest)
                return dict(item)
        except Exception:
            return self._mark_pdf_processing_failure(
                document_id, stage="chunk", error_code="knowledge_chunk_failed",
            )

    def prepare_pdf_retry(self, document_id: str) -> dict:
        """Reset a failed PDF stage while retaining its durable source and stable ID."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if (item.get("content_type") != "application/pdf"
                    or item.get("status") in {"revoked", "revoke_pending"}):
                raise ValueError("knowledge_retry_not_allowed")
            if "failed" not in {
                item.get("parse_status"), item.get("chunk_status"), item.get("index_status"),
            }:
                return dict(item)
            if item.get("parse_status") == "failed":
                item.update(
                    status="draft", parse_status="pending", chunk_status="pending",
                    index_status="pending", parsed_hash=None, content_hash=None,
                    parent_count=None, child_count=None, indexed_count=0,
                    parser="pending", parser_version="pending", pages=None, text_chars=None,
                    text_page_ratio=None, needs_ocr=False, images=[], image_count=0,
                    ocr_image_count=0, ocr_no_text_count=0, parse_warnings=[],
                    ingestion_route="pending", review_type=None,
                )
                parsed = self._parsed_path(item)
                if parsed is not None:
                    parsed.unlink(missing_ok=True)
            elif item.get("chunk_status") == "failed":
                item.update(status="draft", chunk_status="pending", index_status="pending")
            else:
                item.update(status="draft", index_status="pending", indexed_count=0)
            item.update(
                last_error_code=None, processing_started_at=None,
                processing_completed_at=None, updated_at=self._now(),
            )
            self._write_manifest(manifest)
            return dict(item)

    def list_pdf_processing_candidates(self) -> list[str]:
        """Return only unfinished jobs; terminal failures require an explicit retry."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
        return [str(item["document_id"]) for item in manifest["documents"]
                if item.get("content_type") == "application/pdf"
                and item.get("status") not in {"revoked", "revoke_pending", "review_required"}
                and "failed" not in {
                    item.get("parse_status"), item.get("chunk_status"), item.get("index_status"),
                }
                and (item.get("parse_status") in {"pending", "running"}
                     or item.get("chunk_status") in {"pending", "running"}
                     or item.get("index_status") in {"pending", "running"})]

    @staticmethod
    def _select_document_entry(manifest: dict, document_id: str, *,
                               status: str | None = None) -> dict | None:
        """Prefer the newest active row when legacy history reused a document ID."""
        matches = [entry for entry in manifest["documents"]
                   if entry.get("document_id") == document_id]
        if status is not None:
            matches = [entry for entry in matches if entry.get("status") == status]
        elif any(entry.get("status") != "revoked" for entry in matches):
            matches = [entry for entry in matches if entry.get("status") != "revoked"]
        if not matches:
            return None
        return max(matches, key=lambda entry: str(entry.get("updated_at") or ""))

    def import_bytes(self, filename: str, payload: bytes, *, classification: str = "internal",
                     content_type: str | None = None) -> dict:
        if classification not in {"internal", "public"}:
            raise ValueError("invalid_knowledge_classification")
        name = safe_upload_name(filename)
        suffix = Path(name).suffix.lower()
        if suffix == ".pdf":
            return self._import_pdf(name, payload, classification=classification, content_type=content_type)
        if suffix in SUPPORTED_IMAGE_TYPES:
            return self._import_image(name, payload, classification=classification, content_type=content_type)
        return self._import_text(name, payload, classification=classification)

    def _recognize_images(self, rows: list[dict], source_hash: str) -> tuple[ImageArtifact, ...]:
        images: list[ImageArtifact] = []
        for image_index, row in enumerate(rows, 1):
            try:
                output = self.ocr_provider.recognize(row["payload"])
            except OcrProcessingError:
                output = OcrOutput("", None, "failed")
            images.append(make_image_artifact(
                document_source_hash=source_hash, image_index=image_index,
                source_kind=row["source_kind"], page_number=row.get("page_number"),
                page_image_index=int(row["page_image_index"]), payload=row["payload"],
                mime_type=row["mime_type"], width=int(row["width"]), height=int(row["height"]),
                output=output,
            ))
        return tuple(images)

    def _augment_pdf_with_ocr(self, payload: bytes, result: ParseResult,
                              source_hash: str) -> ParseResult:
        if not self.ocr_config.enabled:
            return result
        rows = extract_pdf_images(payload, scanned_pages=set(result.blank_pages),
                                  config=self.ocr_config)
        images = self._recognize_images(rows, source_hash)
        if not images:
            return result
        if (result.needs_ocr and self.ocr_config.enabled
                and all(image.ocr_status == "failed" for image in images)):
            raise OcrProcessingError("ocr_recognition_failed")
        warnings = list(result.warnings)
        if any(image.ocr_status == "low_confidence" for image in images):
            warnings.append(PdfWarningCode.OCR_LOW_CONFIDENCE)
        if any(image.ocr_status == "no_text" for image in images):
            warnings.append(PdfWarningCode.OCR_IMAGE_NO_TEXT)
        covered_pages = {image.page_number for image in images
                         if image.source_kind == ImageSourceKind.PDF_PAGE
                         and image.ocr_status in {"ready", "low_confidence"}
                         and image.ocr_text.strip()}
        unresolved = set(result.blank_pages) - covered_pages
        ocr_chars = sum(len(image.ocr_text) for image in images)
        if result.needs_ocr and not unresolved and ocr_chars:
            warnings = [warning for warning in warnings if warning not in {
                PdfWarningCode.EMPTY_PAGE, PdfWarningCode.PARTIAL_TEXT_COVERAGE,
            }]
            return replace(
                result, needs_ocr=False, status=ProcessingStatus.READY,
                warnings=tuple(dict.fromkeys(warnings)), images=images,
                text_chars=result.text_chars + ocr_chars, text_page_ratio=1.0,
                blank_pages=(),
            )
        return replace(result, warnings=tuple(dict.fromkeys(warnings)), images=images)

    def _import_image(self, name: str, payload: bytes, *, classification: str,
                      content_type: str | None) -> dict:
        suffix = Path(name).suffix.lower()
        expected_type = SUPPORTED_IMAGE_TYPES[suffix]
        supplied_type = (content_type or expected_type).split(";", 1)[0].strip().lower()
        if supplied_type != expected_type:
            raise ValueError("image_signature_mismatch")
        width, height, _, mime_type = validate_image_bytes(payload, supplied_type, self.ocr_config)
        source_hash = hashlib.sha256(payload).hexdigest()
        image = self._recognize_images([{
            "source_kind": ImageSourceKind.STANDALONE, "page_number": None,
            "page_image_index": 1, "payload": payload, "mime_type": mime_type,
            "width": width, "height": height,
        }], source_hash)[0]
        if image.ocr_status == "failed":
            raise RuntimeError("ocr_recognition_failed")
        with self._lock:
            manifest = self._read_upgraded_manifest()
            existing, matches = self._find_duplicate(manifest, source_hash)
            if existing:
                return {**existing, "duplicate": True}
            generation = len(matches) + 1
            document_id = f"web-{source_hash[:24]}" + (f"-v{generation}" if generation > 1 else "")
            generation_suffix = f"-v{generation}" if generation > 1 else ""
            stored_name = f"{self._safe_stem(name)[:60]}-{source_hash[:12]}{generation_suffix}{suffix}"
            ocr_text = image.ocr_text.strip()
            content_hash = hashlib.sha256(ocr_text.encode("utf-8")).hexdigest()
            now = self._now()
            low_confidence = image.ocr_status == "low_confidence"
            no_text = image.ocr_status == "no_text" or not ocr_text
            item = {
                "schema_version": MANIFEST_SCHEMA, "document_id": document_id, "version": 1,
                "title": Path(name).stem, "original_name": name, "stored_name": stored_name,
                "source_stored_name": stored_name, "parsed_stored_name": None, "parsed_hash": None,
                "source_hash": source_hash, "content_hash": content_hash, "content_type": mime_type,
                "business_unit_id": "default", "allowed_roles": [], "classification": classification,
                "status": "review_required" if low_confidence else "published",
                "size_bytes": len(payload), "parse_status": "ready",
                "chunk_status": "ready" if no_text else "pending",
                "index_status": "indexed" if no_text else "pending", "indexed_count": 0,
                "parent_count": 0 if no_text else None, "child_count": 0 if no_text else None,
                "splitter_version": "parent-child-v1",
                "parser": "paddleocr", "parser_version": self.ocr_provider.model_id,
                "pages": 1, "text_chars": len(ocr_text), "text_page_ratio": 1.0,
                "needs_ocr": False,
                "parse_warnings": ([PdfWarningCode.OCR_LOW_CONFIDENCE.value] if low_confidence
                                   else [PdfWarningCode.OCR_IMAGE_NO_TEXT.value] if no_text else []),
                "ingestion_route": "review_required" if low_confidence else "index",
                "image_count": 1, "ocr_image_count": int(bool(ocr_text)),
                "ocr_no_text_count": int(no_text),
                "images": [_image_public_payload(image)], "index_generation": None,
                "embedding_model_id": None, "created_at": now, "updated_at": now,
                "deleted_at": None, "last_error_code": None,
                "review_type": "ocr_low_confidence" if low_confidence else None,
                "review_approved_at": None,
            }
            target = self._write_bytes_atomic(self.documents_dir, stored_name, payload)
            try:
                if not no_text and not low_confidence:
                    document = self._load_entry_document(item)
                    parents, children = self.splitter.split(document)
                    item.update(parent_count=len(parents), child_count=len(children), chunk_status="ready")
                manifest["documents"].append(item); self._write_manifest(manifest)
            except Exception:
                target.unlink(missing_ok=True); raise
            return {**item, "duplicate": False}

    def _import_text(self, filename: str, payload: bytes, *, classification: str) -> dict:
        name, text, content_type = decode_text_document(
            filename, payload, max_bytes=self.max_text_bytes
        )
        source_hash = hashlib.sha256(payload).hexdigest()
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        suffix = Path(name).suffix.lower()
        safe_stem = self._safe_stem(name)
        with self._lock:
            manifest = self._read_upgraded_manifest()
            existing, matches = self._find_duplicate(manifest, source_hash)
            if existing:
                return {**existing, "duplicate": True}
            generation = len(matches) + 1
            document_id = f"web-{source_hash[:24]}" + (f"-v{generation}" if generation > 1 else "")
            generation_suffix = f"-v{generation}" if generation > 1 else ""
            stored_name = f"{safe_stem[:60]}-{source_hash[:12]}{generation_suffix}{suffix}"
            target = self._write_bytes_atomic(self.documents_dir, stored_name, payload)
            now = self._now()
            entry = {
                "schema_version": MANIFEST_SCHEMA,
                "document_id": document_id,
                "version": 1,
                "title": Path(name).stem,
                "original_name": name,
                "stored_name": stored_name,
                "source_stored_name": stored_name,
                "parsed_stored_name": None,
                "parsed_hash": None,
                "source_hash": source_hash,
                "content_hash": content_hash,
                "content_type": content_type,
                "business_unit_id": "default",
                "allowed_roles": [],
                "classification": classification,
                "status": "published",
                "size_bytes": len(payload),
                "parse_status": "ready",
                "chunk_status": "pending",
                "index_status": "pending",
                "indexed_count": None,
                "parent_count": None,
                "child_count": None,
                "splitter_version": "parent-child-v1",
                "parser": "stdlib",
                "parser_version": "stdlib-1",
                "pages": 1,
                "text_chars": len(text),
                "text_page_ratio": 1.0,
                "needs_ocr": False,
                "parse_warnings": [],
                "ingestion_route": "index",
                "index_generation": None,
                "embedding_model_id": None,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
                "last_error_code": None,
            }
            try:
                document = self._load_entry_document(entry)
                parents, children = self.splitter.split(document)
                entry.update(parent_count=len(parents), child_count=len(children),
                             chunk_status="ready", index_status="ready")
                manifest["documents"].append(entry)
                self._write_manifest(manifest)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return {**entry, "duplicate": False}

    def _import_pdf(self, name: str, payload: bytes, *, classification: str,
                    content_type: str | None) -> dict:
        source_hash = hashlib.sha256(payload).hexdigest()
        with self._lock:
            manifest = self._read_upgraded_manifest()
            existing, _ = self._find_duplicate(manifest, source_hash)
            if existing:
                return {**existing, "duplicate": True}

        result = parse_document_bytes(
            name, payload, content_type=content_type, config=self.pdf_config
        )
        result = self._augment_pdf_with_ocr(payload, result, source_hash)
        content_hash = hashlib.sha256(result.content.encode("utf-8")).hexdigest()
        safe_stem = self._safe_stem(name)
        with self._lock:
            manifest = self._read_upgraded_manifest()
            existing, matches = self._find_duplicate(manifest, source_hash)
            if existing:
                return {**existing, "duplicate": True}
            generation = len(matches) + 1
            document_id = f"web-{source_hash[:24]}" + (f"-v{generation}" if generation > 1 else "")
            generation_suffix = f"-v{generation}" if generation > 1 else ""
            stored_name = f"{safe_stem[:60]}-{source_hash[:12]}{generation_suffix}.pdf"
            parsed_name = f"{safe_stem[:60]}-{source_hash[:12]}{generation_suffix}.parsed.json"
            artifact_payload = _parse_result_payload(
                result, source_hash=source_hash, content_hash=content_hash
            )
            artifact_bytes = (
                json.dumps(artifact_payload, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            parsed_hash = hashlib.sha256(artifact_bytes).hexdigest()
            source_target = parsed_target = None
            try:
                source_target = self._write_bytes_atomic(self.documents_dir, stored_name, payload)
                parsed_target = self._write_bytes_atomic(self.parsed_dir, parsed_name, artifact_bytes)
                now = self._now()
                published = result.route == PdfIngestionRoute.INDEX
                entry = {
                    "schema_version": MANIFEST_SCHEMA,
                    "document_id": document_id,
                    "version": 1,
                    "title": Path(name).stem,
                    "original_name": name,
                    "stored_name": stored_name,
                    "source_stored_name": stored_name,
                    "parsed_stored_name": parsed_name,
                    "parsed_hash": parsed_hash,
                    "source_hash": source_hash,
                    "content_hash": content_hash,
                    "content_type": "application/pdf",
                    "business_unit_id": "default",
                    "allowed_roles": [],
                    "classification": classification,
                    "status": "published" if published else "review_required",
                    "size_bytes": len(payload),
                    "parse_status": result.status.value,
                    "chunk_status": "pending",
                    "index_status": "pending",
                    "indexed_count": None,
                    "parent_count": None,
                    "child_count": None,
                    "splitter_version": "pdf-aware-pending",
                    "parser": result.parser,
                    "parser_version": result.parser_version,
                    "pages": result.pages,
                    "text_chars": result.text_chars,
                    "text_page_ratio": result.text_page_ratio,
                    "needs_ocr": result.needs_ocr,
                    "image_count": len(result.images),
                    "ocr_image_count": sum(bool(image.ocr_text) for image in result.images),
                    "ocr_no_text_count": sum(image.ocr_status == "no_text" for image in result.images),
                    "images": [_image_public_payload(image) for image in result.images],
                    "parse_warnings": [warning.value for warning in result.warnings],
                    "ingestion_route": result.route.value,
                    "review_type": (
                        "ocr_low_confidence"
                        if PdfWarningCode.OCR_LOW_CONFIDENCE in result.warnings
                        else "complex_layout"
                        if PdfWarningCode.COMPLEX_LAYOUT_REVIEW in result.warnings
                        else None
                    ),
                    "review_approved_at": None,
                    "index_generation": None,
                    "embedding_model_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "deleted_at": None,
                    "last_error_code": PdfErrorCode.NEEDS_OCR.value if result.needs_ocr else None,
                }
                if result.indexable:
                    document = self._load_pdf_document(entry, result)
                    parents, children = self.pdf_splitter.split(document, result)
                    entry.update(
                        chunk_status="ready",
                        index_status="pending",
                        indexed_count=0,
                        parent_count=len(parents),
                        child_count=len(children),
                        splitter_version=self.pdf_splitter.version,
                    )
                manifest["documents"].append(entry)
                self._write_manifest(manifest)
            except Exception:
                if source_target is not None:
                    source_target.unlink(missing_ok=True)
                if parsed_target is not None:
                    parsed_target.unlink(missing_ok=True)
                raise
            return {**entry, "duplicate": False}

    def preview_images(self, document_id: str, *, limit: int = 100) -> dict:
        item = self.get_document(document_id)
        images = list(item.get("images") or [])
        cap = max(1, min(int(limit), 300))
        return {
            "document_id": document_id,
            "image_count": len(images),
            "ocr_image_count": sum(bool(image.get("ocr_text")) for image in images),
            "items": images[:cap],
        }

    def load_parsed_result(self, document_id: str) -> ParseResult:
        with self._lock:
            manifest = self._read_upgraded_manifest()
            selected = self._select_document_entry(manifest, document_id)
            item = dict(selected) if selected is not None else None
        if item is None:
            raise KeyError("knowledge_document_not_found")
        if item.get("content_type") != "application/pdf":
            raise ValueError("knowledge_document_is_not_pdf")
        source = self._source_path(item)
        parsed = self._parsed_path(item)
        if not source.is_file() or parsed is None or not parsed.is_file():
            raise RuntimeError("knowledge_parsed_artifact_missing")
        try:
            source_bytes = source.read_bytes()
            artifact_bytes = parsed.read_bytes()
            if hashlib.sha256(source_bytes).hexdigest() != item.get("source_hash"):
                raise ValueError("source hash mismatch")
            if hashlib.sha256(artifact_bytes).hexdigest() != item.get("parsed_hash"):
                raise ValueError("parsed hash mismatch")
            payload = json.loads(artifact_bytes.decode("utf-8"))
            result = _parse_result_from_payload(payload)
            if payload.get("source_hash") != item.get("source_hash"):
                raise ValueError("artifact source hash mismatch")
            if payload.get("content_hash") != item.get("content_hash"):
                raise ValueError("artifact content hash mismatch")
            if hashlib.sha256(result.content.encode("utf-8")).hexdigest() != item.get("content_hash"):
                raise ValueError("content hash mismatch")
            return result
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("knowledge_parsed_artifact_invalid") from exc

    def load_pdf_chunks(self, document_id: str):
        """Rebuild deterministic M3 chunks from the authoritative parsed artifact."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
            selected = self._select_document_entry(manifest, document_id)
            item = dict(selected) if selected is not None else None
        if item is None:
            raise KeyError("knowledge_document_not_found")
        if item.get("content_type") != "application/pdf":
            raise ValueError("knowledge_document_is_not_pdf")
        if (item.get("status") not in {"draft", "published"}
                or item.get("ingestion_route") != "index"):
            raise ValueError("knowledge_pdf_not_chunkable")
        result = self.load_parsed_result(document_id)
        reviewed_complex_layout = _approved_review_safe(item)
        document = self._load_pdf_document(
            item,
            result,
            reviewed_complex_layout=reviewed_complex_layout,
        )
        parents, children = self.pdf_splitter.split(
            document, result, reviewed_complex_layout=reviewed_complex_layout
        )
        if (len(parents) != item.get("parent_count") or len(children) != item.get("child_count")
                or item.get("splitter_version") != self.pdf_splitter.version):
            raise RuntimeError("knowledge_chunk_integrity_failed")
        return document, parents, children

    def preview_pdf_chunks(self, document_id: str, *, limit: int = 20) -> dict:
        document, parents, children = self.load_pdf_chunks(document_id)
        cap = max(1, min(int(limit), 100))
        return {
            "document_id": document.document_id,
            "splitter_version": self.pdf_splitter.version,
            "parent_count": len(parents),
            "child_count": len(children),
            "parents": [self.pdf_splitter._preview_row(
                parent.parent_id, parent.location, parent.text, parent.metadata
            ) for parent in parents[:cap]],
            "children": [self.pdf_splitter._preview_row(
                child.child_id, child.location, child.text, child.metadata
            ) for child in children[:cap]],
        }

    def list_pdf_index_candidates(self) -> list[str]:
        with self._lock:
            manifest = self._read_upgraded_manifest()
        return [str(item["document_id"]) for item in manifest["documents"]
                if item.get("content_type") == "application/pdf"
                and item.get("status") == "published"
                and item.get("parse_status") == "ready"
                and item.get("chunk_status") == "ready"
                and item.get("ingestion_route") == "index"
                and item.get("index_status") in {"pending", "indexed"}]

    def approve_review(self, document_id: str) -> dict:
        """Approve either safe low-confidence OCR text or a safe complex layout."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if not _review_approval_eligible(item):
                raise ValueError("knowledge_review_not_approvable")
            review_type = _review_type(item)
            if item.get("content_type") == "application/pdf":
                result = self.load_parsed_result(document_id)
                document = self._load_pdf_document(item, result, reviewed_complex_layout=True)
                parents, children = self.pdf_splitter.split(
                    document, result, reviewed_complex_layout=True
                )
                splitter_version = self.pdf_splitter.version
            elif str(item.get("content_type", "")).startswith("image/"):
                approved_item = dict(item)
                approved_item.update(status="published", ingestion_route="index")
                document = self._load_entry_document(approved_item)
                parents, children = self.splitter.split(document)
                splitter_version = "parent-child-v1"
            else:
                raise ValueError("knowledge_review_not_approvable")
            if not parents or not children:
                raise RuntimeError("knowledge_chunks_empty")
            item.update(
                status="published",
                ingestion_route="index",
                chunk_status="ready",
                index_status="pending",
                parent_count=len(parents),
                child_count=len(children),
                indexed_count=0,
                splitter_version=splitter_version,
                review_type=review_type,
                review_approved_at=self._now(),
                last_error_code=None,
                updated_at=self._now(),
            )
            self._write_manifest(manifest)
            return dict(item)

    def approve_pdf_review(self, document_id: str) -> dict:
        """Backward-compatible alias for the unified review operation."""
        return self.approve_review(document_id)

    def index_pdf(self, document_id: str, *, index_prepared) -> dict:
        """Publish one prepared PDF generation and persist actual backend counts."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if (item.get("content_type") != "application/pdf"
                    or item.get("status") not in {"draft", "published"}
                    or item.get("parse_status") != "ready"
                    or item.get("chunk_status") != "ready"
                    or item.get("ingestion_route") != "index"):
                raise ValueError("knowledge_pdf_not_indexable")
            item.update(index_status="running", last_error_code=None, updated_at=self._now())
            self._write_manifest(manifest)
        try:
            document, parents, children = self.load_pdf_chunks(document_id)
            result = index_prepared(document, parents, children)
            if (int(result.get("parents", -1)) != len(parents)
                    or int(result.get("semantic_indexed", -1)) != len(children)
                    or int(result.get("keyword_indexed", -1)) != len(children)
                    or int(result.get("indexed_count", -1)) != len(children)):
                raise RuntimeError("knowledge_index_count_mismatch")
        except Exception:
            with self._lock:
                manifest = self._read_upgraded_manifest()
                item = self._select_document_entry(manifest, document_id)
                if item is None:
                    raise KeyError("knowledge_document_not_found")
                if item.get("status") not in {"revoked", "revoke_pending"}:
                    item.update(index_status="failed", indexed_count=0,
                                last_error_code=PdfErrorCode.INDEX_FAILED.value,
                                processing_completed_at=self._now(), updated_at=self._now())
                self._write_manifest(manifest)
            raise
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("status") in {"revoked", "revoke_pending"}:
                return dict(item)
            item.update(
                status="published",
                index_status="indexed",
                indexed_count=len(children),
                parent_count=len(parents),
                child_count=len(children),
                index_generation=int(item.get("index_generation") or 0) + 1,
                embedding_model_id=result.get("model_id"),
                last_error_code=None,
                processing_completed_at=self._now(),
                updated_at=self._now(),
            )
            self._write_manifest(manifest)
            return dict(item)

    def index_image(self, document_id: str, *, index_document) -> dict:
        """Publish a standalone image's OCR text and persist actual vector counts."""
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if (not str(item.get("content_type", "")).startswith("image/")
                    or item.get("status") != "published"
                    or item.get("parse_status") != "ready"
                    or item.get("chunk_status") != "ready"):
                raise ValueError("knowledge_image_not_indexable")
            item.update(index_status="running", last_error_code=None, updated_at=self._now())
            self._write_manifest(manifest)
        try:
            document = self._load_entry_document(item)
            result = index_document(document)
            expected = int(item.get("child_count") or 0)
            if (int(result.get("semantic_indexed", -1)) != expected
                    or int(result.get("keyword_indexed", -1)) != expected):
                raise RuntimeError("knowledge_index_count_mismatch")
        except Exception:
            with self._lock:
                manifest = self._read_upgraded_manifest()
                item = self._select_document_entry(manifest, document_id)
                item.update(index_status="failed", indexed_count=0,
                            last_error_code=PdfErrorCode.INDEX_FAILED.value,
                            updated_at=self._now())
                self._write_manifest(manifest)
            raise
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            item.update(index_status="indexed", indexed_count=int(result["indexed_count"]),
                        index_generation=int(item.get("index_generation") or 0) + 1,
                        embedding_model_id=result.get("model_id"), last_error_code=None,
                        updated_at=self._now())
            self._write_manifest(manifest)
            return dict(item)

    def set_classification(self, document_id: str, classification: str) -> dict:
        if classification not in {"internal", "public"}:
            raise ValueError("invalid_knowledge_classification")
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("status") == "revoked":
                raise ValueError("revoked_knowledge_cannot_be_published")
            item["classification"] = classification
            item["updated_at"] = self._now()
            self._write_manifest(manifest)
            return dict(item)

    def load_published(self) -> list[CanonicalDocument]:
        with self._lock:
            manifest = self._read_upgraded_manifest()
        documents = []
        for item in manifest["documents"]:
            if (item.get("status") != "published" or item.get("parse_status") != "ready"
                    or item.get("chunk_status") != "ready"
                    or item.get("index_status") not in {"ready", "pending", "indexed"}
                    or int(item.get("child_count") or 0) == 0):
                continue
            if item.get("content_type") == "application/pdf":
                # M3 only prepares deterministic chunks; M4 owns real index publication.
                continue
            try:
                documents.append(self._load_entry_document(item))
            except Exception:
                continue
        return documents

    def list_documents(self, *, search: str = "", status: str = "", offset: int = 0,
                       limit: int = 50, include_deleted: bool = False) -> tuple[list[dict], int, dict]:
        with self._lock:
            manifest = self._read_upgraded_manifest()
        query = search.strip().casefold()
        items = [dict(item) for item in manifest["documents"] if
                 (include_deleted or item.get("status") != "revoked") and
                 (not query or query in str(item.get("original_name", "")).casefold()) and
                 (not status or item.get("status") == status)]
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        summary = {
            "total_documents": len(items),
            "published_documents": sum(item.get("status") == "published" for item in items),
            "parent_count": sum(int(item.get("parent_count") or 0) for item in items),
            "child_count": sum(int(item.get("child_count") or 0) for item in items),
            "indexed_count": sum(int(item.get("indexed_count") or 0) for item in items),
            "index_error_count": sum(item.get("index_status") == "failed" for item in items),
            "parse_error_count": sum(item.get("parse_status") == "failed" for item in items),
            "needs_ocr_count": sum(bool(item.get("needs_ocr")) for item in items),
            "image_count": sum(int(item.get("image_count") or 0) for item in items),
            "ocr_image_count": sum(int(item.get("ocr_image_count") or 0) for item in items),
        }
        return items[offset:offset + limit], len(items), summary

    def get_document(self, document_id: str) -> dict:
        with self._lock:
            manifest = self._read_upgraded_manifest()
        selected = self._select_document_entry(manifest, document_id)
        item = dict(selected) if selected is not None else None
        if item is None:
            raise KeyError("knowledge_document_not_found")
        return public_document_record(item)

    def _move_artifacts_to_trash(self, item: dict) -> None:
        paths = [self._source_path(item)]
        parsed = self._parsed_path(item)
        if parsed is not None:
            paths.append(parsed)
        for source in paths:
            if not source.is_file():
                continue
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            target = self.trash_dir / source.name
            if target.exists():
                target = self.trash_dir / f"{item['document_id']}-{source.name}"
            shutil.move(str(source), str(target))

    def revoke(self, document_id: str, *, delete_index) -> dict:
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("status") == "revoked":
                return dict(item)
            item.update(status="revoke_pending", index_status="deleting", updated_at=self._now())
            self._write_manifest(manifest)
            try:
                delete_index(document_id, int(item.get("version", 1)))
                self._move_artifacts_to_trash(item)
                item.update(status="revoked", index_status="withdrawn", deleted_at=self._now(),
                            last_error_code=None)
            except Exception:
                item.update(status="revoke_pending", index_status="failed",
                            last_error_code="knowledge_index_delete_failed")
            item["updated_at"] = self._now()
            self._write_manifest(manifest)
            return dict(item)

    def retry_index(self, document_id: str, *, index_document) -> dict:
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(manifest, document_id)
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("index_status") != "failed" or item.get("status") != "published":
                raise ValueError("knowledge_retry_not_allowed")
            if item.get("content_type") == "application/pdf":
                pass
            elif str(item.get("content_type", "")).startswith("image/"):
                pass
            else:
                document = self._load_entry_document(item)
                index_document(document)
                parents, children = self.splitter.split(document)
                item.update(index_status="ready", parent_count=len(parents), child_count=len(children),
                            chunk_status="ready", last_error_code=None, updated_at=self._now())
                self._write_manifest(manifest)
                return dict(item)
        if item.get("content_type") == "application/pdf":
            return self.index_pdf(document_id, index_prepared=index_document)
        return self.index_image(document_id, index_document=index_document)

    def retry_revoke(self, document_id: str, *, delete_index) -> dict:
        with self._lock:
            manifest = self._read_upgraded_manifest()
            item = self._select_document_entry(
                manifest, document_id, status="revoke_pending"
            )
            if item is None:
                raise KeyError("knowledge_document_not_found")
            if item.get("status") != "revoke_pending":
                raise ValueError("knowledge_retry_not_allowed")
            try:
                delete_index(document_id, int(item.get("version", 1)))
                self._move_artifacts_to_trash(item)
                item.update(status="revoked", index_status="withdrawn", deleted_at=self._now(),
                            last_error_code=None, updated_at=self._now())
            except Exception:
                item.update(index_status="failed", last_error_code="knowledge_index_delete_failed",
                            updated_at=self._now())
            self._write_manifest(manifest)
            return dict(item)
