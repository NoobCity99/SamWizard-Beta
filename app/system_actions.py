from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.share_targets import (
    MOUNT_ACCESS_READ_ONLY,
    MOUNT_ACCESS_READ_WRITE,
    READ_ONLY_ONLY_FILESYSTEMS,
    UNSUPPORTED_FILESYSTEMS,
    filesystem_key,
    normalize_mount_access,
    safe_name,
)
from app.system_info import detect_drives


SMB_CONF_PATH = Path("/etc/samba/smb.conf")
FSTAB_PATH = Path("/etc/fstab")
LINUX_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs"}
USER_MOUNT_OPTION_FILESYSTEMS = {"ntfs", "exfat", "vfat", "fat", "msdos"}


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class OperationResult:
    id: str
    title: str
    status: str
    summary: str
    details: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


CommandRunner = Callable[[list[str], str | None, dict[str, str] | None], CommandResult]


def run_command(
    args: list[str],
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            env=command_env,
            input=input_text,
            text=True,
            timeout=command_timeout(args),
        )
    except subprocess.TimeoutExpired as exc:
        timeout = command_timeout(args)
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        detail = f"Command timed out after {timeout} seconds."
        if stderr:
            stderr = f"{detail}\n{stderr}"
        else:
            stderr = detail
        return CommandResult(
            args=args,
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(args=args, returncode=1, stderr=str(exc))
    return CommandResult(
        args=args,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def command_timeout(args: list[str]) -> int:
    if not args:
        return 30
    command = args[0]
    if command == "apt-get":
        return 900
    if command in {"mount", "smbpasswd"}:
        return 120
    if command in {
        "blkid",
        "chmod",
        "chown",
        "findmnt",
        "id",
        "lsblk",
        "mkdir",
        "runuser",
        "service",
        "smbd",
        "systemctl",
        "testparm",
        "useradd",
    }:
        return 30
    return 60


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(callable(geteuid) and geteuid() == 0)


def apply_share_setup(
    location: dict[str, Any],
    share_name: str,
    username: str,
    password: str | None,
    *,
    create_user: bool = True,
    command_runner: CommandRunner = run_command,
    root_checker: Callable[[], bool] = is_root,
    smb_conf_path: Path = SMB_CONF_PATH,
    fstab_path: Path = FSTAB_PATH,
) -> list[dict[str, Any]]:
    results: list[OperationResult] = []

    if location.get("type") != "drive":
        return [
            failed(
                "drive_required",
                "External drive required",
                "Choose an external or additional drive before applying setup.",
                [],
            ).as_dict()
        ]

    if not root_checker():
        return [
            failed(
                "root_required",
                "Administrator access needed",
                "Restart the wizard with sudo before applying real system changes.",
                ["Example: sudo .venv-wsl/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080"],
            ).as_dict()
        ]

    if create_user and not password:
        return [
            failed(
                "samba_password_required",
                "Create private access",
                "Enter a Windows share password before applying first-time setup.",
                [],
            ).as_dict()
        ]

    steps = [lambda: install_samba(command_runner)]
    if create_user:
        steps.append(lambda: ensure_samba_user(username, password or "", command_runner))
    else:
        steps = [
            lambda: verify_samba_installed(command_runner),
            lambda: verify_existing_samba_user(username, command_runner),
        ]
    steps.extend(
        [
            lambda: prepare_share_target(location, share_name, username, command_runner, fstab_path),
            lambda: set_share_owner(location, username, command_runner),
        ]
    )
    if location.get("type") == "drive":
        steps.append(lambda: verify_drive_write_access(location, username, command_runner))
    steps.extend(
        [
            lambda: configure_samba_share(location, share_name, username, smb_conf_path),
            lambda: validate_samba_config(command_runner),
            lambda: reload_samba(command_runner),
        ]
    )

    for step in steps:
        result = step()
        results.append(result)
        if result.status == "failed":
            break

    return [result.as_dict() for result in results]


def install_samba(command_runner: CommandRunner = run_command) -> OperationResult:
    steps = [
        (
            "Updating the package list",
            ["apt-get", "update"],
            None,
            {"DEBIAN_FRONTEND": "noninteractive"},
        ),
        (
            "Installing Windows file sharing support",
            ["apt-get", "install", "-y", "samba"],
            None,
            {"DEBIAN_FRONTEND": "noninteractive"},
        ),
        ("Checking Samba version", ["smbd", "--version"], None, None),
    ]
    details: list[str] = []
    for label, args, input_text, env in steps:
        result = command_runner(args, input_text, env)
        details.append(_command_detail(label, result))
        if result.returncode != 0:
            return failed("install_samba", "Install Samba", f"{label} failed.", details)

    service = _service_status(command_runner)
    details.extend(service.details)
    if service.status == "failed":
        return service
    return passed(
        "install_samba",
        "Install Samba",
        "Windows file sharing support is installed.",
        details,
    )


def verify_samba_installed(command_runner: CommandRunner = run_command) -> OperationResult:
    details: list[str] = []
    version_result = command_runner(["smbd", "--version"], None, None)
    details.append(_command_detail("Check Samba version", version_result))
    if version_result.returncode != 0:
        return failed(
            "verify_samba",
            "Check existing Samba",
            "Samba is no longer responding on this server.",
            details,
        )

    service = _service_status(command_runner)
    details.extend(service.details)
    return passed(
        "verify_samba",
        "Check existing Samba",
        "Existing Samba installation is ready.",
        details,
    )


def verify_existing_samba_user(
    username: str,
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    safe_username = sanitize_username(username)
    details: list[str] = []

    pdbedit_result = command_runner(["pdbedit", "-L"], None, None)
    details.append(_command_detail("Check existing Samba users", pdbedit_result))
    if pdbedit_result.returncode != 0:
        return failed(
            "existing_samba_user",
            "Check existing private user",
            "Existing Samba users could not be checked.",
            details,
        )

    samba_users = {
        line.split(":", 1)[0].strip()
        for line in pdbedit_result.stdout.splitlines()
        if ":" in line and line.split(":", 1)[0].strip()
    }
    if safe_username not in samba_users:
        return failed(
            "existing_samba_user",
            "Check existing private user",
            f"The Samba user {safe_username} was not found.",
            details,
        )

    id_result = command_runner(["id", "-u", safe_username], None, None)
    details.append(_command_detail("Check existing Linux user", id_result))
    if id_result.returncode != 0:
        return failed(
            "existing_samba_user",
            "Check existing private user",
            f"The Linux user {safe_username} was not found.",
            details,
        )

    return passed(
        "existing_samba_user",
        "Check existing private user",
        "Existing private user is ready for the new share.",
        details,
    )


def prepare_share_target(
    location: dict[str, Any],
    share_name: str,
    username: str,
    command_runner: CommandRunner = run_command,
    fstab_path: Path = FSTAB_PATH,
) -> OperationResult:
    if location.get("type") != "drive":
        return failed(
            "drive_required",
            "Prepare shared folder",
            "Choose an external or additional drive before applying setup.",
            [],
        )

    mount_path = Path(location["mount_path"])
    refresh_result = refresh_drive_metadata(location, command_runner)
    if refresh_result.status == "failed":
        return refresh_result

    mount_settings = prepare_drive_mount_settings(location, username, command_runner)
    if mount_settings.status == "failed":
        return mount_settings

    fstab_result = update_fstab_for_drive(fstab_path, location)
    if fstab_result.status == "failed":
        return fstab_result

    details = refresh_result.details + mount_settings.details + fstab_result.details
    reload_mounts = command_runner(["systemctl", "daemon-reload"], None, None)
    if reload_mounts.returncode != 0:
        return failed(
            "prepare_target",
            "Prepare shared folder",
            "The system mount manager did not reload the updated drive settings.",
            details + [_command_detail("Reload mount settings", reload_mounts)],
        )
    details.append(_command_detail("Reload mount settings", reload_mounts))

    mkdir_mount = command_runner(["mkdir", "-p", str(mount_path)], None, None)
    if mkdir_mount.returncode != 0:
        return failed("prepare_target", "Prepare shared folder", "The drive mount folder could not be created.", details + [_command_detail("Create drive mount folder", mkdir_mount)])
    mount_result = command_runner(["mount", str(mount_path)], None, None)
    if mount_result.returncode != 0:
        return failed("prepare_target", "Prepare shared folder", "The selected drive could not be mounted.", details + [_command_detail("Create drive mount folder", mkdir_mount), _command_detail("Mount selected drive", mount_result)])
    verify_result = command_runner(
        [
            "findmnt",
            "--json",
            "--mountpoint",
            str(mount_path),
            "--output",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        ],
        None,
        None,
    )
    verify_mount = verify_drive_mount_options(location, verify_result)
    if verify_result.returncode != 0:
        return failed(
            "prepare_target",
            "Prepare shared folder",
            "The selected drive mount could not be verified.",
            details
            + [
                _command_detail("Create drive mount folder", mkdir_mount),
                _command_detail("Mount selected drive", mount_result),
                _command_detail("Verify selected drive mount", verify_result),
            ],
        )
    if verify_mount.status == "failed":
        return failed(
            "prepare_target",
            "Prepare shared folder",
            verify_mount.summary,
            details
            + [
                _command_detail("Create drive mount folder", mkdir_mount),
                _command_detail("Mount selected drive", mount_result),
                _command_detail("Verify selected drive mount", verify_result),
            ]
            + verify_mount.details,
        )
    target_path = mount_path
    details = details + [
        _command_detail("Create drive mount folder", mkdir_mount),
        _command_detail("Mount selected drive", mount_result),
        _command_detail("Verify selected drive mount", verify_result),
    ] + verify_mount.details

    location["resolved_path"] = str(target_path)
    return passed(
        "prepare_target",
        "Prepare shared folder",
        "The selected drive is mounted and ready to share.",
        details,
    )


def ensure_samba_user(
    username: str,
    password: str,
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    safe_username = sanitize_username(username)
    details: list[str] = []
    id_result = command_runner(["id", "-u", safe_username], None, None)
    if id_result.returncode != 0:
        useradd_result = command_runner(
            ["useradd", "--create-home", "--shell", "/usr/sbin/nologin", safe_username],
            None,
            None,
        )
        details.append(_command_detail("Create private share user", useradd_result))
        if useradd_result.returncode != 0:
            return failed("samba_user", "Create private access", "The private share user could not be created.", details)
    else:
        details.append("Private share user already exists.")

    password_input = f"{password}\n{password}\n"
    smbpasswd_result = command_runner(["smbpasswd", "-a", "-s", safe_username], password_input, None)
    details.append(_command_detail("Set Samba password", smbpasswd_result))
    if smbpasswd_result.returncode != 0:
        return failed("samba_user", "Create private access", "The Samba password could not be set.", details)

    return passed("samba_user", "Create private access", "Private Windows sign-in is ready.", details)


def set_share_owner(
    location: dict[str, Any],
    username: str,
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    target_path = location.get("resolved_path") or location.get("mount_path") or location.get("path")
    safe_username = sanitize_username(username)
    if location.get("type") == "drive":
        if normalize_mount_access(location.get("mount_access")) == MOUNT_ACCESS_READ_ONLY:
            return passed(
                "folder_owner",
                "Lock folder to private user",
                "Read-only drive sharing does not change drive ownership.",
                [f"Drive folder: {target_path}"],
            )
        if filesystem_key(location.get("filesystem")) in USER_MOUNT_OPTION_FILESYSTEMS:
            return passed(
                "folder_owner",
                "Lock folder to private user",
                "Drive ownership is controlled by the mount settings.",
                [f"Drive folder: {target_path}"],
            )

    result = command_runner(["chown", f"{safe_username}:{safe_username}", target_path], None, None)
    if result.returncode != 0:
        return failed(
            "folder_owner",
            "Lock folder to private user",
            "The shared folder owner could not be set.",
            [_command_detail("Set folder owner", result)],
        )
    details = [_command_detail("Set folder owner", result)]
    if location.get("type") == "drive":
        chmod_result = command_runner(["chmod", "0770", target_path], None, None)
        details.append(_command_detail("Set drive folder permissions", chmod_result))
        if chmod_result.returncode != 0:
            return failed(
                "folder_owner",
                "Lock folder to private user",
                "The drive folder permissions could not be set.",
                details,
            )
    return passed(
        "folder_owner",
        "Lock folder to private user",
        "The shared folder is limited to the private user.",
        details,
    )


def configure_samba_share(
    location: dict[str, Any],
    share_name: str,
    username: str,
    smb_conf_path: Path = SMB_CONF_PATH,
) -> OperationResult:
    target_path = location.get("resolved_path") or location.get("mount_path") or location.get("path")
    read_only = (
        location.get("type") == "drive"
        and normalize_mount_access(location.get("mount_access")) == MOUNT_ACCESS_READ_ONLY
    )
    try:
        original = smb_conf_path.read_text(encoding="utf-8") if smb_conf_path.exists() else ""
        updated = update_smb_conf_text(original, share_name, target_path, sanitize_username(username), read_only=read_only)
        if updated != original:
            backup_file(smb_conf_path)
            smb_conf_path.parent.mkdir(parents=True, exist_ok=True)
            smb_conf_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return failed("samba_config", "Configure Windows sharing", "The Samba configuration could not be updated.", [str(exc)])

    return passed(
        "samba_config",
        "Configure Windows sharing",
        "The Windows share was added to Samba.",
        [f"Share name: {safe_name(share_name)}", f"Folder: {target_path}"],
    )


def validate_samba_config(command_runner: CommandRunner = run_command) -> OperationResult:
    result = command_runner(["testparm", "-s"], None, None)
    if result.returncode != 0:
        return failed("validate_samba", "Check sharing settings", "Samba rejected the configuration.", [_command_detail("Validate Samba configuration", result)])
    return passed("validate_samba", "Check sharing settings", "Samba accepted the configuration.", [_command_detail("Validate Samba configuration", result)])


def reload_samba(command_runner: CommandRunner = run_command) -> OperationResult:
    details: list[str] = []
    for args in (["systemctl", "restart", "smbd"], ["service", "smbd", "restart"]):
        result = command_runner(args, None, None)
        details.append(_command_detail("Restart Windows file sharing", result))
        if result.returncode == 0:
            return passed("restart_samba", "Restart Windows file sharing", "Windows file sharing restarted.", details)
    return failed("restart_samba", "Restart Windows file sharing", "Samba could not be restarted automatically.", details)


def update_smb_conf_text(
    original: str,
    share_name: str,
    path: str,
    username: str,
    *,
    read_only: bool = False,
) -> str:
    share = safe_name(share_name)
    start = f"# BEGIN SAMBA WIZARD SHARE {share}"
    end = f"# END SAMBA WIZARD SHARE {share}"
    block = "\n".join(
        [
            start,
            f"[{share}]",
            f"   path = {path}",
            f"   valid users = {username}",
            f"   read only = {'yes' if read_only else 'no'}",
            "   browsable = yes",
            "   create mask = 0660",
            "   directory mask = 0770",
            end,
            "",
        ]
    )
    return replace_managed_block(original, start, end, block)


def update_fstab_text(original: str, location: dict[str, Any]) -> str:
    uuid = location["uuid"]
    mount_path = location["mount_path"]
    filesystem = location.get("mount_fstype") or location["filesystem"]
    mount_options = location.get("mount_options") or fallback_mount_options(location)
    fsck_pass = str(location.get("fsck_pass", fallback_fsck_pass(location)))
    start = f"# BEGIN SAMBA WIZARD MOUNT {uuid}"
    end = f"# END SAMBA WIZARD MOUNT {uuid}"
    block = "\n".join(
        [
            start,
            f"UUID={uuid} {mount_path} {filesystem} {mount_options} 0 {fsck_pass}",
            end,
            "",
        ]
    )
    original = remove_managed_mount_blocks_for_path(original, mount_path)
    return replace_managed_block(original, start, end, block)


def update_fstab_for_drive(fstab_path: Path, location: dict[str, Any]) -> OperationResult:
    if not location.get("uuid") or not location.get("filesystem") or not location.get("mount_path"):
        return failed("fstab", "Prepare selected drive", "The selected drive is missing UUID, filesystem, or mount path details.", [])
    try:
        original = fstab_path.read_text(encoding="utf-8") if fstab_path.exists() else ""
        updated = update_fstab_text(original, location)
        if updated != original:
            backup_file(fstab_path)
            fstab_path.parent.mkdir(parents=True, exist_ok=True)
            fstab_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return failed("fstab", "Prepare selected drive", "The drive mount settings could not be updated.", [str(exc)])
    return passed("fstab", "Prepare selected drive", "The selected drive has a persistent mount entry.", [f"Mount folder: {location['mount_path']}"])


def prepare_drive_mount_settings(
    location: dict[str, Any],
    username: str,
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    filesystem = filesystem_key(location.get("filesystem"))
    mount_access = normalize_mount_access(location.get("mount_access"))
    location["mount_access"] = mount_access

    if filesystem in UNSUPPORTED_FILESYSTEMS:
        return failed(
            "mount_settings",
            "Prepare drive mount settings",
            "APFS drives are not supported by this wizard because Linux write support is experimental.",
            [f"Filesystem: {location.get('filesystem')}"],
        )
    if filesystem in READ_ONLY_ONLY_FILESYSTEMS and mount_access == MOUNT_ACCESS_READ_WRITE:
        return failed(
            "mount_settings",
            "Prepare drive mount settings",
            "Mac HFS drives can only be shared read-only.",
            ["Choose read-only for this drive, then apply again."],
        )

    mount_mode = "ro" if mount_access == MOUNT_ACCESS_READ_ONLY else "rw"
    details = [f"Drive access mode: {'read-only' if mount_mode == 'ro' else 'read/write'}."]
    if filesystem in USER_MOUNT_OPTION_FILESYSTEMS:
        ids = read_user_ids(username, command_runner)
        if ids.status == "failed":
            return ids
        uid, gid = ids.details
        mount_fstype = "ntfs-3g" if filesystem == "ntfs" else location["filesystem"]
        location["mount_fstype"] = mount_fstype
        location["mount_options"] = ",".join([mount_mode, "nofail", f"uid={uid}", f"gid={gid}", "umask=007"])
        location["fsck_pass"] = "0"
        details.append(f"Drive mount type: {mount_fstype}.")
        details.append(f"Drive mount options: {location['mount_options']}.")
    else:
        location["mount_fstype"] = location["filesystem"]
        location["mount_options"] = ",".join(["defaults", mount_mode, "nofail"])
        location["fsck_pass"] = "2" if filesystem in LINUX_FILESYSTEMS else "0"
        details.append(f"Drive mount options: {location['mount_options']}.")

    return passed(
        "mount_settings",
        "Prepare drive mount settings",
        "The selected drive mount settings are ready.",
        details,
    )


def read_user_ids(
    username: str,
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    safe_username = sanitize_username(username)
    uid_result = command_runner(["id", "-u", safe_username], None, None)
    if uid_result.returncode != 0 or not uid_result.stdout.strip():
        return failed(
            "mount_settings",
            "Prepare drive mount settings",
            "The private share user's Linux ID could not be read.",
            [_command_detail("Read private user id", uid_result)],
        )
    gid_result = command_runner(["id", "-g", safe_username], None, None)
    if gid_result.returncode != 0 or not gid_result.stdout.strip():
        return failed(
            "mount_settings",
            "Prepare drive mount settings",
            "The private share user's Linux group ID could not be read.",
            [
                _command_detail("Read private user id", uid_result),
                _command_detail("Read private group id", gid_result),
            ],
        )
    return passed(
        "mount_settings",
        "Prepare drive mount settings",
        "The private share user's Linux IDs were read.",
        [uid_result.stdout.strip().splitlines()[0], gid_result.stdout.strip().splitlines()[0]],
    )


def fallback_mount_options(location: dict[str, Any]) -> str:
    mount_mode = "ro" if normalize_mount_access(location.get("mount_access")) == MOUNT_ACCESS_READ_ONLY else "rw"
    return ",".join(["defaults", mount_mode, "nofail"])


def fallback_fsck_pass(location: dict[str, Any]) -> str:
    return "2" if filesystem_key(location.get("filesystem")) in LINUX_FILESYSTEMS else "0"


def verify_drive_mount_options(
    location: dict[str, Any],
    findmnt_result: CommandResult,
) -> OperationResult:
    if findmnt_result.returncode != 0:
        return failed(
            "mount_verify",
            "Verify selected drive mount",
            "The selected drive mount could not be verified.",
            [],
        )
    options = parse_findmnt_options(findmnt_result.stdout)
    if not options:
        return failed(
            "mount_verify",
            "Verify selected drive mount",
            "The selected drive mount options could not be read.",
            [],
        )

    requested_access = normalize_mount_access(location.get("mount_access"))
    if requested_access == MOUNT_ACCESS_READ_WRITE and "ro" in options and "rw" not in options:
        return failed(
            "mount_verify",
            "Verify selected drive mount",
            "Read/write was selected, but Linux mounted the drive read-only.",
            [f"Mounted options: {','.join(options)}"],
        )
    if requested_access == MOUNT_ACCESS_READ_ONLY and "ro" not in options:
        return failed(
            "mount_verify",
            "Verify selected drive mount",
            "Read-only was selected, but Linux did not report a read-only mount.",
            [f"Mounted options: {','.join(options)}"],
        )
    return passed(
        "mount_verify",
        "Verify selected drive mount",
        "The selected drive mount options match the requested access.",
        [f"Mounted options: {','.join(options)}"],
    )


def parse_findmnt_options(output: str) -> list[str]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    for item in data.get("filesystems") or []:
        options = item.get("options") or ""
        if isinstance(options, str):
            return [option for option in options.split(",") if option]
    return []


def verify_drive_write_access(
    location: dict[str, Any],
    username: str,
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    if normalize_mount_access(location.get("mount_access")) == MOUNT_ACCESS_READ_ONLY:
        return passed(
            "drive_write_probe",
            "Check drive write access",
            "Read-only drive sharing does not need a write test.",
            [f"Drive folder: {location.get('resolved_path') or location.get('mount_path')}"],
        )

    target_path = Path(location.get("resolved_path") or location["mount_path"])
    probe_path = target_path / f".samwizard-write-test-{os.getpid()}"
    safe_username = sanitize_username(username)
    touch_result = command_runner(["runuser", "-u", safe_username, "--", "touch", str(probe_path)], None, None)
    if touch_result.returncode != 0:
        return failed(
            "drive_write_probe",
            "Check drive write access",
            "Read/write was selected, but the private share user could not write to the drive.",
            [_command_detail("Create drive write test file", touch_result)],
        )
    remove_result = command_runner(["runuser", "-u", safe_username, "--", "rm", "-f", str(probe_path)], None, None)
    if remove_result.returncode != 0:
        return failed(
            "drive_write_probe",
            "Check drive write access",
            "The drive write test file could not be removed.",
            [
                _command_detail("Create drive write test file", touch_result),
                _command_detail("Remove drive write test file", remove_result),
            ],
        )
    return passed(
        "drive_write_probe",
        "Check drive write access",
        "The private share user can write to the drive.",
        [
            _command_detail("Create drive write test file", touch_result),
            _command_detail("Remove drive write test file", remove_result),
        ],
    )


def refresh_drive_metadata(
    location: dict[str, Any],
    command_runner: CommandRunner = run_command,
) -> OperationResult:
    selected_path = location.get("path")
    if not selected_path:
        return failed(
            "drive_refresh",
            "Refresh selected drive",
            "The selected drive is missing its Linux device path.",
            [],
        )

    old_uuid = location.get("uuid")
    details: list[str] = []
    current = find_current_drive_by_path(selected_path, command_runner)
    if current:
        details.append(f"Fresh drive scan matched {selected_path}.")
    else:
        details.append(f"Fresh drive scan did not find {selected_path}; checking blkid.")

    if not current or not current.get("uuid") or not current.get("filesystem"):
        blkid_current, blkid_detail = read_blkid_drive_metadata(selected_path, command_runner)
        details.append(blkid_detail)
        if blkid_current:
            current = {**(current or {}), **blkid_current}

    if not current:
        return failed(
            "drive_refresh",
            "Refresh selected drive",
            f"The selected drive path {selected_path} was not found. It may have been disconnected or renamed.",
            details,
        )

    uuid = current.get("uuid")
    filesystem = current.get("filesystem")
    if not uuid or not filesystem:
        return failed(
            "drive_refresh",
            "Refresh selected drive",
            f"The selected drive at {selected_path} is missing a current UUID or filesystem.",
            details,
        )

    location.update(
        {
            "path": selected_path,
            "uuid": uuid,
            "filesystem": filesystem,
            "mountpoints": current.get("mountpoints") or location.get("mountpoints") or [],
            "size": current.get("size") or location.get("size"),
            "label": current.get("label") or location.get("label"),
        }
    )

    if old_uuid and old_uuid != uuid:
        details.append(f"Drive UUID refreshed from {old_uuid} to {uuid} before writing fstab.")
    else:
        details.append(f"Drive UUID confirmed as {uuid} before writing fstab.")
    details.append(f"Drive filesystem confirmed as {filesystem}.")
    return passed(
        "drive_refresh",
        "Refresh selected drive",
        "The selected drive details were refreshed.",
        details,
    )


def find_current_drive_by_path(
    selected_path: str,
    command_runner: CommandRunner = run_command,
) -> dict[str, Any] | None:
    def detection_runner(args: list[str]) -> CommandResult:
        return command_runner(args, None, None)

    drives = detect_drives(detection_runner)
    if not drives.get("available"):
        return None
    return next(
        (
            item
            for item in drives.get("items", [])
            if item.get("path") == selected_path
        ),
        None,
    )


def read_blkid_drive_metadata(
    selected_path: str,
    command_runner: CommandRunner = run_command,
) -> tuple[dict[str, Any] | None, str]:
    result = command_runner(["blkid", "-o", "export", selected_path], None, None)
    detail = _command_detail("Read selected drive identity fallback", result)
    if result.returncode != 0 or not result.stdout.strip():
        return None, detail

    values = parse_blkid_export(result.stdout)
    uuid = values.get("UUID")
    filesystem = values.get("TYPE")
    if not uuid or not filesystem:
        return None, detail

    return (
        {
            "path": values.get("DEVNAME") or selected_path,
            "uuid": uuid,
            "filesystem": filesystem,
            "label": values.get("LABEL"),
        },
        detail,
    )


def parse_blkid_export(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            values[key] = value
    return values


def remove_managed_mount_blocks_for_path(original: str, mount_path: str) -> str:
    lines = original.splitlines(keepends=True)
    output: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.startswith("# BEGIN SAMBA WIZARD MOUNT "):
            output.append(line)
            index += 1
            continue

        block_lines = [line]
        index += 1
        while index < len(lines):
            block_lines.append(lines[index])
            if lines[index].startswith("# END SAMBA WIZARD MOUNT "):
                index += 1
                break
            index += 1

        block = "".join(block_lines)
        if managed_mount_block_uses_path(block, mount_path):
            if index < len(lines) and not lines[index].strip():
                index += 1
            continue
        output.append(block)

    return "".join(output).rstrip() + ("\n" if output else "")


def managed_mount_block_uses_path(block: str, mount_path: str) -> bool:
    for line in block.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == mount_path:
            return True
    return False


def replace_managed_block(original: str, start: str, end: str, block: str) -> str:
    if start in original and end in original:
        before, rest = original.split(start, 1)
        _old, after = rest.split(end, 1)
        return before.rstrip() + "\n\n" + block + after.lstrip("\n")
    separator = "\n\n" if original.strip() else ""
    return original.rstrip() + separator + block


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.samba-wizard.{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def sanitize_username(username: str) -> str:
    return safe_name(username.lower(), default="sambauser")


def passed(id: str, title: str, summary: str, details: list[str]) -> OperationResult:
    return OperationResult(id=id, title=title, status="passed", summary=summary, details=details)


def failed(id: str, title: str, summary: str, details: list[str]) -> OperationResult:
    return OperationResult(id=id, title=title, status="failed", summary=summary, details=details)


def _service_status(command_runner: CommandRunner) -> OperationResult:
    details: list[str] = []
    systemctl = command_runner(["systemctl", "is-active", "smbd"], None, None)
    details.append(_command_detail("Check Samba service", systemctl))
    if systemctl.returncode == 0:
        return passed("samba_service", "Check Samba service", "Samba service is active.", details)

    service = command_runner(["service", "smbd", "status"], None, None)
    details.append(_command_detail("Check Samba service fallback", service))
    if service.returncode == 0:
        return passed("samba_service", "Check Samba service", "Samba service responded.", details)

    return passed(
        "samba_service",
        "Check Samba service",
        "Samba is installed. Service status could not be confirmed in this environment.",
        details,
    )


def _command_detail(label: str, result: CommandResult) -> str:
    command = " ".join(result.args)
    output = (result.stdout or result.stderr or "").strip()
    if output:
        first_line = output.splitlines()[0]
        return f"{label}: `{command}` exited {result.returncode}. {first_line}"
    return f"{label}: `{command}` exited {result.returncode}."
