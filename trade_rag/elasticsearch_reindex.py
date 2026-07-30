from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_rag_keyword_config
from .elasticsearch_store import ElasticsearchKeywordStore
from .knowledge_repository import KnowledgeRepository


def _active_chunks(repository: KnowledgeRepository):
    for document in repository.load_published():
        _parents, children = repository.splitter.split(document)
        yield document, list(children)
    for document_id in repository.list_pdf_index_candidates():
        document, _parents, children = repository.load_pdf_chunks(document_id)
        yield document, list(children)


def rebuild_elasticsearch_keyword_index(
    repository: KnowledgeRepository,
    store: ElasticsearchKeywordStore,
    *,
    apply: bool = False,
) -> dict[str, int | str]:
    active = list(_active_chunks(repository))
    active_keys = {(document.document_id, document.version) for document, _ in active}
    records, _, _ = repository.list_documents(offset=0, limit=10_000, include_deleted=True)
    stale = [
        (str(row.get("document_id", "")), int(row.get("version", 1)))
        for row in records
        if row.get("document_id") and (str(row["document_id"]), int(row.get("version", 1))) not in active_keys
    ]
    result: dict[str, int | str] = {
        "mode": "apply" if apply else "dry_run",
        "active_documents": len(active),
        "active_chunks": sum(len(children) for _, children in active),
        "stale_documents": len(stale),
        "indexed_chunks": 0,
        "deleted_chunks": 0,
    }
    if not apply:
        return result
    store.check_ready()
    for document, children in active:
        result["indexed_chunks"] = int(result["indexed_chunks"]) + store.upsert(children, document)
    for document_id, version in stale:
        result["deleted_chunks"] = int(result["deleted_chunks"]) + store.delete_by_document(
            document_id, version
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap or rebuild the Elasticsearch keyword index")
    parser.add_argument("--apply", action="store_true", help="write the derived Elasticsearch index")
    parser.add_argument("--check", action="store_true", help="check Elasticsearch readiness only")
    parser.add_argument("--knowledge-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.apply and args.check:
        parser.error("--apply and --check are mutually exclusive")

    config = load_rag_keyword_config()
    if config.backend != "elasticsearch":
        raise ValueError("RAG_KEYWORD_BACKEND=elasticsearch is required")
    store = ElasticsearchKeywordStore(config)
    try:
        if args.check:
            store.check_ready()
            payload: dict[str, object] = {"status": "ready", "backend": "elasticsearch"}
        else:
            repository = KnowledgeRepository(args.knowledge_root) if args.knowledge_root else KnowledgeRepository()
            payload = rebuild_elasticsearch_keyword_index(repository, store, apply=args.apply)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
