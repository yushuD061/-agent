"""Deterministic, model-independent memory access policy."""

from __future__ import annotations

from .errors import MemoryAccessDenied
from .models import ActorContext, MemoryScope


class MemoryPolicy:
    CUSTOMER_READ_PURPOSES = frozenset(
        {"customer_support", "rfq_review", "complaint_resolution"}
    )

    def require_search(self, actor: ActorContext, scope: MemoryScope) -> None:
        """Authorize a search without exposing whether a target exists."""
        try:
            actor.validate()
            scope.validate()
        except ValueError:
            self._deny()
        if actor.tenant_id != scope.tenant_id:
            self._deny()

        if actor.actor_kind == "customer":
            self._require_customer(actor, scope)
        elif actor.actor_kind == "workspace_operator":
            self._require_workspace_operator(actor, scope)
        else:
            self._require_service(actor, scope)

    def require_workspace_customer_read(
        self, actor: ActorContext, purpose: str
    ) -> None:
        if (
            actor.actor_kind != "workspace_operator"
            or not actor.authenticated
            or "customer_memory_reader" not in actor.roles
            or purpose not in self.CUSTOMER_READ_PURPOSES
        ):
            self._deny()

    def _require_customer(self, actor: ActorContext, scope: MemoryScope) -> None:
        if scope.realm == "workspace_private":
            self._deny()
        if scope.realm in {"customer_private", "customer_conversation"} and (
            not actor.authenticated or actor.actor_id != scope.account_id
        ):
            self._deny()

    def _require_workspace_operator(
        self, actor: ActorContext, scope: MemoryScope
    ) -> None:
        if not actor.authenticated:
            self._deny()
        if scope.realm in {"customer_private", "customer_conversation"}:
            self.require_workspace_customer_read(actor, scope.purpose)
        elif scope.realm == "workspace_private" and not (
            {"workspace_memory_reader", "workspace_memory_writer"} & actor.roles
        ):
            self._deny()

    def _require_service(self, actor: ActorContext, scope: MemoryScope) -> None:
        allowed_roles = {
            "workspace_private": "workspace_memory_service",
            "customer_private": "customer_memory_service",
            "customer_conversation": "customer_memory_service",
            "public_approved": "public_memory_service",
        }
        if not actor.authenticated or allowed_roles[scope.realm] not in actor.roles:
            self._deny()

    @staticmethod
    def _deny() -> None:
        raise MemoryAccessDenied()

