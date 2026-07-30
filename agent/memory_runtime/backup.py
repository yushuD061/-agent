"""Verified SQLite backup and restore staging for memory databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MemoryBackupManager:
    MANIFEST_VERSION = 1

    def create(self, sources: dict[str, sqlite3.Connection], target_directory: str | Path) -> Path:
        target = Path(target_directory)
        if target.exists() and any(target.iterdir()):
            raise FileExistsError("memory_backup_target_not_empty")
        target.mkdir(parents=True, exist_ok=True)
        files = []
        for name, source in sorted(sources.items()):
            if not name or Path(name).name != name:
                raise ValueError("memory_backup_name_invalid")
            destination = target / f"{name}.db"
            output = sqlite3.connect(destination)
            try:
                source.backup(output)
                check = output.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise RuntimeError("memory_backup_integrity_failed")
                schema_version = output.execute("PRAGMA user_version").fetchone()[0]
            finally:
                output.close()
            files.append({
                "name": name, "file": destination.name, "sha256": _sha256(destination),
                "schema_version": schema_version,
                "source_identity": next(
                    (row[2] for row in source.execute("PRAGMA database_list") if row[1] == "main"),
                    "",
                ),
            })
        manifest = {
            "manifest_version": self.MANIFEST_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": files,
        }
        path = target / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def restore_to_staging(
        self, backup_directory: str | Path, staging_directory: str | Path,
    ) -> dict[str, Path]:
        source, staging = Path(backup_directory), Path(staging_directory)
        if staging.exists() and any(staging.iterdir()):
            raise FileExistsError("memory_restore_target_not_empty")
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("manifest_version") != self.MANIFEST_VERSION:
            raise ValueError("memory_backup_manifest_version_invalid")
        verified: list[tuple[dict, Path]] = []
        for entry in manifest.get("files", []):
            path = source / entry["file"]
            if not path.is_file() or _sha256(path) != entry["sha256"]:
                raise ValueError("memory_backup_checksum_invalid")
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("memory_backup_integrity_invalid")
                if connection.execute("PRAGMA user_version").fetchone()[0] != entry["schema_version"]:
                    raise ValueError("memory_backup_schema_version_invalid")
            finally:
                connection.close()
            verified.append((entry, path))
        staging.mkdir(parents=True, exist_ok=True)
        restored = {}
        for entry, path in verified:
            destination = staging / entry["file"]
            source_db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            target_db = sqlite3.connect(destination)
            try:
                source_db.backup(target_db)
            finally:
                target_db.close()
                source_db.close()
            restored[entry["name"]] = destination
        return restored
