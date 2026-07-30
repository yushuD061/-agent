"""A public customer agent isolated from NanoClaw's internal agent runtime."""

from __future__ import annotations

import json

from agent.customer_security import CustomerDataGuard
from agent.loop import AgentLoop
from providers.base import LLMResponse


class CustomerAgent(AgentLoop):
    """Public agent limited to explicitly filtered read-only data tools."""

    SAFE_TOOLS = frozenset({"search_public_knowledge", "search_public_product_catalog"})

    def __init__(self, *args, peer_coordinator=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.peer_coordinator = peer_coordinator

    def _language(self) -> str:
        language = str(self._request_context.get("language", "en"))
        return language if language in {"zh", "en", "de"} else "en"

    async def run(self, user_message: str) -> str:
        refusal = CustomerDataGuard.inspect_request(user_message, self._language())
        if refusal is None:
            if self.peer_coordinator is not None:
                peer_result = await self.peer_coordinator.analyze(
                    self.session_key, user_message, self._language())
                self._request_context["workspace_peer_result"] = peer_result
            return await super().run(user_message)

        user_msg = {"role": "user", "content": user_message}
        assistant_msg = {"role": "assistant", "content": refusal}
        self.session_manager.save_message(self.session_key, user_msg)
        self.session_manager.save_message(self.session_key, assistant_msg)
        self._session_history.extend((user_msg, assistant_msg))
        return refusal

    def _finalize_response(self, content: str) -> str:
        return CustomerDataGuard.sanitize_response(content, self._language())

    def _tool_calls_allowed(self, response: LLMResponse) -> bool:
        for tool_call in response.tool_calls:
            if tool_call.name not in self.SAFE_TOOLS:
                return False
            arguments = json.dumps(tool_call.arguments, ensure_ascii=False)
            if CustomerDataGuard.inspect_request(arguments, self._language()):
                return False
            if CustomerDataGuard.sanitize_response(arguments, self._language()) != arguments:
                return False
        return True

    def _disallowed_tool_response(self) -> str:
        return CustomerDataGuard.refusal(self._language())

    def clear_peer_history(self) -> None:
        if self.peer_coordinator is not None:
            self.peer_coordinator.clear(self.session_key)

    def clear_history(self) -> None:
        super().clear_history()
        self.clear_peer_history()
