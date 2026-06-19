from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_TREE_ID = "ubuntu_cli_basics"
DEFAULT_TREE_PATH = Path(__file__).resolve().parent / "trees" / f"{DEFAULT_TREE_ID}.json"
ALLOWED_LAYOUT_SLOTS = {
    "top-left",
    "top-center",
    "top-right",
    "upper-left",
    "upper-right",
    "middle-left",
    "middle-right",
    "far-left",
    "lower-left",
    "lower-center",
    "bottom-center",
    "far-right",
}


class TreeValidationError(ValueError):
    """Raised when an Academy skill tree file is not valid."""


def load_tree(path: Path = DEFAULT_TREE_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        tree = json.load(file)
    validate_tree(tree)
    return tree


def validate_tree(tree: dict[str, Any]) -> None:
    if not isinstance(tree, dict):
        raise TreeValidationError("Academy tree must be a JSON object.")
    for field in ("id", "title", "skills"):
        if field not in tree:
            raise TreeValidationError(f"Academy tree is missing {field!r}.")
    if not isinstance(tree["id"], str) or not tree["id"].strip():
        raise TreeValidationError("Academy tree id must be a non-empty string.")
    if not isinstance(tree["title"], str) or not tree["title"].strip():
        raise TreeValidationError("Academy tree title must be a non-empty string.")
    if not isinstance(tree["skills"], list) or not tree["skills"]:
        raise TreeValidationError("Academy tree skills must be a non-empty list.")

    seen: set[str] = set()
    for skill in tree["skills"]:
        validate_skill(skill)
        skill_id = skill["id"]
        if skill_id in seen:
            raise TreeValidationError(f"Duplicate Academy skill id: {skill_id}.")
        seen.add(skill_id)

    for skill in tree["skills"]:
        for target_id in skill.get("unlocks", []):
            if target_id not in seen:
                raise TreeValidationError(
                    f"Skill {skill['id']} unlocks missing skill {target_id}."
                )


def validate_skill(skill: Any) -> None:
    if not isinstance(skill, dict):
        raise TreeValidationError("Each Academy skill must be an object.")
    for field in ("id", "title", "summary", "map", "lesson"):
        if field not in skill:
            raise TreeValidationError(f"Academy skill is missing {field!r}.")
    for field in ("id", "title", "summary"):
        if not isinstance(skill[field], str) or not skill[field].strip():
            raise TreeValidationError(f"Academy skill {field!r} must be a non-empty string.")

    map_position = skill["map"]
    if not isinstance(map_position, dict):
        raise TreeValidationError(f"Skill {skill['id']} map must be an object.")
    for axis in ("row", "col"):
        if not isinstance(map_position.get(axis), int):
            raise TreeValidationError(f"Skill {skill['id']} map.{axis} must be an integer.")

    unlocks = skill.get("unlocks", [])
    if not isinstance(unlocks, list) or not all(isinstance(item, str) for item in unlocks):
        raise TreeValidationError(f"Skill {skill['id']} unlocks must be a list of ids.")
    if "starts_unlocked" in skill and not isinstance(skill["starts_unlocked"], bool):
        raise TreeValidationError(f"Skill {skill['id']} starts_unlocked must be true or false.")

    validate_layout(skill)

    icon = skill.get("icon", {"type": "builtin", "name": "spark"})
    if not isinstance(icon, dict) or not isinstance(icon.get("type"), str):
        raise TreeValidationError(f"Skill {skill['id']} icon must be an object.")

    lesson = skill["lesson"]
    if not isinstance(lesson, dict):
        raise TreeValidationError(f"Skill {skill['id']} lesson must be an object.")
    intro = lesson.get("intro", [])
    if not isinstance(intro, list) or not all(isinstance(item, str) for item in intro):
        raise TreeValidationError(f"Skill {skill['id']} lesson intro must be a list of strings.")
    commands = lesson.get("commands", [])
    if not isinstance(commands, list):
        raise TreeValidationError(f"Skill {skill['id']} lesson commands must be a list.")
    for command in commands:
        validate_command(skill["id"], command)
    for field in ("exercise", "paste_prompt"):
        if field in lesson and not isinstance(lesson[field], str):
            raise TreeValidationError(f"Skill {skill['id']} lesson {field} must be a string.")


def validate_command(skill_id: str, command: Any) -> None:
    if not isinstance(command, dict):
        raise TreeValidationError(f"Skill {skill_id} command entries must be objects.")
    for field in ("command", "origin", "purpose", "outcome"):
        if not isinstance(command.get(field), str) or not command[field].strip():
            raise TreeValidationError(
                f"Skill {skill_id} command {field!r} must be a non-empty string."
            )


def validate_layout(skill: dict[str, Any]) -> None:
    layout = skill.get("layout")
    if layout is None:
        return
    if not isinstance(layout, dict):
        raise TreeValidationError(f"Skill {skill['id']} layout must be an object.")
    if "slot" in layout and layout["slot"] not in ALLOWED_LAYOUT_SLOTS:
        raise TreeValidationError(f"Skill {skill['id']} layout.slot is not a known slot.")
    for field in ("rank", "lane"):
        if field in layout:
            value = layout[field]
            if not isinstance(value, int) or value < 0:
                raise TreeValidationError(
                    f"Skill {skill['id']} layout.{field} must be a non-negative integer."
                )
    if "group" in layout and not isinstance(layout["group"], str):
        raise TreeValidationError(f"Skill {skill['id']} layout.group must be a string.")


def skills_by_id(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {skill["id"]: skill for skill in tree["skills"]}


def parent_titles_by_skill(tree: dict[str, Any]) -> dict[str, list[str]]:
    parents: dict[str, list[str]] = {skill["id"]: [] for skill in tree["skills"]}
    for skill in tree["skills"]:
        for child_id in skill.get("unlocks", []):
            parents.setdefault(child_id, []).append(skill["title"])
    return parents


def starting_skill_ids(tree: dict[str, Any]) -> list[str]:
    return [skill["id"] for skill in tree["skills"] if skill.get("starts_unlocked", False)]
