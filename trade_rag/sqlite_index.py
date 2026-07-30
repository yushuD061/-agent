"""Inspect and reconcile the derived local SQLite knowledge indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .config import load_rag_keyword_config, load_rag_vector_config
from .knowledge_repository import KnowledgeRepository
from .pipeline import RagPipeline
from .sqlite_store import DEFAULT_MODEL_ID, SqliteKnowledgeDatabase, SqliteVectorStore


def _active_documents(repository: KnowledgeRepository):
    for document in repository.load_published():
        parents, children = repository.splitter.split(document)
        yield document, list(parents), list(children)
    for document_id in repository.list_pdf_index_candidates():
        document, parents, children = repository.load_pdf_chunks(document_id)
        yield document, list(parents), list(children)


def _database(pipeline: RagPipeline) -> SqliteKnowledgeDatabase:
    delegate = getattr(pipeline.store, "_delegate", pipeline.store)
    if not isinstance(delegate, SqliteVectorStore):
        raise ValueError("RAG_VECTOR_BACKEND=sqlite is required")
    return delegate.db


def _fingerprint(document, parents, children) -> str:
    payload = {
        "document": {
            "id": document.document_id,
            "version": document.version,
            "content_hash": document.content_hash,
            "business_unit_id": document.business_unit_id,
            "allowed_roles": sorted(document.allowed_roles),
            "classification": document.classification,
            "status": document.status.value,
            "expires_at": document.expires_at.isoformat() if document.expires_at else None,
            "parser_version": document.parser_version,
        },
        "parents": [(row.parent_id, row.content_hash) for row in parents],
        "children": [(row.child_id, row.content_hash) for row in children],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reconcile_sqlite_index(
    repository: KnowledgeRepository,
    pipeline: RagPipeline,
    *,
    apply: bool = False,
) -> dict[str, int | str]:
    database = _database(pipeline)
    active = list(_active_documents(repository))
    active_keys = {(document.document_id, document.version) for document, _, _ in active}
    state = database.state()
    stale = sorted((database.document_keys() | set(state)) - active_keys)
    fingerprints = {
        (document.document_id, document.version): _fingerprint(document, parents, children)
        for document, parents, children in active
    }
    changed = [
        item for item in active
        if state.get((item[0].document_id, item[0].version))
        != (fingerprints[(item[0].document_id, item[0].version)], database.model_id)
    ]
    result: dict[str, int | str] = {
        "mode": "apply" if apply else "dry_run",
        "active_documents": len(active),
        "active_chunks": sum(len(children) for _, _, children in active),
        "documents_to_index": len(changed),
        "stale_documents": len(stale),
        "indexed_chunks": 0,
        "deleted_chunks": 0,
    }
    if not apply:
        return result
    for document_id, version in stale:
        deleted = pipeline.delete_by_document(document_id, version)
        result["deleted_chunks"] = int(result["deleted_chunks"]) + max(
            int(deleted.get("semantic_deleted", 0)),
            int(deleted.get("keyword_deleted", 0)),
        )
        database.unmark(document_id, version)
    for document, parents, children in changed:
        indexed = pipeline.index_prepared(document, parents, children)
        database.mark_indexed(
            document, fingerprints[(document.document_id, document.version)]
        )
        result["indexed_chunks"] = int(result["indexed_chunks"]) + int(
            indexed["indexed_count"]
        )
    return result


def check_sqlite_index() -> dict[str, object]:
    vector = load_rag_vector_config()
    keyword = load_rag_keyword_config()
    if vector.backend != "sqlite" or keyword.backend != "sqlite":
        raise ValueError("RAG_VECTOR_BACKEND and RAG_KEYWORD_BACKEND must both be sqlite")
    if Path(vector.sqlite_path) != Path(keyword.sqlite_path):
        raise ValueError("SQLite vector and keyword paths must match")
    database = SqliteKnowledgeDatabase(
        vector.sqlite_path, vector.dimensions, DEFAULT_MODEL_ID,
    )
    try:
        database.check_ready()
        connection = database.connection
        return {
            "status": "ready",
            "backend": "sqlite",
            "path": str(database.path),
            "schema_version": 1,
            "embedding_model_id": database.model_id,
            "dimensions": database.dimensions,
            "vector_rows": int(connection.execute(
                "SELECT count(*) FROM rag_child_vec"
            ).fetchone()[0]),
            "keyword_rows": int(connection.execute(
                "SELECT count(*) FROM rag_child_fts"
            ).fetchone()[0]),
            "parent_rows": int(connection.execute(
                "SELECT count(*) FROM rag_parent"
            ).fetchone()[0]),
        }
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or rebuild the local SQLite RAG index")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--knowledge-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.check:
        payload = check_sqlite_index()
    else:
        repository = (KnowledgeRepository(args.knowledge_root)
                      if args.knowledge_root else KnowledgeRepository())
        pipeline = RagPipeline()
        payload = reconcile_sqlite_index(repository, pipeline, apply=args.apply)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
