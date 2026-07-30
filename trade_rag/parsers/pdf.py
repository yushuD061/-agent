"""Safe, page-aware PDF parsing for the M1 knowledge-ingestion stage."""

from __future__ import annotations

import math
import multiprocessing
import re
import threading
import time
from collections import Counter
from io import BytesIO
from pathlib import Path

import pdfplumber
import pypdf
from pypdf import PdfReader

from ..config import PdfIngestionConfig, load_pdf_ingestion_config
from ..contracts import (
    ParseResult,
    ParsedBlock,
    ParsedBlockType,
    PdfErrorCode,
    PdfWarningCode,
    ProcessingStatus,
    SourceLocation,
)

PDF_MAGIC = b"%PDF-"
PDF_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}
GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
_HYPHENATED_LINE_BREAK = re.compile(r"(?<=[A-Za-z])-[ \t]*\n[ \t]*(?=[A-Za-z])")
_LIST_LINE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_SEMAPHORE_LOCK = threading.Lock()


class PdfProcessingError(ValueError):
    """Stable, body-free error returned by the PDF parsing boundary."""

    def __init__(self, code: PdfErrorCode):
        self.code = code
        super().__init__(code.value)


def _semaphore(limit: int) -> threading.BoundedSemaphore:
    with _SEMAPHORE_LOCK:
        return _SEMAPHORES.setdefault(limit, threading.BoundedSemaphore(limit))


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise PdfProcessingError(PdfErrorCode.PARSE_TIMEOUT)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "").replace("\u00ad", "")
    text = _HYPHENATED_LINE_BREAK.sub("", text)
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _repeated_boundary(pages: list[str], *, first: bool) -> str | None:
    if len(pages) < 3:
        return None
    values = []
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        values.append((lines[0] if first else lines[-1]) if lines else "")
    threshold = max(3, math.ceil(len(pages) * 0.6))
    value, count = Counter(value for value in values if value).most_common(1)[0] if any(values) else ("", 0)
    return value if count >= threshold and len(value) <= 160 else None


def _strip_repeated_boundaries(pages: list[str]) -> tuple[list[str], list[PdfWarningCode]]:
    header = _repeated_boundary(pages, first=True)
    footer = _repeated_boundary(pages, first=False)
    warnings: list[PdfWarningCode] = []
    if header:
        warnings.append(PdfWarningCode.REPEATED_HEADER_REMOVED)
    if footer and footer != header:
        warnings.append(PdfWarningCode.REPEATED_FOOTER_REMOVED)
    output = []
    for page in pages:
        lines = page.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if header and lines and lines[0].strip() == header:
            lines.pop(0)
        if footer and footer != header and lines and lines[-1].strip() == footer:
            lines.pop()
        output.append(_normalize_text("\n".join(lines)))
    return output, warnings


def _looks_complex_layout(page) -> bool:
    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    except Exception:
        return False
    by_row: dict[int, list[float]] = {}
    for word in words:
        by_row.setdefault(round(float(word.get("top", 0)) / 8), []).append(float(word.get("x0", 0)))
    split_left = float(page.width) * 0.42
    split_right = float(page.width) * 0.52
    column_rows = sum(
        1 for positions in by_row.values()
        if any(x < split_left for x in positions)
        and any(x > split_right for x in positions)
        and not any(split_left <= x <= split_right for x in positions)
    )
    return column_rows >= 2


def _extract_with_pdfplumber(payload: bytes, deadline: float) -> tuple[list[str], bool]:
    pages: list[str] = []
    complex_layout = False
    with pdfplumber.open(BytesIO(payload)) as document:
        for page in document.pages:
            _check_deadline(deadline)
            pages.append(page.extract_text(layout=True) or "")
            complex_layout = _looks_complex_layout(page) or complex_layout
    return pages, complex_layout


def _extract_with_pypdf(reader: PdfReader, deadline: float) -> list[str]:
    pages = []
    for page in reader.pages:
        _check_deadline(deadline)
        pages.append(page.extract_text() or "")
    return pages


def _max_consecutive_blank(pages: list[str]) -> int:
    longest = current = 0
    for page in pages:
        current = 0 if page.strip() else current + 1
        longest = max(longest, current)
    return longest


def _blocks_from_pages(pages: list[str]) -> tuple[ParsedBlock, ...]:
    blocks: list[ParsedBlock] = []
    current_section: tuple[str, ...] = ()
    for page_number, page in enumerate(pages, 1):
        for fragment in re.split(r"\n\s*\n", page):
            text = fragment.strip()
            if not text:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if any("|" in line for line in lines):
                block_type = ParsedBlockType.TABLE
            elif all(_LIST_LINE.match(line) for line in lines):
                block_type = ParsedBlockType.LIST
            elif len(lines) == 1 and len(lines[0]) <= 80 and not re.search(r"[。！？.!?]$", lines[0]):
                block_type = ParsedBlockType.HEADING
                current_section = (lines[0],)
            else:
                block_type = ParsedBlockType.PARAGRAPH
            location = SourceLocation(page_number, page_number, current_section, block_type)
            blocks.append(ParsedBlock(text, location, len(blocks), {"page": page_number}))
    return tuple(blocks)


def _validate_pdf_identity(filename: str, payload: bytes, content_type: str | None) -> None:
    suffix = Path(filename.replace("\\", "/")).suffix.lower()
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    valid_type = media_type in PDF_CONTENT_TYPES or media_type in GENERIC_CONTENT_TYPES
    if suffix != ".pdf" or not valid_type or not payload.startswith(PDF_MAGIC):
        raise PdfProcessingError(PdfErrorCode.SIGNATURE_MISMATCH)


def _open_reader(payload: bytes) -> PdfReader:
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise PdfProcessingError(PdfErrorCode.ENCRYPTED)
        return reader
    except PdfProcessingError:
        raise
    except Exception as exc:
        raise PdfProcessingError(PdfErrorCode.PARSE_FAILED) from exc


def _parse_pdf_bytes_in_process(
    filename: str,
    payload: bytes,
    *,
    content_type: str | None = None,
    config: PdfIngestionConfig | None = None,
) -> ParseResult:
    """Parse one PDF without persistence, indexing, OCR or external network calls."""
    config = config or load_pdf_ingestion_config()
    if len(payload) > config.max_bytes:
        raise PdfProcessingError(PdfErrorCode.FILE_TOO_LARGE)
    _validate_pdf_identity(filename, payload, content_type)
    deadline = time.monotonic() + config.parse_timeout_seconds
    gate = _semaphore(config.max_concurrent_parses)
    if not gate.acquire(timeout=config.parse_timeout_seconds):
        raise PdfProcessingError(PdfErrorCode.PARSE_TIMEOUT)
    try:
        _check_deadline(deadline)
        reader = _open_reader(payload)
        try:
            page_count = len(reader.pages)
        except Exception as exc:
            raise PdfProcessingError(PdfErrorCode.PARSE_FAILED) from exc
        if page_count < 1:
            raise PdfProcessingError(PdfErrorCode.PARSE_FAILED)
        if page_count > config.max_pages:
            raise PdfProcessingError(PdfErrorCode.PAGE_LIMIT_EXCEEDED)
        _check_deadline(deadline)

        warnings: list[PdfWarningCode] = []
        parser = "pdfplumber"
        parser_version = f"pdfplumber-{pdfplumber.__version__}+pdf-v1"
        try:
            raw_pages, complex_layout = _extract_with_pdfplumber(payload, deadline)
        except PdfProcessingError:
            raise
        except Exception:
            raw_pages = _extract_with_pypdf(reader, deadline)
            complex_layout = False
            parser = "pypdf"
            parser_version = f"pypdf-{pypdf.__version__}+pdf-v1"
            warnings.append(PdfWarningCode.FALLBACK_PARSER_USED)

        normalized = [_normalize_text(page) for page in raw_pages]
        primary_chars = sum(len(page) for page in normalized)
        primary_ratio = sum(bool(page) for page in normalized) / page_count
        if parser == "pdfplumber" and (
            len(normalized) != page_count
            or primary_chars == 0
            or primary_ratio < config.min_text_page_ratio
        ):
            try:
                fallback = [_normalize_text(page) for page in _extract_with_pypdf(reader, deadline)]
                if sum(len(page) for page in fallback) > primary_chars:
                    normalized = fallback
                    parser = "pypdf"
                    parser_version = f"pypdf-{pypdf.__version__}+pdf-v1"
                    warnings.append(PdfWarningCode.FALLBACK_PARSER_USED)
            except PdfProcessingError:
                raise
            except Exception:
                pass

        if len(normalized) != page_count:
            raise PdfProcessingError(PdfErrorCode.PARSE_FAILED)
        raw_page_char_counts = tuple(len(page) for page in normalized)
        if any(count > config.max_chars_per_page for count in raw_page_char_counts):
            raise PdfProcessingError(PdfErrorCode.TEXT_LIMIT_EXCEEDED)
        if sum(raw_page_char_counts) > config.max_total_chars:
            raise PdfProcessingError(PdfErrorCode.TEXT_LIMIT_EXCEEDED)

        normalized, boundary_warnings = _strip_repeated_boundaries(normalized)
        warnings.extend(boundary_warnings)
        page_char_counts = tuple(len(page) for page in normalized)
        if any(count > config.max_chars_per_page for count in page_char_counts):
            raise PdfProcessingError(PdfErrorCode.TEXT_LIMIT_EXCEEDED)
        text_chars = sum(page_char_counts)
        if text_chars > config.max_total_chars:
            raise PdfProcessingError(PdfErrorCode.TEXT_LIMIT_EXCEEDED)
        blank_pages = tuple(index for index, count in enumerate(page_char_counts, 1) if count == 0)
        text_pages = page_count - len(blank_pages)
        text_page_ratio = text_pages / page_count
        average_chars = text_chars / text_pages if text_pages else 0
        if blank_pages:
            warnings.append(PdfWarningCode.EMPTY_PAGE)
        if 0 < text_page_ratio < 1:
            warnings.append(PdfWarningCode.PARTIAL_TEXT_COVERAGE)
        if complex_layout:
            warnings.append(PdfWarningCode.COMPLEX_LAYOUT_REVIEW)
        needs_ocr = (
            text_pages == 0
            or _max_consecutive_blank(normalized) > config.max_consecutive_blank_pages
            or (text_page_ratio < config.min_text_page_ratio
                and average_chars < config.min_average_chars_per_text_page)
        )
        status = ProcessingStatus.NEEDS_OCR if needs_ocr else ProcessingStatus.READY
        unique_warnings = tuple(dict.fromkeys(warnings))
        blocks = _blocks_from_pages(normalized)
        _check_deadline(deadline)
        return ParseResult(
            parser=parser,
            parser_version=parser_version,
            pages=page_count,
            text_chars=text_chars,
            text_page_ratio=text_page_ratio,
            needs_ocr=needs_ocr,
            status=status,
            warnings=unique_warnings,
            blocks=blocks,
            page_char_counts=page_char_counts,
            blank_pages=blank_pages,
        )
    finally:
        gate.release()


def _pdf_worker(send_connection, filename: str, payload: bytes,
                content_type: str | None, config: PdfIngestionConfig) -> None:
    try:
        result = _parse_pdf_bytes_in_process(
            filename, payload, content_type=content_type, config=config
        )
        send_connection.send(("ok", result))
    except PdfProcessingError as exc:
        send_connection.send(("error", exc.code.value))
    except Exception:
        send_connection.send(("error", PdfErrorCode.PARSE_FAILED.value))
    finally:
        send_connection.close()


def parse_pdf_bytes(
    filename: str,
    payload: bytes,
    *,
    content_type: str | None = None,
    config: PdfIngestionConfig | None = None,
) -> ParseResult:
    """Run parsing in a killable child process so the timeout is a hard boundary."""
    config = config or load_pdf_ingestion_config()
    if len(payload) > config.max_bytes:
        raise PdfProcessingError(PdfErrorCode.FILE_TOO_LARGE)
    _validate_pdf_identity(filename, payload, content_type)
    gate = _semaphore(config.max_concurrent_parses)
    if not gate.acquire(timeout=config.parse_timeout_seconds):
        raise PdfProcessingError(PdfErrorCode.PARSE_TIMEOUT)
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_worker,
        args=(send_connection, filename, payload, content_type, config),
        daemon=True,
    )
    started = False
    try:
        try:
            process.start()
            started = True
        except (OSError, RuntimeError) as exc:
            raise PdfProcessingError(PdfErrorCode.PARSE_FAILED) from exc
        send_connection.close()
        if not receive_connection.poll(config.parse_timeout_seconds):
            process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            raise PdfProcessingError(PdfErrorCode.PARSE_TIMEOUT)
        try:
            kind, value = receive_connection.recv()
        except (EOFError, OSError) as exc:
            raise PdfProcessingError(PdfErrorCode.PARSE_FAILED) from exc
        process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if kind == "ok" and isinstance(value, ParseResult):
            return value
        if kind == "error":
            try:
                code = PdfErrorCode(value)
            except ValueError as exc:
                raise PdfProcessingError(PdfErrorCode.PARSE_FAILED) from exc
            raise PdfProcessingError(code)
        raise PdfProcessingError(PdfErrorCode.PARSE_FAILED)
    finally:
        receive_connection.close()
        send_connection.close()
        if started and process.is_alive():
            process.terminate()
            process.join(timeout=1)
        gate.release()


def parse_document_bytes(
    filename: str,
    payload: bytes,
    *,
    content_type: str | None = None,
    config: PdfIngestionConfig | None = None,
) -> ParseResult:
    """M1 binary dispatcher. Text ingestion remains on the existing bounded text path."""
    return parse_pdf_bytes(filename, payload, content_type=content_type, config=config)
