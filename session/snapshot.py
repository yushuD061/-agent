"""Privacy-safe, read-only pre-migration snapshot for JSONL sessions."""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SnapshotSummary:
    file_count: int = 0
    valid_file_count: int = 0
    invalid_file_count: int = 0
    valid_record_count: int = 0
    invalid_record_count: int = 0


def inspect_sessions(sessions_dir: Path) -> SnapshotSummary:
    """Validate JSONL structure without retaining or returning message bodies."""
    files = sorted(sessions_dir.glob("*.jsonl")) if sessions_dir.is_dir() else []
    valid_files = invalid_files = valid_records = invalid_records = 0
    for path in files:
        file_valid = True
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("record_not_object")
                        valid_records += 1
                    except (json.JSONDecodeError, ValueError):
                        invalid_records += 1
                        file_valid = False
        except (OSError, UnicodeError):
            file_valid = False
        if file_valid:
            valid_files += 1
        else:
            invalid_files += 1
    return SnapshotSummary(len(files), valid_files, invalid_files, valid_records, invalid_records)


def render_summary(summary: SnapshotSummary) -> str:
    """Return anonymous aggregate JSON only."""
    return json.dumps({"mode": "dry-run", **asdict(summary)}, ensure_ascii=True, sort_keys=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate session JSONL files without printing content.")
    parser.add_argument("--dry-run", action="store_true", help="required; never modifies session files")
    parser.add_argument("--sessions-dir", type=Path, default=Path("workspace/sessions"))
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("--dry-run is required")
    print(render_summary(inspect_sessions(args.sessions_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
