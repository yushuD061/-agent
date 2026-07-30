"""No-network SMTP test double for the explicit M5 local acceptance path."""

from __future__ import annotations


class MockSmtpSender:
    """Accepts a delivery in memory without opening a socket.

    Only non-content identifiers are retained so the acceptance report cannot
    accidentally become another store for recipients or message bodies.
    """

    def __init__(self):
        self.accepted: list[dict] = []

    def send(self, delivery: dict, account: dict) -> str:
        receipt = {
            "delivery_id": delivery["delivery_id"],
            "account_id": account["account_id"],
            "provider": account["provider"],
            "message_id": delivery["smtp_message_id"],
        }
        self.accepted.append(receipt)
        return delivery["smtp_message_id"]

