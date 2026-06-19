from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
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


TAILSCALE_INTERFACE = "tailscale0"
DEFAULT_SAMWIZARD_PORT = 8080
SAMBA_FALLBACK_RULES = [
    ("445/tcp", "Windows file sharing over TCP"),
    ("139/tcp", "Older Windows file sharing over TCP"),
    ("137/udp", "Windows name service"),
    ("138/udp", "Windows browser service"),
]


@dataclass(frozen=True)
class FirewallRule:
    id: str
    title: str
    summary: str
    args: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "args": list(self.args),
            "command": " ".join(self.args),
        }


def firewall_context(command_runner: CommandRunner = run_command) -> dict[str, Any]:
    status = ufw_status(command_runner)
    lan_cidrs = detect_lan_cidrs(command_runner)
    samba_app = samba_app_available(command_runner)
    return {
        "ufw_status": status,
        "ufw_active": status == "active",
        "lan_cidrs": lan_cidrs,
        "samba_app_available": samba_app,
        "preview": preview_firewall_rules(
            lan_cidrs=lan_cidrs,
            ufw_active=status == "active",
            samba_app_available=samba_app,
        ),
    }


def ufw_status(command_runner: CommandRunner = run_command) -> str:
    result = command_runner(["ufw", "status"], None, None)
    if result.returncode != 0:
        return "unavailable"
    first_line = next((line.strip().lower() for line in result.stdout.splitlines() if line.strip()), "")
    if "inactive" in first_line:
        return "inactive"
    if "active" in first_line:
        return "active"
    return "unknown"


def samba_app_available(command_runner: CommandRunner = run_command) -> bool:
    result = command_runner(["ufw", "app", "list"], None, None)
    if result.returncode != 0:
        return False
    return any(line.strip().lower() == "samba" for line in result.stdout.splitlines())


def detect_lan_cidrs(command_runner: CommandRunner = run_command) -> list[str]:
    route_result = command_runner(["ip", "-j", "-4", "route", "show", "scope", "link"], None, None)
    if route_result.returncode == 0:
        cidrs = parse_route_cidrs(route_result.stdout)
        if cidrs:
            return cidrs

    addr_result = command_runner(["ip", "-j", "-4", "addr", "show", "scope", "global"], None, None)
    if addr_result.returncode == 0:
        return parse_addr_cidrs(addr_result.stdout)
    return []


def parse_route_cidrs(output: str) -> list[str]:
    try:
        routes = json.loads(output)
    except json.JSONDecodeError:
        return []
    cidrs: list[str] = []
    for route in routes if isinstance(routes, list) else []:
        dst = route.get("dst")
        if not dst or dst == "default":
            continue
        normalized = normalize_cidr(dst)
        if normalized:
            cidrs.append(normalized)
    return dedupe(cidrs)


def parse_addr_cidrs(output: str) -> list[str]:
    try:
        interfaces = json.loads(output)
    except json.JSONDecodeError:
        return []
    cidrs: list[str] = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        for addr in interface.get("addr_info") or []:
            if addr.get("family") != "inet":
                continue
            local = addr.get("local")
            prefixlen = addr.get("prefixlen")
            if not local or prefixlen is None:
                continue
            normalized = normalize_cidr(f"{local}/{prefixlen}", strict=False)
            if normalized:
                cidrs.append(normalized)
    return dedupe(cidrs)


def normalize_cidr(value: str, *, strict: bool = True) -> str | None:
    try:
        return str(ipaddress.ip_network(value, strict=strict))
    except ValueError:
        return None


def preview_firewall_rules(
    *,
    lan_cidrs: list[str],
    ufw_active: bool,
    samba_app_available: bool,
    samwizard_port: int = DEFAULT_SAMWIZARD_PORT,
) -> dict[str, Any]:
    if not lan_cidrs:
        return {
            "can_apply": False,
            "message": "SamWizard could not identify the local network range, so it will not guess firewall rules.",
            "rules": [],
        }

    rules = build_firewall_rules(
        lan_cidrs=lan_cidrs,
        ufw_active=ufw_active,
        samba_app_available=samba_app_available,
        samwizard_port=samwizard_port,
    )
    return {
        "can_apply": True,
        "message": "These rules keep Samba available on your home network and through Tailscale.",
        "rules": [rule.as_dict() for rule in rules],
    }


def apply_firewall_rules(
    command_runner: CommandRunner = run_command,
    *,
    lan_cidrs: list[str],
    ufw_active: bool,
    samba_app_available: bool,
    samwizard_port: int = DEFAULT_SAMWIZARD_PORT,
    root_checker: Callable[[], bool] = is_root,
) -> list[dict[str, Any]]:
    if not root_checker():
        return [
            failed(
                "ufw_root_required",
                "Administrator access needed",
                "Restart the wizard with sudo before changing firewall settings.",
                ["Example: sudo .venv-wsl/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080"],
            ).as_dict()
        ]
    if not lan_cidrs:
        return [
            failed(
                "ufw_lan_required",
                "Find home network",
                "SamWizard could not identify the local network range, so it did not change firewall settings.",
                [],
            ).as_dict()
        ]

    results: list[OperationResult] = []
    rules = build_firewall_rules(
        lan_cidrs=lan_cidrs,
        ufw_active=ufw_active,
        samba_app_available=samba_app_available,
        samwizard_port=samwizard_port,
    )
    for rule in rules:
        result = command_runner(rule.args, None, None)
        details = [command_detail(rule.title, result)]
        if result.returncode != 0:
            results.append(failed(rule.id, rule.title, "Firewall setup stopped before finishing.", details))
            break
        results.append(passed(rule.id, rule.title, rule.summary, details))
    return [result.as_dict() for result in results]


def build_firewall_rules(
    *,
    lan_cidrs: list[str],
    ufw_active: bool,
    samba_app_available: bool,
    samwizard_port: int,
) -> list[FirewallRule]:
    rules: list[FirewallRule] = []
    if not ufw_active:
        rules.extend(
            [
                FirewallRule(
                    "ufw_default_deny",
                    "Set firewall default",
                    "New incoming connections are blocked unless SamWizard allows them below.",
                    ["ufw", "default", "deny", "incoming"],
                ),
                FirewallRule(
                    "ufw_default_allow_out",
                    "Allow outgoing connections",
                    "The server can still reach the internet for updates and downloads.",
                    ["ufw", "default", "allow", "outgoing"],
                ),
            ]
        )

    for cidr in lan_cidrs:
        safe_id = cidr.replace("/", "_").replace(".", "_")
        rules.extend(
            [
                FirewallRule(
                    f"ufw_lan_ssh_{safe_id}",
                    "Keep SSH available at home",
                    f"SSH stays reachable from your home network range {cidr}.",
                    ["ufw", "allow", "proto", "tcp", "from", cidr, "to", "any", "port", "22"],
                ),
                FirewallRule(
                    f"ufw_lan_web_{safe_id}",
                    "Keep SamWizard available at home",
                    f"SamWizard stays reachable from your home network range {cidr}.",
                    ["ufw", "allow", "proto", "tcp", "from", cidr, "to", "any", "port", str(samwizard_port)],
                ),
            ]
        )
        rules.extend(samba_rules_for_source(cidr, samba_app_available))

    rules.extend(
        [
            FirewallRule(
                "ufw_tailscale_ssh",
                "Keep SSH available through Tailscale",
                "SSH stays reachable through your private Tailscale network.",
                ["ufw", "allow", "in", "on", TAILSCALE_INTERFACE, "to", "any", "port", "22", "proto", "tcp"],
            ),
            FirewallRule(
                "ufw_tailscale_web",
                "Keep SamWizard available through Tailscale",
                "SamWizard stays reachable through your private Tailscale network.",
                [
                    "ufw",
                    "allow",
                    "in",
                    "on",
                    TAILSCALE_INTERFACE,
                    "to",
                    "any",
                    "port",
                    str(samwizard_port),
                    "proto",
                    "tcp",
                ],
            ),
        ]
    )
    rules.extend(samba_rules_for_interface(samba_app_available))
    if not ufw_active:
        rules.append(
            FirewallRule(
                "ufw_enable",
                "Activate firewall",
                "Firewall is active with the safe access rules above.",
                ["ufw", "--force", "enable"],
            )
        )
    return rules


def samba_rules_for_source(cidr: str, samba_app_available: bool) -> list[FirewallRule]:
    safe_id = cidr.replace("/", "_").replace(".", "_")
    if samba_app_available:
        return [
            FirewallRule(
                f"ufw_lan_samba_{safe_id}",
                "Keep Samba available at home",
                f"Samba stays reachable from your home network range {cidr}.",
                ["ufw", "allow", "from", cidr, "to", "any", "app", "Samba"],
            )
        ]
    rules: list[FirewallRule] = []
    for port, label in SAMBA_FALLBACK_RULES:
        number, protocol = port.split("/", 1)
        rules.append(
            FirewallRule(
                f"ufw_lan_samba_{number}_{protocol}_{safe_id}",
                "Keep Samba available at home",
                f"{label} stays reachable from your home network range {cidr}.",
                ["ufw", "allow", "proto", protocol, "from", cidr, "to", "any", "port", number],
            )
        )
    return rules


def samba_rules_for_interface(samba_app_available: bool) -> list[FirewallRule]:
    if samba_app_available:
        return [
            FirewallRule(
                "ufw_tailscale_samba",
                "Keep Samba available through Tailscale",
                "Samba stays reachable through your private Tailscale network.",
                ["ufw", "allow", "in", "on", TAILSCALE_INTERFACE, "to", "any", "app", "Samba"],
            )
        ]
    rules: list[FirewallRule] = []
    for port, label in SAMBA_FALLBACK_RULES:
        number, protocol = port.split("/", 1)
        rules.append(
            FirewallRule(
                f"ufw_tailscale_samba_{number}_{protocol}",
                "Keep Samba available through Tailscale",
                f"{label} stays reachable through your private Tailscale network.",
                [
                    "ufw",
                    "allow",
                    "in",
                    "on",
                    TAILSCALE_INTERFACE,
                    "to",
                    "any",
                    "port",
                    number,
                    "proto",
                    protocol,
                ],
            )
        )
    return rules


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def command_detail(label: str, result: CommandResult) -> str:
    command = " ".join(result.args)
    output = (result.stdout or result.stderr or "").strip()
    if output:
        return f"{label}: `{command}` exited {result.returncode}. {output.splitlines()[0]}"
    return f"{label}: `{command}` exited {result.returncode}."
