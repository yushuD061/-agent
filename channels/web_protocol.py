"""Versioned WebSocket contracts and legacy response compatibility helpers."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


PROTOCOL_VERSION = 2


class WebSocketEventV2(BaseModel):
    """Base contract for server-to-browser WebSocket events."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    protocol_version: Literal[2] = PROTOCOL_VERSION


class AssistantMessageEvent(WebSocketEventV2):
    """A completed assistant response."""

    type: Literal["assistant.message"] = "assistant.message"
    content: str
    conversation_id: str | None = None
    request_id: str | None = None


def encode_assistant_message(content: str, *, conversation_id: str | None = None,
                             request_id: str | None = None) -> str:
    """Serialize a response using the frozen v2 envelope."""
    return AssistantMessageEvent(
        content=content, conversation_id=conversation_id, request_id=request_id,
    ).model_dump_json(exclude_none=True)


def decode_assistant_message(payload: str) -> str:
    """Read v2 responses while preserving legacy plain-text compatibility."""
    try:
        event = AssistantMessageEvent.model_validate_json(payload)
    except (ValueError, TypeError):
        return payload
    return event.content


def event_schema() -> dict[str, Any]:
    """Expose the contract schema for documentation and contract checks."""
    return AssistantMessageEvent.model_json_schema()
