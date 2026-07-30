"""CLI entrypoint for one-shot mock/IMAP ingestion and human review."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent.business.email_config import load_email_config
from agent.business.email_ingestion import EmailIngestionService
from agent.business.email_repository import EmailRepository
from agent.business.rfq_extractor import validate_rfq_v2

from .imap_source import ImapEmailSource
from .mock_source import MockEmailSource


async def _ingest_mock(path: str) -> None:
    source = MockEmailSource(path)
    config = load_email_config()
    config.validate_qq_notification()
    service = EmailIngestionService(EmailRepository(), qq_target_id=config.qq_target_id if config.qq_notify_enabled else "",
                                    qq_target_type=config.qq_target_type)
    print(json.dumps(await service.poll_once(source, source.account_id, "fixtures"), ensure_ascii=False, indent=2))


async def _ingest_imap() -> None:
    config = load_email_config()
    if not config.enabled:
        raise SystemExit("NANOCLAW_EMAIL_ENABLED is false")
    source = ImapEmailSource(config.imap_settings(), config.limits())
    config.validate_qq_notification()
    service = EmailIngestionService(EmailRepository(), qq_target_id=config.qq_target_id if config.qq_notify_enabled else "",
                                    qq_target_type=config.qq_target_type)
    print(json.dumps(await service.poll_once(source, config.account_id, config.folder), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only email RFQ ingestion")
    sub = parser.add_subparsers(dest="command", required=True)
    mock = sub.add_parser("ingest-mock")
    mock.add_argument("directory")
    sub.add_parser("ingest-imap")
    review = sub.add_parser("list-review")
    review.add_argument("--limit", type=int, default=20)
    confirm = sub.add_parser("confirm")
    confirm.add_argument("email_id", type=int)
    confirm.add_argument("--reviewer", required=True)
    confirm.add_argument("--result-json", help="UTF-8 RFQ v2 JSON reviewed by the operator")
    args = parser.parse_args()
    if args.command == "ingest-mock":
        asyncio.run(_ingest_mock(args.directory))
    elif args.command == "ingest-imap":
        asyncio.run(_ingest_imap())
    elif args.command == "list-review":
        print(json.dumps(EmailRepository().list_reviews(args.limit), ensure_ascii=False, indent=2))
    else:
        repository = EmailRepository()
        reviewed = None
        if args.result_json:
            reviewed = json.loads(Path(args.result_json).read_text(encoding="utf-8"))
            row = repository.get(args.email_id)
            if not row:
                raise SystemExit("email not found")
            envelope = json.loads(row["envelope_json"])
            reviewed = validate_rfq_v2(reviewed, subject=envelope.get("subject", ""),
                                       body=envelope.get("text_body", ""),
                                       from_address=envelope.get("from_address", ""))
        print(json.dumps(repository.confirm(args.email_id, args.reviewer, reviewed), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
