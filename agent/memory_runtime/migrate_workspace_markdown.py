"""Dry-run/apply migration from legacy MEMORY.md to pending workspace candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent.memory_runtime.models import ActorContext, MemoryScope
from agent.memory_runtime.services.workspace_memory import (
    WorkspaceMemoryService,
    stable_project_id,
)
from agent.memory_runtime.stores.sqlite import WorkspaceSQLiteMemoryStore
from config import load_config


def parse_markdown(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    items = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("- ", "* ")):
            text = text[2:].strip()
        if not text or text in {"---", "```"}:
            continue
        items.append({"content": text, "line": line_number})
    return items


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_snapshot(workspace: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = workspace / "workspace" / "memory" / "migration_backups" / stamp
    target.mkdir(parents=True, exist_ok=False)
    manifest = {"created_at": stamp, "files": []}
    candidates = [
        workspace / "workspace" / "memory" / "MEMORY.md",
        workspace / "workspace" / "memory" / "HISTORY.md",
        workspace / "workspace" / "memory" / "workspace_memory.db",
    ]
    sessions = workspace / "workspace" / "sessions"
    if sessions.is_dir():
        session_target = target / "sessions"
        shutil.copytree(sessions, session_target)
        for path in sorted(session_target.rglob("*")):
            if path.is_file():
                manifest["files"].append({
                    "path": str(path.relative_to(target)).replace("\\", "/"),
                    "sha256": _sha256(path), "bytes": path.stat().st_size,
                })
    for source in candidates:
        if source.is_file():
            destination = target / source.name
            if source.suffix == ".db":
                # SQLite's backup API produces a transactionally consistent
                # snapshot even when the local Agent has the database open.
                source_db = sqlite3.connect(source)
                target_db = sqlite3.connect(destination)
                try:
                    source_db.backup(target_db)
                finally:
                    target_db.close()
                    source_db.close()
            else:
                shutil.copy2(source, destination)
            manifest["files"].append({
                "path": destination.name, "sha256": _sha256(destination),
                "bytes": destination.stat().st_size,
            })
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8",
    )
    return target


def run(*, apply: bool, config_path: str = "config.json") -> dict:
    config = load_config(config_path)
    workspace = Path(config.workspace).resolve()
    legacy = workspace / "workspace" / "memory" / "MEMORY.md"
    entries = parse_markdown(legacy)
    result = {
        "mode": "apply" if apply else "dry_run", "candidate_count": len(entries),
        "legacy_sha256": _sha256(legacy) if legacy.is_file() else None,
        "project_id": stable_project_id(str(workspace)), "created": 0,
    }
    if not apply:
        return result
    result["snapshot"] = str(create_snapshot(workspace))
    store = WorkspaceSQLiteMemoryStore(
        workspace / "workspace" / "memory" / "workspace_memory.db",
        indexing_enabled=False,
    )
    service = WorkspaceMemoryService(store)
    actor = ActorContext(
        "workspace_operator", config.workspace_operator_id or "local",
        config.workspace_operator_tenant_id or "default",
        frozenset({"workspace_memory_reader", "workspace_memory_writer"}), True,
    )
    scope = MemoryScope(
        "workspace_private", actor.tenant_id, subject_id=actor.actor_id,
        project_id=result["project_id"], purpose="project_assistance",
    )
    before_count = store.count_scope(actor, scope)
    before = legacy.read_bytes() if legacy.is_file() else b""
    for entry in entries:
        service.create_candidate(
            actor, scope, content=entry["content"], summary=entry["content"][:1000],
            memory_type="semantic",
            source_refs=(f"legacy:workspace/memory/MEMORY.md#L{entry['line']}",),
            confidence=0.5, importance=0.5, sensitivity="internal",
        )
    result["created"] = max(
        0, store.count_scope(actor, scope) - before_count,
    )
    if legacy.is_file() and legacy.read_bytes() != before:
        raise RuntimeError("legacy_memory_modified")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply, config_path=args.config), ensure_ascii=False))


if __name__ == "__main__":
    main()
