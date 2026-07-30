"""M6 delivery metrics and explicit retention redaction command."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from agent.business.config import load_business_config
from agent.business.email_delivery_service import create_default_email_delivery_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect email delivery metrics or redact expired content")
    parser.add_argument("--metrics", action="store_true", help="print privacy-safe delivery metrics")
    parser.add_argument("--retention-days", type=int,
                        default=int(os.environ.get("NANOCLAW_EMAIL_DELIVERY_RETENTION_DAYS", "90")))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--apply-retention", action="store_true",
                        help="apply terminal-content redaction; without this flag the command is a dry run")
    args = parser.parse_args()
    if not 1 <= args.retention_days <= 3650 or not 1 <= args.limit <= 5000:
        parser.error("retention-days must be 1..3650 and limit must be 1..5000")
    service = create_default_email_delivery_service()
    repository = service.repository
    result: dict = {"backend": load_business_config().database_backend}
    if args.metrics:
        result["metrics"] = repository.metrics()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=args.retention_days)
    cutoff = (cutoff_dt.replace(tzinfo=None) if result["backend"] == "mysql"
              else cutoff_dt.isoformat().replace("+00:00", "Z"))
    actor = os.environ.get("NANOCLAW_EMAIL_GOVERNANCE_ACTOR", "").strip()
    if args.apply_retention and not actor:
        parser.error("NANOCLAW_EMAIL_GOVERNANCE_ACTOR is required with --apply-retention")
    result["retention"] = {
        "apply": args.apply_retention,
        "retention_days": args.retention_days,
        "candidate_count": repository.redact_terminal_content(
            cutoff, actor=actor or "email_governance_dry_run", limit=args.limit,
            apply=args.apply_retention,
        ),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

