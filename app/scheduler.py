"""APScheduler-driven background jobs: scheduled backups + opt-in updates.

Replaces the original's external cron + wget approach. One in-process scheduler,
no separate crond needed in the container.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from . import events, service
from .db import get_int, get_setting, session_scope
from .models import Device, utcnow

log = logging.getLogger("tasmobackup.scheduler")
scheduler = AsyncIOScheduler()


async def run_scheduled_backups() -> None:
    """Back up every device whose last backup is older than the configured interval."""
    with session_scope() as s:
        interval = get_int(s, "backup_interval_hours")
        auto_global = get_setting(s, "auto_update_global", "N") == "Y"
        mqtt_autoscan = get_setting(s, "mqtt_autoscan", "N") == "Y"
        cutoff = utcnow() - timedelta(hours=interval)

    # Optionally discover new devices via MQTT before backing up.
    if mqtt_autoscan:
        ok, messages = await service.mqtt_discover()
        log.info("mqtt autoscan: %s", "; ".join(messages) if ok else f"skipped ({messages})")

    with session_scope() as s:
        due = [
            (d.id, d.auto_update)
            for d in s.scalars(select(Device)).all()
            if d.last_backup is None or d.last_backup < cutoff
        ]

    log.info("scheduled run: %d device(s) due", len(due))
    for device_id, auto_update in due:
        # One device's unexpected error must not cost every device after it its backup.
        try:
            ok, msg = await service.backup_device(device_id)
            log.info(msg)
            # Opt-in auto-update: only when the global switch AND the per-device flag
            # are on. update_device() backs up again right before flashing.
            if ok and auto_global and auto_update:
                u_ok, u_msg = await service.update_device(device_id, force=False)
                log.info(u_msg)
        except Exception:  # noqa: BLE001 — logged with traceback, run continues
            log.exception("scheduled run: device %s failed", device_id)

    events.prune()


async def run_online_checks() -> None:
    """Contact every device so the dashboard's online column stays current.

    Reuses refresh_all(), which updates name/version/mac as a side effect and feeds
    _mark_reachability() — so an unreachable device raises an event on its own.
    """
    await service.refresh_all()


def _online_interval() -> int:
    with session_scope() as s:
        return get_int(s, "online_check_minutes")


def reschedule_online_check() -> None:
    """(Re)install the reachability job from the current setting. 0 disables it.

    Called at startup and again whenever the settings page is saved, so a changed
    interval takes effect without restarting the container.
    """
    minutes = _online_interval()
    existing = scheduler.get_job("online")
    if minutes <= 0:
        if existing:
            scheduler.remove_job("online")
            log.info("online check disabled")
        return
    scheduler.add_job(
        run_online_checks,
        "interval",
        minutes=minutes,
        id="online",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now() + timedelta(seconds=45),
    )
    log.info("online check every %d min", minutes)


def start() -> None:
    # Check every 15 min so short intervals (e.g. 1 h) are honoured; the interval gate
    # inside the job decides which devices are actually due. Fire once shortly after
    # startup too, so a fresh container takes an initial backup instead of waiting.
    scheduler.add_job(
        run_scheduled_backups,
        "interval",
        minutes=15,
        id="backups",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now() + timedelta(seconds=20),
    )
    scheduler.start()
    reschedule_online_check()
    log.info("scheduler started (checks every 15 min, first run in ~20 s)")


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
