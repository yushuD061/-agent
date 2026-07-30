from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence

from .contracts import HistoryMessage


class QueryRewriter:
    """历史感知多查询改写；生成失败时始终回退原始问题。"""

    def __init__(
        self,
        generate: Callable[[str], str] | None = None,
        num_queries: int = 3,
        *,
        max_history: int = 6,
        max_history_chars: int = 200,
        max_query_chars: int = 50,
        max_input_chars: int = 1000,
    ):
        self.generate = generate
        self.num_queries = num_queries if num_queries > 0 else 3
        self.max_history = max(0, max_history)
        self.max_history_chars = max(1, max_history_chars)
        self.max_query_chars = max(1, max_query_chars)
        self.max_input_chars = max(1, max_input_chars)

    def rewrite(self, query: str, history: Sequence[HistoryMessage] = ()) -> list[str]:
        original = query.strip() if query else ""
        if not original:
            return []
        if self.generate is None or self.num_queries <= 1:
            return [original]
        try:
            parsed = self._parse(self.generate(self.build_prompt(original, history)))
        except Exception:
            return [original]
        candidates = parsed + [original]
        output, seen = [], set()
        for item in candidates:
            value = item.strip()
            key = value.casefold()
            if not value or len(value) > self.max_query_chars or key in seen:
                continue
            seen.add(key); output.append(value)
            if len(output) >= self.num_queries:
                break
        return output or [original]

    def build_prompt(self, query: str, history: Sequence[HistoryMessage]) -> str:
        recent = list(history)[-self.max_history :]
        lines = [f"[{m.role}] {m.content[:self.max_history_chars]}" for m in recent]
        history_text = "\n".join(lines) if lines else "（无历史，直接改写当前问题）"
        bounded_query = query[:self.max_input_chars]
        return (
            "你是查询改写器。历史只用于消除指代，不得改变意图或编造实体。\n"
            f"返回严格 JSON：{{\"queries\":[...]}}，共 {self.num_queries} 条；"
            f"第一条必须自包含，每条不超过 {self.max_query_chars} 字，不输出答案或解释。\n"
            f"最近对话历史：\n{history_text}\n当前问题：{bounded_query}"
        )

    def _parse(self, raw: str) -> list[str]:
        text = raw.strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1)
        payload = json.loads(text)
        values = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise ValueError("queries must be a list")
        return [value for value in values if isinstance(value, str)]
