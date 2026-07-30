from __future__ import annotations
from dataclasses import replace
from .contracts import Actor, SearchResult

class HybridRetriever:
    def __init__(self, semantic_store, keyword_store=None, embedder=None, semantic_weight: float = .7, rrf_k: int = 60):
        if not 0 <= semantic_weight <= 1: raise ValueError("semantic_weight must be between 0 and 1")
        if rrf_k <= 0: raise ValueError("rrf_k must be greater than 0")
        self.semantic_store, self.keyword_store, self.embedder = semantic_store, keyword_store, embedder
        self.semantic_weight, self.keyword_weight, self.rrf_k = semantic_weight, 1 - semantic_weight, rrf_k
        self.last_mode = "unavailable"

    def search(self, query: str, actor: Actor, limit: int = 30):
        semantic=[]; keyword=[]
        if self.semantic_store is not None and self.embedder is not None:
            try: semantic=self.semantic_store.search(self.embedder.embed([query])[0], actor, limit)
            except Exception: semantic=[]
        if self.keyword_store is not None:
            try: keyword=self.keyword_store.search(query, actor, limit)
            except Exception: keyword=[]
        if semantic and keyword: self.last_mode="hybrid"
        elif semantic: self.last_mode="semantic"
        elif keyword: self.last_mode="keyword"
        else: self.last_mode="unavailable"
        return self._fuse(semantic, keyword, limit)

    def _fuse(self, semantic, keyword, limit):
        rows={}; scores={}
        for weight, results in ((self.semantic_weight, semantic), (self.keyword_weight, keyword)):
            for rank, result in enumerate(results, 1):
                key=result.child.child_id; rows.setdefault(key, result)
                scores[key]=scores.get(key, 0) + weight/(self.rrf_k+rank)
        output=[]
        for key in sorted(rows, key=lambda k: (-scores[k], -rows[k].score)):
            output.append(replace(rows[key], score=rows[key].score, rrf_score=scores[key], retrieval_source=self.last_mode+"+rrf"))
        return output[:limit]
