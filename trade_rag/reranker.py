from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import replace

from .contracts import SearchResult


class ListwiseReranker:
    """一次 Listwise 调用完成排序；任何失败均返回原候选顺序。"""

    model_id = "listwise-json-v1"

    def __init__(self, generate: Callable[[str], str] | None = None, preview_len: int = 200):
        self.generate = generate
        self.preview_len = max(1, int(preview_len))
        self.last_status = "not_called"

    def rerank(self, query: str, results: list[SearchResult], top_k: int | None = None) -> list[SearchResult]:
        limit = max(0, top_k if top_k is not None else len(results))
        fallback = list(results[:limit])
        if not results or self.generate is None or len(results) == 1:
            self.last_status = "fallback"
            return fallback
        try:
            scores = self._parse(self.generate(self.build_prompt(query, results)), len(results))
        except Exception:
            self.last_status = "fallback"
            return fallback
        if not scores:
            self.last_status = "fallback"
            return fallback
        ranked = []
        for index, result in enumerate(results):
            value = scores.get(index, 0)
            original_score = result.rrf_score if result.rrf_score is not None else result.score
            ranked.append((value, original_score, -index, replace(result, score=value / 10.0, rrf_score=original_score, rerank_score=value / 10.0, retrieval_source=result.retrieval_source + "+rerank")))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        self.last_status = "success"
        return [item[3] for item in ranked[:limit]]

    def build_prompt(self, query: str, results: list[SearchResult]) -> str:
        previews = []
        for index, result in enumerate(results):
            text = result.child.text
            preview = text if len(text) <= self.preview_len else text[: self.preview_len] + "..."
            previews.append(f"[{index}] {preview}")
        return (
            "仅依据候选段落评估与问题的相关性和信息密度，不得引入外部知识。\n"
            "返回严格 JSON：{\"scores\":[{\"idx\":0,\"score\":0}]}。"
            "每个候选恰好一次，idx 为候选下标，score 为 0 到 10 的整数。\n"
            f"用户问题：{query}\n候选段落：\n" + "\n".join(previews)
        )

    def _parse(self, raw: str, count: int) -> dict[int, int]:
        text = raw.strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1)
        payload = json.loads(text)
        items = payload.get("scores") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("scores must be a list")
        scores: dict[int, int] = {}
        for item in items:
            if not isinstance(item, dict): continue
            idx, score = item.get("idx"), item.get("score")
            if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= count or idx in scores: continue
            if isinstance(score, bool) or not isinstance(score, int) or not math.isfinite(score) or not 0 <= score <= 10: continue
            scores[idx] = score
        return scores


def unique_parent_results(results: list[SearchResult], top_k: int) -> list[SearchResult]:
    output, seen = [], set()
    for result in results:
        if result.child.parent_id in seen: continue
        seen.add(result.child.parent_id); output.append(result)
        if len(output) >= max(0, top_k): break
    return output
