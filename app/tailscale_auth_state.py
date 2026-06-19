from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.settings import samwizard_state_dir


AUTH_SCHEMA_VERSION = 1
AUTH_DIRNAME = "tailscale-auth"
SAFE_AUTH_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tailscale_auth_dir(state_dir: Path | None = None) -> Path:
    return (state_dir or samwizard_state_dir()) / AUTH_DIRNAME


def safe_auth_id(auth_id: str | None) -> str | None:
    if not isinstance(auth_id, str) or not auth_id:
        return None
    if not SAFE_AUTH_ID_RE.fullmatch(auth_id):
        return None
    return auth_id


def tailscale_auth_path(auth_id: str, state_dir: Path | None = None) -> Path:
    safe_id = safe_auth_id(auth_id)
    if safe_id is None:
        raise ValueError("Invalid Tailscale auth id.")
    return tailscale_auth_dir(state_dir) / f"{safe_id}.json"


def save_pending_tailscale_auth(
    auth_id: str | None,
    *,
    login_url: str | None,
    authorize_result: dict[str, Any] | None,
    state_dir: Path | None = None,
) -> None:
    safe_id = safe_auth_id(auth_id)
    if safe_id is None:
        return

    target = tailscale_auth_path(safe_id, state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass

    payload = {
        "schema_version": AUTH_SCHEMA_VERSION,
        "login_url": login_url if isinstance(login_url, str) else None,
        "authorize_result": authorize_result if isinstance(authorize_result, dict) else None,
        "updated_at": utc_now(),
    }

    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=target.parent,
            encoding="utf-8",
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as file:
            temp_name = file.name
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        Path(temp_name).replace(target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        if temp_name:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()


def load_pending_tailscale_auth(auth_id: str | None, state_dir: Path | None = None) -> dict[str, Any]:
    safe_id = safe_auth_id(auth_id)
    if safe_id is None:
        return {}
    try:
        with tailscale_auth_path(safe_id, state_dir).open(encoding="utf-8") as file:
            payload = json.load(file)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != AUTH_SCHEMA_VERSION:
        return {}

    login_url = payload.get("login_url")
    authorize_result = payload.get("authorize_result")
    updated_at = payload.get("updated_at")
    return {
        "login_url": login_url if isinstance(login_url, str) else None,
        "authorize_result": authorize_result if isinstance(authorize_result, dict) else None,
        "updated_at": updated_at if isinstance(updated_at, str) else "",
    }
