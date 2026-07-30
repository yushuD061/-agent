from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from .config import RagVectorConfig
from .contracts import Actor, CanonicalDocument, ChildChunk, DocumentStatus, SearchResult


COLLECTION_PREFIX = "trade_knowledge_v"
ACTIVE_ALIAS = "trade_knowledge_active"
_GENERATION_RE = re.compile(r"^trade_knowledge_v([1-9][0-9]*)$")

OUTPUT_FIELDS = [
    "child_id", "document_id", "document_version", "parent_id", "child_text",
    "child_location", "child_content_hash", "child_metadata", "source_uri",
    "source_title", "source_content_hash", "content_type", "source_location",
    "language", "business_unit_id", "allowed_roles", "classification",
    "document_status", "expires_at_epoch_ms", "parser_version", "source_metadata",
    "embedding_model_id",
]


def collection_name(generation: int) -> str:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("Milvus generation must be a positive integer")
    return f"{COLLECTION_PREFIX}{generation}"


def _literal(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Milvus filter values must be strings")
    return json.dumps(value, ensure_ascii=True)


def _expires_epoch_ms(value: datetime | None) -> int:
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


class MilvusStore:
    """Synchronous Milvus implementation of the stable semantic-store contract."""

    def __init__(self, config: RagVectorConfig, *, model_id: str = "mock-hash-v1", client=None,
                 collection: str | None = None):
        config.validate()
        if config.backend != "milvus":
            raise ValueError("MilvusStore requires RAG_VECTOR_BACKEND=milvus")
        self.config = config
        self.model_id = model_id
        self._client = client
        if collection is not None and _GENERATION_RE.fullmatch(collection) is None:
            raise ValueError("Milvus collection override must be a generation collection")
        self.collection = collection or config.milvus_alias

    def _get_client(self):
        if self._client is None:
            from pymilvus import MilvusClient
            self._client = MilvusClient(**self.config.milvus_connection_kwargs)
        return self._client

    def check_ready(self) -> None:
        client = self._get_client()
        try:
            if self.collection == self.config.milvus_alias:
                alias = client.describe_alias(self.config.milvus_alias)
                target = alias.get("collection_name") or alias.get("collection")
            else:
                target = self.collection
            if not isinstance(target, str) or _GENERATION_RE.fullmatch(target) is None:
                raise RuntimeError("milvus_alias_not_ready")
            description = client.describe_collection(target)
            fields = description.get("fields", [])
            embedding = next((field for field in fields if field.get("name") == "embedding"), None)
            params = (embedding or {}).get("params", {})
            dimension = params.get("dim") or (embedding or {}).get("dim")
            if int(dimension or 0) != self.config.dimensions:
                raise RuntimeError("milvus_schema_not_ready")
            state = client.get_load_state(target)
            if str(state.get("state", "")).lower() not in {"loaded", "loadstate.loaded"}:
                raise RuntimeError("milvus_collection_not_loaded")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("milvus_not_ready") from exc

    def _validated_rows(self, children, vectors) -> tuple[list[ChildChunk], list[list[float]]]:
        rows = list(children)
        values = [list(vector) for vector in vectors]
        if len(rows) != len(values):
            raise ValueError("embedding_count_mismatch")
        if any(len(vector) != self.config.dimensions for vector in values):
            raise ValueError("embedding_dimension_mismatch")
        return rows, values

    def _entity(self, child: ChildChunk, source: CanonicalDocument, vector: list[float]) -> dict[str, Any]:
        if child.document_id != source.document_id:
            raise ValueError("child_document_mismatch")
        return {
            "child_id": child.child_id,
            "document_id": source.document_id,
            "document_version": source.version,
            "parent_id": child.parent_id,
            "child_text": child.text,
            "child_location": child.location,
            "child_content_hash": child.content_hash,
            "child_metadata": child.metadata,
            "source_uri": source.source_uri,
            "source_title": source.title,
            "source_content_hash": source.content_hash,
            "content_type": source.content_type,
            "source_location": source.location,
            "language": source.language,
            "business_unit_id": source.business_unit_id,
            "allowed_roles": sorted(source.allowed_roles),
            "is_public": not source.allowed_roles,
            "classification": source.classification,
            "document_status": source.status.value,
            "expires_at_epoch_ms": _expires_epoch_ms(source.expires_at),
            "parser_version": source.parser_version,
            "source_metadata": source.metadata,
            "embedding_model_id": self.model_id,
            "embedding": vector,
            "updated_at_epoch_ms": int(time.time() * 1000),
        }

    def upsert(self, children, source: CanonicalDocument, vectors, *, flush: bool = True) -> int:
        rows, values = self._validated_rows(children, vectors)
        entities = [self._entity(child, source, vector) for child, vector in zip(rows, values)]
        if not entities:
            return 0
        client = self._get_client()
        client.upsert(self.collection, entities)
        if flush:
            client.flush(self.collection)
        return len(entities)

    def _search_filter(self, actor: Actor) -> str:
        now_ms = int(time.time() * 1000)
        role_filter = "is_public == true"
        if actor.roles:
            roles = json.dumps(sorted(actor.roles), ensure_ascii=True)
            role_filter = f"(is_public == true or ARRAY_CONTAINS_ANY(allowed_roles, {roles}))"
        return (
            f"business_unit_id == {_literal(actor.business_unit_id)} and {role_filter} "
            'and document_status in ["approved", "published"] '
            f"and (expires_at_epoch_ms == 0 or expires_at_epoch_ms > {now_ms})"
        )

    def search(self, vector, actor: Actor, limit: int = 30) -> list[SearchResult]:
        values = list(vector)
        if len(values) != self.config.dimensions:
            raise ValueError("embedding_dimension_mismatch")
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        response = self._get_client().search(
            self.collection,
            data=[values],
            anns_field="embedding",
            filter=self._search_filter(actor),
            limit=limit,
            output_fields=OUTPUT_FIELDS,
            search_params={"metric_type": "COSINE", "params": {}},
            consistency_level="Strong",
        )
        results: list[SearchResult] = []
        for hit in (response[0] if response else []):
            entity = hit.get("entity", hit)
            expires_ms = int(entity.get("expires_at_epoch_ms") or 0)
            expires_at = datetime.fromtimestamp(expires_ms / 1000, timezone.utc) if expires_ms else None
            child = ChildChunk(
                entity["child_id"], entity["parent_id"], entity["document_id"],
                entity["child_text"], entity.get("child_location", ""),
                entity["child_content_hash"], entity.get("child_metadata") or {},
            )
            source = CanonicalDocument(
                document_id=entity["document_id"], version=int(entity["document_version"]),
                source_uri=entity["source_uri"], title=entity["source_title"],
                content=entity["child_text"], content_hash=entity["source_content_hash"],
                content_type=entity["content_type"], location=entity.get("source_location", ""),
                language=entity.get("language", "und"),
                business_unit_id=entity["business_unit_id"],
                allowed_roles=frozenset(entity.get("allowed_roles") or ()),
                classification=entity["classification"],
                status=DocumentStatus(entity["document_status"]), expires_at=expires_at,
                parser_version=entity["parser_version"], metadata=entity.get("source_metadata") or {},
            )
            score = float(hit.get("distance", hit.get("score", 0.0)))
            results.append(SearchResult(child, score, source))
        results.sort(key=lambda item: (-item.score, item.child.child_id))
        return results[:limit]

    def _generation_collections(self) -> list[str]:
        names = self._get_client().list_collections()
        return sorted(name for name in names if _GENERATION_RE.fullmatch(name))

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        expression = f"document_id == {_literal(document_id)}"
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
                raise ValueError("document version must be a positive integer")
            expression += f" and document_version == {version}"
        client = self._get_client()
        deleted_ids: set[str] = set()
        targets = self._generation_collections()
        for target in targets:
            rows = client.query(target, filter=expression, output_fields=["child_id"],
                                consistency_level="Strong")
            deleted_ids.update(str(row["child_id"]) for row in rows)
            if rows:
                client.delete(target, filter=expression)
                client.flush(target)
        for target in targets:
            remaining = client.query(target, filter=expression, output_fields=["child_id"],
                                     limit=1, consistency_level="Strong")
            if remaining:
                raise RuntimeError("milvus_withdrawal_not_propagated")
        return len(deleted_ids)
