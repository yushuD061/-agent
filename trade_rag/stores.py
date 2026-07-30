from __future__ import annotations
import math
from dataclasses import dataclass
import re
from typing import Protocol
from .contracts import ChildChunk, CanonicalDocument, ParentChunk, Actor, SearchResult


class VectorStore(Protocol):
    """Stable semantic-store contract shared by memory, SQLite, pgvector and Milvus."""

    def upsert(self, children, source: CanonicalDocument, vectors) -> int: ...
    def search(self, vector, actor: Actor, limit: int = 30) -> list[SearchResult]: ...
    def delete_by_document(self, document_id: str, version: int | None = None) -> int: ...

@dataclass
class _Entry:
    child: ChildChunk; source: CanonicalDocument; vector: list[float]

class InMemoryVectorStore:
    """隔离测试存储；生产通过同一契约替换 pgvector/Milvus。"""
    def __init__(self): self._entries: dict[str, _Entry] = {}
    def upsert(self, children, source, vectors):
        rows = list(children); values = list(vectors)
        if len(rows) != len(values): raise ValueError("embedding_count_mismatch")
        for child, vector in zip(rows, values): self._entries[child.child_id] = _Entry(child, source, vector)
        return len(rows)
    def search(self, vector, actor: Actor, limit: int = 30):
        def cosine(v): return sum(a*b for a,b in zip(v, vector)) / ((sum(a*a for a in v)*sum(a*a for a in vector))**0.5 or 1)
        rows=[]
        for e in self._entries.values():
            if not e.source.is_searchable() or e.source.business_unit_id != actor.business_unit_id: continue
            if e.source.allowed_roles and not (e.source.allowed_roles & actor.roles): continue
            rows.append(SearchResult(e.child, cosine(e.vector), e.source))
        return sorted(rows, key=lambda x: (-x.score, x.child.child_id))[:limit]
    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        keys=[key for key,e in self._entries.items() if e.source.document_id==document_id and (version is None or e.source.version==version)]
        for key in keys: self._entries.pop(key, None)
        return len(keys)


class InMemoryKeywordStore:
    """隔离测试用 BM25 近似实现；生产替换为 Elasticsearch adapter。"""
    def __init__(self): self._entries: dict[str, tuple[ChildChunk, CanonicalDocument]] = {}
    def upsert(self, children, source):
        rows = list(children)
        for child in rows: self._entries[child.child_id] = (child, source)
        return len(rows)
    def search(self, query: str, actor: Actor, limit: int = 30):
        terms = re.findall(r"[\w\u4e00-\u9fff]+", query.casefold())
        rows=[]
        for child, source in self._entries.values():
            if not source.is_searchable() or source.business_unit_id != actor.business_unit_id: continue
            if source.allowed_roles and not (source.allowed_roles & actor.roles): continue
            text = child.text.casefold(); score = sum(text.count(term) for term in terms)
            if score: rows.append(SearchResult(child, float(score), source, retrieval_source="keyword"))
        return sorted(rows, key=lambda x: x.score, reverse=True)[:limit]
    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        keys=[key for key,(_,source) in self._entries.items() if source.document_id==document_id and (version is None or source.version==version)]
        for key in keys: self._entries.pop(key, None)
        return len(keys)


@dataclass
class _ParentEntry:
    parent: ParentChunk
    source: CanonicalDocument


class InMemoryParentStore:
    """M3 local Parent Store; production adapters must preserve the same stable IDs."""

    def __init__(self):
        self._entries: dict[str, _ParentEntry] = {}

    def upsert(self, parents, source: CanonicalDocument) -> int:
        rows = list(parents)
        if any(parent.document_id != source.document_id for parent in rows):
            raise ValueError("parent_document_mismatch")
        for parent in rows:
            self._entries[parent.parent_id] = _ParentEntry(parent, source)
        return len(rows)

    def get(self, parent_id: str, actor: Actor | None = None) -> ParentChunk | None:
        entry = self._entries.get(parent_id)
        if entry is None:
            return None
        if actor is not None:
            source = entry.source
            if not source.is_searchable() or source.business_unit_id != actor.business_unit_id:
                return None
            if source.allowed_roles and not (source.allowed_roles & actor.roles):
                return None
        return entry.parent

    def get_many(self, parent_ids, actor: Actor | None = None) -> list[ParentChunk]:
        return [parent for parent_id in parent_ids if (parent := self.get(parent_id, actor)) is not None]

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        keys = [key for key, entry in self._entries.items()
                if entry.source.document_id == document_id
                and (version is None or entry.source.version == version)]
        for key in keys:
            self._entries.pop(key, None)
        return len(keys)
