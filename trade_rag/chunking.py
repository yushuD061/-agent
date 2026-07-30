from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .contracts import (
    CanonicalDocument,
    ChildChunk,
    ParseResult,
    ParsedBlock,
    ParsedBlockType,
    ParentChunk,
    SourceLocation,
)


@dataclass(frozen=True)
class Chunk:
    id: int
    content: str


DEFAULT_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", " ", "")
_FENCE = re.compile(r"(?ms)^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)\s*$")
_HEADING = re.compile(r"^#{1,6} .+$")


class RecursiveSplitter:
    """按字符数切分，优先保留语义边界；不依赖第三方 tokenizer。"""

    def __init__(self, chunk_size: int = 200, chunk_overlap: int = 50, separators=None):
        self.chunk_size = max(1, int(chunk_size))
        overlap = max(0, int(chunk_overlap))
        self.chunk_overlap = min(overlap, self.chunk_size - 1)
        self.separators = tuple(separators) if separators is not None else DEFAULT_SEPARATORS
        if not self.separators or self.separators[-1] != "":
            self.separators = self.separators + ("",)

    def split(self, text: str | None) -> list[Chunk]:
        if not text or not text.strip():
            return []
        pieces = self._protected_pieces(text)
        raw: list[str] = []
        for piece, atomic in pieces:
            raw.extend([piece] if atomic else self._recursive(piece, 0))
        raw = self._merge_small(raw)
        raw = self._join_headings(raw)
        result: list[Chunk] = []
        for i, value in enumerate(raw):
            if i and self.chunk_overlap:
                value = raw[i - 1][-self.chunk_overlap :] + value
            result.append(Chunk(i, value))
        return result

    def _protected_pieces(self, text: str):
        cursor = 0
        for match in _FENCE.finditer(text):
            if match.start() > cursor:
                yield text[cursor : match.start()], False
            yield match.group(0), True
            cursor = match.end()
        if cursor < len(text):
            yield text[cursor:], False

    def _recursive(self, text: str, level: int) -> list[str]:
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []
        separator = self.separators[min(level, len(self.separators) - 1)]
        if separator == "":
            return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]
        if separator not in text:
            return self._recursive(text, level + 1)
        parts = text.split(separator)
        fragments = [parts[0]] + [separator + part for part in parts[1:]]
        out: list[str] = []
        for part in fragments:
            if len(part) <= self.chunk_size:
                if part.strip(): out.append(part)
            else:
                out.extend(self._recursive(part, level + 1))
        return out

    def _merge_small(self, parts: list[str]) -> list[str]:
        merged: list[str] = []
        for part in parts:
            if not part.strip():
                continue
            if merged and len(merged[-1]) + len(part) <= self.chunk_size:
                merged[-1] += part
            else:
                merged.append(part)
        return merged

    def _join_headings(self, parts: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(parts):
            current = parts[i].strip()
            if _HEADING.fullmatch(current) and i + 1 < len(parts):
                out.append(parts[i] + parts[i + 1]); i += 2
            else:
                out.append(parts[i]); i += 1
        return out


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


class ParentChildSplitter:
    def __init__(self, parent_chars: int = 800, child_chars: int = 200, overlap: int = 50):
        self.parent_splitter = RecursiveSplitter(parent_chars, overlap * 2)
        self.child_splitter = RecursiveSplitter(child_chars, overlap)

    def split(self, doc: CanonicalDocument) -> tuple[list[ParentChunk], list[ChildChunk]]:
        image_rows = {int(row["image_index"]): row for row in doc.metadata.get("images", ())}
        if image_rows:
            parents, children = [], []
            for image_index in sorted(image_rows):
                row = image_rows[image_index]
                text = str(row.get("ocr_text", "")).strip()
                if not text:
                    continue
                location = f"image:{image_index}"
                parent_id = _id(doc.document_id, str(doc.version), location, text)
                base = {
                    "business_unit_id": doc.business_unit_id,
                    "allowed_roles": tuple(doc.allowed_roles),
                    "classification": doc.classification,
                    "block_type": "image", "image_id": row["image_id"],
                    "image_index": image_index, "image_source_kind": row["source_kind"],
                    "page_number": row.get("page_number"),
                    "page_image_index": row["page_image_index"],
                    "ocr_confidence": row.get("ocr_confidence"),
                    "ocr_status": row["ocr_status"], "parent_id": parent_id,
                }
                parents.append(ParentChunk(parent_id, doc.document_id, text, location,
                                           _id(text), dict(base)))
                for part in self.child_splitter.split(text):
                    child_id = _id(parent_id, str(part.id), part.content)
                    children.append(ChildChunk(child_id, parent_id, doc.document_id,
                                               part.content, location, _id(part.content), dict(base)))
            return parents, children
        parents, children = [], []
        for pi, parent_chunk in enumerate(self.parent_splitter.split(doc.content)):
            pid = _id(doc.document_id, str(doc.version), str(pi), parent_chunk.content)
            parents.append(ParentChunk(pid, doc.document_id, parent_chunk.content, doc.location, _id(parent_chunk.content)))
            for child_chunk in self.child_splitter.split(parent_chunk.content):
                cid = _id(pid, str(child_chunk.id), child_chunk.content)
                children.append(ChildChunk(cid, pid, doc.document_id, child_chunk.content, doc.location, _id(child_chunk.content), {"business_unit_id": doc.business_unit_id, "allowed_roles": tuple(doc.allowed_roles)}))
        return parents, children


PDF_SPLITTER_VERSION = "pdf-aware-v1"
_PAGE_ARTIFACT = re.compile(
    r"^(?:page\s*)?\d+(?:\s*(?:/|of)\s*\d+)?$|^(?:copyright|©)\s*\d*\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _PdfUnit:
    text: str
    location: SourceLocation
    ordinal: int


def _full_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _location_label(location: SourceLocation) -> str:
    pages = (f"page:{location.page_start}" if location.page_start == location.page_end
             else f"pages:{location.page_start}-{location.page_end}")
    if not location.section_path:
        return pages
    return pages + "#" + " > ".join(location.section_path)


def _merge_location(units: list[_PdfUnit]) -> SourceLocation:
    first = units[0].location
    section = first.section_path
    if any(unit.location.section_path != section for unit in units):
        section = ()
    block_type = (first.block_type if all(unit.location.block_type == first.block_type for unit in units)
                  else ParsedBlockType.PARAGRAPH)
    return SourceLocation(
        min(unit.location.page_start for unit in units),
        max(unit.location.page_end for unit in units),
        section,
        block_type,
    )


class PdfAwareParentChildSplitter:
    """Deterministic structure-aware PDF splitter retaining exact page provenance."""

    version = PDF_SPLITTER_VERSION

    def __init__(self, parent_chars: int = 800, child_chars: int = 200, overlap: int = 50):
        self.parent_chars = max(100, int(parent_chars))
        self.child_chars = max(50, int(child_chars))
        self.overlap = min(max(0, int(overlap)), self.child_chars - 1)
        self.parent_splitter = RecursiveSplitter(self.parent_chars, 0)
        self.child_splitter = RecursiveSplitter(self.child_chars, self.overlap)

    def split(self, document: CanonicalDocument, parsed: ParseResult, *,
              reviewed_complex_layout: bool = False) -> tuple[list[ParentChunk], list[ChildChunk]]:
        if not parsed.indexable and not reviewed_complex_layout:
            raise ValueError("pdf_parse_result_not_indexable")
        units = self._units(parsed.blocks)
        units.extend(self._image_units(parsed))
        units.sort(key=lambda unit: unit.ordinal)
        if not units:
            raise ValueError("pdf_chunks_empty")
        groups = self._parent_groups(units)
        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []
        for parent_ordinal, group in enumerate(groups):
            location = _merge_location(group)
            parent_text = "\n\n".join(unit.text for unit in group).strip()
            content_hash = _full_hash(parent_text)
            parent_id = _id(
                document.document_id,
                str(document.version),
                self.version,
                str(location.page_start),
                str(location.page_end),
                str(parent_ordinal),
                content_hash,
            )
            parent_metadata = self._metadata(document, location, parent_id)
            parent_metadata["parent_ordinal"] = parent_ordinal
            parents.append(ParentChunk(
                parent_id, document.document_id, parent_text, _location_label(location),
                content_hash, parent_metadata,
            ))
            children.extend(self._children(document, parent_id, group))
        if not children:
            raise ValueError("pdf_chunks_empty")
        return parents, children

    def preview(self, document: CanonicalDocument, parsed: ParseResult, *, limit: int = 20) -> dict:
        parents, children = self.split(document, parsed)
        cap = max(1, min(int(limit), 100))
        return {
            "splitter_version": self.version,
            "parent_count": len(parents),
            "child_count": len(children),
            "parents": [self._preview_row(parent.parent_id, parent.location, parent.text, parent.metadata)
                        for parent in parents[:cap]],
            "children": [self._preview_row(child.child_id, child.location, child.text, child.metadata)
                         for child in children[:cap]],
        }

    @staticmethod
    def _preview_row(chunk_id: str, location: str, text: str, metadata: dict) -> dict:
        return {
            "chunk_id": chunk_id,
            "location": location,
            "text": text[:240] + ("..." if len(text) > 240 else ""),
            "page_start": metadata["page_start"],
            "page_end": metadata["page_end"],
            "section_path": list(metadata["section_path"]),
            "block_type": metadata["block_type"],
            "parent_id": metadata["parent_id"],
        }

    def _units(self, blocks: tuple[ParsedBlock, ...]) -> list[_PdfUnit]:
        units: list[_PdfUnit] = []
        ordered = sorted(blocks, key=lambda item: item.ordinal)
        index = 0
        while index < len(ordered):
            block = ordered[index]
            text = block.text.strip()
            if not text or _PAGE_ARTIFACT.fullmatch(text):
                index += 1
                continue
            if block.location.block_type == ParsedBlockType.TABLE:
                table_parts = [text]
                next_index = index + 1
                while next_index < len(ordered):
                    following = ordered[next_index]
                    if (following.location.block_type != ParsedBlockType.TABLE
                            or following.location.page_start != block.location.page_start
                            or following.location.page_end != block.location.page_end):
                        break
                    table_parts.append(following.text.strip())
                    next_index += 1
                text = "\n".join(part for part in table_parts if part)
                pieces = self._split_table(text, self.parent_chars)
                index = next_index
            else:
                pieces = [chunk.content.strip() for chunk in self.parent_splitter.split(text)]
                index += 1
            for part_index, piece in enumerate(pieces):
                if piece:
                    units.append(_PdfUnit(piece, block.location, block.ordinal * 10000 + part_index))
        return units

    @staticmethod
    def _image_units(parsed: ParseResult) -> list[_PdfUnit]:
        units: list[_PdfUnit] = []
        for image in parsed.images:
            if not image.ocr_text.strip() or image.ocr_status not in {"ready", "low_confidence"}:
                continue
            page = image.page_number or 1
            location = SourceLocation(
                page, page, (f"image:{image.image_index}",), ParsedBlockType.IMAGE,
            )
            # Keep each image independent from flowing body text and retain its stable index.
            units.append(_PdfUnit(image.ocr_text.strip(), location, 1_000_000_000 + image.image_index))
        return units

    def _parent_groups(self, units: list[_PdfUnit]) -> list[list[_PdfUnit]]:
        groups: list[list[_PdfUnit]] = []
        current: list[_PdfUnit] = []
        current_chars = 0
        for unit in units:
            is_table = unit.location.block_type == ParsedBlockType.TABLE
            is_image = unit.location.block_type == ParsedBlockType.IMAGE
            section_changed = bool(
                current and current[-1].location.section_path != unit.location.section_path
                and unit.location.section_path
            )
            page_gap = bool(current and unit.location.page_start > current[-1].location.page_end + 1)
            must_flush = bool(current and (
                is_table
                or is_image
                or current[-1].location.block_type == ParsedBlockType.TABLE
                or current[-1].location.block_type == ParsedBlockType.IMAGE
                or section_changed
                or page_gap
                or current_chars + 2 + len(unit.text) > self.parent_chars
            ))
            if must_flush:
                groups.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += len(unit.text) + (2 if current_chars else 0)
            if is_table or is_image:
                groups.append(current)
                current = []
                current_chars = 0
        if current:
            groups.append(current)
        return groups

    def _children(self, document: CanonicalDocument, parent_id: str,
                  group: list[_PdfUnit]) -> list[ChildChunk]:
        output: list[ChildChunk] = []
        heading = ""
        for unit in group:
            if unit.location.block_type == ParsedBlockType.HEADING:
                heading = unit.text.strip()
                continue
            if unit.location.block_type == ParsedBlockType.TABLE:
                pieces = self._split_table(unit.text, self.child_chars)
            else:
                pieces = [chunk.content.strip() for chunk in self.child_splitter.split(unit.text)]
            for part_index, piece in enumerate(pieces):
                text = self._with_heading(heading, piece)
                content_hash = _full_hash(text)
                child_id = _id(
                    parent_id,
                    self.version,
                    str(unit.location.page_start),
                    str(unit.location.page_end),
                    str(unit.ordinal),
                    str(part_index),
                    content_hash,
                )
                metadata = self._metadata(document, unit.location, parent_id)
                metadata.update({"block_ordinal": unit.ordinal, "child_ordinal": part_index})
                if unit.location.block_type == ParsedBlockType.IMAGE:
                    image_index = int(unit.location.section_path[0].split(":", 1)[1])
                    image = next(image for image in document.metadata.get("images", ())
                                 if int(image.get("image_index", 0)) == image_index)
                    metadata.update({
                        "image_id": image["image_id"], "image_index": image_index,
                        "image_source_kind": image["source_kind"],
                        "page_number": image.get("page_number"),
                        "page_image_index": image["page_image_index"],
                        "ocr_confidence": image.get("ocr_confidence"),
                        "ocr_status": image["ocr_status"],
                    })
                output.append(ChildChunk(
                    child_id, parent_id, document.document_id, text,
                    _location_label(unit.location), content_hash, metadata,
                ))
        if output:
            return output
        location = _merge_location(group)
        text = "\n\n".join(unit.text for unit in group).strip()
        content_hash = _full_hash(text)
        child_id = _id(parent_id, self.version, "heading-only", content_hash)
        return [ChildChunk(
            child_id, parent_id, document.document_id, text, _location_label(location),
            content_hash, self._metadata(document, location, parent_id),
        )]

    def _metadata(self, document: CanonicalDocument, location: SourceLocation,
                  parent_id: str) -> dict:
        return {
            "business_unit_id": document.business_unit_id,
            "allowed_roles": tuple(sorted(document.allowed_roles)),
            "classification": document.classification,
            "page_start": location.page_start,
            "page_end": location.page_end,
            "section_path": tuple(location.section_path),
            "block_type": location.block_type.value,
            "language": document.language,
            "parent_id": parent_id,
            "source_hash": str(document.metadata.get("source_hash", "")),
            "splitter_version": self.version,
        }

    def _with_heading(self, heading: str, text: str) -> str:
        heading = heading.strip()
        if not heading or text.startswith(heading):
            return text
        prefix = heading[:80] + "\n"
        available = max(1, self.child_chars - len(prefix))
        return prefix + text[:available]

    @staticmethod
    def _split_table(text: str, limit: int) -> list[str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        if len(lines) == 1:
            return [lines[0][offset:offset + limit] for offset in range(0, len(lines[0]), limit)]
        column_header = next((index for index, line in enumerate(lines) if "|" in line), 0)
        header_count = column_header + 1
        if len(lines) > header_count and re.fullmatch(r"[|:+\-\s]+", lines[header_count]):
            header_count += 1
        header = "\n".join(lines[:header_count])
        rows = lines[header_count:]
        if not rows:
            return [header]
        chunks: list[str] = []
        current = header
        for row in rows:
            candidate = current + "\n" + row
            if len(candidate) <= limit or current == header:
                if len(candidate) <= limit:
                    current = candidate
                    continue
                room = max(1, limit - len(header) - 1)
                for offset in range(0, len(row), room):
                    chunks.append(header + "\n" + row[offset:offset + room])
                current = header
                continue
            chunks.append(current)
            current = header + "\n" + row
        if current != header:
            chunks.append(current)
        return chunks or [header]
