"""受控 MCP 服务入口。默认仅提供固定参数的知识检索工具。"""
from mcp.server.fastmcp import FastMCP
from .contracts import Actor, QueryRequest
from .pipeline import RagPipeline
from .knowledge_repository import KnowledgeRepository

mcp = FastMCP("trade-rag")
pipeline = RagPipeline()
repository = KnowledgeRepository()
indexed_hashes: set[str] = set()

def _reconcile_index() -> int:
    published = repository.load_published()
    pdf_ids = set(repository.list_pdf_index_candidates())
    active = {(document.document_id, document.version) for document in published}
    active.update((document_id, int(repository.get_document(document_id).get("version", 1)))
                  for document_id in pdf_ids)
    records, _, _ = repository.list_documents(
        offset=0, limit=10_000, include_deleted=True,
    )
    for entry in records:
        key = (str(entry.get("document_id", "")), int(entry.get("version", 1)))
        if key[0] and key not in active:
            pipeline.delete_by_document(*key)
            indexed_hashes.discard(str(entry.get("content_hash", "")))
            indexed_hashes.discard(str(entry.get("source_hash", "")))
    return len(active)

def _refresh_imported_knowledge() -> int:
    if getattr(pipeline.store, "backend", "") == "sqlite":
        from .sqlite_index import reconcile_sqlite_index
        result = reconcile_sqlite_index(repository, pipeline, apply=True)
        return int(result["documents_to_index"])
    _reconcile_index()
    indexed = 0
    for document in repository.load_published():
        if document.content_hash in indexed_hashes:
            continue
        pipeline.index(document)
        indexed_hashes.add(document.content_hash)
        indexed += 1
    for document_id in repository.list_pdf_index_candidates():
        current = repository.get_document(document_id)
        index_key = str(current.get("source_hash") or document_id)
        if index_key in indexed_hashes:
            continue
        repository.index_pdf(document_id, index_prepared=pipeline.index_prepared)
        indexed_hashes.add(index_key)
        indexed += 1
    return indexed

@mcp.tool()
def search_enterprise_knowledge(query: str, actor_id: str, business_unit_id: str = "default", roles: list[str] | None = None) -> dict:
    """在已批准、未过期且服务端 ACL 过滤的企业知识中检索；动态业务数据必须走 MySQL。"""
    try:
        _refresh_imported_knowledge()
        actor=Actor(actor_id=actor_id, roles=frozenset(roles or []), business_unit_id=business_unit_id)
        return pipeline.query(QueryRequest(query=query, actor=actor))
    except Exception:
        return {"status": "VECTOR_BACKEND_UNAVAILABLE", "answer": "", "citations": []}

if __name__ == "__main__": mcp.run(transport="stdio")
