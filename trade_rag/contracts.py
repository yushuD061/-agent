from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

def utcnow() -> datetime: return datetime.now(timezone.utc)

class DocumentStatus(str, Enum):
    DRAFT = "draft"; REVIEW_REQUIRED = "review_required"; APPROVED = "approved"; PUBLISHED = "published"; REVOKED = "revoked"; EXPIRED = "expired"

class ProcessingStatus(str, Enum):
    """Shared manifest status vocabulary for parse, chunk and index stages."""
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    NEEDS_OCR = "needs_ocr"

class PdfIngestionRoute(str, Enum):
    INDEX = "index"
    REVIEW_REQUIRED = "review_required"
    NEEDS_OCR = "needs_ocr"
    REJECT = "reject"

class ParsedBlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    IMAGE = "image"

class ImageSourceKind(str, Enum):
    PDF_PAGE = "pdf_page"
    PDF_EMBEDDED = "pdf_embedded"
    STANDALONE = "standalone"

class PdfErrorCode(str, Enum):
    SIGNATURE_MISMATCH = "pdf_signature_mismatch"
    ENCRYPTED = "pdf_encrypted"
    PAGE_LIMIT_EXCEEDED = "pdf_page_limit_exceeded"
    PARSE_TIMEOUT = "pdf_parse_timeout"
    PARSE_FAILED = "pdf_parse_failed"
    NEEDS_OCR = "pdf_needs_ocr"
    TEXT_LIMIT_EXCEEDED = "pdf_text_limit_exceeded"
    FILE_TOO_LARGE = "file_too_large"
    INDEX_FAILED = "knowledge_index_failed"

class PdfWarningCode(str, Enum):
    EMPTY_PAGE = "pdf_empty_page"
    PARTIAL_TEXT_COVERAGE = "pdf_partial_text_coverage"
    REPEATED_HEADER_REMOVED = "pdf_repeated_header_removed"
    REPEATED_FOOTER_REMOVED = "pdf_repeated_footer_removed"
    TABLE_FALLBACK = "pdf_table_fallback"
    FALLBACK_PARSER_USED = "pdf_fallback_parser_used"
    COMPLEX_LAYOUT_REVIEW = "pdf_complex_layout_review"
    OCR_LOW_CONFIDENCE = "ocr_low_confidence"
    OCR_IMAGE_NO_TEXT = "ocr_image_no_text"

@dataclass(frozen=True)
class SourceLocation:
    """One-based, inclusive source location retained through chunking and citations."""
    page_start: int
    page_end: int
    section_path: tuple[str, ...] = ()
    block_type: ParsedBlockType = ParsedBlockType.PARAGRAPH
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.page_start < 1:
            raise ValueError("page_start must be at least 1")
        if self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        if any(not str(part).strip() for part in self.section_path):
            raise ValueError("section_path entries must be non-empty")
        if not isinstance(self.block_type, ParsedBlockType):
            raise ValueError("block_type must be a ParsedBlockType")
        if self.bbox is not None:
            if len(self.bbox) != 4 or not all(isinstance(value, (int, float)) for value in self.bbox):
                raise ValueError("bbox must contain four numeric values")
            left, top, right, bottom = self.bbox
            if right < left or bottom < top:
                raise ValueError("bbox coordinates are invalid")

@dataclass(frozen=True)
class ParsedBlock:
    text: str
    location: SourceLocation
    ordinal: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("parsed block text must be non-empty")
        if self.ordinal < 0:
            raise ValueError("parsed block ordinal must not be negative")

@dataclass(frozen=True)
class ImageArtifact:
    """One stable, one-based image record retained through OCR and vector indexing."""
    image_id: str
    image_index: int
    source_kind: ImageSourceKind
    page_number: int | None
    page_image_index: int
    source_hash: str
    mime_type: str
    width: int
    height: int
    ocr_text: str = ""
    ocr_confidence: float | None = None
    ocr_status: str = "pending"

    def __post_init__(self) -> None:
        if not self.image_id.strip() or self.image_index < 1:
            raise ValueError("image_id and positive image_index are required")
        if not isinstance(self.source_kind, ImageSourceKind):
            raise ValueError("source_kind must be an ImageSourceKind")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be positive when present")
        if self.page_image_index < 1:
            raise ValueError("page_image_index must be positive")
        if not self.source_hash.strip() or not self.mime_type.startswith("image/"):
            raise ValueError("image source hash and image MIME type are required")
        if self.width < 1 or self.height < 1:
            raise ValueError("image dimensions must be positive")
        if self.ocr_confidence is not None and not 0.0 <= self.ocr_confidence <= 1.0:
            raise ValueError("ocr_confidence must be between 0 and 1")
        if self.ocr_status not in {"ready", "no_text", "low_confidence", "failed", "pending"}:
            raise ValueError("unsupported OCR image status")

@dataclass(frozen=True)
class ParseResult:
    parser: str
    parser_version: str
    pages: int
    text_chars: int
    text_page_ratio: float
    needs_ocr: bool
    status: ProcessingStatus
    warnings: tuple[PdfWarningCode, ...] = ()
    blocks: tuple[ParsedBlock, ...] = ()
    page_char_counts: tuple[int, ...] = ()
    blank_pages: tuple[int, ...] = ()
    images: tuple[ImageArtifact, ...] = ()

    def __post_init__(self) -> None:
        if not self.parser.strip() or not self.parser_version.strip():
            raise ValueError("parser and parser_version must be non-empty")
        if self.pages < 1:
            raise ValueError("pages must be at least 1")
        if self.text_chars < 0:
            raise ValueError("text_chars must not be negative")
        if not 0.0 <= self.text_page_ratio <= 1.0:
            raise ValueError("text_page_ratio must be between 0 and 1")
        if not isinstance(self.status, ProcessingStatus):
            raise ValueError("status must be a ProcessingStatus")
        if any(not isinstance(warning, PdfWarningCode) for warning in self.warnings):
            raise ValueError("warnings must contain PdfWarningCode values")
        if any(not isinstance(block, ParsedBlock) for block in self.blocks):
            raise ValueError("blocks must contain ParsedBlock values")
        if self.page_char_counts and len(self.page_char_counts) != self.pages:
            raise ValueError("page_char_counts must contain one value per page")
        if any(value < 0 for value in self.page_char_counts):
            raise ValueError("page_char_counts must not contain negative values")
        if any(page < 1 or page > self.pages for page in self.blank_pages):
            raise ValueError("blank_pages must contain valid one-based page numbers")
        if any(not isinstance(image, ImageArtifact) for image in self.images):
            raise ValueError("images must contain ImageArtifact values")
        indexes = [image.image_index for image in self.images]
        if indexes and indexes != list(range(1, len(indexes) + 1)):
            raise ValueError("image indexes must be contiguous and one-based")
        if len({image.image_id for image in self.images}) != len(self.images):
            raise ValueError("image IDs must be unique")
        if self.needs_ocr != (self.status == ProcessingStatus.NEEDS_OCR):
            raise ValueError("needs_ocr must match the needs_ocr processing status")
        image_text = sum(len(image.ocr_text.strip()) for image in self.images)
        if self.status == ProcessingStatus.READY and (self.text_chars + image_text == 0
                                                       or (not self.blocks and image_text == 0)):
            raise ValueError("ready parse results require extracted text blocks")

    @property
    def route(self) -> PdfIngestionRoute:
        if self.needs_ocr:
            return PdfIngestionRoute.NEEDS_OCR
        if any(warning in self.warnings for warning in (
            PdfWarningCode.PARTIAL_TEXT_COVERAGE,
            PdfWarningCode.COMPLEX_LAYOUT_REVIEW,
            PdfWarningCode.OCR_LOW_CONFIDENCE,
        )):
            return PdfIngestionRoute.REVIEW_REQUIRED
        return PdfIngestionRoute.INDEX

    @property
    def indexable(self) -> bool:
        return self.status == ProcessingStatus.READY and self.route == PdfIngestionRoute.INDEX

    @property
    def content(self) -> str:
        parts = [block.text for block in self.blocks]
        parts.extend(image.ocr_text for image in self.images if image.ocr_text.strip())
        return "\n\n".join(parts)

@dataclass(frozen=True)
class Actor:
    actor_id: str
    roles: frozenset[str] = frozenset()
    business_unit_id: str = "default"

@dataclass
class CanonicalDocument:
    document_id: str
    version: int
    source_uri: str
    title: str
    content: str
    content_hash: str
    content_type: str = "text/markdown"
    location: str = ""
    language: str = "und"
    business_unit_id: str = "default"
    allowed_roles: frozenset[str] = frozenset()
    classification: str = "internal"
    status: DocumentStatus = DocumentStatus.DRAFT
    expires_at: datetime | None = None
    parser_version: str = "stdlib-1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_searchable(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.status in {DocumentStatus.APPROVED, DocumentStatus.PUBLISHED} and (self.expires_at is None or self.expires_at > now)

@dataclass(frozen=True)
class ParentChunk:
    parent_id: str; document_id: str; text: str; location: str; content_hash: str; metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ChildChunk:
    child_id: str; parent_id: str; document_id: str; text: str; location: str; content_hash: str; metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class QueryRequest:
    query: str; actor: Actor; top_n: int = 30; top_k: int = 8; rerank: bool = True; history: tuple["HistoryMessage", ...] = ()

@dataclass(frozen=True)
class HistoryMessage:
    role: str
    content: str

@dataclass
class SearchResult:
    child: ChildChunk
    score: float
    source: CanonicalDocument
    rrf_score: float | None = None
    rerank_score: float | None = None
    retrieval_source: str = "semantic"
    parent: ParentChunk | None = None

@dataclass(frozen=True)
class Citation:
    document_id: str
    version: int
    location: str
    child_id: str
    image_id: str | None = None
    image_index: int | None = None
    page_number: int | None = None
