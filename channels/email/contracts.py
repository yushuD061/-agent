"""Provider-neutral contracts for read-only inbound email."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AttachmentMeta:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    processing_status: str = "not_processed"


@dataclass(frozen=True)
class EmailEnvelope:
    account_id: str
    provider: str
    provider_message_id: str
    folder: str
    uidvalidity: int
    uid: int
    internet_message_id: str = ""
    thread_id: str = ""
    from_name: str = ""
    from_address: str = ""
    reply_to: tuple[str, ...] = ()
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    subject: str = ""
    sent_at: str = ""
    received_at: str = ""
    text_body: str = ""
    html_body_hash: str = ""
    attachments: tuple[AttachmentMeta, ...] = field(default_factory=tuple)
    raw_sha256: str = ""

    @property
    def idempotency_key(self) -> str:
        return f"{self.account_id}:{self.folder}:{self.uidvalidity}:{self.uid}"

    def to_dict(self) -> dict:
        return asdict(self)


class EmailSource(Protocol):
    def fetch_after(self, last_uid: int, limit: int = 50) -> list[EmailEnvelope]: ...
