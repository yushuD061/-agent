"""Scope-first keyword/vector fusion with authoritative-store revalidation."""

from __future__ import annotations

from .models import ActorContext, MemoryHit, MemoryScope


class HybridMemoryRetriever:
    """Derived indexes nominate IDs; the authoritative store decides visibility."""

    def __init__(
        self, authoritative_store, keyword_index, vector_index, *,
        keyword_weight: float = 1.0, vector_weight: float = 1.0,
        rrf_k: int = 60, score_threshold: float = 0.0,
    ):
        if keyword_weight < 0 or vector_weight < 0 or not (keyword_weight or vector_weight):
            raise ValueError("memory_hybrid_weights_invalid")
        if rrf_k < 1 or score_threshold < 0:
            raise ValueError("memory_hybrid_settings_invalid")
        self.authoritative_store = authoritative_store
        self.keyword_index = keyword_index
        self.vector_index = vector_index
        self.keyword_weight = keyword_weight
        self.vector_weight = vector_weight
        self.rrf_k = rrf_k
        self.score_threshold = score_threshold
        self.last_mode = "hybrid"

    @property
    def index_version(self) -> str:
        return f"k{self.keyword_index.index_version}:v{self.vector_index.index_version}"

    def search(
        self, actor: ActorContext, scope: MemoryScope, query: str, top_k: int,
    ) -> list[MemoryHit]:
        if not 1 <= top_k <= 50:
            raise ValueError("memory_top_k_invalid")
        pool = min(50, max(top_k * 4, 10))
        keyword_error = vector_error = None
        try:
            keyword_hits = self.keyword_index.search(scope, query, pool)
        except Exception as exc:
            keyword_hits, keyword_error = [], type(exc).__name__
        try:
            vector_hits = self.vector_index.search(scope, query, pool)
        except Exception as exc:
            vector_hits, vector_error = [], type(exc).__name__
        if keyword_error and vector_error:
            self.last_mode = "unavailable"
            return []
        if vector_error:
            self.last_mode = "keyword_degraded"
        elif keyword_error:
            self.last_mode = "vector_degraded"
        else:
            self.last_mode = "hybrid"
        scores: dict[str, float] = {}
        explanations: dict[str, dict] = {}
        for rank, hit in enumerate(keyword_hits, 1):
            contribution = self.keyword_weight / (self.rrf_k + rank)
            scores[hit.memory_id] = scores.get(hit.memory_id, 0.0) + contribution
            explanations.setdefault(hit.memory_id, {})["keyword"] = {
                "rank": rank, "raw_score": hit.score, "contribution": contribution,
            }
        for rank, hit in enumerate(vector_hits, 1):
            contribution = self.vector_weight / (self.rrf_k + rank)
            scores[hit.memory_id] = scores.get(hit.memory_id, 0.0) + contribution
            explanations.setdefault(hit.memory_id, {})["vector"] = {
                "rank": rank, "raw_score": hit.score, "contribution": contribution,
            }
        candidate_ids = [
            memory_id for memory_id, score in sorted(
                scores.items(), key=lambda item: (-item[1], item[0])
            ) if score >= self.score_threshold
        ]
        if not candidate_ids:
            return []
        active = self.authoritative_store.get_active_by_ids(
            actor, scope, candidate_ids,
        )
        return [
            MemoryHit(active[memory_id], scores[memory_id], {
                **explanations[memory_id], "fusion": "rrf", "retrieval_mode": self.last_mode,
                "index_version": self.index_version,
            })
            for memory_id in candidate_ids[:pool] if memory_id in active
        ][:top_k]
