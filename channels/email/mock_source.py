"""Offline source for de-identified .eml fixtures."""

from pathlib import Path

from .contracts import EmailEnvelope
from .mime_parser import EmailParseLimits, parse_email


class MockEmailSource:
    def __init__(self, directory: str | Path, account_id: str = "mock-rfq", limits: EmailParseLimits | None = None):
        self.directory = Path(directory)
        self.account_id = account_id
        self.limits = limits

    def fetch_after(self, last_uid: int, limit: int = 50) -> list[EmailEnvelope]:
        paths = sorted(self.directory.glob("*.eml"))
        result = []
        for uid, path in enumerate(paths, 1):
            if uid <= last_uid:
                continue
            result.append(parse_email(path.read_bytes(), account_id=self.account_id, provider="mock",
                                      folder="fixtures", uidvalidity=1, uid=uid, limits=self.limits))
            if len(result) >= limit:
                break
        return result
