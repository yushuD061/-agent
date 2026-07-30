from __future__ import annotations

import json
from datetime import datetime

import httpx

from .config import RagKeywordConfig
from .contracts import (
    Actor,
    CanonicalDocument,
    ChildChunk,
    DocumentStatus,
    SearchResult,
)


INDEX_DEFINITION = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "child_id": {"type": "keyword"},
            "parent_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "document_version": {"type": "integer"},
            "child_text": {"type": "text", "analyzer": "cjk", "similarity": "BM25"},
            "child_location": {"type": "keyword", "index": False},
            "child_content_hash": {"type": "keyword"},
            "child_metadata": {"type": "object", "enabled": False},
            "source_uri": {"type": "keyword", "index": False},
            "source_title": {"type": "text", "analyzer": "cjk", "similarity": "BM25"},
            "source_content_hash": {"type": "keyword"},
            "content_type": {"type": "keyword"},
            "source_location": {"type": "keyword", "index": False},
            "language": {"type": "keyword"},
            "business_unit_id": {"type": "keyword"},
            "allowed_roles": {"type": "keyword"},
            "classification": {"type": "keyword"},
            "document_status": {"type": "keyword"},
            "expires_at": {"type": "date"},
            "parser_version": {"type": "keyword"},
            "source_metadata": {"type": "object", "enabled": False},
        },
    },
}


class ElasticsearchKeywordStore:
    """Elasticsearch-backed BM25 keyword store with server-side ACL filters."""

    def __init__(self, config: RagKeywordConfig, *, client: httpx.Client | None = None):
        config.validate()
        if config.backend != "elasticsearch":
            raise ValueError("ElasticsearchKeywordStore requires RAG_KEYWORD_BACKEND=elasticsearch")
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=config.url,
            auth=(config.username, config.password),
            timeout=config.timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._index_ready = False

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _ensure_index(self) -> None:
        if self._index_ready:
            return
        response = self.client.head(f"/{self.config.index}")
        if response.status_code == 404:
            created = self.client.put(f"/{self.config.index}", json=INDEX_DEFINITION)
            if created.status_code not in {200, 201}:
                # Another process may have created it after our HEAD request.
                if created.status_code != 400 or "resource_already_exists_exception" not in created.text:
                    created.raise_for_status()
        else:
            response.raise_for_status()
        self._index_ready = True

    def check_ready(self) -> None:
        response = self.client.get("/_cluster/health", params={"wait_for_status": "yellow", "timeout": "3s"})
        response.raise_for_status()
        if response.json().get("timed_out"):
            raise RuntimeError("elasticsearch_cluster_not_ready")
        self._ensure_index()

    @staticmethod
    def _document(child: ChildChunk, source: CanonicalDocument) -> dict[str, object]:
        row: dict[str, object] = {
            "child_id": child.child_id,
            "parent_id": child.parent_id,
            "document_id": source.document_id,
            "document_version": source.version,
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
            "classification": source.classification,
            "document_status": source.status.value,
            "parser_version": source.parser_version,
            "source_metadata": source.metadata,
        }
        if source.expires_at is not None:
            row["expires_at"] = source.expires_at.isoformat()
        return row

    def upsert(self, children, source: CanonicalDocument) -> int:
        rows = list(children)
        if not rows:
            return 0
        self._ensure_index()
        lines: list[str] = []
        for child in rows:
            if child.document_id != source.document_id:
                raise ValueError("child_document_mismatch")
            lines.append(json.dumps({"index": {"_index": self.config.index, "_id": child.child_id}}))
            lines.append(json.dumps(self._document(child, source), ensure_ascii=False, default=str))
        response = self.client.post(
            "/_bulk",
            params={"refresh": "wait_for"},
            content="\n".join(lines) + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError("elasticsearch_bulk_index_failed")
        return len(rows)

    @staticmethod
    def _role_filter(actor: Actor) -> dict[str, object]:
        choices: list[dict[str, object]] = [
            {"bool": {"must_not": {"exists": {"field": "allowed_roles"}}}},
        ]
        if actor.roles:
            choices.append({"terms": {"allowed_roles": sorted(actor.roles)}})
        return {"bool": {"should": choices, "minimum_should_match": 1}}

    def search(self, query: str, actor: Actor, limit: int = 30) -> list[SearchResult]:
        if not query.strip():
            return []
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        self._ensure_index()
        request = {
            "size": limit,
            "query": {
                "bool": {
                    "must": [{
                        "multi_match": {
                            "query": query,
                            "fields": ["source_title^2", "child_text"],
                            "type": "best_fields",
                        },
                    }],
                    "filter": [
                        {"term": {"business_unit_id": actor.business_unit_id}},
                        {"terms": {"document_status": ["approved", "published"]}},
                        self._role_filter(actor),
                        {"bool": {"should": [
                            {"bool": {"must_not": {"exists": {"field": "expires_at"}}}},
                            {"range": {"expires_at": {"gt": "now"}}},
                        ], "minimum_should_match": 1}},
                    ],
                },
            },
        }
        response = self.client.post(f"/{self.config.index}/_search", json=request)
        response.raise_for_status()
        results: list[SearchResult] = []
        for hit in response.json().get("hits", {}).get("hits", []):
            row = hit["_source"]
            expires = row.get("expires_at")
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00")) if expires else None
            child = ChildChunk(
                row["child_id"], row["parent_id"], row["document_id"], row["child_text"],
                row.get("child_location", ""), row["child_content_hash"],
                row.get("child_metadata") or {},
            )
            source = CanonicalDocument(
                document_id=row["document_id"], version=int(row["document_version"]),
                source_uri=row.get("source_uri", ""), title=row.get("source_title", ""),
                content=row["child_text"], content_hash=row["source_content_hash"],
                content_type=row.get("content_type", "text/markdown"),
                location=row.get("source_location", ""), language=row.get("language", "und"),
                business_unit_id=row["business_unit_id"],
                allowed_roles=frozenset(row.get("allowed_roles") or ()),
                classification=row.get("classification", "internal"),
                status=DocumentStatus(row["document_status"]), expires_at=expires_at,
                parser_version=row.get("parser_version", "stdlib-1"),
                metadata=row.get("source_metadata") or {},
            )
            results.append(SearchResult(
                child, float(hit.get("_score") or 0.0), source, retrieval_source="keyword"
            ))
        return results

    def delete_by_document(self, document_id: str, version: int | None = None) -> int:
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version <= 0):
            raise ValueError("document version must be a positive integer")
        self._ensure_index()
        filters: list[dict[str, object]] = [{"term": {"document_id": document_id}}]
        if version is not None:
            filters.append({"term": {"document_version": version}})
        response = self.client.post(
            f"/{self.config.index}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"bool": {"filter": filters}}},
        )
        response.raise_for_status()
        return int(response.json().get("deleted", 0))
