from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.academy.tree import parent_titles_by_skill, skills_by_id, starting_skill_ids
from app.settings import samwizard_state_dir


SCHEMA_VERSION = 1
PROGRESS_FILENAME = "academy-progress.json"


class AcademyProgressError(ValueError):
    """Raised when an Academy progress action is not allowed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def progress_path(state_dir: Path | None = None) -> Path:
    directory = state_dir or samwizard_state_dir()
    return directory / PROGRESS_FILENAME


def initial_progress(tree: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "active_tree_id": tree["id"],
        "completed_skill_ids": [],
        "unlocked_skill_ids": unlocked_skill_ids_for_completed(tree, []),
        "last_opened_skill_id": None,
        "updated_at": utc_now(),
    }


def load_progress(tree: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = path or progress_path()
    try:
        with target.open(encoding="utf-8") as file:
            progress = json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return initial_progress(tree)

    if not isinstance(progress, dict):
        return initial_progress(tree)
    if progress.get("schema_version") != SCHEMA_VERSION:
        return initial_progress(tree)
    if progress.get("active_tree_id") != tree["id"]:
        return initial_progress(tree)

    skill_ids = set(skills_by_id(tree))
    completed = clean_id_list(progress.get("completed_skill_ids"), skill_ids)
    unlocked = unlocked_skill_ids_for_completed(tree, completed)
    last_opened = progress.get("last_opened_skill_id")
    if last_opened is not None and last_opened not in skill_ids:
        last_opened = None

    return {
        "schema_version": SCHEMA_VERSION,
        "active_tree_id": tree["id"],
        "completed_skill_ids": completed,
        "unlocked_skill_ids": unlocked,
        "last_opened_skill_id": last_opened,
        "updated_at": str(progress.get("updated_at") or utc_now()),
    }


def load_or_create_progress(tree: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    target = path or progress_path()
    progress = load_progress(tree, target)
    if not target.exists():
        save_progress(progress, target)
    return progress


def clean_id_list(value: Any, valid_ids: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        if isinstance(item, str) and item in valid_ids and item not in cleaned:
            cleaned.append(item)
    return cleaned


def parent_ids_by_skill(tree: dict[str, Any]) -> dict[str, list[str]]:
    parents: dict[str, list[str]] = {skill["id"]: [] for skill in tree["skills"]}
    for skill in tree["skills"]:
        parent_id = skill["id"]
        for child_id in skill.get("unlocks", []):
            if child_id in parents and parent_id not in parents[child_id]:
                parents[child_id].append(parent_id)
    return parents


def unlocked_skill_ids_for_completed(tree: dict[str, Any], completed_ids: list[str]) -> list[str]:
    completed = set(completed_ids)
    starters = set(starting_skill_ids(tree))
    parents = parent_ids_by_skill(tree)
    unlocked: list[str] = []

    for skill in tree["skills"]:
        skill_id = skill["id"]
        parent_ids = parents.get(skill_id, [])
        if (
            skill_id in starters
            or skill_id in completed
            or (parent_ids and all(parent_id in completed for parent_id in parent_ids))
        ):
            unlocked.append(skill_id)

    return unlocked


def save_progress(progress: dict[str, Any], path: Path | None = None) -> None:
    target = path or progress_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=target.parent,
            encoding="utf-8",
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as file:
            temp_name = file.name
            json.dump(progress, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        Path(temp_name).replace(target)
    finally:
        if temp_name:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()


def open_skill(tree: dict[str, Any], skill_id: str, path: Path | None = None) -> dict[str, Any]:
    if skill_id not in skills_by_id(tree):
        raise AcademyProgressError("That Academy skill does not exist.")
    progress = load_progress(tree, path)
    progress["last_opened_skill_id"] = skill_id
    progress["updated_at"] = utc_now()
    save_progress(progress, path)
    return progress


def complete_skill(tree: dict[str, Any], skill_id: str, path: Path | None = None) -> dict[str, Any]:
    skill_lookup = skills_by_id(tree)
    skill = skill_lookup.get(skill_id)
    if skill is None:
        raise AcademyProgressError("That Academy skill does not exist.")

    progress = load_progress(tree, path)
    completed = list(progress["completed_skill_ids"])

    if skill_id not in progress["unlocked_skill_ids"] and skill_id not in completed:
        raise AcademyProgressError("Complete the prerequisite skill before marking this one complete.")

    if skill_id not in completed:
        completed.append(skill_id)

    progress["completed_skill_ids"] = completed
    progress["unlocked_skill_ids"] = unlocked_skill_ids_for_completed(tree, completed)
    progress["last_opened_skill_id"] = skill_id
    progress["updated_at"] = utc_now()
    save_progress(progress, path)
    return progress


def reset_progress(
    tree: dict[str, Any],
    confirm: str,
    path: Path | None = None,
) -> dict[str, Any]:
    if confirm != "RESET":
        raise AcademyProgressError("Type RESET to confirm progress reset.")
    progress = initial_progress(tree)
    save_progress(progress, path)
    return progress


def serialize_state(tree: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    completed = set(progress["completed_skill_ids"])
    unlocked = set(progress["unlocked_skill_ids"])
    parent_titles = parent_titles_by_skill(tree)

    skills: list[dict[str, Any]] = []
    for skill in tree["skills"]:
        skill_id = skill["id"]
        if skill_id in completed:
            status = "completed"
        elif skill_id in unlocked:
            status = "unlocked"
        else:
            status = "locked"
        skills.append(
            {
                "id": skill_id,
                "title": skill["title"],
                "summary": skill["summary"],
                "icon": skill.get("icon", {"type": "builtin", "name": "spark"}),
                "map": skill["map"],
                "layout": skill.get("layout", {}),
                "unlocks": skill.get("unlocks", []),
                "lesson": skill["lesson"],
                "status": status,
                "locked_reason": locked_reason(parent_titles.get(skill_id, []), status),
            }
        )

    return {
        "tree": {
            "id": tree["id"],
            "title": tree["title"],
            "summary": tree.get("summary", ""),
        },
        "progress": progress,
        "skills": skills,
    }


def locked_reason(parent_titles: list[str], status: str) -> str:
    if status != "locked":
        return ""
    if not parent_titles:
        return "This skill is locked until the Academy opens it."
    if len(parent_titles) == 1:
        return f"Complete {parent_titles[0]} to unlock this skill."
    return "Complete all prerequisite lessons to unlock this: " + ", ".join(parent_titles) + "."
