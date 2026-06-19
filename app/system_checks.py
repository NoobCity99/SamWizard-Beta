from __future__ import annotations

from typing import Any


def system_checks_from_info(system_info: dict[str, Any]) -> list[dict[str, Any]]:
    hostname = system_info["hostname"]
    local_ips = system_info["local_ips"]
    os_info = system_info["os"]
    samba = system_info["samba"]
    drives = system_info["drives"]
    mounts = system_info["mounts"]
    internet = system_info["internet"]

    return [
        {
            "id": "hostname",
            "title": "Server name",
            "value": hostname.get("value") or "Unavailable",
            "status": "passed" if hostname.get("available") else "unavailable",
            "status_label": "Found" if hostname.get("available") else "Unavailable",
            "summary": hostname.get("message")
            or "This is the name this computer reports for itself.",
            "critical": False,
            "details": [f"Source: {hostname.get('source')}"] if hostname.get("source") else [],
        },
        {
            "id": "local_ips",
            "title": "Local addresses",
            "value": ", ".join(local_ips.get("items", [])) or "Unavailable",
            "status": "passed" if local_ips.get("available") else "unavailable",
            "status_label": "Found" if local_ips.get("available") else "Unavailable",
            "summary": local_ips.get("message")
            or "Use one of these addresses when opening the wizard from another computer on your home network.",
            "critical": False,
            "details": [f"Source: {local_ips.get('source')}"] if local_ips.get("source") else [],
        },
        {
            "id": "os_version",
            "title": "Linux version",
            "value": os_info.get("pretty_name") or "Unavailable",
            "status": "passed" if os_info.get("available") else "unavailable",
            "status_label": "Found" if os_info.get("available") else "Unavailable",
            "summary": os_info.get("message")
            or "This page reads /etc/os-release when Linux provides it.",
            "critical": False,
            "details": _os_details(os_info),
        },
        {
            "id": "internet_connectivity",
            "title": "Internet connection",
            "value": "Connected" if internet.get("connected") else "Not connected",
            "status": "passed" if internet.get("connected") else "needs_attention",
            "status_label": "Connected" if internet.get("connected") else "Needs attention",
            "summary": internet.get("message")
            or "The wizard checks internet access without changing network settings.",
            "critical": not internet.get("connected"),
            "details": internet.get("details") or [f"Source: {internet.get('source')}"],
        },
        {
            "id": "samba_installed",
            "title": "Samba installed",
            "value": "Appears installed" if samba.get("installed") else "Not found",
            "status": "passed" if samba.get("installed") else "needs_attention",
            "status_label": "Found" if samba.get("installed") else "Not found",
            "summary": samba.get("message")
            or "This check only looks for Samba. It does not install anything.",
            "critical": False,
            "details": _samba_details(samba),
            "actions": _samba_actions(samba),
        },
        {
            "id": "drives",
            "title": "Drives and partitions",
            "value": _count_label(drives.get("items", []), "item"),
            "status": "passed" if drives.get("available") else "unavailable",
            "status_label": "Found" if drives.get("available") else "Unavailable",
            "summary": drives.get("message")
            or "These are the drives and partitions Linux reported. Nothing was mounted or changed.",
            "critical": False,
            "details": _drive_details(drives.get("items", [])),
        },
        {
            "id": "mounts",
            "title": "Mounted folders",
            "value": _count_label(mounts.get("items", []), "mounted folder"),
            "status": "passed" if mounts.get("available") else "unavailable",
            "status_label": "Found" if mounts.get("available") else "Unavailable",
            "summary": mounts.get("message")
            or "These folders are already mounted. The wizard did not mount or unmount anything.",
            "critical": False,
            "details": _mount_details(mounts.get("items", [])),
        },
    ]


def system_summary(system_info: dict[str, Any]) -> dict[str, str]:
    local_ips = system_info.get("local_ips", {}).get("items", [])
    return {
        "hostname": system_info.get("hostname", {}).get("value") or "this-server",
        "ip_address": local_ips[0] if local_ips else "localhost",
        "ubuntu_version": system_info.get("os", {}).get("pretty_name") or "Unknown Linux version",
    }


def _count_label(items: list[Any], singular: str) -> str:
    count = len(items)
    if count == 1:
        return f"1 {singular}"
    return f"{count} {singular}s"


def _os_details(os_info: dict[str, Any]) -> list[str]:
    details = []
    if os_info.get("id"):
        details.append(f"System ID: {os_info['id']}")
    if os_info.get("version"):
        details.append(f"Version: {os_info['version']}")
    if os_info.get("source"):
        details.append(f"Source: {os_info['source']}")
    return details


def _samba_details(samba: dict[str, Any]) -> list[str]:
    if not samba.get("installed"):
        return samba.get("evidence") or ["Samba is not yet installed, which is perfect, that's what we're here to do!."]

    details = list(samba.get("evidence") or [])
    user_count = int(samba.get("user_count") or 0)
    shares = samba.get("shares") or []
    active_sessions = samba.get("active_sessions") or []
    details.append(f"Configured Samba users: {user_count}.")
    details.append(f"Configured shares: {len(shares)}.")
    details.append(f"Active connections: {len(active_sessions)}.")
    if samba.get("service_status"):
        details.append(f"Service status: {samba['service_status']}.")
    return details


def _samba_actions(samba: dict[str, Any]) -> list[dict[str, str]]:
    if not samba.get("installed"):
        return []
    return [
        {
            "label": "See your Samba System",
            "href": "/samba-system",
            "target": "_blank",
        }
    ]


def _drive_details(drives: list[dict[str, Any]], limit: int = 8) -> list[str]:
    details = []
    for drive in drives[:limit]:
        label = drive.get("label") or drive.get("model") or drive.get(
            "path") or drive.get("name") or "Drive"
        mountpoints = ", ".join(drive.get("mountpoints") or [])
        mounted = f", mounted at {mountpoints}" if mountpoints else ", not mounted"
        details.append(
            f"{label}: {drive.get('type') or 'device'}, {drive.get('size') or 'unknown size'}{mounted}"
        )
    if len(drives) > limit:
        details.append(
            f"{len(drives) - limit} more drive or partition items not shown.")
    return details


def _mount_details(mounts: list[dict[str, Any]], limit: int = 8) -> list[str]:
    details = []
    for mount in mounts[:limit]:
        target = mount.get("target") or "Mounted folder"
        source = mount.get("source") or "unknown source"
        fstype = mount.get("fstype") or "unknown type"
        details.append(f"{target}: {source} ({fstype})")
    if len(mounts) > limit:
        details.append(
            f"{len(mounts) - limit} more mounted folders not shown.")
    return details
