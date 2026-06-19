from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from app.settings import beta_enabled, samwizard_state_dir
from app.system_actions import CommandResult, run_command


MAX_LOG_ENTRIES = 100
MAX_OUTPUT_CHARS = 2000
MASKED_STDIN = "********"
LOG_ID_KEY = "command_log_id"
BETA_LOG_DIRNAME = "beta-command-logs"

_LOGS: dict[str, list[dict[str, Any]]] = {}
_LOCK = Lock()


def command_log_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    log_id = command_log_id_from_state(state)
    with _LOCK:
        return list(_LOGS.setdefault(log_id, []))


def command_log_id_from_state(state: dict[str, Any]) -> str:
    old_log = state.pop("command_log", None)
    log_id = state.get(LOG_ID_KEY)
    if not isinstance(log_id, str) or not log_id:
        log_id = uuid4().hex
        state[LOG_ID_KEY] = log_id

    with _LOCK:
        log = _LOGS.setdefault(log_id, [])
        if isinstance(old_log, list) and old_log and not log:
            log.extend(old_log[-MAX_LOG_ENTRIES:])
    return log_id


def clear_command_log(state: dict[str, Any]) -> None:
    log_id = command_log_id_from_state(state)
    with _LOCK:
        _LOGS[log_id] = []


def add_log_entry(
    state: dict[str, Any],
    *,
    phase: str,
    command: list[str] | str,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    stdin_hidden: bool = False,
    summary: str = "",
) -> None:
    log_id = command_log_id_from_state(state)
    if isinstance(command, list):
        display_command = " ".join(command)
    else:
        display_command = command

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": phase,
        "command": display_command,
        "exit_code": exit_code,
        "stdin": MASKED_STDIN if stdin_hidden else "",
        "stdin_hidden": stdin_hidden,
        "stdout": truncate_output(stdout),
        "stderr": truncate_output(stderr),
        "summary": summary or default_summary(exit_code),
    }
    with _LOCK:
        log = _LOGS.setdefault(log_id, [])
        log.append(entry)
        del log[:-MAX_LOG_ENTRIES]
    mirror_beta_log_entry(log_id, entry)


def beta_command_log_path(log_id: str) -> Path:
    return samwizard_state_dir() / BETA_LOG_DIRNAME / f"{log_id}.jsonl"


def mirror_beta_log_entry(log_id: str, entry: dict[str, Any]) -> None:
    if not beta_enabled():
        return
    try:
        path = beta_command_log_path(log_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        return


def persisted_command_log_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    log_id = command_log_id_from_state(state)
    try:
        lines = beta_command_log_path(log_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries[-MAX_LOG_ENTRIES:]


def truncate_output(value: str) -> str:
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return value[:MAX_OUTPUT_CHARS] + "\n... output truncated ..."


def default_summary(exit_code: int | None) -> str:
    if exit_code is None:
        return "Recorded note."
    if exit_code == 0:
        return "Command completed."
    return "Command reported a problem."


def result_summary(result: CommandResult) -> str:
    if getattr(result, "timed_out", False):
        return "Command timed out."
    return default_summary(result.returncode)


def logged_command_runner(
    state: dict[str, Any],
    phase: str,
    *,
    base_runner: Callable[[list[str], str | None, dict[str, str] | None], CommandResult] = run_command,
    secret_commands: tuple[str, ...] = ("smbpasswd",),
    log_start: bool | None = None,
) -> Callable[[list[str], str | None, dict[str, str] | None], CommandResult]:
    def runner(
        args: list[str],
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        command_name = args[0] if args else ""
        hide_stdin = input_text is not None or command_name in secret_commands
        should_log_start = phase == "Apply" if log_start is None else log_start
        if should_log_start:
            add_log_entry(
                state,
                phase=phase,
                command=args,
                exit_code=None,
                stdin_hidden=hide_stdin,
                summary="Starting command...",
            )

        result = base_runner(args, input_text, env)
        add_log_entry(
            state,
            phase=phase,
            command=args,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            stdin_hidden=hide_stdin,
            summary=result_summary(result),
        )
        return result

    return runner


def logged_detection_runner(
    state: dict[str, Any],
    phase: str = "System Check",
    *,
    base_runner: Callable[[list[str], str | None, dict[str, str] | None], CommandResult] = run_command,
) -> Callable[[list[str]], CommandResult | None]:
    command_runner = logged_command_runner(state, phase, base_runner=base_runner)

    def runner(args: list[str]) -> CommandResult | None:
        return command_runner(args, None, None)

    return runner
