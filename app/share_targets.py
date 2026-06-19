from __future__ import annotations

import re
from typing import Any


DRIVE_MOUNT_ROOT = "/srv/samba/drives"
MOUNT_ACCESS_READ_WRITE = "read_write"
MOUNT_ACCESS_READ_ONLY = "read_only"
DEFAULT_MOUNT_ACCESS = MOUNT_ACCESS_READ_WRITE
MOUNT_ACCESS_VALUES = {MOUNT_ACCESS_READ_WRITE, MOUNT_ACCESS_READ_ONLY}
READ_ONLY_ONLY_FILESYSTEMS = {"hfs", "hfsplus"}
UNSUPPORTED_FILESYSTEMS = {"apfs"}
SYSTEM_MOUNTPOINTS = {"/", "/boot", "/boot/efi"}


def safe_name(value: str, default: str = "share") -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
    return clean or default


def share_locations(system_info: dict[str, Any]) -> list[dict[str, Any]]:
    locations = []
    os_disk_ids = os_disk_identifiers(system_info.get("drives", {}).get("items", []))

    for drive in system_info.get("drives", {}).get("items", []):
        if not _is_eligible_partition(drive, os_disk_ids):
            continue
        uuid = drive["uuid"]
        label = drive.get("label") or drive.get("model") or drive.get("name") or uuid
        mount_name = safe_name(label, default=uuid)
        mountpoints = drive.get("mountpoints") or []
        mount_status = (
            f"currently mounted at {', '.join(mountpoints)}"
            if mountpoints
            else "not currently mounted"
        )
        filesystem = drive.get("filesystem") or "unknown filesystem"
        size = drive.get("size") or "unknown size"
        locations.append(
            {
                "id": f"drive:{uuid}",
                "type": "drive",
                "name": f"{label} drive",
                "description": f"{size}, {filesystem}, {mount_status}",
                "path": drive.get("path"),
                "uuid": uuid,
                "filesystem": filesystem,
                "mount_path": f"{DRIVE_MOUNT_ROOT}/{mount_name}",
                "mountpoints": mountpoints,
                "size": size,
                "label": label,
                "mount_access": DEFAULT_MOUNT_ACCESS,
            }
        )

    return locations


def drive_diagnostics(system_info: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = []
    drives = system_info.get("drives", {}).get("items", [])
    os_disk_ids = os_disk_identifiers(drives)
    for drive in drives:
        reasons = ineligible_reasons(drive, os_disk_ids)
        mountpoints = drive.get("mountpoints") or []
        diagnostics.append(
            {
                "name": drive.get("label")
                or drive.get("model")
                or drive.get("name")
                or drive.get("path")
                or "Detected storage",
                "path": drive.get("path") or "Unavailable",
                "type": drive.get("type") or "unknown",
                "size": drive.get("size") or "unknown size",
                "filesystem": drive.get("filesystem") or "missing",
                "uuid": drive.get("uuid") or "missing",
                "mountpoints": ", ".join(mountpoints) if mountpoints else "not mounted",
                "eligible": not reasons,
                "reason": "Ready to share" if not reasons else ", ".join(reasons),
            }
        )
    return diagnostics


def has_eligible_drive(system_info: dict[str, Any]) -> bool:
    return any(
        location.get("type") == "drive"
        for location in share_locations(system_info)
    )


def selected_location_from(
    location_id: str | None,
    system_info: dict[str, Any],
) -> dict[str, Any] | None:
    return next(
        (item for item in share_locations(system_info) if item["id"] == location_id),
        None,
    )


def _is_eligible_partition(drive: dict[str, Any], os_disk_ids: set[str]) -> bool:
    return not ineligible_reasons(drive, os_disk_ids)


def ineligible_reasons(drive: dict[str, Any], os_disk_ids: set[str] | None = None) -> list[str]:
    os_disk_ids = os_disk_ids or set()
    reasons = []
    if drive.get("type") != "part":
        reasons.append("not a partition")
    if is_os_drive_partition(drive, os_disk_ids):
        reasons.append("part of the server OS drive")
    if not drive.get("uuid"):
        reasons.append("missing UUID")
    if not drive.get("filesystem"):
        reasons.append("missing filesystem")
    elif filesystem_key(drive.get("filesystem")) in UNSUPPORTED_FILESYSTEMS:
        reasons.append("APFS is not supported by this wizard")
    return reasons


def os_disk_identifiers(drives: list[dict[str, Any]]) -> set[str]:
    identifiers: set[str] = set()
    for drive in drives:
        if drive.get("type") != "part":
            continue
        if not any(mount in SYSTEM_MOUNTPOINTS for mount in drive.get("mountpoints") or []):
            continue
        for value in (
            drive.get("parent_disk_path"),
            drive.get("parent_disk_name"),
            drive.get("parent_path"),
            drive.get("parent_name"),
            drive.get("path"),
            drive.get("name"),
        ):
            if value:
                identifiers.add(str(value))
    return identifiers


def is_os_drive_partition(drive: dict[str, Any], os_disk_ids: set[str]) -> bool:
    if drive.get("type") != "part":
        return False
    if any(mount in SYSTEM_MOUNTPOINTS for mount in drive.get("mountpoints") or []):
        return True
    return any(
        str(value) in os_disk_ids
        for value in (
            drive.get("parent_disk_path"),
            drive.get("parent_disk_name"),
            drive.get("parent_path"),
            drive.get("parent_name"),
        )
        if value
    )


def filesystem_key(filesystem: Any) -> str:
    return str(filesystem or "").strip().lower().replace("-", "")


def normalize_mount_access(value: str | None) -> str:
    if value in MOUNT_ACCESS_VALUES:
        return value
    return DEFAULT_MOUNT_ACCESS


def mount_access_label(value: str | None) -> str:
    return "Read-only" if normalize_mount_access(value) == MOUNT_ACCESS_READ_ONLY else "Read/write"


def mount_access_error(location: dict[str, Any], mount_access: str) -> str | None:
    if location.get("type") != "drive":
        return None
    filesystem = filesystem_key(location.get("filesystem"))
    if filesystem in UNSUPPORTED_FILESYSTEMS:
        return "APFS drives are not supported by this wizard because Linux write support is experimental."
    if filesystem in READ_ONLY_ONLY_FILESYSTEMS and mount_access == MOUNT_ACCESS_READ_WRITE:
        return "Mac HFS drives can only be shared read-only. Choose read-only to continue."
    return None
