from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from app.command_log import add_log_entry
from app.system_actions import (
    CommandResult,
    OperationResult,
    backup_file,
    failed,
    is_root,
    passed,
    run_command,
)
from app.system_info import detect_internet_connectivity


NETPLAN_WIFI_PATH = Path("/etc/netplan/99-samba-wizard-wifi.yaml")


def netplan_wifi_yaml(interface: str, ssid: str, password: str) -> str:
    return "\n".join(
        [
            "network:",
            "  version: 2",
            "  renderer: networkd",
            "  wifis:",
            f"    {interface}:",
            "      dhcp4: true",
            "      access-points:",
            f'        "{ssid}":',
            f'          password: "{password}"',
            "",
        ]
    )


def apply_wifi_setup(
    state: dict,
    *,
    interface: str,
    ssid: str,
    password: str,
    command_runner: Callable[[list[str], str | None,
                              dict[str, str] | None], CommandResult] = run_command,
    root_checker: Callable[[], bool] = is_root,
    netplan_path: Path = NETPLAN_WIFI_PATH,
    internet_checker: Callable[[], dict] = detect_internet_connectivity,
) -> list[dict]:
    results: list[OperationResult] = []

    if not root_checker():
        return [
            failed(
                "wifi_root_required",
                "Administrator access needed",
                "Restart the wizard with sudo before applying Wi-Fi settings.",
                ["Example: sudo bash samwizard"],
            ).as_dict()
        ]

    write_result = write_netplan_file(
        state,
        interface=interface,
        ssid=ssid,
        password=password,
        netplan_path=netplan_path,
    )
    results.append(write_result)
    if write_result.status == "failed":
        return [result.as_dict() for result in results]

    for title, args in (
        ("Check Wi-Fi settings", ["netplan", "generate"]),
        ("Apply Wi-Fi settings", ["netplan", "apply"]),
    ):
        command_result = command_runner(args, None, None)
        if command_result.returncode != 0:
            results.append(
                failed(
                    args[-1],
                    title,
                    "The Wi-Fi settings could not be applied.",
                    [command_detail(title, command_result)],
                )
            )
            return [result.as_dict() for result in results]
        results.append(
            passed(
                args[-1],
                title,
                "Wi-Fi settings step completed.",
                [command_detail(title, command_result)],
            )
        )

    internet = internet_checker()
    add_log_entry(
        state,
        phase="Wi-Fi Setup",
        command="Python internet connectivity check",
        exit_code=0 if internet.get("connected") else 1,
        summary=internet.get("message") or "Internet check completed.",
    )
    if internet.get("connected"):
        results.append(
            passed(
                "wifi_internet",
                "Check internet connection",
                "Internet connection found after applying Wi-Fi settings.",
                internet.get("details", []),
            )
        )
    else:
        results.append(
            failed(
                "wifi_internet",
                "Check internet connection",
                "Wi-Fi settings were applied, but internet is still not available.",
                internet.get("details", []),
            )
        )
    return [result.as_dict() for result in results]


def write_netplan_file(
    state: dict,
    *,
    interface: str,
    ssid: str,
    password: str,
    netplan_path: Path = NETPLAN_WIFI_PATH,
) -> OperationResult:
    try:
        if netplan_path.exists():
            backup_file(netplan_path)
        netplan_path.parent.mkdir(parents=True, exist_ok=True)
        netplan_path.write_text(
            netplan_wifi_yaml(interface, ssid, password),
            encoding="utf-8",
        )
        os.chmod(netplan_path, 0o600)
    except OSError as exc:
        return failed(
            "wifi_netplan_file",
            "Write Wi-Fi settings",
            "The Wi-Fi settings file could not be written.",
            [str(exc)],
        )

    add_log_entry(
        state,
        phase="Wi-Fi Setup",
        command=f"Write managed Netplan file {netplan_path}",
        exit_code=0,
        stdin_hidden=True,
        summary="Wi-Fi settings file written with password hidden.",
    )
    return passed(
        "wifi_netplan_file",
        "Write Wi-Fi settings",
        "Wi-Fi settings file was written.",
        [f"Managed file: {netplan_path}", "Password hidden from logs."],
    )


def command_detail(label: str, result: CommandResult) -> str:
    output = (result.stdout or result.stderr or "").strip()
    if output:
        return f"{label}: `{' '.join(result.args)}` exited {result.returncode}. {output.splitlines()[0]}"
    return f"{label}: `{' '.join(result.args)}` exited {result.returncode}."
