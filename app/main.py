from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.academy.routes import router as academy_router
from app.apply_jobs import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    get_apply_job,
    start_apply_job,
)
from app.beta_diagnostics import build_diagnostic_bundle, tester_notes
from app.command_log import (
    LOG_ID_KEY,
    add_log_entry,
    clear_command_log,
    command_log_id_from_state,
    command_log_from_state,
    logged_command_runner,
    logged_detection_runner,
)
from app.firewall_manager import (
    apply_firewall_rules,
    firewall_context as build_firewall_context,
)
from app.share_targets import (
    DEFAULT_MOUNT_ACCESS,
    drive_diagnostics,
    has_eligible_drive,
    mount_access_error,
    mount_access_label,
    normalize_mount_access,
    safe_name,
    selected_location_from,
    share_locations,
)
from app.settings import beta_build_label, beta_enabled, beta_label, session_secret_key
from app.system_actions import apply_share_setup
from app.system_checks import system_checks_from_info, system_summary as build_system_summary
from app.system_info import detect_internet_connectivity, detect_system_info
from app.tailscale_auth_state import load_pending_tailscale_auth, save_pending_tailscale_auth
from app.tailscale_manager import (
    detect_tailscale,
    install_tailscale,
    start_tailscale_login,
)
from app.wifi_actions import apply_wifi_setup


app = FastAPI(title="SamWizard")
app.add_middleware(SessionMiddleware, secret_key=session_secret_key())
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(academy_router)

templates = Jinja2Templates(directory="app/templates")


STATIC_DIR = Path("app/static")
LANDING_ASSET_DIR = STATIC_DIR / "assets" / "landing"
LANDING_ASSET_CONFIG = {
    "banner": {
        "filename": "landing_banner.png",
        "ratio": "1920x631",
    },
    "samba_intro": {
        "filename": "landing_samba_intro.png",
        "ratio": "16:9",
    },
    "server_intro": {
        "filename": "landing_server_intro.png",
        "ratio": "16:9",
    },
    "wiz_academy_1": {
        "filename": "WizAcademy1.png",
        "ratio": "16:9",
    },
    "wiz_academy_2": {
        "filename": "WizAcademy2.png",
        "ratio": "16:9",
    },
}
STEP_ORDER = [
    "Welcome",
    "System Check",
    "Drive Selection",
    "Share Name",
    "User Setup",
    "Review",
    "Apply",
    "Done",
]
ADD_DRIVE_STEP_ORDER = [
    "Welcome",
    "System Check",
    "Drive Selection",
    "Share Name",
    "Review",
    "Apply",
    "Done",
]
TAILSCALE_STEP_ORDER = [
    "Choose Share",
    "What Changes",
    "Check",
    "Install",
    "Authorize",
    "Firewall",
    "New Address",
    "Windows PC",
]
SETUP_MODE_INITIAL = "initial_setup"
SETUP_MODE_ADD_DRIVE = "add_drive"
SETUP_MODE_UNSUPPORTED = "unsupported_existing_samba"
RUNNING_JOB_STATUSES = {STATUS_PENDING, STATUS_RUNNING}


def wizard_state(request: Request) -> dict[str, Any]:
    state = request.session.setdefault("wizard", {})
    if not isinstance(state, dict):
        state = {}
        request.session["wizard"] = state
    return state


def tailscale_state(request: Request) -> dict[str, Any]:
    state = wizard_state(request)
    tailscale = state.setdefault("tailscale", {})
    if not isinstance(tailscale, dict):
        tailscale = {}
        state["tailscale"] = tailscale
    return tailscale


def pending_tailscale_auth_from_state(state: dict[str, Any]) -> dict[str, Any]:
    return load_pending_tailscale_auth(state.get(LOG_ID_KEY))


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def beta_context() -> dict[str, Any]:
    return {
        "beta_enabled": beta_enabled(),
        "beta_label": beta_label(),
        "beta_build_label": beta_build_label(),
    }


def landing_assets() -> dict[str, dict[str, str | bool]]:
    assets: dict[str, dict[str, str | bool]] = {}
    for name, details in LANDING_ASSET_CONFIG.items():
        filename = details["filename"]
        assets[name] = {
            "filename": filename,
            "ratio": details["ratio"],
            "src": f"/assets/landing/{filename}",
            "exists": (LANDING_ASSET_DIR / filename).is_file(),
        }
    return assets


def selected_location(state: dict[str, Any]) -> dict[str, Any] | None:
    location = state.get("selected_location")
    if isinstance(location, dict) and location.get("type") == "drive":
        return location
    return None


def setup_mode(state: dict[str, Any]) -> str:
    mode = state.get("samba_setup_mode")
    if mode in {SETUP_MODE_INITIAL, SETUP_MODE_ADD_DRIVE, SETUP_MODE_UNSUPPORTED}:
        return mode
    return SETUP_MODE_INITIAL


def is_add_drive_mode(state: dict[str, Any]) -> bool:
    return setup_mode(state) == SETUP_MODE_ADD_DRIVE


def step_order_for_state(state: dict[str, Any]) -> list[str]:
    if is_add_drive_mode(state):
        return ADD_DRIVE_STEP_ORDER
    return STEP_ORDER


def apply_samba_setup_mode(state: dict[str, Any], system_info: dict[str, Any]) -> str:
    samba = system_info.get("samba", {})
    mode = samba.get("setup_mode") or SETUP_MODE_INITIAL
    if mode not in {SETUP_MODE_INITIAL, SETUP_MODE_ADD_DRIVE, SETUP_MODE_UNSUPPORTED}:
        mode = SETUP_MODE_INITIAL

    state["samba_setup_mode"] = mode
    state["samba_setup_message"] = samba.get(
        "setup_message") or samba.get("message")

    users = samba.get("users") or []
    if mode == SETUP_MODE_ADD_DRIVE and len(users) == 1:
        state["username"] = users[0].get("name") or ""
        state["existing_samba_user"] = state["username"]
    elif mode != SETUP_MODE_ADD_DRIVE:
        state.pop("existing_samba_user", None)
    return mode


def system_checks_for_request(request: Request) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state = wizard_state(request)
    system_info = detect_system_info(
        command_runner=logged_detection_runner(state),
        internet_detector=lambda: logged_internet_check(state),
    )
    return system_checks_from_info(system_info), system_info


def logged_internet_check(state: dict[str, Any]) -> dict[str, Any]:
    internet = detect_internet_connectivity()
    add_log_entry(
        state,
        phase="System Check",
        command="Python internet connectivity check",
        exit_code=0 if internet.get("connected") else 1,
        summary=internet.get("message") or "Internet check completed.",
    )
    return internet


def unresolved_critical_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        check
        for check in checks
        if check["critical"] and check["status"] == "needs_attention"
    ]


def system_check_state(request: Request) -> dict[str, Any]:
    state = wizard_state(request)
    checks = state.setdefault("system_checks", {})
    if not isinstance(checks, dict):
        checks = {}
    checks.setdefault("wifi", {})
    checks.setdefault("show_wifi", False)
    state["system_checks"] = checks
    request.session["wizard"] = state
    return checks


def system_check_page(
    request: Request,
    error: str | None = None,
    wifi_error: str | None = None,
    status_code: int = 200,
):
    checks, system_info = system_checks_for_request(request)
    critical_unresolved = unresolved_critical_checks(checks)
    check_state = system_check_state(request)
    wifi_state = check_state.get("wifi", {})
    wifi_results = check_state.get("wifi_results", [])
    show_wifi = bool(check_state.get("show_wifi")) and not beta_enabled()
    internet_connected = bool(system_info.get("internet", {}).get("connected"))
    state = wizard_state(request)
    state["system_summary"] = system_summary(system_info)
    request.session["wizard"] = state

    return templates.TemplateResponse(
        "system_check.html",
        context(
            request,
            "System Check",
            checks=checks,
            critical_unresolved=critical_unresolved,
            system=system_summary(system_info),
            error=error,
            wifi_error=wifi_error,
            wifi_state=wifi_state,
            show_wifi=show_wifi,
            internet_connected=internet_connected,
            wifi_results=wifi_results,
        ),
        status_code=status_code,
    )


def system_summary(system_info: dict[str, Any] | None = None) -> dict[str, str]:
    if system_info is None:
        system_info = detect_system_info()
    return build_system_summary(system_info)


def cached_system_summary(state: dict[str, Any]) -> dict[str, str]:
    cached = state.get("system_summary")
    if isinstance(cached, dict):
        return {
            "hostname": str(cached.get("hostname") or "this-server"),
            "ip_address": str(cached.get("ip_address") or "localhost"),
            "ubuntu_version": str(cached.get("ubuntu_version") or "Unknown Linux version"),
        }
    return {
        "hostname": "this-server",
        "ip_address": "localhost",
        "ubuntu_version": "Unknown Linux version",
    }


def context(request: Request, step: str, **extra: Any) -> dict[str, Any]:
    state = wizard_state(request)
    state["step"] = step
    command_log_id_from_state(state)
    system = extra.pop("system", None) or system_summary()
    steps = extra.pop("steps", None) or step_order_for_state(state)
    base = {
        "request": request,
        "step": step,
        "steps": steps,
        "step_index": steps.index(step) + 1 if step in steps else 1,
        "step_count": len(steps),
        "state": state,
        "system": system,
        "selected_location": selected_location(state),
        "mount_access_label": mount_access_label,
        "add_drive_mode": is_add_drive_mode(state),
    }
    base.update(beta_context())
    base.update(extra)
    return base


def tailscale_context(request: Request, step: str, **extra: Any) -> dict[str, Any]:
    return context(request, step, steps=TAILSCALE_STEP_ORDER, **extra)


def windows_share_path(ip_address: str, share_name: str) -> str:
    return f"\\\\{ip_address}\\{share_name}"


def samba_share_options(request: Request) -> tuple[list[dict[str, str]], dict[str, Any]]:
    state = wizard_state(request)
    options: list[dict[str, str]] = []
    seen_names: set[str] = set()

    cached = cached_system_summary(state)
    if state.get("applied") and state.get("share_name"):
        name = safe_name(state.get("share_name") or "Backups")
        add_samba_share_option(
            options,
            seen_names,
            {
                "id": "session",
                "name": name,
                "path": str(selected_location(state).get("mount_path") if selected_location(state) else ""),
                "local_ip": cached["ip_address"],
                "source": "Current SamWizard session",
            },
        )

    system_info = detect_system_info(
        command_runner=logged_detection_runner(state, phase="Tailscale Check"))
    system = system_summary(system_info)
    for index, share in enumerate(system_info.get("samba", {}).get("shares") or []):
        name = str(share.get("name") or "").strip()
        if not name:
            continue
        add_samba_share_option(
            options,
            seen_names,
            {
                "id": f"detected-{index}",
                "name": name,
                "path": str(share.get("path") or ""),
                "local_ip": system["ip_address"],
                "source": "Detected Samba configuration",
            },
        )

    for option in options:
        option["local_path"] = windows_share_path(option["local_ip"], option["name"])
    return options, system_info


def add_samba_share_option(
    options: list[dict[str, str]],
    seen_names: set[str],
    option: dict[str, str],
) -> None:
    key = option["name"].strip().lower()
    if key in seen_names:
        return
    options.append(option)
    seen_names.add(key)


def selected_tailscale_share(request: Request) -> dict[str, str] | None:
    share = tailscale_state(request).get("share")
    if isinstance(share, dict) and share.get("name") and share.get("local_path"):
        return {str(key): str(value) for key, value in share.items()}
    return None


def store_tailscale_share(request: Request, share: dict[str, str]) -> None:
    tailscale_state(request)["share"] = dict(share)
    request.session["wizard"] = wizard_state(request)


def tailscale_info_for_request(request: Request) -> dict[str, Any]:
    state = wizard_state(request)
    return detect_tailscale(
        command_runner=logged_command_runner(
            state,
            "Tailscale Check",
            log_start=False,
        )
    )


@app.get("/")
def landing(request: Request):
    return templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "assets": landing_assets(),
            **beta_context(),
        },
    )


@app.get("/samba")
def welcome(request: Request):
    return templates.TemplateResponse("welcome.html", context(request, "Welcome"))


@app.post("/start")
def start(request: Request):
    request.session["wizard"] = {}
    return redirect("/system-check")


@app.get("/tailscale")
def tailscale_choose(request: Request):
    options, system_info = samba_share_options(request)
    if not options:
        return templates.TemplateResponse(
            "tailscale_choose.html",
            tailscale_context(
                request,
                "Choose Share",
                share_options=[],
                no_share=True,
                error=None,
                system=system_summary(system_info),
            ),
        )
    if len(options) == 1:
        store_tailscale_share(request, options[0])
        return redirect("/tailscale/what-changes")
    return templates.TemplateResponse(
        "tailscale_choose.html",
        tailscale_context(
            request,
            "Choose Share",
            share_options=options,
            no_share=False,
            error=None,
            system=system_summary(system_info),
        ),
    )


@app.post("/tailscale")
def tailscale_select_share(request: Request, share_id: str = Form("")):
    options, system_info = samba_share_options(request)
    selected = next((option for option in options if option["id"] == share_id), None)
    if selected is None:
        return templates.TemplateResponse(
            "tailscale_choose.html",
            tailscale_context(
                request,
                "Choose Share",
                share_options=options,
                no_share=not options,
                error="Choose a Samba share before continuing.",
                system=system_summary(system_info),
            ),
            status_code=400,
        )
    store_tailscale_share(request, selected)
    return redirect("/tailscale/what-changes")


@app.get("/tailscale/what-changes")
def tailscale_what_changes(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    return templates.TemplateResponse(
        "tailscale_what_changes.html",
        tailscale_context(request, "What Changes", share=share),
    )


@app.get("/tailscale/check")
def tailscale_check(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    info = tailscale_info_for_request(request)
    internet = detect_internet_connectivity()
    return templates.TemplateResponse(
        "tailscale_check.html",
        tailscale_context(
            request,
            "Check",
            share=share,
            tailscale=info,
            internet=internet,
            error=None,
        ),
    )


@app.post("/tailscale/check")
def tailscale_check_next(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    info = tailscale_info_for_request(request)
    if info.get("connected") and info.get("ipv4"):
        tailscale_state(request)["tailscale_ip"] = info["ipv4"]
        request.session["wizard"] = wizard_state(request)
        return redirect("/tailscale/firewall")
    if not info.get("installed"):
        return redirect("/tailscale/install")
    return redirect("/tailscale/authorize")


@app.get("/tailscale/install")
def tailscale_install_page(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    info = tailscale_info_for_request(request)
    state = tailscale_state(request)
    return templates.TemplateResponse(
        "tailscale_install.html",
        tailscale_context(
            request,
            "Install",
            share=share,
            tailscale=info,
            results=state.get("install_results", []),
            error=None,
        ),
    )


@app.post("/tailscale/install")
def tailscale_run_install(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    state = wizard_state(request)
    results = install_tailscale(
        command_runner=logged_command_runner(state, "Tailscale Install"))
    tailscale_state(request)["install_results"] = results
    request.session["wizard"] = state
    if all(result.get("status") == "passed" for result in results):
        return redirect("/tailscale/authorize")
    return templates.TemplateResponse(
        "tailscale_install.html",
        tailscale_context(
            request,
            "Install",
            share=share,
            tailscale=tailscale_info_for_request(request),
            results=results,
            error="Tailscale install stopped before finishing. Check the failed step below.",
        ),
        status_code=400,
    )


@app.get("/tailscale/authorize")
def tailscale_authorize_page(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    info = tailscale_info_for_request(request)
    if info.get("connected") and info.get("ipv4"):
        tailscale_state(request)["tailscale_ip"] = info["ipv4"]
        request.session["wizard"] = wizard_state(request)
        return redirect("/tailscale/firewall")
    state = wizard_state(request)
    pending_auth = pending_tailscale_auth_from_state(state)
    return templates.TemplateResponse(
        "tailscale_authorize.html",
        tailscale_context(
            request,
            "Authorize",
            share=share,
            tailscale=info,
            login_url=pending_auth.get("login_url"),
            result=pending_auth.get("authorize_result"),
            error=None,
        ),
    )


@app.post("/tailscale/authorize/start")
def tailscale_authorize_start(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    state = wizard_state(request)
    auth_id = command_log_id_from_state(state)
    outcome = start_tailscale_login(
        command_runner=logged_command_runner(state, "Tailscale Authorize"))
    tailscale = tailscale_state(request)
    tailscale.pop("login_url", None)
    tailscale.pop("authorize_result", None)
    save_pending_tailscale_auth(
        auth_id,
        login_url=outcome.get("login_url"),
        authorize_result=outcome.get("result"),
    )
    request.session["wizard"] = state
    return redirect("/tailscale/authorize")


@app.get("/tailscale/authorize/status")
def tailscale_authorize_status(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return JSONResponse(
            {
                "status": "missing_share",
                "message": "Choose a Samba share before authorizing Tailscale.",
                "redirect": "/tailscale",
            },
            status_code=400,
        )

    info = tailscale_info_for_request(request)
    if info.get("connected") and info.get("ipv4"):
        tailscale_state(request)["tailscale_ip"] = info["ipv4"]
        request.session["wizard"] = wizard_state(request)
        return JSONResponse(
            {
                "status": "connected",
                "message": "Tailscale is connected.",
                "redirect": "/tailscale/firewall",
                "tailscale": info,
            }
        )

    return JSONResponse(
        {
            "status": "waiting",
            "message": "Waiting for Tailscale login.",
            "redirect": None,
            "tailscale": info,
        }
    )


@app.post("/tailscale/authorize/check")
def tailscale_authorize_check(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    info = tailscale_info_for_request(request)
    if info.get("connected") and info.get("ipv4"):
        tailscale_state(request)["tailscale_ip"] = info["ipv4"]
        request.session["wizard"] = wizard_state(request)
        return redirect("/tailscale/firewall")
    state = wizard_state(request)
    pending_auth = pending_tailscale_auth_from_state(state)
    return templates.TemplateResponse(
        "tailscale_authorize.html",
        tailscale_context(
            request,
            "Authorize",
            share=share,
            tailscale=info,
            login_url=pending_auth.get("login_url"),
            result=pending_auth.get("authorize_result"),
            error="Tailscale does not show this server as connected yet. Approve the server, then check again.",
        ),
        status_code=400,
    )


@app.get("/tailscale/firewall")
def tailscale_firewall_page(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    info = tailscale_info_for_request(request)
    tailscale_ip = tailscale_state(request).get("tailscale_ip") or info.get("ipv4")
    if not tailscale_ip:
        return redirect("/tailscale/authorize")
    tailscale_state(request)["tailscale_ip"] = tailscale_ip
    firewall = build_firewall_context(
        logged_command_runner(wizard_state(request), "Firewall Check", log_start=False))
    request.session["wizard"] = wizard_state(request)
    return templates.TemplateResponse(
        "tailscale_firewall.html",
        tailscale_context(
            request,
            "Firewall",
            share=share,
            tailscale_ip=tailscale_ip,
            firewall=firewall,
            results=tailscale_state(request).get("firewall_results", []),
            error=None,
        ),
    )


@app.post("/tailscale/firewall")
def tailscale_firewall_apply(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    tailscale_ip = tailscale_state(request).get("tailscale_ip")
    if not tailscale_ip:
        return redirect("/tailscale/authorize")
    state = wizard_state(request)
    firewall = build_firewall_context(
        logged_command_runner(state, "Firewall Check", log_start=False))
    preview = firewall.get("preview", {})
    if not preview.get("can_apply"):
        return templates.TemplateResponse(
            "tailscale_firewall.html",
            tailscale_context(
                request,
                "Firewall",
                share=share,
                tailscale_ip=tailscale_ip,
                firewall=firewall,
                results=[],
                error=preview.get("message") or "Firewall rules could not be prepared.",
            ),
            status_code=400,
        )
    results = apply_firewall_rules(
        logged_command_runner(state, "Firewall"),
        lan_cidrs=firewall.get("lan_cidrs", []),
        ufw_active=bool(firewall.get("ufw_active")),
        samba_app_available=bool(firewall.get("samba_app_available")),
    )
    tailscale_state(request)["firewall_results"] = results
    request.session["wizard"] = state
    if all(result.get("status") == "passed" for result in results):
        return redirect("/tailscale/done")
    return templates.TemplateResponse(
        "tailscale_firewall.html",
        tailscale_context(
            request,
            "Firewall",
            share=share,
            tailscale_ip=tailscale_ip,
            firewall=firewall,
            results=results,
            error="Firewall setup stopped before finishing. Review the failed step below.",
        ),
        status_code=400,
    )


@app.get("/tailscale/done")
def tailscale_done(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    tailscale_ip = tailscale_state(request).get("tailscale_ip")
    if not tailscale_ip:
        info = tailscale_info_for_request(request)
        tailscale_ip = info.get("ipv4")
    if not tailscale_ip:
        return redirect("/tailscale/authorize")
    tailscale_state(request)["tailscale_ip"] = tailscale_ip
    request.session["wizard"] = wizard_state(request)
    return templates.TemplateResponse(
        "tailscale_done.html",
        tailscale_context(
            request,
            "New Address",
            share=share,
            local_path=share["local_path"],
            tailscale_path=windows_share_path(str(tailscale_ip), share["name"]),
        ),
    )


@app.get("/tailscale/windows")
def tailscale_windows(request: Request):
    share = selected_tailscale_share(request)
    if share is None:
        return redirect("/tailscale")
    tailscale_ip = tailscale_state(request).get("tailscale_ip")
    if not tailscale_ip:
        return redirect("/tailscale/done")
    return templates.TemplateResponse(
        "tailscale_windows.html",
        tailscale_context(
            request,
            "Windows PC",
            share=share,
            tailscale_path=windows_share_path(str(tailscale_ip), share["name"]),
        ),
    )


@app.get("/system-check")
def system_check(request: Request, wifi: str | None = None):
    if wifi == "show" and not beta_enabled():
        check_state = system_check_state(request)
        check_state["show_wifi"] = True
        state = wizard_state(request)
        state["system_checks"] = check_state
        request.session["wizard"] = state
    return system_check_page(request)


@app.get("/samba-system")
def samba_system(request: Request):
    state = wizard_state(request)
    system_info = detect_system_info(
        command_runner=logged_detection_runner(state, phase="Samba System"))
    return templates.TemplateResponse(
        "samba_system.html",
        context(
            request,
            "System Check",
            samba=system_info.get("samba", {}),
            system=system_summary(system_info),
        ),
    )


@app.post("/system-check")
def system_check_next(request: Request):
    checks, system_info = system_checks_for_request(request)
    state = wizard_state(request)
    mode = apply_samba_setup_mode(state, system_info)
    state["system_summary"] = system_summary(system_info)
    request.session["wizard"] = state
    if unresolved_critical_checks(checks):
        return system_check_page(
            request,
            error="Resolve the critical checks before continuing.",
        )
    if mode == SETUP_MODE_UNSUPPORTED:
        return system_check_page(
            request,
            error=state.get("samba_setup_message")
            or "SamWizard found multiple Samba users and cannot choose which account should own a new share.",
        )
    return redirect("/drive-selection")


@app.post("/system-check/wifi-preview")
def wifi_preview(
    request: Request,
    wifi_interface: str = Form(""),
    wifi_ssid: str = Form(""),
    wifi_password: str = Form(""),
):
    if beta_enabled():
        return system_check_page(
            request,
            wifi_error="Wi-Fi setup is disabled during this beta. Use ethernet or configure Wi-Fi outside SamWizard.",
            status_code=403,
        )

    clean_interface = wifi_interface.strip()
    clean_ssid = wifi_ssid.strip()
    check_state = system_check_state(request)
    check_state["show_wifi"] = True
    state = wizard_state(request)
    state["system_checks"] = check_state
    request.session["wizard"] = state

    if not clean_interface:
        return system_check_page(request, wifi_error="Enter the Wi-Fi adapter name.")
    if not clean_ssid:
        return system_check_page(request, wifi_error="Enter the Wi-Fi network name.")
    if not wifi_password:
        return system_check_page(request, wifi_error="Enter the Wi-Fi password.")

    results = apply_wifi_setup(
        state,
        interface=clean_interface,
        ssid=clean_ssid,
        password=wifi_password,
        command_runner=logged_command_runner(state, "Wi-Fi Setup"),
        internet_checker=detect_internet_connectivity,
    )
    check_state["wifi"] = {
        "interface": clean_interface,
        "ssid": clean_ssid,
        "password_provided": True,
    }
    check_state["wifi_results"] = results
    state["system_checks"] = check_state
    request.session["wizard"] = state

    if all(result["status"] == "passed" for result in results):
        return redirect("/system-check")
    return system_check_page(
        request,
        wifi_error="Wi-Fi setup stopped before finishing. Review the failed step below.",
    )


@app.get("/drive-selection")
def drive_selection(request: Request):
    state = wizard_state(request)
    system_info = detect_system_info(
        command_runner=logged_detection_runner(state))
    state["system_summary"] = system_summary(system_info)
    request.session["wizard"] = state
    return templates.TemplateResponse(
        "drive_selection.html",
        context(
            request,
            "Drive Selection",
            locations=share_locations(system_info),
            drive_diagnostics=drive_diagnostics(system_info),
            has_eligible_drive=has_eligible_drive(system_info),
            system=system_summary(system_info),
            error=None,
        ),
    )


@app.post("/drive-selection")
def save_drive_selection(
    request: Request,
    location_id: str = Form(""),
    mount_access: str = Form(DEFAULT_MOUNT_ACCESS),
):
    state = wizard_state(request)
    system_info = detect_system_info(
        command_runner=logged_detection_runner(state))
    state["system_summary"] = system_summary(system_info)
    location = selected_location_from(location_id, system_info)
    selected_access = normalize_mount_access(mount_access)
    if location is None:
        return templates.TemplateResponse(
            "drive_selection.html",
            context(
                request,
                "Drive Selection",
                locations=share_locations(system_info),
                drive_diagnostics=drive_diagnostics(system_info),
                has_eligible_drive=has_eligible_drive(system_info),
                system=system_summary(system_info),
                error="Choose an external or additional drive to continue.",
            ),
            status_code=400,
        )

    access_error = mount_access_error(location, selected_access)
    if access_error:
        return templates.TemplateResponse(
            "drive_selection.html",
            context(
                request,
                "Drive Selection",
                locations=share_locations(system_info),
                drive_diagnostics=drive_diagnostics(system_info),
                has_eligible_drive=has_eligible_drive(system_info),
                system=system_summary(system_info),
                error=access_error,
            ),
            status_code=400,
        )

    if location.get("type") == "drive":
        location["mount_access"] = selected_access
        state["mount_access"] = selected_access
    else:
        state.pop("mount_access", None)
    state["location_id"] = location_id
    state["selected_location"] = location
    request.session["wizard"] = state
    return redirect("/share-name")


@app.get("/share-name")
def share_name(request: Request):
    if selected_location(wizard_state(request)) is None:
        return redirect("/drive-selection")
    return templates.TemplateResponse(
        "share_name.html",
        context(request, "Share Name", error=None),
    )


@app.post("/share-name")
def save_share_name(request: Request, share_name: str = Form("")):
    clean_name = share_name.strip()
    if not clean_name:
        return templates.TemplateResponse(
            "share_name.html",
            context(request, "Share Name",
                    error="Enter a name for the Windows share."),
            status_code=400,
        )

    state = wizard_state(request)
    state["share_name"] = clean_name
    request.session["wizard"] = state
    if is_add_drive_mode(state):
        return redirect("/review")
    return redirect("/user-setup")


@app.get("/user-setup")
def user_setup(request: Request):
    state = wizard_state(request)
    if is_add_drive_mode(state):
        return redirect("/review" if state.get("share_name") else "/share-name")
    if not state.get("share_name"):
        return redirect("/share-name")
    return templates.TemplateResponse(
        "user_setup.html",
        context(request, "User Setup", error=None),
    )


@app.post("/user-setup")
def save_user_setup(
    request: Request,
    username: str = Form(""),
):
    state = wizard_state(request)
    if is_add_drive_mode(state):
        return redirect("/review" if state.get("share_name") else "/share-name")

    clean_username = username.strip()
    if not clean_username:
        error = "Enter a username for the private share."
    else:
        error = None

    if error:
        return templates.TemplateResponse(
            "user_setup.html",
            context(request, "User Setup", error=error),
            status_code=400,
        )

    state["username"] = clean_username
    request.session["wizard"] = state
    return redirect("/review")


@app.get("/review")
def review(request: Request):
    state = wizard_state(request)
    if selected_location(state) is None:
        return redirect("/drive-selection")
    if not state.get("share_name"):
        return redirect("/share-name")
    if not state.get("username"):
        return redirect("/system-check" if is_add_drive_mode(state) else "/user-setup")

    return templates.TemplateResponse("review.html", context(request, "Review"))


@app.post("/review")
def confirm_review(request: Request):
    state = wizard_state(request)
    state["reviewed"] = True
    state.pop("apply_results", None)
    request.session["wizard"] = state
    return redirect("/apply")


@app.get("/apply")
def apply_setup(request: Request):
    state = wizard_state(request)
    if not state.get("reviewed"):
        return redirect("/review")
    job = reconcile_apply_job_state(state)
    request.session["wizard"] = state
    if job and job.get("status") == STATUS_SUCCEEDED:
        return redirect("/done")
    error = (
        "Setup stopped before finishing. Review the failed step below."
        if state.get("apply_status") == STATUS_FAILED
        else None
    )
    return templates.TemplateResponse(
        "apply.html",
        context(request, "Apply", results=state.get(
            "apply_results", []), error=error),
    )


@app.post("/apply")
def run_apply(
    request: Request,
    samba_password: str = Form(""),
    confirm_samba_password: str = Form(""),
    beta_confirm_system_changes: str = Form(""),
):
    state = wizard_state(request)
    if not state.get("reviewed"):
        return redirect("/review")
    add_drive = is_add_drive_mode(state)
    if not add_drive and not samba_password:
        return templates.TemplateResponse(
            "apply.html",
            context(request, "Apply", results=state.get(
                "apply_results", []), error="Enter the Windows share password."),
            status_code=400,
        )
    if not add_drive and samba_password != confirm_samba_password:
        return templates.TemplateResponse(
            "apply.html",
            context(request, "Apply", results=state.get(
                "apply_results", []), error="The passwords do not match."),
            status_code=400,
        )
    if beta_enabled() and beta_confirm_system_changes != "yes":
        return templates.TemplateResponse(
            "apply.html",
            context(
                request,
                "Apply",
                results=state.get("apply_results", []),
                error="Confirm this is a test machine or backed-up drive before applying beta changes.",
            ),
            status_code=400,
        )

    location = selected_location(state)
    if location is None:
        return redirect("/drive-selection")
    if not state.get("share_name") or not state.get("username"):
        return redirect("/review")

    current_job = get_apply_job(state.get("apply_job_id"))
    if current_job and current_job.get("status") in RUNNING_JOB_STATUSES:
        return redirect("/apply/progress")

    command_log_id_from_state(state)
    job_state = dict(state)
    job_location = dict(location)
    share_name = state["share_name"]
    username = state["username"]
    password = samba_password if not add_drive else None
    create_user = not add_drive

    def apply_runner():
        results = apply_share_setup(
            location=job_location,
            share_name=share_name,
            username=username,
            password=password,
            create_user=create_user,
            command_runner=logged_command_runner(job_state, "Apply"),
        )
        return results, job_location

    job = start_apply_job(apply_runner)
    state["apply_job_id"] = job.id
    state["apply_status"] = STATUS_RUNNING
    state["apply_results"] = []
    state["applied"] = False
    request.session["wizard"] = state

    return redirect("/apply/progress")


@app.get("/apply/progress")
def apply_progress(request: Request):
    state = wizard_state(request)
    if not state.get("reviewed"):
        return redirect("/review")
    job = reconcile_apply_job_state(
        state) or get_apply_job(state.get("apply_job_id"))
    request.session["wizard"] = state
    if job and job.get("status") == STATUS_SUCCEEDED:
        return redirect("/done")
    if job and job.get("status") == STATUS_FAILED:
        return redirect("/apply")
    latest = latest_apply_log_entry(state)
    return templates.TemplateResponse(
        "apply_progress.html",
        context(
            request,
            "Apply",
            job=job,
            latest_entry=latest,
            error=None,
            system=cached_system_summary(state),
        ),
    )


@app.get("/apply/status")
def apply_status(request: Request):
    state = wizard_state(request)
    job = reconcile_apply_job_state(
        state) or get_apply_job(state.get("apply_job_id"))
    latest = latest_apply_log_entry(state)
    if job is None:
        return JSONResponse(
            {
                "status": "lost",
                "message": "Apply status was lost. The service may have restarted.",
                "latest_entry": latest,
            }
        )

    if job["status"] in {STATUS_SUCCEEDED, STATUS_FAILED}:
        request.session["wizard"] = state

    return JSONResponse(
        {
            "status": job["status"],
            "results": job.get("results") or [],
            "error": job.get("error"),
            "latest_entry": latest,
            "updated_at": job.get("updated_at"),
            "redirect": "/done" if job["status"] == STATUS_SUCCEEDED else None,
            "failure_url": "/apply" if job["status"] == STATUS_FAILED else None,
        }
    )


def latest_apply_log_entry(state: dict[str, Any]) -> dict[str, Any] | None:
    for entry in reversed(command_log_from_state(state)):
        if entry.get("phase") == "Apply":
            return entry
    return None


def reconcile_apply_job_state(state: dict[str, Any]) -> dict[str, Any] | None:
    job = get_apply_job(state.get("apply_job_id"))
    if not job or job.get("status") not in {STATUS_SUCCEEDED, STATUS_FAILED}:
        return job

    state["apply_status"] = job["status"]
    state["apply_results"] = job.get("results") or []
    if job.get("selected_location"):
        state["selected_location"] = job["selected_location"]
    state["applied"] = job["status"] == STATUS_SUCCEEDED
    return job


@app.post("/command-log/clear")
def clear_log(request: Request):
    state = wizard_state(request)
    clear_command_log(state)
    request.session["wizard"] = state
    referer = request.headers.get("referer") if hasattr(
        request, "headers") else None
    return redirect(referer or "/system-check")


@app.get("/logs")
def logs_page(request: Request):
    state = wizard_state(request)
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "command_log": command_log_from_state(state),
            **beta_context(),
        },
    )


@app.get("/logs/data")
def logs_data(request: Request):
    state = wizard_state(request)
    return JSONResponse({"entries": command_log_from_state(state)})


@app.get("/beta/diagnostics")
def beta_diagnostics_page(request: Request):
    if not beta_enabled():
        return redirect("/")
    state = wizard_state(request)
    return templates.TemplateResponse(
        "beta_diagnostics.html",
        {
            "request": request,
            "command_log": command_log_from_state(state),
            **beta_context(),
        },
    )


@app.post("/beta/diagnostics/download")
def beta_diagnostics_download(
    request: Request,
    tester_id: str = Form(""),
    scenario: str = Form(""),
    expected_result: str = Form(""),
    actual_result: str = Form(""),
    notes: str = Form(""),
):
    if not beta_enabled():
        return redirect("/")
    state = wizard_state(request)
    filename, payload = build_diagnostic_bundle(
        request,
        state,
        tester_notes(
            tester_id=tester_id,
            scenario=scenario,
            expected_result=expected_result,
            actual_result=actual_result,
            notes=notes,
        ),
    )
    return Response(
        payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/beta/recovery")
def beta_recovery_page(request: Request):
    if not beta_enabled():
        return redirect("/")
    return templates.TemplateResponse(
        "beta_recovery.html",
        {
            "request": request,
            **beta_context(),
        },
    )


@app.get("/done")
def done(request: Request):
    state = wizard_state(request)
    reconcile_apply_job_state(state)
    request.session["wizard"] = state
    if not state.get("applied"):
        return redirect("/apply")
    share_name = safe_name(state.get("share_name") or "Backups")
    system = cached_system_summary(state)
    windows_path = f"\\\\{system['ip_address']}\\{share_name}"
    return templates.TemplateResponse(
        "done.html",
        context(request, "Done", windows_path=windows_path, system=system),
    )
