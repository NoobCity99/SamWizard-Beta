from __future__ import annotations

import io
import ipaddress
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request

from app.command_log import LOG_ID_KEY, command_log_from_state, logged_command_runner, persisted_command_log_from_state
from app.firewall_manager import firewall_context
from app.settings import app_version, beta_build_label, beta_label
from app.system_info import detect_system_info
from app.tailscale_auth_state import load_pending_tailscale_auth
from app.tailscale_manager import detect_tailscale


SENSITIVE_KEY_PARTS = ("password", "secret", "token", "cookie", "key")
TAILSCALE_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+")
TAILSCALE_NODE_KEY_RE = re.compile(r"\b(?:nodekey|mkey):[A-Za-z0-9._~+/=-]+")
TAILNET_DNS_RE = re.compile(
    r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:ts\.net|beta\.tailscale\.net)\.?(?=$|[\s\"',}\]])"
)
TAILSCALE_USER_ID_TEXT_RE = re.compile(r'("UserID"\s*:\s*)\d+', re.IGNORECASE)
IP_ADDRESS_RE = re.compile(
    r"(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w:])"
    r"|(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?::\d{1,5})?(?![\w:])"
)
DIAGNOSTIC_NOTE_FIELDS = (
    "tester_id",
    "scenario",
    "expected_result",
    "actual_result",
    "notes",
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def zip_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def clean_note_value(value: str) -> str:
    return value.strip()[:4000]


def tester_notes(**values: str) -> dict[str, str]:
    return {field: clean_note_value(values.get(field, "")) for field in DIAGNOSTIC_NOTE_FIELDS}


def build_diagnostic_bundle(
    request: Request,
    state: dict[str, Any],
    notes: dict[str, str],
) -> tuple[str, bytes]:
    system_info = detect_system_info(
        command_runner=logged_command_runner(state, "Beta Diagnostics", log_start=False)
    )
    tailscale_info = detect_tailscale(
        command_runner=logged_command_runner(state, "Beta Diagnostics", log_start=False)
    )
    firewall_info = firewall_context(
        command_runner=logged_command_runner(state, "Beta Diagnostics", log_start=False)
    )
    log_entries = merged_command_log(state)
    generated_at = utc_timestamp()
    source_route = source_route_from_request(request)

    summary = {
        "generated_at": generated_at,
        "app": {
            "name": "SamWizard",
            "version": app_version(),
            "beta_label": beta_label(),
            "build_label": beta_build_label(),
        },
        "source_route": source_route,
        "current_step": state.get("step") or state.get("apply_status") or "unknown",
        "system_summary": {
            "hostname": system_info.get("hostname", {}).get("value"),
            "local_ips": system_info.get("local_ips", {}).get("items", []),
            "os": system_info.get("os", {}).get("pretty_name"),
            "internet": system_info.get("internet", {}).get("status"),
        },
        "log_entry_count": len(log_entries),
    }

    files = {
        "summary.json": summary,
        "tester-notes.json": scrub(notes),
        "wizard-state.json": safe_wizard_state_summary(state),
        "command-log.json": scrub(log_entries),
        "system-info.json": scrub(system_info),
        "samba-status.json": scrub(system_info.get("samba", {})),
        "drive-status.json": scrub(system_info.get("drives", {})),
        "tailscale-status.json": scrub(tailscale_info),
        "firewall-status.json": scrub(firewall_info),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", diagnostic_readme(generated_at))
        for name, payload in files.items():
            archive.writestr(name, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    return f"samwizard-beta-diagnostics-{zip_timestamp()}.zip", buffer.getvalue()


def merged_command_log(state: dict[str, Any]) -> list[dict[str, Any]]:
    entries = persisted_command_log_from_state(state) + command_log_from_state(state)
    seen: set[tuple[Any, ...]] = set()
    merged: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            entry.get("timestamp"),
            entry.get("phase"),
            entry.get("command"),
            entry.get("summary"),
            entry.get("exit_code"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def source_route_from_request(request: Request) -> dict[str, str]:
    url = getattr(request, "url", "")
    headers = getattr(request, "headers", {}) or {}
    return {
        "url": str(url) if url else "",
        "referer": str(headers.get("referer") or ""),
    }


def safe_wizard_state_summary(state: dict[str, Any]) -> dict[str, Any]:
    selected_location = state.get("selected_location")
    if isinstance(selected_location, dict):
        location = {
            "type": selected_location.get("type"),
            "name": selected_location.get("name"),
            "path": selected_location.get("path"),
            "uuid": selected_location.get("uuid"),
            "filesystem": selected_location.get("filesystem"),
            "mount_path": selected_location.get("mount_path"),
            "mount_access": selected_location.get("mount_access"),
        }
    else:
        location = None
    tailscale = state.get("tailscale", {})
    tailscale_summary = dict(tailscale) if isinstance(tailscale, dict) else {}
    pending_auth = load_pending_tailscale_auth(state.get(LOG_ID_KEY))
    if pending_auth:
        tailscale_summary["pending_auth"] = pending_auth

    return scrub(
        {
            "samba_setup_mode": state.get("samba_setup_mode"),
            "samba_setup_message": state.get("samba_setup_message"),
            "share_name": state.get("share_name"),
            "username": state.get("username"),
            "mount_access": state.get("mount_access"),
            "selected_location": location,
            "reviewed": state.get("reviewed"),
            "applied": state.get("applied"),
            "apply_status": state.get("apply_status"),
            "apply_results": state.get("apply_results", []),
            "system_summary": state.get("system_summary", {}),
            "tailscale": tailscale_summary,
        }
    )


def scrub(value: Any, key_name: str = "") -> Any:
    if isinstance(value, str) and contains_tailscale_diagnostic_value(value):
        return redact_diagnostic_text(value)
    if key_name and any(part in key_name.lower() for part in SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if is_tailscale_user_id_key(key_name):
        return "[redacted]"
    if is_tailnet_name_key(key_name) and isinstance(value, str):
        return "[REDACTED_TAILNET_NAME]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key == "stdin" and value.get("stdin_hidden"):
                output[text_key] = "********"
            else:
                output[text_key] = scrub(item, text_key)
        return output
    if isinstance(value, list):
        return [scrub(item, key_name) for item in value]
    if isinstance(value, str):
        return redact_diagnostic_text(value)
    return value


def is_tailscale_user_id_key(key_name: str) -> bool:
    normalized = key_name.replace("_", "").replace("-", "").lower()
    return normalized in {"userid", "userids"} or normalized.endswith("userid")


def is_tailnet_name_key(key_name: str) -> bool:
    normalized = key_name.replace("_", "").replace("-", "").lower()
    return normalized in {"tailnet", "tailnetname", "magicdnssuffix"}


def redact_diagnostic_text(value: str) -> str:
    redacted = TAILSCALE_AUTH_URL_RE.sub("[REDACTED_TAILSCALE_AUTH_URL]", value)
    redacted = TAILSCALE_NODE_KEY_RE.sub("[REDACTED_TAILSCALE_NODE_KEY]", redacted)
    redacted = TAILNET_DNS_RE.sub("[REDACTED_TAILNET_NAME]", redacted)
    redacted = TAILSCALE_USER_ID_TEXT_RE.sub(r'\1"[redacted]"', redacted)
    return IP_ADDRESS_RE.sub(redact_public_ip_match, redacted)


def contains_tailscale_diagnostic_value(value: str) -> bool:
    return bool(
        TAILSCALE_AUTH_URL_RE.search(value)
        or TAILSCALE_NODE_KEY_RE.search(value)
        or TAILNET_DNS_RE.search(value)
    )


def redact_public_ip_match(match: re.Match[str]) -> str:
    text = match.group(0)
    host, separator, port = text.rpartition(":")
    candidate = host if separator and "." in host else text
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return text
    if address.is_global:
        return f"[REDACTED_PUBLIC_IP]{separator}{port}" if separator and "." in host else "[REDACTED_PUBLIC_IP]"
    return text


def diagnostic_readme(generated_at: str) -> str:
    return "\n".join(
        [
            "SamWizard beta diagnostics",
            f"Generated: {generated_at}",
            "",
            "This bundle is intended for beta troubleshooting.",
            "Passwords, session secrets, cookies, tokens, and hidden command input are redacted.",
            "",
        ]
    )
