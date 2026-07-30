"""Executable M5 local closed-loop acceptance with SQLite and mock SMTP.

This runner is deliberately isolated from the configured business database and
never opens an IMAP or SMTP connection. It proves component wiring, not real
mailbox interoperability.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.business.email_account_repository import EmailAccountRepository
from agent.business.email_account_service import EmailAccountService
from agent.business.email_delivery_repository import EmailDeliveryRepository, canonical_quote_hash
from agent.business.email_delivery_service import EmailDeliveryService
from agent.business.email_secret_store import MemoryEmailSecretStore
from agent.workflow import WorkflowService
from bus import MessageBus
from channels.email.delivery_worker import EmailDeliveryWorker
from channels.email.mock_smtp_sender import MockSmtpSender
from channels.web import WebChannel
from session.conversation import ConversationService


_PROVIDER_ADDRESSES = {
    "qq": "m5-sales@qq.com",
    "netease_163": "m5-sales@163.com",
    "netease_126": "m5-sales@126.com",
}
_ADMIN_TOKEN = "m5-local-acceptance-token"
_AUTH_CODE = "m5-memory-only-authorization-code"
_RECIPIENT = "m5-buyer@example.test"


class NoNetworkConnectionTester:
    """Marks the configured account healthy without touching the network."""

    def __init__(self):
        self.calls: list[str] = []

    def test(self, account: dict, auth_code: str) -> None:
        if auth_code != _AUTH_CODE:
            raise RuntimeError("m5_mock_credential_mismatch")
        self.calls.append(account["account_id"])


def _quote_payload() -> dict:
    return {
        "version": 1,
        "items": [{
            "product_sku": "M5-DEMO-001",
            "product_name_en": "De-identified demo component",
            "quantity": 10,
            "unit_price_usd": 12.5,
            "total_price_usd": 125.0,
        }],
        "subtotal_usd": 125.0,
        "discount_percent": 0.0,
        "discount_amount": 0.0,
        "packaging_cost_usd": 0.0,
        "freight_cost_usd": 0.0,
        "total_usd": 125.0,
        "valid_until": "2099-12-31",
        "payment_terms": "T/T",
        "delivery_term": "FOB Test Port",
        "remarks_en": "M5 local acceptance fixture. No real customer data.",
    }


def _init_trade_fixture(connection: sqlite3.Connection) -> tuple[int, int]:
    connection.executescript("""
    CREATE TABLE quotes(
      id INTEGER PRIMARY KEY,rfq_id INTEGER,status TEXT,current_version INTEGER,
      version_data TEXT,created_at TEXT
    );
    CREATE TABLE approval_records(
      id INTEGER PRIMARY KEY,quote_id INTEGER,version INTEGER,status TEXT,reviewer TEXT,
      comment TEXT,content_hash TEXT,created_at TEXT,decided_at TEXT
    );
    """)
    quote_id, approval_key = 5001, 7001
    version = _quote_payload()
    digest = canonical_quote_hash(version)
    connection.execute(
        "INSERT INTO quotes VALUES(?,1,'approved',1,?,'2026-07-23T00:00:00Z')",
        (quote_id, json.dumps([version], ensure_ascii=False)),
    )
    connection.execute(
        "INSERT INTO approval_records VALUES(?,?,1,'approved','m5-local-reviewer','fixture',?,"
        "'2026-07-23T00:01:00Z','2026-07-23T00:02:00Z')",
        (approval_key, quote_id, digest),
    )
    connection.commit()
    return quote_id, approval_key


def _run_in_directory(root: Path, provider: str) -> dict:
    if provider not in _PROVIDER_ADDRESSES:
        raise ValueError("email_invalid_provider")
    database_path = root / "m5-email-acceptance.sqlite3"
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    account_repository = EmailAccountRepository(connection)
    quote_id, approval_key = _init_trade_fixture(connection)
    secret_store = MemoryEmailSecretStore()
    connection_tester = NoNetworkConnectionTester()
    account_service = EmailAccountService(account_repository, secret_store, connection_tester)
    delivery_repository = EmailDeliveryRepository(connection)
    delivery_service = EmailDeliveryService(delivery_repository)
    channel = WebChannel(
        MessageBus(),
        conversation_service=ConversationService(root / "sessions"),
        workflow_service=WorkflowService(root / "workflows"),
        email_admin_token=_ADMIN_TOKEN,
        email_account_service=account_service,
        email_delivery_service=delivery_service,
    )
    channel._app = FastAPI(title="NanoClaw M5 local acceptance")
    channel._register_routes()
    headers = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}

    with TestClient(channel._app) as client:
        created_response = client.post("/api/email/accounts", headers=headers, json={
            "display_name": f"M5 {provider} mock account",
            "provider": provider,
            "address": _PROVIDER_ADDRESSES[provider],
            "auth_code": _AUTH_CODE,
            "inbound_enabled": True,
            "outbound_enabled": True,
            "poll_seconds": 60,
            "sender_name": "NanoClaw M5",
            "allowed_senders": [],
            "allowed_recipients": [_RECIPIENT],
        })
        if created_response.status_code != 201:
            raise RuntimeError(f"m5_account_create_failed:{created_response.status_code}")
        account = created_response.json()
        account_id = account["account_id"]
        enabled = client.post(
            f"/api/email/accounts/{account_id}/enable", headers=headers
        )
        tested = client.post(
            f"/api/email/accounts/{account_id}/test", headers=headers
        )
        if enabled.status_code != 200 or tested.status_code != 200:
            raise RuntimeError("m5_mock_connection_test_failed")
        if tested.json()["status"] != "healthy":
            raise RuntimeError("m5_account_not_healthy")

        sendable = client.get("/api/email/sendable-quotes", headers=headers)
        if sendable.status_code != 200 or not sendable.json().get("items"):
            raise RuntimeError("m5_sendable_quote_missing")
        request = {
            "account_id": account_id,
            "quote_id": quote_id,
            "quote_version": 1,
            "approval_key": approval_key,
            "recipient": _RECIPIENT,
        }
        queued_response = client.post("/api/email/deliveries", headers=headers, json=request)
        duplicate_response = client.post("/api/email/deliveries", headers=headers, json=request)
        if queued_response.status_code != 200 or duplicate_response.status_code != 200:
            raise RuntimeError("m5_delivery_queue_failed")
        queued = queued_response.json()
        duplicate = duplicate_response.json()
        if queued["delivery_id"] != duplicate["delivery_id"]:
            raise RuntimeError("m5_delivery_idempotency_failed")

        mock_sender = MockSmtpSender()
        accepted_row = EmailDeliveryWorker(
            delivery_repository, mock_sender, worker_id="m5-local-worker"
        ).run_once()
        if accepted_row is None or accepted_row["status"] != "accepted":
            raise RuntimeError("m5_mock_smtp_not_accepted")
        listed_response = client.get("/api/email/deliveries", headers=headers)
        listed = listed_response.json().get("items", [])
        record = next(
            (item for item in listed if item["delivery_id"] == queued["delivery_id"]), None
        )
        if record is None or record["status"] != "accepted":
            raise RuntimeError("m5_delivery_api_not_accepted")

    database_dump = "\n".join(connection.iterdump())
    connection.close()
    return {
        "milestone": "M5",
        "provider": provider,
        "account_id": account_id,
        "account_status": "healthy",
        "quote_id": quote_id,
        "approval_key": approval_key,
        "delivery_id": queued["delivery_id"],
        "duplicate_delivery_id": duplicate["delivery_id"],
        "delivery_status": record["status"],
        "attempt_count": record["attempt_count"],
        "recipient_masked": record["recipient_masked"],
        "mock_smtp_accept_count": len(mock_sender.accepted),
        "connection_test_count": len(connection_tester.calls),
        "credential_in_database": _AUTH_CODE in database_dump,
        "real_network_used": False,
    }


def run_email_m5_acceptance(provider: str = "qq", workspace: Path | None = None) -> dict:
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
        return _run_in_directory(workspace, provider)
    with tempfile.TemporaryDirectory(prefix="nanoclaw-email-m5-") as directory:
        return _run_in_directory(Path(directory), provider)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the no-network M5 email acceptance")
    parser.add_argument(
        "--provider", choices=tuple(_PROVIDER_ADDRESSES), default="qq",
        help="provider preset to exercise against the mock SMTP adapter",
    )
    args = parser.parse_args()
    result = run_email_m5_acceptance(args.provider)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["delivery_status"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())

