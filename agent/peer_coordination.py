"""Same-level customer/workspace agent coordination with a strict public boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from agent.customer_security import CustomerDataGuard
from agent.loop import AgentLoop


class WorkspacePeerCoordinator:
    """Route customer requirements to a peer workspace agent, never a subagent."""

    _PRIVATE_OUTPUT = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\b(internal[_ -]?(?:unit[_ -]?price|exact[_ -]?inventory)|cost|margin|profit)\b\s*[:=]",
        r"\b(?:exact\s+inventory|base\s+price|unit\s+cost|gross\s+margin)\b",
        r"\b(?:inventory|stock|unit_price|price|cost|margin|profit)\b\s*(?:is|[:=])\s*[$€£]?\d",
        r"\b(?:customer|supplier|counterparty)[_ -]?(?:name|email|record|list)\b\s*[:=]",
        r"(?:内部价格|成本|利润率|精确库存|库存|现货|价格|客户名单|供应商名单)\s*(?:为|是|有|[:：=])\s*[￥$€£]?\d",
    ))
    _ALLOWED_BASIS = frozenset({
        "public_catalog", "public_knowledge", "availability_boolean", "human_confirmation",
    })

    def __init__(self, agent_factory: Callable[[str], AgentLoop]) -> None:
        self._agent_factory = agent_factory
        self._agents: dict[str, AgentLoop] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _peer_key(customer_session_key: str) -> str:
        digest = hashlib.sha256(customer_session_key.encode("utf-8")).hexdigest()[:24]
        return f"workspace_peer:{digest}"

    @staticmethod
    def _fallback(language: str) -> dict[str, Any]:
        answer = {
            "zh": "该需求需要业务人员进一步确认。",
            "de": "Diese Anfrage muss von einem Mitarbeiter bestätigt werden.",
            "en": "This request requires confirmation by a staff member.",
        }.get(language, "This request requires confirmation by a staff member.")
        return {"status": "needs_human_confirmation", "public_answer": answer,
                "basis": ["human_confirmation"]}

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        candidate = str(text or "").strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _public_result(self, raw: str, language: str) -> dict[str, Any]:
        payload = self._parse_json(raw)
        if payload is None or set(payload) - {"status", "public_answer", "basis"}:
            return self._fallback(language)
        status = str(payload.get("status", ""))
        answer = str(payload.get("public_answer", "")).strip()[:4000]
        basis = payload.get("basis", [])
        if status not in {"answered", "needs_human_confirmation"}:
            return self._fallback(language)
        if not isinstance(basis, list) or not basis or any(item not in self._ALLOWED_BASIS for item in basis):
            return self._fallback(language)
        if not answer or any(pattern.search(answer) for pattern in self._PRIVATE_OUTPUT):
            return self._fallback(language)
        if CustomerDataGuard.sanitize_response(answer, language) != answer:
            return self._fallback(language)
        return {"status": status, "public_answer": answer, "basis": basis}

    async def analyze(self, customer_session_key: str, requirement: str, language: str) -> dict[str, Any]:
        peer_key = self._peer_key(customer_session_key)
        lock = self._locks.setdefault(peer_key, asyncio.Lock())
        async with lock:
            agent = self._agents.get(peer_key)
            if agent is None:
                agent = self._agent_factory(peer_key)
                self._agents[peer_key] = agent
            agent.set_request_context({
                "channel": "customer_workspace_bridge",
                "language": language if language in {"zh", "en", "de"} else "en",
            })
            try:
                raw = await agent.run(str(requirement or "")[:8000])
            except Exception:
                return self._fallback(language)
            return self._public_result(raw, language)

    def clear(self, customer_session_key: str) -> None:
        """Clear the paired workspace history when the customer conversation is cleared."""
        peer_key = self._peer_key(customer_session_key)
        agent = self._agents.pop(peer_key, None)
        self._locks.pop(peer_key, None)
        if agent is not None:
            agent.clear_history()
