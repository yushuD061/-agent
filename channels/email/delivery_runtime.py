"""Runtime assembly for the real SMTP worker.

This module is deliberately not an Agent tool. It only consumes delivery rows
that already passed the protected approval, version, hash, account, and
recipient allowlist gates.
"""

from __future__ import annotations

from agent.business.email_delivery_service import create_default_email_delivery_service
from agent.business.email_secret_store import create_default_email_secret_store
from channels.email.delivery_worker import EmailDeliveryWorker
from channels.email.smtp_sender import SmtpSslSender


def create_default_email_delivery_worker(*, worker_id: str,
                                         timeout_seconds: int = 20) -> EmailDeliveryWorker:
    service = create_default_email_delivery_service()
    sender = SmtpSslSender(
        create_default_email_secret_store(), timeout_seconds=timeout_seconds
    )
    return EmailDeliveryWorker(service.repository, sender, worker_id=worker_id)


def run_delivery_batch(worker: EmailDeliveryWorker, batch_size: int) -> list[dict]:
    """Process at most ``batch_size`` rows and stop immediately when idle."""
    results: list[dict] = []
    for _ in range(batch_size):
        result = worker.run_once()
        if result is None:
            break
        results.append(result)
    return results
