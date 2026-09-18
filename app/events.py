"""Event log + outbound notifications.

Every notable failure (backup failed, update aborted, device went offline) is
recorded in the `events` table so it becomes visible in the web UI, and — when a
notification URL is configured — pushed out via HTTP so the user learns about it
without opening the dashboard.

Kept separate from service.py so the scheduler, the web layer and the service
layer all produce the same history.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import timedelta

import httpx
from sqlalchemy import delete, select

from .db import get_int, get_setting, session_scope
from .i18n import Msg, msg
from .models import LEVEL_ERROR, LEVEL_INFO, LEVEL_ORDER, LEVEL_WARN, Device, Event, utcnow

log = logging.getLogger("tasmobackup.events")

# ntfy maps priority 1-5; we only ever use these three.
_NTFY_PRIORITY = {LEVEL_INFO: "3", LEVEL_WARN: "4", LEVEL_ERROR: "5"}
_NTFY_TAGS = {LEVEL_INFO: "information_source", LEVEL_WARN: "warning", LEVEL_ERROR: "rotating_light"}

def _header_safe(value: str) -> str:
    """Make a string safe for an HTTP header.

    Headers are ASCII-only, so a device called "Küchenlampe" would make httpx raise
    UnicodeEncodeError and kill the whole notification. RFC 2047 encoded-words are
    understood by ntfy (and ignored gracefully elsewhere), so umlauts survive.
    """
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


# Fire-and-forget notification tasks; kept referenced so they aren't GC'd mid-flight.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def record(level: str, message: str, device_id: int | None = None,
           device_name: str = "", *, notify: bool = True, key: str = "") -> None:
    """Write an event and, if it meets the configured threshold, push a notification.

    `key` makes the event state-based instead of repeating: while the device's stored
    alert state already equals `key`, the same problem is dropped silently. A device
    that stays offline is therefore reported once, not on every scheduler run. Call
    clear_alert() when the device is healthy again so the next fault is reported.

    Safe to call from anywhere: notification delivery happens in the background and
    never propagates errors into the caller's code path.
    """
    with session_scope() as s:
        if key and device_id is not None:
            device = s.get(Device, device_id)
            if device is not None:
                if device.alert_state == key:
                    return  # unchanged state — already reported
                device.alert_state = key
        s.add(
            Event(
                device_id=device_id,
                device_name=device_name,
                level=level,
                message=message,
            )
        )
        url = get_setting(s, "notify_url", "").strip()
        min_level = get_setting(s, "notify_min_level", LEVEL_WARN)
        fmt = get_setting(s, "notify_format", "ntfy")

    log.info("[%s] %s%s", level, f"{device_name}: " if device_name else "", message)

    if not notify or not url:
        return
    if LEVEL_ORDER.get(level, 0) < LEVEL_ORDER.get(min_level, 1):
        return
    try:
        _spawn(_send(url, fmt, level, message, device_name))
    except RuntimeError:
        # No running event loop (e.g. called from a sync test) — skip the push.
        log.debug("no event loop, notification skipped")


async def _send(url: str, fmt: str, level: str, message: str, device_name: str) -> None:
    title = f"TasmoBackup: {device_name}" if device_name else "TasmoBackup"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if fmt == "json":
                await client.post(
                    url,
                    json={
                        "level": level,
                        "device": device_name,
                        "message": message,
                        "time": utcnow().isoformat() + "Z",
                    },
                )
            else:
                # ntfy: plain-text body, metadata in headers. Works with ntfy.sh and
                # self-hosted instances; headers are ignored harmlessly elsewhere.
                await client.post(
                    url,
                    content=message.encode("utf-8"),
                    headers={
                        "Title": _header_safe(title),
                        "Priority": _NTFY_PRIORITY.get(level, "3"),
                        "Tags": _NTFY_TAGS.get(level, "information_source"),
                    },
                )
    except Exception as exc:  # noqa: BLE001 — a push must never break a backup run
        log.warning("notification to %s failed: %s", url, exc)


async def send_test(url: str, fmt: str = "ntfy") -> tuple[bool, Msg]:
    """Deliver a test message synchronously so the settings page can report the result."""
    if not url.strip():
        return False, msg("msg.notify_no_url")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if fmt == "json":
                r = await client.post(
                    url, json={"level": LEVEL_INFO, "device": "", "message": "TasmoBackup test",
                               "time": utcnow().isoformat() + "Z"}
                )
            else:
                r = await client.post(
                    url,
                    content=b"TasmoBackup test notification",
                    headers={"Title": "TasmoBackup", "Priority": "3", "Tags": "white_check_mark"},
                )
        if r.status_code < 400:
            return True, msg("msg.notify_sent", code=r.status_code)
        return False, msg("msg.notify_http", code=r.status_code)
    except Exception as exc:  # noqa: BLE001 — report the reason instead of a 500 page
        return False, msg("msg.notify_failed", error=exc)


def clear_alert(device_id: int) -> None:
    """Mark a device as healthy so its next fault is reported again."""
    with session_scope() as s:
        device = s.get(Device, device_id)
        if device is not None and device.alert_state:
            device.alert_state = ""


def unseen_count() -> int:
    with session_scope() as s:
        return s.query(Event).filter(
            Event.seen.is_(False), Event.level.in_([LEVEL_WARN, LEVEL_ERROR])
        ).count()


def mark_all_seen() -> None:
    with session_scope() as s:
        for e in s.scalars(select(Event).where(Event.seen.is_(False))).all():
            e.seen = True


def recent(limit: int = 200) -> list[Event]:
    with session_scope() as s:
        return list(s.scalars(select(Event).order_by(Event.created_at.desc()).limit(limit)).all())


def prune() -> None:
    """Drop events older than the configured retention so the table can't grow forever."""
    with session_scope() as s:
        days = get_int(s, "events_max_days")
        if days <= 0:
            return
        cutoff = utcnow() - timedelta(days=days)
        s.execute(delete(Event).where(Event.created_at < cutoff))


def clear_all() -> None:
    with session_scope() as s:
        s.execute(delete(Event))
