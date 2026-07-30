from __future__ import annotations
from .contracts import QueryRequest, SearchResult

class QueryRouter:
    dynamic_terms = ("价格", "库存", "汇率", "报价", "price", "inventory", "exchange rate", "quote")
    def route(self, query: str) -> str:
        return "mysql" if any(term in query.lower() for term in self.dynamic_terms) else "rag"

class QualityGate:
    def classify(self, results: list[SearchResult]) -> str:
        if not results: return "NO_EVIDENCE"
        if results[0].score < 0.15: return "AMBIGUOUS"
        if len(results) > 1 and abs(results[0].score-results[1].score) < 0.02: return "AMBIGUOUS"
        return "HIGH_CONFIDENCE"

def reciprocal_rank_fusion(result_sets: list[list[SearchResult]], limit: int, k: int = 60) -> list[SearchResult]:
    """按稳定 child_id 融合多查询结果，避免重复父/子命中膨胀。"""
    scores: dict[str, float] = {}
    relevance: dict[str, float] = {}
    rows: dict[str, SearchResult] = {}
    for results in result_sets:
        for rank, result in enumerate(results, start=1):
            key = result.child.child_id
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            relevance[key] = max(relevance.get(key, float("-inf")), result.score)
            rows.setdefault(key, result)
    fused = sorted(rows.values(), key=lambda item: scores[item.child.child_id], reverse=True)
    for item in fused:
        item.score = relevance[item.child.child_id]
        item.rrf_score = scores[item.child.child_id]
    return fused[:limit]
