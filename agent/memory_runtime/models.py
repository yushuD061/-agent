"""Domain contracts shared by workspace and customer memory runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import MemoryScopeInvalid


ActorKind = Literal["customer", "workspace_operator", "service"]
MemoryRealm = Literal[
    "workspace_private",
    "customer_private",
    "customer_conversation",
    "public_approved",
]
MemoryType = Literal["semantic", "episodic", "procedural"]
MemoryStatus = Literal[
    "pending_consent", "pending_confirmation", "active", "superseded", "invalid", "deleted"
]
Sensitivity = Literal["public", "customer_private", "internal", "restricted"]


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryScopeInvalid(f"memory_scope_invalid:{field_name}")


@dataclass(frozen=True)
class ActorContext:
    """Trusted actor assembled by authentication or an in-process service."""

    actor_kind: ActorKind
    actor_id: str
    tenant_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    authenticated: bool = False

    def validate(self) -> None:
        if self.actor_kind not in {"customer", "workspace_operator", "service"}:
            raise MemoryScopeInvalid("memory_scope_invalid:actor_kind")
        _require_text(self.actor_id, "actor_id")
        _require_text(self.tenant_id, "tenant_id")


@dataclass(frozen=True)
class MemoryScope:
    realm: MemoryRealm
    tenant_id: str
    account_id: str | None = None
    subject_id: str | None = None
    project_id: str | None = None
    conversation_id: str | None = None
    purpose: str = ""

    def validate(self) -> None:
        if self.realm not in {
            "workspace_private",
            "customer_private",
            "customer_conversation",
            "public_approved",
        }:
            raise MemoryScopeInvalid("memory_scope_invalid:realm")
        _require_text(self.tenant_id, "tenant_id")

        if self.realm == "customer_private":
            _require_text(self.account_id or "", "account_id")
            if self.project_id is not None or self.subject_id is not None:
                raise MemoryScopeInvalid("memory_scope_invalid:customer_private")
        elif self.realm == "customer_conversation":
            _require_text(self.account_id or "", "account_id")
            _require_text(self.conversation_id or "", "conversation_id")
            if self.project_id is not None or self.subject_id is not None:
                raise MemoryScopeInvalid("memory_scope_invalid:customer_conversation")
        elif self.realm == "workspace_private":
            if not (self.subject_id or self.project_id):
                raise MemoryScopeInvalid("memory_scope_invalid:workspace_owner")
            if self.account_id is not None:
                raise MemoryScopeInvalid("memory_scope_invalid:workspace_account")
        elif any(
            value is not None
            for value in (
                self.account_id,
                self.subject_id,
                self.project_id,
                self.conversation_id,
            )
        ):
            raise MemoryScopeInvalid("memory_scope_invalid:public_subject")


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    scope: MemoryScope
    memory_type: MemoryType
    content: str
    summary: str
    source_refs: tuple[str, ...]
    status: MemoryStatus
    confidence: float
    importance: float
    sensitivity: Sensitivity
    consent_record_id: str | None
    version: int
    supersedes: str | None
    created_at: str
    updated_at: str
    valid_from: str
    expires_at: str | None
    content_hash: str
    embedding_model: str | None

    def validate(self) -> None:
        self.scope.validate()
        _require_text(self.memory_id, "memory_id")
        _require_text(self.content, "content")
        _require_text(self.content_hash, "content_hash")
        if self.memory_type not in {"semantic", "episodic", "procedural"}:
            raise MemoryScopeInvalid("memory_scope_invalid:memory_type")
        if self.status not in {
            "pending_consent", "pending_confirmation", "active", "superseded",
            "invalid", "deleted"
        }:
            raise MemoryScopeInvalid("memory_scope_invalid:status")
        if not 0.0 <= self.confidence <= 1.0 or not 0.0 <= self.importance <= 1.0:
            raise MemoryScopeInvalid("memory_scope_invalid:score")
        if self.version < 1:
            raise MemoryScopeInvalid("memory_scope_invalid:version")
        if (
            self.scope.realm in {"customer_private", "customer_conversation"}
            and self.status == "active"
            and not self.consent_record_id
        ):
            raise MemoryScopeInvalid("memory_scope_invalid:customer_consent")


@dataclass(frozen=True)
class SourcedValue:
    value: Any
    state: Literal["pending", "confirmed"]
    source_message_id: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceMemoryValue:
    """A scoped working-memory value with provenance and confirmation state."""

    value: Any
    state: Literal["pending", "confirmed"]
    source_ref: str
    updated_at: str


@dataclass
class WorkspaceWorkingMemory:
    """Fixed workspace workbench described by doc/记忆管理.md."""

    goal: WorkspaceMemoryValue | None = None
    confirmed_facts: dict[str, WorkspaceMemoryValue] = field(default_factory=dict)
    confirmed_conclusions: list[WorkspaceMemoryValue] = field(default_factory=list)
    pending_hypotheses: list[WorkspaceMemoryValue] = field(default_factory=list)
    pending_actions: list[WorkspaceMemoryValue] = field(default_factory=list)
    completed_actions: list[WorkspaceMemoryValue] = field(default_factory=list)
    version: int = 0


@dataclass
class CustomerInquiryWorkingMemory:
    account_id: str
    conversation_id: str
    intent: str
    fields: dict[str, SourcedValue]
    missing_fields: list[str]
    pending_confirmations: list[str]
    inquiry_record_id: str | None
    version: int


@dataclass(frozen=True)
class MemoryConsent:
    consent_record_id: str
    tenant_id: str
    account_id: str
    purpose: str
    categories: tuple[str, ...]
    status: Literal["active", "withdrawn", "expired"]
    granted_at: str
    expires_at: str | None = None
    withdrawn_at: str | None = None


@dataclass(frozen=True)
class MemoryHit:
    item: MemoryItem
    score: float
    explanation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CustomerMemoryReadModel:
    """Minimized internal view returned by the one-way M4 reader."""

    memory_id: str
    memory_type: MemoryType
    summary: str
    confidence: float
    valid_from: str


@dataclass(frozen=True)
class DeletionJob:
    job_id: str
    request_id: str
    status: str


@dataclass(frozen=True)
class MemoryExport:
    scope: MemoryScope
    items: tuple[MemoryItem, ...]


@dataclass(frozen=True)
class PreparedMemory:
    history: tuple[dict[str, Any], ...] = ()
    working_memory: dict[str, Any] | None = None
    recalled: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class TurnRequest:
    request_id: str
    actor: ActorContext
    scope: MemoryScope
    current_message: str
    history: tuple[dict[str, Any], ...] = ()
    session_key: str = ""


@dataclass(frozen=True)
class ToolResultEvent:
    request_id: str
    tool_name: str
    result: Any
    tool_call_id: str = ""
    source_ref: str = ""


@dataclass(frozen=True)
class TurnCompletedEvent:
    request_id: str
    response: str
    source_ref: str = ""


@dataclass(frozen=True)
class TurnMemoryOutcome:
    """Non-authoritative notices produced after a completed turn."""

    candidate_ids: tuple[str, ...] = ()
    notice: str = ""


@dataclass(frozen=True)
class TurnAbortedEvent:
    request_id: str
    error_code: str
