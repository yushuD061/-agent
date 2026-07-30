"""Single-step M4 delivery worker suitable for a supervised polling loop."""

from __future__ import annotations

from agent.business.email_delivery_repository import EmailDeliveryRepository, EmailDeliveryRepositoryError
from channels.email.smtp_sender import SmtpDeliveryError


class EmailDeliveryWorker:
    def __init__(self, repository: EmailDeliveryRepository, sender, *, worker_id: str):
        self.repository = repository
        self.sender = sender
        self.worker_id = worker_id

    def run_once(self) -> dict | None:
        claimed = self.repository.claim_delivery(self.worker_id)
        if claimed is None:
            return None
        try:
            delivery, account = self.repository.revalidate_claim(claimed["delivery_id"], self.worker_id)
        except EmailDeliveryRepositoryError as exc:
            return self.repository.mark_stale(claimed["delivery_id"], self.worker_id, str(exc))
        try:
            internet_message_id = self.sender.send(delivery, account)
        except SmtpDeliveryError as exc:
            return self.repository.fail_delivery(
                delivery["delivery_id"], self.worker_id, exc.code,
                permanent=exc.permanent, outcome_unknown=exc.outcome_unknown,
            )
        return self.repository.mark_smtp_accepted(
            delivery["delivery_id"], self.worker_id, internet_message_id
        )

