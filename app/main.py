"""FastAPI application: web UI + JSON-ish form endpoints."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import events, github, i18n, scheduler, service, tz
from .config import get_config
from .db import (INT_SETTINGS, all_settings, get_setting, init_db, parse_int_setting,
                 session_scope, set_setting)
from .i18n import Msg, msg
from .models import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, TYPE_TASMOTA, Backup, Device, utcnow
from .security import decrypt, encrypt, hash_password, verify_password

cfg = get_config()
BASE_DIR = Path(__file__).parent

# Make our INFO logs (scheduler runs, backups) visible alongside uvicorn's output.
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_app_log = logging.getLogger("tasmobackup")
if not _app_log.handlers:
    _app_log.addHandler(_log_handler)
_app_log.setLevel(logging.INFO)
_app_log.propagate = False


def _zone(tzname: str) -> ZoneInfo:
    try:
        return ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _i18n_context(request: Request) -> dict:
    """Inject the translation helper, language, theme and a UTC->local formatter."""
    with session_scope() as s:
        lang = get_setting(s, "language", i18n.DEFAULT_LANG)
        theme = get_setting(s, "theme", "auto")
        tzname = get_setting(s, "timezone", "Europe/Berlin")
    zone = _zone(tzname)

    def fmt_dt(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if dt is None:
            return ""
        # Stored timestamps are naive UTC; attach UTC then convert to the chosen zone.
        return dt.replace(tzinfo=timezone.utc).astimezone(zone).strftime(fmt)

    def ago(dt: datetime | None) -> str:
        """Compact relative age ("3 min", "2 h", "5 d") for the last-seen column."""
        if dt is None:
            return ""
        seconds = int((utcnow() - dt).total_seconds())
        if seconds < 60:
            return i18n.translate(lang, "time.now")
        if seconds < 3600:
            return f"{seconds // 60} min"
        if seconds < 86400:
            return f"{seconds // 3600} h"
        return f"{seconds // 86400} d"

    # Flash messages are consumed on the first full page render. htmx polls the
    # dashboard every 30 s with hx-select="#devices" — popping there would silently
    # swallow the message, so leave it in the session for those requests.
    if request.headers.get("HX-Request") == "true":
        flash = []
    else:
        flash = request.session.pop("flash", [])

    # NOTE: use dedicated keys, not 'settings' — context processors override the
    # per-route context in Starlette, which would clobber the settings page's data.
    return {
        "t": lambda key, **kw: i18n.translate(lang, key, **kw),
        "lang": lang,
        "theme": theme,
        "tz": tzname,
        "fmt_dt": fmt_dt,
        "ago": ago,
        "flash": flash,
        "unseen_events": events.unseen_count(),
    }


def _lang() -> str:
    with session_scope() as s:
        return get_setting(s, "language", i18n.DEFAULT_LANG)


def _tr(key: str, **kwargs: object) -> str:
    """Translate into the UI language currently configured in the settings."""
    return i18n.translate(_lang(), key, **kwargs)


# Flash messages travel in the signed session cookie, which browsers drop above
# ~4 KB — a scan that finds dozens of devices must not produce a longer text.
_MAX_DETAILS = 600


def _join(messages: list[Msg], sep: str = "; ") -> str:
    lang = _lang()
    text = sep.join(m.text(lang) for m in messages)
    return text if len(text) <= _MAX_DETAILS else text[:_MAX_DETAILS].rstrip() + " …"


def _flash(request: Request, level: str, text: str | Msg) -> None:
    """Queue a one-off message shown on the next full page render.

    Translated here: the session cookie can only hold plain strings.
    """
    if isinstance(text, Msg):
        text = text.text(_lang())
    request.session.setdefault("flash", []).append({"level": level, "text": text})


def _tz_state(device: Device, tzname: str) -> tuple[int | None, bool, int | None]:
    """(drift_minutes, fixed_offset_problem, expected_offset) for one device.

    drift: how far the clock is off right now. fixed: correct today, but the device
    uses a fixed offset in a zone that switches, so it breaks at the changeover.
    """
    expected = tz.offset_minutes(tzname)
    if device.tz_offset is None or expected is None:
        return None, False, expected
    drift = device.tz_offset - expected
    has_dst = tz.has_dst(tzname, datetime.now(timezone.utc).year)
    return drift, bool(has_dst and device.tz_dst is False and drift == 0), expected


def _flash_result(request: Request, ok: bool, message: str | Msg) -> None:
    _flash(request, LEVEL_INFO if ok else LEVEL_ERROR, message)


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"), context_processors=[_i18n_context])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _ensure_admin()
    _encrypt_legacy_mqtt_password()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="TasmoBackup-py", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key)
_static = BASE_DIR / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


def _ensure_admin() -> None:
    with session_scope() as s:
        if not get_setting(s, "admin_password_hash"):
            set_setting(s, "admin_user", cfg.admin_user)
            set_setting(s, "admin_password_hash", hash_password(cfg.admin_password))


def _encrypt_legacy_mqtt_password() -> None:
    """Older versions stored the MQTT password in plain text — encrypt it once.

    decrypt() yields "" for anything that is not a token of ours, which is exactly
    the case for a plain-text value.
    """
    with session_scope() as s:
        raw = get_setting(s, "mqtt_password", "")
        if raw and not decrypt(raw):
            set_setting(s, "mqtt_password", encrypt(raw))


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def require_login(request: Request) -> None:
    if not cfg.auth_enabled:
        return
    if not request.session.get("user"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with session_scope() as s:
        ok = username == get_setting(s, "admin_user") and verify_password(
            password, get_setting(s, "admin_password_hash")
        )
    if not ok:
        return templates.TemplateResponse(
            request, "login.html", {"error": _tr("login.invalid")}, status_code=401
        )
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def index(request: Request):
    latest = await github.latest_version()
    with session_scope() as s:
        default_scan = get_setting(s, "scan_subnet", "") or "192.168.178.1/24"
        mqtt_configured = bool(get_setting(s, "mqtt_host", "").strip())
        devices = list(s.scalars(select(Device).order_by(Device.name)).all())
        counts = {
            d.id: s.query(Backup).filter(Backup.device_id == d.id).count() for d in devices
        }
        tzname = get_setting(s, "timezone", "Europe/Berlin")
        rows = []
        for d in devices:
            num, tag = github.parse_version(d.version)
            custom = github.is_custom_build(d.version)
            # A custom build is not "behind" the official release — it is a different
            # thing, and flagging it would invite a click that replaces it.
            # `latest` is the newest *Tasmota* release; a WLED version is unrelated.
            outdated = bool(d.type == TYPE_TASMOTA and not custom and num and latest
                            and github._semver(num) < github._semver(latest))
            drift, fixed_tz, _ = _tz_state(d, tzname)
            rows.append(
                {
                    "d": d,
                    "num": num,
                    "tag": tag,
                    "outdated": outdated,
                    "custom": custom,
                    "backups": counts[d.id],
                    "drift": drift,
                    "fixed_tz": fixed_tz,
                }
            )
    base, _, cidr = default_scan.partition("/")
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            "latest": latest,
            "scan_base": base,
            "scan_cidr": cidr or "24",
            "mqtt_configured": mqtt_configured,
            "tzname": tzname,
        },
    )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@app.get("/events", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def events_page(request: Request):
    rows = events.recent(200)
    # Opening the page clears the dashboard's warning badge.
    events.mark_all_seen()
    return templates.TemplateResponse(request, "events.html", {"events": rows})


@app.post("/events/clear", dependencies=[Depends(require_login)])
async def events_clear(request: Request):
    events.clear_all()
    _flash(request, LEVEL_INFO, msg("msg.events_cleared"))
    return RedirectResponse("/events", status_code=303)


@app.post("/devices/add", dependencies=[Depends(require_login)])
async def add_device(request: Request, ip: str = Form(...), username: str = Form(""),
                     password: str = Form("")):
    result = await service.add_device(ip.strip(), username.strip(), password)
    ok = result.key in ("msg.add_added", "msg.add_updated")
    _flash(request, LEVEL_INFO if ok else LEVEL_WARN, result)
    return RedirectResponse("/", status_code=303)


@app.get("/devices/{device_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def edit_form(request: Request, device_id: int):
    with session_scope() as s:
        device = s.get(Device, device_id)
        tzname = get_setting(s, "timezone", "Europe/Berlin")
    if not device:
        raise HTTPException(404, "device not found")
    drift, fixed_tz, expected_off = _tz_state(device, tzname)
    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "device": device,
            "tzname": tzname,
            "expected_off": expected_off or 0,
            "drift": drift,
            "fixed_tz": fixed_tz,
            "commands": tz.tasmota_commands(tzname, datetime.now(timezone.utc).year) or [],
        },
    )


@app.post("/devices/{device_id}/edit", dependencies=[Depends(require_login)])
async def edit_device(device_id: int, name: str = Form(""), auto_name: str = Form("")):
    with session_scope() as s:
        device = s.get(Device, device_id)
        if device:
            new_name = name.strip()
            if new_name:
                device.name = new_name
            # Checkbox present -> keep syncing name from device; absent -> name is custom.
            device.name_custom = not bool(auto_name)
    return RedirectResponse("/", status_code=303)


@app.post("/devices/{device_id}/delete", dependencies=[Depends(require_login)])
async def delete_device(device_id: int):
    service.delete_device(device_id)
    return RedirectResponse("/", status_code=303)


@app.post("/devices/{device_id}/toggle-auto-update", dependencies=[Depends(require_login)])
async def toggle_auto_update(device_id: int):
    with session_scope() as s:
        d = s.get(Device, device_id)
        if d:
            d.auto_update = not d.auto_update
    return RedirectResponse("/", status_code=303)


@app.post("/devices/{device_id}/backup", dependencies=[Depends(require_login)])
async def backup_now(request: Request, device_id: int):
    ok, result = await service.backup_device(device_id)
    _flash_result(request, ok, result)
    return RedirectResponse("/", status_code=303)


@app.post("/devices/{device_id}/update", dependencies=[Depends(require_login)])
async def update_now(request: Request, device_id: int):
    # Manual button forces the update (still backs up first inside the service).
    ok, result = await service.update_device(device_id, force=True)
    _flash_result(request, ok, result)
    return RedirectResponse("/", status_code=303)


@app.post("/backup-all", dependencies=[Depends(require_login)])
async def backup_all(request: Request):
    total, failed = await service.backup_all()
    if failed:
        _flash(request, LEVEL_ERROR, msg("msg.backup_all_failed", ok=total - len(failed),
                                         total=total, failed=_join(failed)))
    else:
        _flash(request, LEVEL_INFO, msg("msg.backup_all_ok", total=total))
    return RedirectResponse("/", status_code=303)


@app.post("/devices/{device_id}/refresh", dependencies=[Depends(require_login)])
async def refresh_one(request: Request, device_id: int):
    ok, _ = await service.refresh_device(device_id)
    _flash(request, LEVEL_INFO if ok else LEVEL_WARN,
           msg("msg.refresh_ok" if ok else "msg.refresh_failed"))
    return RedirectResponse("/", status_code=303)


@app.post("/devices/{device_id}/sync-time", dependencies=[Depends(require_login)])
async def sync_time_one(request: Request, device_id: int):
    """Apply the configured time zone to a single device, from its edit page."""
    ok, result = await service.sync_timezone(device_id)
    _flash_result(request, ok, result)
    return RedirectResponse(f"/devices/{device_id}/edit", status_code=303)


@app.post("/refresh-all", dependencies=[Depends(require_login)])
async def refresh_all(request: Request):
    await service.refresh_all()
    with session_scope() as s:
        devices = list(s.scalars(select(Device)).all())
        offline = [d.name for d in devices if not d.online]
    if offline:
        _flash(request, LEVEL_WARN, msg("msg.refresh_all_offline", online=len(devices) - len(offline),
                                        total=len(devices), names=", ".join(offline)))
    else:
        _flash(request, LEVEL_INFO, msg("msg.refresh_all_ok", total=len(devices)))
    return RedirectResponse("/", status_code=303)


@app.post("/scan", dependencies=[Depends(require_login)])
async def scan(request: Request, subnet_base: str = Form(...), subnet_cidr: str = Form("24")):
    cidr = subnet_cidr.strip().lstrip("/") or "24"
    error, results = await service.scan_subnet(f"{subnet_base.strip()}/{cidr}")
    if error:
        _flash(request, LEVEL_ERROR, error)
    elif results:
        _flash(request, LEVEL_INFO, msg("msg.scan_found", count=len(results), details=_join(results)))
    else:
        _flash(request, LEVEL_INFO, msg("msg.scan_none"))
    return RedirectResponse("/", status_code=303)


@app.post("/mqtt-scan", dependencies=[Depends(require_login)])
async def mqtt_scan(request: Request):
    ok, messages = await service.mqtt_discover()
    _flash(request, LEVEL_INFO if ok else LEVEL_ERROR, _join(messages))
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------------------- #
# Backups list + download
# --------------------------------------------------------------------------- #
@app.get("/devices/{device_id}/backups", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def list_backups(request: Request, device_id: int):
    with session_scope() as s:
        device = s.get(Device, device_id)
        backups = list(
            s.scalars(
                select(Backup).where(Backup.device_id == device_id).order_by(Backup.created_at.desc())
            ).all()
        )
    return templates.TemplateResponse(
        request, "backups.html", {"device": device, "backups": backups}
    )


@app.get("/backups/{backup_id}/download", dependencies=[Depends(require_login)])
async def download(backup_id: int):
    with session_scope() as s:
        b = s.get(Backup, backup_id)
        if not b or not Path(b.filename).exists():
            raise HTTPException(404, "backup not found")
        path = b.filename
    return FileResponse(path, filename=Path(path).name, media_type="application/octet-stream")


@app.post("/backups/{backup_id}/restore", dependencies=[Depends(require_login)])
async def restore(request: Request, backup_id: int):
    with session_scope() as s:
        b = s.get(Backup, backup_id)
        device_id = b.device_id if b else None
    if not device_id:  # backup vanished (e.g. pruned) — nothing to go back to
        _flash(request, LEVEL_ERROR, msg("msg.restore_not_found"))
        return RedirectResponse("/", status_code=303)
    ok, result = await service.restore_device(device_id, backup_id)
    _flash_result(request, ok, result)
    return RedirectResponse(f"/devices/{device_id}/backups", status_code=303)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def settings_form(request: Request):
    with session_scope() as s:
        data = all_settings(s)
    data.pop("admin_password_hash", None)
    # Only whether one is saved — the password itself never goes into the page.
    data["mqtt_password_set"] = bool(data.pop("mqtt_password", ""))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": data,
            "languages": i18n.available_languages(),
            "timezones": sorted(available_timezones()),
        },
    )


# Label (locale key) of each numeric setting, for the "invalid value" message.
_SETTING_LABELS = {
    "backup_interval_hours": "settings.interval",
    "backup_max_count": "settings.maxcount",
    "backup_max_days": "settings.maxdays",
    "online_check_minutes": "settings.online_minutes",
    "events_max_days": "settings.events_days",
    "mqtt_port": "settings.mqtt_port",
}


@app.post("/settings", dependencies=[Depends(require_login)])
async def save_settings(request: Request):
    form = await request.form()
    invalid: list[str] = []
    with session_scope() as s:
        for key in (
            "backup_interval_hours",
            "backup_max_count",
            "backup_max_days",
            "auto_update_global",
            "scan_subnet",
            "mqtt_host",
            "mqtt_port",
            "mqtt_user",
            "mqtt_topic",
            "mqtt_autoscan",
            "theme",
            "language",
            "timezone",
            "online_check_minutes",
            "notify_url",
            "notify_format",
            "notify_min_level",
            "events_max_days",
        ):
            if key not in form:
                continue
            value = str(form[key]).strip()
            # A bad number is refused instead of stored: the scheduler reads these.
            if key in INT_SETTINGS and parse_int_setting(key, value) is None:
                invalid.append(key)
                continue
            set_setting(s, key, value)
        if form.get("new_password"):
            set_setting(s, "admin_password_hash", hash_password(str(form["new_password"])))
        # Stored encrypted and never sent back to the browser: an empty field keeps
        # the saved password, the checkbox removes it.
        if form.get("mqtt_password_clear"):
            set_setting(s, "mqtt_password", "")
        elif form.get("mqtt_password"):
            set_setting(s, "mqtt_password", encrypt(str(form["mqtt_password"])))

    # The reachability job's interval lives in the DB, so re-install it here instead
    # of making the user restart the container.
    scheduler.reschedule_online_check()

    if invalid:
        fields = ", ".join(_tr(_SETTING_LABELS[k]) for k in invalid)
        _flash(request, LEVEL_ERROR, _tr("settings.invalid", fields=fields))

    # "Save & test" — deliver a probe synchronously so the result can be shown.
    if form.get("test_notify"):
        with session_scope() as s:
            url = get_setting(s, "notify_url", "")
            fmt = get_setting(s, "notify_format", "ntfy")
        ok, result = await events.send_test(url, fmt)
        _flash_result(request, ok, result)
    else:
        _flash(request, LEVEL_INFO, msg("msg.settings_saved"))
    return RedirectResponse("/settings", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
