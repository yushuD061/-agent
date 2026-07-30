"""Optional AgentLoop memory lifecycle contract and disabled implementation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from dataclasses import asdict
from .models import CustomerInquiryWorkingMemory

from .models import (
    PreparedMemory,
    ToolResultEvent,
    TurnAbortedEvent,
    TurnCompletedEvent,
    TurnRequest,
)


@runtime_checkable
class AgentMemoryLifecycle(Protocol):
    async def prepare_turn(self, request: TurnRequest) -> PreparedMemory: ...
    async def observe_tool_result(self, event: ToolResultEvent) -> None: ...
    async def complete_turn(self, event: TurnCompletedEvent) -> None: ...
    async def abort_turn(self, event: TurnAbortedEvent) -> None: ...


class NoOpAgentMemoryLifecycle:
    """Disabled lifecycle: retain the caller's history and perform no writes."""

    async def prepare_turn(self, request: TurnRequest) -> PreparedMemory:
        request.actor.validate()
        request.scope.validate()
        return PreparedMemory(history=request.history)

    async def observe_tool_result(self, event: ToolResultEvent) -> None:
        return None

    async def complete_turn(self, event: TurnCompletedEvent) -> None:
        return None

    async def abort_turn(self, event: TurnAbortedEvent) -> None:
        return None


class BoundedWorkingMemoryLifecycle:
    """M2 lifecycle: bounded history plus scoped working-memory reads only."""

    def __init__(self, session_manager, *, customer_store=None, workspace_store=None,
                 long_term_store=None, recall_top_k: int = 3):
        self.session_manager = session_manager
        self.customer_store = customer_store
        self.workspace_store = workspace_store
        self.long_term_store = long_term_store
        self.recall_top_k = recall_top_k
        self._active_request: TurnRequest | None = None

    async def prepare_turn(self, request: TurnRequest) -> PreparedMemory:
        request.actor.validate()
        request.scope.validate()
        history = await self.session_manager.prepare_active_history(request.session_key)
        working = None
        scope = request.scope
        if scope.realm == "customer_conversation" and self.customer_store is not None:
            state = self.customer_store.get(
                scope.tenant_id, scope.account_id, scope.conversation_id
            )
            if state is None:
                state = self.customer_store.put(
                    scope.tenant_id,
                    CustomerInquiryWorkingMemory(
                        account_id=scope.account_id or "",
                        conversation_id=scope.conversation_id or "",
                        intent="",
                        fields={},
                        missing_fields=[],
                        pending_confirmations=[],
                        inquiry_record_id=None,
                        version=0,
                    ),
                    expected_version=None,
                )
            working = asdict(state) if state is not None else None
        elif scope.realm == "workspace_private" and self.workspace_store is not None:
            working = self.workspace_store.get(
                scope.tenant_id, request.actor.actor_id, scope.project_id or "default",
                scope.conversation_id or request.request_id,
            )
            if working is None:
                working = self.workspace_store.put(
                    scope.tenant_id,
                    request.actor.actor_id,
                    scope.project_id or "default",
                    scope.conversation_id or request.request_id,
                    {
                        "goal": "",
                        "facts": {},
                        "pending_confirmations": [],
                    },
                    expected_version=None,
                )
        self._active_request = request
        recalled = ()
        if self.long_term_store is not None:
            recalled = tuple(self.long_term_store.search(
                request.actor, request.scope, request.current_message, self.recall_top_k
            ))
        return PreparedMemory(
            history=tuple(history), working_memory=working, recalled=recalled
        )

    async def observe_tool_result(self, event: ToolResultEvent) -> None:
        # M2 never upgrades arbitrary tool/model output into confirmed state.
        return None

    async def complete_turn(self, event: TurnCompletedEvent) -> None:
        self._active_request = None

    async def abort_turn(self, event: TurnAbortedEvent) -> None:
        self._active_request = None
