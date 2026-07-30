"""Idempotent legacy Web conversation index migration."""

import argparse
import json
from pathlib import Path
from typing import Iterable

from session.conversation import ConversationService


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index legacy Web JSONL files without rewriting them.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report anonymous counts only")
    mode.add_argument("--apply", action="store_true", help="atomically update the metadata index")
    parser.add_argument("--sessions-dir", type=Path, default=Path("workspace/sessions"))
    args = parser.parse_args(argv)
    service = ConversationService(args.sessions_dir, auto_index_legacy=False)
    result = service.index_legacy_web_sessions(dry_run=args.dry_run)
    print(json.dumps({"mode": "dry-run" if args.dry_run else "apply", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
