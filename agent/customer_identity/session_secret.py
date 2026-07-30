"""Restart-stable customer session secret management for local deployments."""

from __future__ import annotations

import os
from pathlib import Path
import secrets


MIN_SECRET_LENGTH = 32
LOCAL_SECRET_PATH = Path("workspace/customer_auth/session-secret.key")


def load_or_create_customer_session_secret(
    workspace: str | Path, configured_secret: str | None = None,
) -> str:
    """Return an explicit secret or atomically create a private local secret file.

    Production deployments should continue supplying
    ``NANOCLAW_CUSTOMER_SESSION_SECRET`` through their secret manager.  The file
    fallback exists so a local registration-enabled checkout remains stable
    across restarts without committing or printing a credential.
    """
    configured = str(configured_secret or "").strip()
    if configured:
        if len(configured) < MIN_SECRET_LENGTH:
            raise RuntimeError("customer_session_secret_too_short")
        return configured

    root = Path(workspace).resolve()
    path = (root / LOCAL_SECRET_PATH).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("customer_session_secret_path_invalid") from exc
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        stored = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        stored = ""
    if stored:
        if len(stored) < MIN_SECRET_LENGTH:
            raise RuntimeError("customer_session_secret_file_invalid")
        return stored

    generated = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        stored = path.read_text(encoding="utf-8").strip()
        if len(stored) < MIN_SECRET_LENGTH:
            raise RuntimeError("customer_session_secret_file_invalid")
        return stored
    try:
        os.write(descriptor, generated.encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return generated
