"""One-shot worker for approved trade outreach drafts.

The worker is deliberately not registered as an Agent tool and no polling loop
is started by the application.  Operators must first approve a draft and then
queue it through the separate protected command before this worker can claim it.
"""

from __future__ import annotations

from agent.business.trade_workbench_repository import TradeWorkbenchError
from channels.email.smtp_sender import SmtpDeliveryError


class TradeOutreachWorker:
    def __init__(self, repository, sender, *, worker_id: str):
        self.repository = repository
        self.sender = sender
        self.worker_id = worker_id

    def run_once(self) -> dict | None:
        claimed = self.repository.claim_outreach(self.worker_id)
        if claimed is None:
            return None
        command_id = claimed["command_id"]
        try:
            delivery, account = self.repository.revalidate_outreach(command_id, self.worker_id)
        except TradeWorkbenchError as exc:
            return self.repository.finish_outreach(
                command_id, self.worker_id, status="stale", error_code=exc.code,
            )
        try:
            self.sender.send(delivery, account)
        except SmtpDeliveryError as exc:
            if exc.outcome_unknown:
                status = "outcome_unknown"
            elif exc.permanent or int(claimed["attempt_count"]) >= int(claimed["max_attempts"]):
                status = "dead_letter"
            else:
                status = "retry_wait"
            return self.repository.finish_outreach(
                command_id, self.worker_id, status=status, error_code=exc.code,
            )
        return self.repository.finish_outreach(
            command_id, self.worker_id, status="accepted", error_code=None,
        )


def run_outreach_batch(worker: TradeOutreachWorker, batch_size: int) -> list[dict]:
    results: list[dict] = []
    for _ in range(max(0, min(int(batch_size), 100))):
        result = worker.run_once()
        if result is None:
            break
        results.append(result)
    return results


def create_default_trade_outreach_worker(*, worker_id: str,
                                         timeout_seconds: int = 20) -> TradeOutreachWorker:
    """Assemble the existing SMTP adapter; callers still control whether it runs."""
    from agent.business.email_secret_store import create_default_email_secret_store
    from agent.business.trade_workbench_repository import create_trade_workbench_repository
    from channels.email.smtp_sender import SmtpSslSender
    return TradeOutreachWorker(
        create_trade_workbench_repository(),
        SmtpSslSender(create_default_email_secret_store(), timeout_seconds=timeout_seconds),
        worker_id=worker_id,
    )
