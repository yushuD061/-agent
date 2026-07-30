from __future__ import annotations
from .chunking import ParentChildSplitter
from dataclasses import replace

from .contracts import CanonicalDocument, QueryRequest
from .embeddings import MockEmbeddingProvider
from .generation import answer_with_citations
from .query_rewriter import QueryRewriter
from .reranker import ListwiseReranker, unique_parent_results
from .retrieval import QualityGate, QueryRouter, reciprocal_rank_fusion
from .stores import InMemoryKeywordStore, InMemoryParentStore
from .keyword_runtime import create_application_keyword_store
from .vector_runtime import create_application_vector_store
from .parent_runtime import create_application_parent_store
from .hybrid import HybridRetriever

class RagPipeline:
    def __init__(self, *, embedder=None, store=None, keyword_store=None, parent_store=None, splitter=None, rewriter=None, reranker=None, semantic_weight=.7, rrf_k=60):
        explicit_store = store is not None
        self.embedder=embedder or MockEmbeddingProvider(); self.store=store or create_application_vector_store(); self.keyword_store=keyword_store if keyword_store is not None else (InMemoryKeywordStore() if explicit_store else create_application_keyword_store()); self.parent_store=parent_store if parent_store is not None else (InMemoryParentStore() if explicit_store else create_application_parent_store()); self.splitter=splitter or ParentChildSplitter(); self.router=QueryRouter(); self.gate=QualityGate(); self.reranker=reranker or ListwiseReranker(); self.rewriter=rewriter or QueryRewriter(); self.retriever=HybridRetriever(self.store, self.keyword_store, self.embedder, semantic_weight, rrf_k)
    def index(self, document: CanonicalDocument):
        parents, children=self.splitter.split(document)
        return self.index_prepared(document, parents, children)
    def index_prepared(self, document, parents, children):
        parents=list(parents); children=list(children)
        if not parents or not children: raise ValueError("knowledge_chunks_empty")
        parent_ids={parent.parent_id for parent in parents}
        if any(child.parent_id not in parent_ids for child in children): raise ValueError("knowledge_parent_link_invalid")
        vectors=self.embedder.embed([child.text for child in children])
        self.delete_by_document(document.document_id, document.version)
        try:
            parent_count=self.parent_store.upsert(parents, document)
            semantic_count=self.store.upsert(children, document, vectors)
            keyword_count=self.keyword_store.upsert(children, document)
            if parent_count != len(parents) or semantic_count != len(children) or keyword_count != len(children):
                raise RuntimeError("knowledge_index_count_mismatch")
        except Exception:
            self.delete_by_document(document.document_id, document.version)
            raise
        return {"document_id":document.document_id,"parents":parent_count,"children":len(children),"semantic_indexed":semantic_count,"keyword_indexed":keyword_count,"indexed_count":len(children),"model_id":self.embedder.model_id}
    def delete_by_document(self, document_id: str, version: int | None = None):
        deleted={"semantic":0,"keyword":0,"parents":0}; errors=[]
        for name, store in (("semantic", self.store), ("keyword", self.keyword_store), ("parents", self.parent_store)):
            try: deleted[name]=store.delete_by_document(document_id, version)
            except Exception as exc: errors.append(exc)
        if errors: raise RuntimeError("knowledge_index_delete_failed") from errors[0]
        semantic=deleted["semantic"]; keyword=deleted["keyword"]; parents=deleted["parents"]
        return {"document_id":document_id,"semantic_deleted":semantic,"keyword_deleted":keyword,"parents_deleted":parents}
    def query(self, request: QueryRequest):
        if self.router.route(request.query) == "mysql": return {"status":"BUSINESS_DATA_REQUIRED", "answer":"该问题必须回查 MySQL 业务权威库。", "citations":[]}
        queries=self.rewriter.rewrite(request.query, request.history)
        pool=max(request.top_k * (4 if request.rerank else 2), 10)
        recall_limit=max(request.top_n, pool)
        result_sets=[self.retriever.search(query, request.actor, recall_limit) for query in queries]
        results=reciprocal_rank_fusion(result_sets, pool)
        status=self.gate.classify(results)
        primary=queries[0] if queries else request.query
        if status == "HIGH_CONFIDENCE" and request.rerank and len(results)>1: results=self.reranker.rerank(primary, results, len(results))
        results=unique_parent_results(results, request.top_k)
        expanded=[]
        for result in results:
            parent=self.parent_store.get(result.child.parent_id, request.actor)
            expanded.append(replace(result, parent=parent) if parent is not None else result)
        results=expanded
        response=answer_with_citations(primary, results, status)
        response["query_count"] = len(queries)
        response["rerank_status"] = getattr(self.reranker, "last_status", "unknown") if request.rerank else "disabled"
        response["retrieval_mode"] = self.retriever.last_mode
        return response
