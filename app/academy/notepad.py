from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.settings import samwizard_state_dir


SCHEMA_VERSION = 1
NOTEPAD_FILENAME = "academy-notepad.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def notepad_path(state_dir: Path | None = None) -> Path:
    directory = state_dir or samwizard_state_dir()
    return directory / NOTEPAD_FILENAME


def initial_notepad(text: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "text": text,
        "updated_at": utc_now(),
    }


def load_notepad(path: Path | None = None) -> dict[str, Any]:
    target = path or notepad_path()
    try:
        with target.open(encoding="utf-8") as file:
            payload = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return initial_notepad()

    if not isinstance(payload, dict):
        return initial_notepad()
    if payload.get("schema_version") != SCHEMA_VERSION:
        return initial_notepad()

    text = payload.get("text", "")
    if not isinstance(text, str):
        text = ""

    return {
        "schema_version": SCHEMA_VERSION,
        "text": text,
        "updated_at": str(payload.get("updated_at") or utc_now()),
    }


def save_notepad_text(text: str, path: Path | None = None) -> dict[str, Any]:
    payload = initial_notepad(text)
    save_notepad(payload, path)
    return payload


def save_notepad(payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or notepad_path()
    target.parent.mkdir(parents=True, exist_ok=True)
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
    finally:
        if temp_name:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()
