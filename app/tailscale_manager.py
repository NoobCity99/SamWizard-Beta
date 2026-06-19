from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Callable

from app.system_actions import (
    CommandResult,
    CommandRunner,
    OperationResult,
    failed,
    is_root,
    passed,
    run_command,
)
from app.system_info import parse_os_release


TAILSCALE_KEYRING_PATH = Path("/usr/share/keyrings/tailscale-archive-keyring.gpg")
TAILSCALE_LIST_PATH = Path("/etc/apt/sources.list.d/tailscale.list")
LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+")


def detect_tailscale(command_runner: CommandRunner = run_command) -> dict[str, Any]:
    installed = is_tailscale_installed(command_runner)
    service_status = service_status_label(command_runner) if installed else "not_installed"
    status = tailscale_status(command_runner) if installed else default_status("not_installed")
    ipv4 = tailscale_ipv4(command_runner) if installed else None
    connected = bool(ipv4) and service_status == "active"
    return {
        "installed": installed,
        "service_status": service_status,
        "backend_state": status.get("backend_state") or "Unknown",
        "logged_in": bool(status.get("logged_in")),
        "connected": connected,
        "ipv4": ipv4,
        "message": tailscale_message(installed, service_status, status, ipv4),
    }


def is_tailscale_installed(command_runner: CommandRunner = run_command) -> bool:
    result = command_runner(["tailscale", "version"], None, None)
    return result.returncode == 0


def service_status_label(command_runner: CommandRunner = run_command) -> str:
    result = command_runner(["systemctl", "is-active", "tailscaled"], None, None)
    if result.returncode == 0:
        return (result.stdout.strip() or "active").splitlines()[0]
    return "inactive"


def tailscale_status(command_runner: CommandRunner = run_command) -> dict[str, Any]:
    result = command_runner(["tailscale", "status", "--json"], None, None)
    if result.returncode != 0 or not result.stdout.strip():
        return default_status("Unavailable")
    return parse_tailscale_status(result.stdout)


def parse_tailscale_status(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return default_status("Unreadable")
    backend_state = str(payload.get("BackendState") or payload.get("backendState") or "Unknown")
    tailscale_ips = payload.get("TailscaleIPs") or payload.get("tailscaleIPs") or []
    return {
        "backend_state": backend_state,
        "logged_in": backend_state.lower() == "running" or bool(tailscale_ips),
        "tailscale_ips": tailscale_ips,
        "self": payload.get("Self") or {},
    }


def tailscale_ipv4(command_runner: CommandRunner = run_command) -> str | None:
    result = command_runner(["tailscale", "ip", "-4"], None, None)
    if result.returncode != 0:
        return None
    for item in result.stdout.split():
        if is_ipv4(item):
            return item
    return None


def is_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def install_tailscale(
    command_runner: CommandRunner = run_command,
    *,
    root_checker: Callable[[], bool] = is_root,
    os_release_path: str | Path = "/etc/os-release",
    keyring_path: Path = TAILSCALE_KEYRING_PATH,
    list_path: Path = TAILSCALE_LIST_PATH,
) -> list[dict[str, Any]]:
    if not root_checker():
        return [
            failed(
                "tailscale_root_required",
                "Administrator access needed",
                "Restart the wizard with sudo before installing Tailscale.",
                ["Example: sudo .venv-wsl/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080"],
            ).as_dict()
        ]

    codename = ubuntu_codename(os_release_path)
    if not codename:
        return [
            failed(
                "tailscale_os_codename",
                "Check Ubuntu version",
                "SamWizard could not identify this Ubuntu release for the Tailscale package repository.",
                [],
            ).as_dict()
        ]

    gpg_url = f"https://pkgs.tailscale.com/stable/ubuntu/{codename}.noarmor.gpg"
    list_url = f"https://pkgs.tailscale.com/stable/ubuntu/{codename}.tailscale-keyring.list"
    steps = [
        (
            "tailscale_keyring_dir",
            "Prepare package key folder",
            "Package key folder is ready.",
            ["mkdir", "-p", "--mode=0755", str(keyring_path.parent)],
            None,
        ),
        (
            "tailscale_keyring",
            "Download Tailscale package key",
            "Tailscale package key is ready.",
            ["curl", "-fsSL", gpg_url, "-o", str(keyring_path)],
            "Tailscale package key could not be downloaded. Check internet access.",
        ),
        (
            "tailscale_package_list",
            "Add Tailscale package source",
            "Tailscale package source is ready.",
            ["curl", "-fsSL", list_url, "-o", str(list_path)],
            "Tailscale package source could not be downloaded. Check internet access.",
        ),
        (
            "tailscale_apt_update",
            "Refresh package list",
            "Package list refreshed.",
            ["apt-get", "update"],
            "The package list could not be refreshed. Check internet access.",
        ),
        (
            "tailscale_install",
            "Install private remote access support",
            "Private remote access support is installed.",
            ["apt-get", "install", "-y", "tailscale"],
            "Tailscale could not be installed. Check internet access and package manager errors.",
        ),
        (
            "tailscale_service",
            "Start private remote access service",
            "Private remote access service is running.",
            ["systemctl", "enable", "--now", "tailscaled"],
            "The Tailscale service could not be started.",
        ),
    ]

    results: list[OperationResult] = []
    for step_id, title, summary, args, failure_summary in steps:
        result = command_runner(args, None, {"DEBIAN_FRONTEND": "noninteractive"})
        details = [command_detail(title, result)]
        if result.returncode != 0:
            results.append(failed(step_id, title, failure_summary or f"{title} failed.", details))
            break
        results.append(passed(step_id, title, summary, details))
    return [result.as_dict() for result in results]


def start_tailscale_login(
    command_runner: CommandRunner = run_command,
    *,
    root_checker: Callable[[], bool] = is_root,
) -> dict[str, Any]:
    if not root_checker():
        result = failed(
            "tailscale_root_required",
            "Administrator access needed",
            "Restart the wizard with sudo before connecting this server to Tailscale.",
            ["Example: sudo .venv-wsl/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080"],
        )
        return {"result": result.as_dict(), "login_url": None}

    result = command_runner(["tailscale", "up", "--accept-dns=false", "--timeout=10s"], None, None)
    login_url = extract_login_url(result.stdout + "\n" + result.stderr)
    if login_url:
        operation = passed(
            "tailscale_authorize",
            "Waiting for Tailscale login",
            "Waiting for Tailscale login. Open the approval link, then return to SamWizard.",
            [command_detail("Start Tailscale sign-in", result)],
        )
    elif result.returncode == 0:
        operation = passed(
            "tailscale_authorize",
            "Authorize this server",
            "Tailscale accepted the connection command.",
            [command_detail("Start Tailscale sign-in", result)],
        )
    else:
        operation = failed(
            "tailscale_authorize",
            "Authorize this server",
            "Tailscale could not start the approval process.",
            [command_detail("Start Tailscale sign-in", result)],
        )
    return {"result": operation.as_dict(), "login_url": login_url}


def extract_login_url(output: str) -> str | None:
    match = LOGIN_URL_RE.search(output)
    return match.group(0) if match else None


def ubuntu_codename(os_release_path: str | Path = "/etc/os-release") -> str | None:
    path = Path(os_release_path)
    try:
        values = parse_os_release(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    codename = (
        values.get("VERSION_CODENAME")
        or values.get("UBUNTU_CODENAME")
        or values.get("DISTRIB_CODENAME")
    )
    return codename.strip().lower() if codename else None


def default_status(backend_state: str) -> dict[str, Any]:
    return {
        "backend_state": backend_state,
        "logged_in": False,
        "tailscale_ips": [],
        "self": {},
    }


def tailscale_message(
    installed: bool,
    service_status: str,
    status: dict[str, Any],
    ipv4: str | None,
) -> str:
    if not installed:
        return "Tailscale is not installed yet."
    if service_status != "active":
        return "Tailscale is installed, but the service is not running."
    if not status.get("logged_in"):
        return "Tailscale is installed and running, but this server still needs approval."
    if not ipv4:
        return "Tailscale is connected, but no Tailscale IPv4 address was found yet."
    return "Tailscale is connected and ready."


def command_detail(label: str, result: CommandResult) -> str:
    command = " ".join(result.args)
    output = (result.stdout or result.stderr or "").strip()
    if output:
        return f"{label}: `{command}` exited {result.returncode}. {output.splitlines()[0]}"
    return f"{label}: `{command}` exited {result.returncode}."
