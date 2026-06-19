from __future__ import annotations

import os
from pathlib import Path


DEV_SESSION_SECRET = "samwizard-dev-session-secret"
DEV_STATE_DIR = Path(".samwizard-state")
DEFAULT_BETA_LABEL = "BETA"
VERSION_PATH = Path("VERSION")


def session_secret_key() -> str:
    return os.environ.get("SAMWIZARD_SECRET_KEY") or DEV_SESSION_SECRET


def samwizard_state_dir() -> Path:
    return Path(os.environ.get("SAMWIZARD_STATE_DIR") or DEV_STATE_DIR)


def beta_enabled() -> bool:
    value = os.environ.get("SAMWIZARD_BETA", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def beta_label() -> str:
    label = os.environ.get("SAMWIZARD_BETA_LABEL", "").strip()
    return label or DEFAULT_BETA_LABEL


def app_version(version_path: Path = VERSION_PATH) -> str:
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"
    return version or "dev"


def beta_build_label() -> str:
    return f"{beta_label()} {app_version()}"
