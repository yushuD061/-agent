"""Complete-turn history compaction planning and safe summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


SummaryFunction = Callable[[list[dict[str, Any]]], Awaitable[str]]


@dataclass(frozen=True)
class CompactionPlan:
    original: tuple[dict[str, Any], ...]
    evicted: tuple[dict[str, Any], ...]
    retained: tuple[dict[str, Any], ...]

    @property
    def needed(self) -> bool:
        return bool(self.evicted)


def plan_complete_turns(
    history: list[dict[str, Any]], max_turns: int
) -> CompactionPlan:
    """Retain the last N complete user turns without splitting tool groups."""
    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    user_indices = [index for index, item in enumerate(history) if item.get("role") == "user"]
    if len(user_indices) <= max_turns:
        return CompactionPlan(tuple(history), (), tuple(history))
    boundary = user_indices[-max_turns]
    evicted, retained = history[:boundary], history[boundary:]
    validate_tool_groups(retained)
    return CompactionPlan(tuple(history), tuple(evicted), tuple(retained))


def validate_tool_groups(messages: list[dict[str, Any]]) -> None:
    """Reject retained histories containing an orphaned tool result or call."""
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or ():
            call_id = call.get("id")
            if call_id:
                call_ids.add(str(call_id))
        if message.get("role") == "tool" and message.get("tool_call_id"):
            result_ids.add(str(message["tool_call_id"]))
    if call_ids != result_ids:
        raise ValueError("incomplete_tool_group")


class ProviderTurnSummarizer:
    """Bounded summary adapter used only after complete-turn selection."""

    def __init__(self, provider, *, model: str | None = None, max_chars: int = 24_000):
        self.provider, self.model, self.max_chars = provider, model, max_chars

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        safe_messages = []
        for item in messages:
            if (
                item.get("role") in {"user", "assistant"}
                or (item.get("role") == "system" and item.get("memory_compaction_id"))
            ) and isinstance(item.get("content"), str):
                safe_messages.append({"role": item["role"], "content": item["content"]})
        text = json.dumps(safe_messages, ensure_ascii=False)[: self.max_chars]
        response = await self.provider.chat(
            messages=[{
                "role": "user",
                "content": (
                    "Summarize the following older complete conversation turns. Preserve decisions, "
                    "confirmed facts, unresolved questions, and source uncertainty. Do not claim any "
                    f"message was deleted. Return only the summary.\n\n{text}"
                ),
            }],
            tools=None,
            model=self.model,
        )
        summary = (response.content or "").strip()
        if not summary:
            raise RuntimeError("history_summary_unavailable")
        return summary
