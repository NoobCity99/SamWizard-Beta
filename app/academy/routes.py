from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.academy.notepad import load_notepad, save_notepad_text
from app.academy.progress import (
    AcademyProgressError,
    complete_skill,
    load_or_create_progress,
    open_skill,
    reset_progress,
    serialize_state,
)
from app.academy.tree import load_tree
from app.settings import beta_build_label, beta_enabled, beta_label


router = APIRouter(prefix="/academy", tags=["academy"])
templates = Jinja2Templates(directory="app/templates")
STATIC_DIR = Path("app/static")
ACADEMY_ASSET_DIR = STATIC_DIR / "assets" / "academy"
BETA_ACADEMY_SKILL_IDS = ("ubuntu-server", "terminal-basics", "server-users")


def beta_context() -> dict[str, Any]:
    return {
        "beta_enabled": beta_enabled(),
        "beta_label": beta_label(),
        "beta_build_label": beta_build_label(),
    }


def academy_assets() -> dict[str, dict[str, str | bool]]:
    banner_filename = "academy_banner.png"
    return {
        "banner": {
            "filename": banner_filename,
            "ratio": "1920x631",
            "src": f"/assets/academy/{banner_filename}",
            "exists": (ACADEMY_ASSET_DIR / banner_filename).is_file(),
        }
    }


def academy_state() -> dict[str, Any]:
    tree = beta_limited_tree(load_tree()) if beta_enabled() else load_tree()
    progress = load_or_create_progress(tree)
    return serialize_state(tree, progress)


def beta_limited_tree(tree: dict[str, Any]) -> dict[str, Any]:
    allowed = set(BETA_ACADEMY_SKILL_IDS)
    limited = dict(tree)
    skills = []
    for skill in tree["skills"]:
        if skill["id"] not in allowed:
            continue
        visible_skill = dict(skill)
        visible_skill["unlocks"] = [
            skill_id for skill_id in visible_skill.get("unlocks", []) if skill_id in allowed
        ]
        skills.append(visible_skill)
    limited["skills"] = skills
    return limited


def academy_tree_for_mode() -> dict[str, Any]:
    tree = load_tree()
    return beta_limited_tree(tree) if beta_enabled() else tree


@router.get("")
def academy_page(request: Request):
    return templates.TemplateResponse(
        "academy.html",
        {
            "request": request,
            "assets": academy_assets(),
            **beta_context(),
        },
    )


@router.get("/api/state")
def get_academy_state():
    return JSONResponse(academy_state())


@router.get("/api/notepad")
def get_academy_notepad():
    return JSONResponse(load_notepad())


@router.put("/api/notepad")
def save_academy_notepad(payload: dict[str, Any] | None = Body(default=None)):
    text = (payload or {}).get("text", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Notepad text must be a string.")
    return JSONResponse(save_notepad_text(text))


@router.post("/api/skills/{skill_id}/open")
def open_academy_skill(skill_id: str):
    tree = academy_tree_for_mode()
    try:
        progress = open_skill(tree, skill_id)
    except AcademyProgressError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(serialize_state(tree, progress))


@router.post("/api/skills/{skill_id}/complete")
def complete_academy_skill(skill_id: str):
    tree = academy_tree_for_mode()
    try:
        progress = complete_skill(tree, skill_id)
    except AcademyProgressError as exc:
        message = str(exc)
        status_code = 403 if "prerequisite" in message else 404
        raise HTTPException(status_code=status_code, detail=message) from exc
    return JSONResponse(serialize_state(tree, progress))


@router.post("/api/progress/reset")
def reset_academy_progress(payload: dict[str, str] | None = Body(default=None)):
    tree = academy_tree_for_mode()
    try:
        progress = reset_progress(tree, (payload or {}).get("confirm", ""))
    except AcademyProgressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(serialize_state(tree, progress))
