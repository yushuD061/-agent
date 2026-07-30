"""Automatic read-only ingestion for accounts saved in the email workspace."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from agent.business.config import load_business_config
from agent.business.email_account_repository import EmailAccountRepository
from agent.business.email_account_service import _stable_connection_error
from agent.business.email_config import EmailIngestionConfig
from agent.business.email_ingestion import EmailIngestionService
from agent.business.email_repository import EmailRepository
from agent.business.email_secret_store import EmailSecretStore, create_default_email_secret_store
from agent.business.email_trade_classifier import classify_trade_rfq_email
from agent.business.rfq_extractor import (
    DETERMINISTIC_RULE_VERSION,
    deterministic_extract_rfq_fields,
    extract_rfq_fields,
)
from channels.email.imap_source import ImapEmailSource, ImapSettings


async def _deterministic_extractor(document: str, context: dict) -> dict:
    return deterministic_extract_rfq_fields(document, context)


class ManagedEmailIngestionRuntime:
    def __init__(self, config: EmailIngestionConfig, *,
                 account_repository: EmailAccountRepository | None = None,
                 email_repository: EmailRepository | None = None,
                 secret_store: EmailSecretStore | None = None,
                 source_factory: Callable = ImapEmailSource,
                 clock: Callable[[], float] = time.monotonic):
        if load_business_config().database_backend != "sqlite" and (
                account_repository is None or email_repository is None):
            raise RuntimeError("managed_email_ingestion_backend_not_supported")
        self.config = config
        self.account_repository = account_repository or EmailAccountRepository()
        self.email_repository = email_repository or EmailRepository()
        self.secret_store = secret_store or create_default_email_secret_store()
        self.source_factory = source_factory
        self.clock = clock
        self._next_due: dict[str, float] = {}
        self.worker_id = f"managed-email-{uuid.uuid4()}"

    def eligible_accounts(self) -> list[dict]:
        return [account for account in self.account_repository.list_active()
                if account["inbound_enabled"] and account["status"] in {"healthy", "validating", "degraded"}]

    async def poll_due(self) -> list[dict]:
        now = self.clock()
        results: list[dict] = []
        active_ids = set()
        for account in self.eligible_accounts():
            account_id = account["account_id"]
            active_ids.add(account_id)
            if now < self._next_due.get(account_id, 0):
                continue
            self._next_due[account_id] = now + max(30, min(int(account["poll_seconds"]), 3600))
            try:
                auth_code = self.secret_store.get(account["secret_ref"])
                settings = ImapSettings(
                    provider=account["provider"], account_id=account_id,
                    address=account["address"], auth_code=auth_code,
                    folder=account["folder"], timeout_seconds=30,
                )
                source = self.source_factory(settings, self.config.limits())
                remote = self.config.remote_extraction_approved
                service = EmailIngestionService(
                    self.email_repository,
                    extractor=extract_rfq_fields if remote else _deterministic_extractor,
                    worker_id=f"{self.worker_id}:{account_id}",
                    extraction_timeout_seconds=self.config.extraction_timeout_seconds,
                    message_filter=lambda envelope, item=account: classify_trade_rfq_email(
                        envelope, mailbox_address=item["address"],
                        allowed_senders=tuple(item["allowed_senders"]),
                    ),
                    extractor_mode="llm_full" if remote else "deterministic_local",
                    extractor_version="configured-model" if remote else DETERMINISTIC_RULE_VERSION,
                )
                messages = await service.poll_once(source, account_id, account["folder"])
                if account["status"] != "healthy":
                    self.account_repository.record_health(
                        account_id, "healthy", None, actor="managed_ingestion_recovery"
                    )
                results.append({"account_id": account_id, "status": "polled", "messages": messages})
            except Exception as exc:
                code, status = _stable_connection_error(exc)
                self.account_repository.record_health(
                    account_id, status.value, code, actor="managed_ingestion_recovery"
                )
                results.append({"account_id": account_id, "status": "error",
                                "error_code": code})
        self._next_due = {key: value for key, value in self._next_due.items() if key in active_ids}
        return results
