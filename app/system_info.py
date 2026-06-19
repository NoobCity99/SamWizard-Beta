from __future__ import annotations

import ipaddress
import json
import platform
import shlex
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str]], CommandResult | None]
DnsResolver = Callable[[str, int, int, int, int], list[Any]]
SocketConnector = Callable[[tuple[Any, int], float], Any]
InternetDetector = Callable[[], dict[str, Any]]


def run_command(args: list[str]) -> CommandResult | None:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None

    return CommandResult(
        args=args,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def detect_system_info(
    command_runner: CommandRunner = run_command,
    os_release_path: str | Path = "/etc/os-release",
    proc_mounts_path: str | Path = "/proc/mounts",
    internet_detector: InternetDetector | None = None,
) -> dict[str, Any]:
    detect_internet = internet_detector or detect_internet_connectivity
    return {
        "hostname": detect_hostname(),
        "local_ips": detect_local_ips(command_runner),
        "os": detect_os_release(os_release_path),
        "samba": detect_samba(command_runner),
        "drives": detect_drives(command_runner),
        "mounts": detect_mounts(command_runner, proc_mounts_path),
        "internet": detect_internet(),
    }


def detect_hostname() -> dict[str, Any]:
    hostname = socket.gethostname().strip()
    if hostname:
        return {"available": True, "value": hostname, "source": "system hostname"}

    node = platform.node().strip()
    if node:
        return {"available": True, "value": node, "source": "platform node name"}

    return {
        "available": False,
        "value": None,
        "source": "unavailable",
        "message": "The server name could not be read on this system.",
    }


def detect_local_ips(command_runner: CommandRunner = run_command) -> dict[str, Any]:
    addresses: list[str] = []
    sources: list[str] = []

    result = command_runner(["hostname", "-I"])
    if result and result.returncode == 0:
        for item in result.stdout.split():
            if _is_usable_ip(item):
                addresses.append(_clean_ip(item))
        if addresses:
            sources.append("hostname -I")

    if not addresses:
        for item in _socket_addresses():
            if _is_usable_ip(item):
                addresses.append(_clean_ip(item))
        if addresses:
            sources.append("Python socket lookup")

    unique_addresses = _dedupe(addresses)
    return {
        "available": bool(unique_addresses),
        "items": unique_addresses,
        "source": ", ".join(sources) if sources else "unavailable",
        "message": None
        if unique_addresses
        else "No non-loopback local IP address was found.",
    }


def detect_internet_connectivity(
    host: str = "ubuntu.com",
    port: int = 443,
    timeout: float = 3.0,
    dns_resolver: DnsResolver = socket.getaddrinfo,
    socket_connector: SocketConnector = socket.create_connection,
) -> dict[str, Any]:
    details: list[str] = []

    try:
        addresses = dns_resolver(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0)
    except Exception as exc:
        details.append(f"DNS lookup failed for {host}: {exc}")
        return {
            "available": False,
            "connected": False,
            "status": "not_connected",
            "message": "No internet connection was found. Connect ethernet first, then check again.",
            "details": details,
            "source": f"DNS lookup and HTTPS connection to {host}:{port}",
        }

    if not addresses:
        details.append(f"DNS lookup returned no addresses for {host}.")
        return {
            "available": False,
            "connected": False,
            "status": "not_connected",
            "message": "No internet connection was found. Connect ethernet first, then check again.",
            "details": details,
            "source": f"DNS lookup and HTTPS connection to {host}:{port}",
        }

    details.append(f"DNS lookup found {len(addresses)} address candidate(s) for {host}.")
    last_error: Exception | None = None

    for address_info in addresses:
        sockaddr = address_info[4]
        try:
            connection = socket_connector(sockaddr, timeout)
        except Exception as exc:
            last_error = exc
            continue

        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

        details.append(f"HTTPS port {port} accepted a connection.")
        return {
            "available": True,
            "connected": True,
            "status": "connected",
            "message": "Internet connection found.",
            "details": details,
            "source": f"DNS lookup and HTTPS connection to {host}:{port}",
        }

    if last_error is not None:
        details.append(f"HTTPS connection failed: {last_error}")
    else:
        details.append(f"HTTPS connection to {host}:{port} could not be tested.")

    return {
        "available": False,
        "connected": False,
        "status": "not_connected",
        "message": "No internet connection was found. Connect ethernet first, then check again.",
        "details": details,
        "source": f"DNS lookup and HTTPS connection to {host}:{port}",
    }


def detect_os_release(os_release_path: str | Path = "/etc/os-release") -> dict[str, Any]:
    path = Path(os_release_path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {
            "available": False,
            "name": None,
            "pretty_name": None,
            "version": None,
            "id": None,
            "source": str(path),
            "message": "Linux version details are unavailable on this system.",
        }

    values = parse_os_release(content)
    pretty_name = values.get("PRETTY_NAME") or values.get("NAME")
    return {
        "available": bool(pretty_name),
        "name": values.get("NAME"),
        "pretty_name": pretty_name,
        "version": values.get("VERSION") or values.get("VERSION_ID"),
        "id": values.get("ID"),
        "source": str(path),
        "message": None if pretty_name else "The OS release file did not include a name.",
    }


def parse_os_release(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        values[key] = _strip_os_release_value(value)
    return values


def detect_samba(command_runner: CommandRunner = run_command) -> dict[str, Any]:
    evidence: list[str] = []
    installed = False
    version: str | None = None

    for args in (["smbd", "--version"], ["samba", "--version"], ["testparm", "-V"]):
        result = command_runner(args)
        if result and result.returncode == 0:
            version = _first_non_empty_line(result.stdout) or "Samba command responded"
            evidence.append(f"{args[0]} responded: {version}")
            installed = True
            break

    if not installed:
        dpkg_result = command_runner(
            [
                "dpkg-query",
                "-W",
                "-f=${binary:Package}\t${Version}\t${Status}\n",
                "samba",
                "smbd",
                "samba-common-bin",
            ]
        )
        if dpkg_result:
            for line in dpkg_result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3 and "install ok installed" in parts[2]:
                    package, version = parts[0], parts[1]
                    evidence.append(f"{package} package is installed: {version}")
                    installed = True
                    break

    if installed:
        users, user_details = detect_samba_users(command_runner)
        shares, share_details = detect_samba_shares(command_runner)
        active_sessions, session_details = detect_samba_active_sessions(command_runner)
        service_status, service_details = detect_samba_service(command_runner)
        evidence.extend(user_details)
        evidence.extend(share_details)
        evidence.extend(session_details)
        evidence.extend(service_details)
        setup_mode, setup_message = samba_setup_mode(users)
        return {
            "available": True,
            "installed": True,
            "status": "found",
            "version": version,
            "evidence": evidence,
            "message": setup_message,
            "users": users,
            "user_count": len(users),
            "shares": shares,
            "active_sessions": active_sessions,
            "service_status": service_status,
            "setup_mode": setup_mode,
            "setup_message": setup_message,
        }

    return {
        "available": True,
        "installed": False,
        "status": "not_found",
        "version": None,
        "evidence": evidence,
        "message": "Samba was not found. Nothing was installed or changed.",
        "users": [],
        "user_count": 0,
        "shares": [],
        "active_sessions": [],
        "service_status": "unknown",
        "setup_mode": "initial_setup",
        "setup_message": "Samba was not found. Nothing was installed or changed.",
    }


def detect_samba_users(command_runner: CommandRunner = run_command) -> tuple[list[dict[str, Any]], list[str]]:
    result = command_runner(["pdbedit", "-L"])
    if not result:
        return [], ["Samba users could not be checked because pdbedit did not respond."]
    if result.returncode != 0:
        return [], [f"Samba users could not be checked: pdbedit exited {result.returncode}."]
    users = parse_pdbedit_users(result.stdout)
    count = len(users)
    return users, [f"Samba users found: {count}."]


def parse_pdbedit_users(output: str) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        username, separator, rest = line.partition(":")
        username = username.strip()
        if not separator or not username or username in seen:
            continue
        seen.add(username)
        display_name = rest.split(":", 1)[-1].strip() if ":" in rest else ""
        users.append({"name": username, "display_name": display_name})
    return users


def detect_samba_shares(command_runner: CommandRunner = run_command) -> tuple[list[dict[str, Any]], list[str]]:
    result = command_runner(["testparm", "-s"])
    if not result:
        return [], ["Samba shares could not be checked because testparm did not respond."]
    if result.returncode != 0:
        return [], [f"Samba shares could not be checked: testparm exited {result.returncode}."]
    shares = parse_testparm_shares(result.stdout + "\n" + result.stderr)
    return shares, [f"Samba shares found: {len(shares)}."]


def parse_testparm_shares(output: str) -> list[dict[str, Any]]:
    ignored = {"global", "homes", "printers", "print$"}
    shares: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            current = None
            if name and name.lower() not in ignored:
                current = {"name": name, "path": None}
                shares.append(current)
            continue
        if current is not None and "=" in line:
            key, value = [part.strip() for part in line.split("=", 1)]
            if key.lower() == "path":
                current["path"] = value
    return shares


def detect_samba_active_sessions(command_runner: CommandRunner = run_command) -> tuple[list[dict[str, Any]], list[str]]:
    result = command_runner(["smbstatus"])
    if not result:
        return [], ["Samba active sessions could not be checked because smbstatus did not respond."]
    if result.returncode != 0:
        return [], [f"Samba active sessions could not be checked: smbstatus exited {result.returncode}."]
    sessions = parse_smbstatus_sessions(result.stdout)
    return sessions, [f"Samba active sessions found: {len(sessions)}."]


def parse_smbstatus_sessions(output: str) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    in_pid_section = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("PID") and "Username" in line:
            in_pid_section = True
            continue
        if in_pid_section and set(line) <= {"-"}:
            continue
        if in_pid_section:
            if line.startswith("Service") or line.startswith("Locked files:"):
                break
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                sessions.append({"pid": parts[0], "username": parts[1]})
    return sessions


def detect_samba_service(command_runner: CommandRunner = run_command) -> tuple[str, list[str]]:
    result = command_runner(["systemctl", "is-active", "smbd"])
    if result and result.returncode == 0:
        status = _first_non_empty_line(result.stdout) or "active"
        return status, [f"Samba service status: {status}."]
    if result:
        return "unknown", [f"Samba service status could not be confirmed: systemctl exited {result.returncode}."]
    return "unknown", ["Samba service status could not be checked because systemctl did not respond."]


def samba_setup_mode(users: list[dict[str, Any]]) -> tuple[str, str]:
    if len(users) == 1:
        username = users[0].get("name") or "the existing Samba user"
        return (
            "add_drive",
            f"Samba is installed with one existing user, {username}. The wizard can add another drive for that user.",
        )
    if len(users) > 1:
        return (
            "unsupported_existing_samba",
            "Samba is installed with multiple users. SamWizard stops here instead of guessing which account should own a new share.",
        )
    return (
        "initial_setup",
        "Samba appears to be installed, but no Samba users were found. Continue with first-time setup.",
    )


def detect_drives(command_runner: CommandRunner = run_command) -> dict[str, Any]:
    result = command_runner(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--output",
            "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,LABEL,MODEL,UUID",
        ]
    )
    if not result or result.returncode != 0 or not result.stdout.strip():
        result = command_runner(
            [
                "lsblk",
                "--json",
                "--bytes",
                "--output",
                "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,LABEL,MODEL,UUID",
            ]
        )

    if not result or result.returncode != 0 or not result.stdout.strip():
        return {
            "available": False,
            "items": [],
            "source": "lsblk",
            "message": "Drive details are unavailable because lsblk could not be read.",
        }

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "available": False,
            "items": [],
            "source": "lsblk",
            "message": "Drive details are unavailable because lsblk returned unreadable data.",
        }

    items = [_normalize_block_device(device) for device in _flatten_block_devices(data)]
    return {
        "available": True,
        "items": items,
        "source": "lsblk JSON",
        "message": None if items else "No drives or partitions were reported by lsblk.",
    }


def detect_mounts(
    command_runner: CommandRunner = run_command,
    proc_mounts_path: str | Path = "/proc/mounts",
) -> dict[str, Any]:
    result = command_runner(["findmnt", "--json", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    if result and result.returncode == 0 and result.stdout.strip():
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            items = [_normalize_mount(item) for item in _flatten_findmnt(data)]
            return {
                "available": True,
                "items": items,
                "source": "findmnt JSON",
                "message": None if items else "No mounted folders were reported by findmnt.",
            }

    proc_items = parse_proc_mounts(Path(proc_mounts_path))
    if proc_items:
        return {
            "available": True,
            "items": proc_items,
            "source": str(proc_mounts_path),
            "message": None,
        }

    return {
        "available": False,
        "items": [],
        "source": "findmnt and /proc/mounts",
        "message": "Mounted folder details are unavailable on this system.",
    }


def parse_proc_mounts(path: Path) -> list[dict[str, Any]]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    items: list[dict[str, Any]] = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        source, target, fstype, options = parts[:4]
        items.append(
            {
                "target": _decode_mount_field(target),
                "source": _decode_mount_field(source),
                "fstype": fstype,
                "options": options,
            }
        )
    return items


def _strip_os_release_value(value: str) -> str:
    try:
        parsed = shlex.split(value, comments=False, posix=True)
    except ValueError:
        parsed = []
    if len(parsed) == 1:
        return parsed[0]
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        return value[1:-1]
    return value


def _socket_addresses() -> Iterable[str]:
    hostnames = {socket.gethostname(), platform.node()}
    for hostname in {item for item in hostnames if item}:
        try:
            for info in socket.getaddrinfo(hostname, None):
                yield info[4][0]
        except OSError:
            continue


def _is_usable_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(_clean_ip(value))
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_multicast
        or address.is_unspecified
        or address.is_link_local
    )


def _clean_ip(value: str) -> str:
    return value.split("%", 1)[0].strip()


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _first_non_empty_line(value: str) -> str | None:
    return next((line.strip() for line in value.splitlines() if line.strip()), None)


def _flatten_block_devices(data: dict[str, Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    def walk(
        device: dict[str, Any],
        parent: dict[str, Any] | None = None,
        parent_disk: dict[str, Any] | None = None,
    ) -> None:
        current = dict(device)
        if parent:
            current["_parent_name"] = parent.get("name")
            current["_parent_path"] = parent.get("path") or _path_from_name(parent.get("name"))
            current["_parent_type"] = parent.get("type")
        disk = current if current.get("type") == "disk" else parent_disk
        if disk:
            current["_parent_disk_name"] = disk.get("name")
            current["_parent_disk_path"] = disk.get("path") or _path_from_name(disk.get("name"))
        flattened.append(current)
        for child in device.get("children") or []:
            walk(child, current, disk)

    for device in data.get("blockdevices") or []:
        walk(device)
    return flattened


def _normalize_block_device(device: dict[str, Any]) -> dict[str, Any]:
    mountpoints = device.get("mountpoints")
    if mountpoints is None:
        mountpoint = device.get("mountpoint")
        mountpoints = [mountpoint] if mountpoint else []
    if isinstance(mountpoints, str):
        mountpoints = [mountpoints]

    clean_mountpoints = [item for item in mountpoints if item]
    return {
        "name": device.get("name"),
        "path": device.get("path") or _path_from_name(device.get("name")),
        "type": device.get("type"),
        "size_bytes": _safe_int(device.get("size")),
        "size": format_bytes(_safe_int(device.get("size"))),
        "filesystem": device.get("fstype"),
        "mountpoints": clean_mountpoints,
        "label": device.get("label"),
        "model": _clean_optional_text(device.get("model")),
        "uuid": device.get("uuid"),
        "parent_name": device.get("_parent_name"),
        "parent_path": device.get("_parent_path"),
        "parent_type": device.get("_parent_type"),
        "parent_disk_name": device.get("_parent_disk_name"),
        "parent_disk_path": device.get("_parent_disk_path"),
    }


def _flatten_findmnt(data: dict[str, Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    def walk(item: dict[str, Any]) -> None:
        flattened.append(item)
        for child in item.get("children") or []:
            walk(child)

    for item in data.get("filesystems") or []:
        walk(item)
    return flattened


def _normalize_mount(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": item.get("target"),
        "source": item.get("source"),
        "fstype": item.get("fstype"),
        "options": item.get("options"),
    }


def _path_from_name(name: str | None) -> str | None:
    return f"/dev/{name}" if name else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _decode_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "Unknown size"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    unit = units[0]
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"
