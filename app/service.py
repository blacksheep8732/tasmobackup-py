"""Business logic: add/scan devices, take backups, restore, and opt-in updates.

Kept separate from the web layer (main.py) and the scheduler so all three share the
same well-tested code paths.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from . import events, github, mqtt, tasmota, tz
from .config import get_config
from .db import get_int, get_setting, session_scope
from .models import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, TYPE_WLED, Backup, Device
from .security import decrypt, encrypt

_cfg = get_config()
log = logging.getLogger("tasmobackup.service")

# Keep references to fire-and-forget background tasks so they aren't garbage collected.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _safe(name: str) -> str:
    name = re.sub(r"\s+", "_", name.strip())
    return re.sub(r"[^A-Za-z0-9_\-]", "", name) or "device"


def _safe_version(version: str) -> str:
    """Version string as reported by the device, made safe for a file name.

    Keeps the readable form ('15.6.0(release-tasmota)'); anything else — a '/'
    would point into a non-existent sub-folder and fail the backup — becomes '_'.
    """
    return re.sub(r"[^A-Za-z0-9._()\-]", "_", version.strip())[:64]


def _local_now() -> datetime:
    """Current time in the configured display timezone (for backup filenames)."""
    with session_scope() as s:
        tzname = get_setting(s, "timezone", "Europe/Berlin")
    try:
        return datetime.now(ZoneInfo(tzname))
    except Exception:
        return datetime.now()


def _creds(device: Device) -> tuple[str, str]:
    user = device.username or _cfg.tasmota_user
    password = decrypt(device.password_enc) or _cfg.tasmota_password
    return user, password


# Consecutive failed checks before a device counts as offline. WiFi devices drop the
# odd request; alerting on the first miss would produce constant false alarms.
OFFLINE_THRESHOLD = 2

# Devices currently being flashed. A device reboots (twice, when Tasmota takes the
# minimal-image detour) during an update, so its unreachability is expected and must
# not raise an offline alert.
_updating: set[int] = set()


def _mark_reachability(device_id: int, reachable: bool) -> None:
    """Record the outcome of a device contact and raise an event on state changes.

    Called after every get_info()/backup attempt, so the dashboard's online column
    reflects the most recent real contact rather than a separate ping.
    """
    with session_scope() as s:
        device = s.get(Device, device_id)
        if not device:
            return
        name = device.name
        was_online = device.online
        first_contact = device.last_seen is None
        if reachable:
            device.online = True
            device.last_seen = datetime.utcnow()
            device.fail_count = 0
            # Don't announce "back online" for a device we are seeing for the first
            # time (fresh install or right after the schema migration).
            recovered = not was_online and not first_contact
            lost = False
        else:
            device.fail_count += 1
            lost = was_online and device.fail_count >= OFFLINE_THRESHOLD
            if device.fail_count >= OFFLINE_THRESHOLD:
                device.online = False
            recovered = False

    if lost and device_id not in _updating:
        events.record(LEVEL_WARN, f"no longer reachable ({OFFLINE_THRESHOLD} failed checks)",
                      device_id, name, key="unreachable")
    elif recovered:
        # Back up: clear the stored fault first, so this and any later problem are
        # reported again instead of being swallowed as "already known".
        events.clear_alert(device_id)
        events.record(LEVEL_INFO, "reachable again", device_id, name)


# --------------------------------------------------------------------------- #
# Device management
# --------------------------------------------------------------------------- #
async def add_device(ip: str, username: str = "", password: str = "") -> str:
    user = username or _cfg.tasmota_user
    pw = password or _cfg.tasmota_password
    dtype = await tasmota.probe(ip, user, pw)
    if dtype is None:
        return f"{ip}: no Tasmota/WLED device found."
    info = await tasmota.get_info(ip, user, pw, dtype)
    if info is None:
        return f"{ip}: device not responding."

    with session_scope() as s:
        existing = None
        if info.mac:
            existing = s.scalar(select(Device).where(Device.mac == info.mac))
        if existing is None:
            existing = s.scalar(select(Device).where(Device.ip == ip))
        if existing:
            existing.ip, existing.version = ip, info.version
            if info.name and not existing.name_custom:
                existing.name = info.name
            return f"{ip}: {info.name} updated."
        s.add(
            Device(
                name=info.name or ip,
                ip=ip,
                mac=info.mac,
                type=info.type,
                version=info.version,
                username=user,
                password_enc=encrypt(pw),
            )
        )
    return f"{ip}: {info.name or ip} added."


def _remove_backup_file(filename: str) -> None:
    """Delete a backup file — but only if it really lies inside the backup folder."""
    path = Path(filename).resolve()
    root = _cfg.backup_dir.resolve()
    if path.is_relative_to(root):
        path.unlink(missing_ok=True)
    else:
        log.warning("refusing to delete %s: outside %s", path, root)


def delete_device(device_id: int) -> bool:
    """Remove a device together with its backups (rows and files).

    Left behind, the backups would be invisible in the UI and never pruned again,
    and re-adding the device creates a new id that doesn't see them either. The
    event log is kept — it stores the device name, so the history stays readable.
    """
    with session_scope() as s:
        device = s.get(Device, device_id)
        if not device:
            return False
        folders = set()
        for b in s.scalars(select(Backup).where(Backup.device_id == device_id)).all():
            _remove_backup_file(b.filename)
            folders.add(Path(b.filename).resolve().parent)
            s.delete(b)
        s.delete(device)
    root = _cfg.backup_dir.resolve()
    for folder in folders:
        # Only the device's own sub-folder, and only once it is empty.
        if folder != root and folder.is_relative_to(root) and folder.is_dir() \
                and not any(folder.iterdir()):
            folder.rmdir()
    _updating.discard(device_id)
    return True


async def scan_subnet(cidr: str, username: str = "", password: str = "") -> list[str]:
    """Probe every host in a CIDR range and add the ones that answer."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return [f"invalid subnet: {cidr}"]
    user = username or _cfg.tasmota_user
    pw = password or _cfg.tasmota_password
    sem = asyncio.Semaphore(_cfg.concurrency)
    results: list[str] = []

    async def check(ip: str) -> None:
        async with sem:
            if await tasmota.probe(ip, user, pw) is not None:
                results.append(await add_device(ip, user, pw))

    await asyncio.gather(*(check(str(h)) for h in net.hosts()))
    return results


async def mqtt_discover() -> tuple[bool, list[str]]:
    """Discover Tasmota devices via MQTT (broker config from settings), then add them.

    Returns (ok, messages). ok=False means the broker could not be reached/configured.
    """
    with session_scope() as s:
        host = get_setting(s, "mqtt_host", "").strip()
        port = get_int(s, "mqtt_port")
        user = get_setting(s, "mqtt_user", "")
        password = decrypt(get_setting(s, "mqtt_password", ""))
        group = get_setting(s, "mqtt_topic", "tasmotas").strip() or "tasmotas"
    if not host:
        return False, ["MQTT host not configured"]

    found = await mqtt.discover(host, port, user, password, group)
    if found is None:
        return False, [f"MQTT connection to {host}:{port} failed"]

    # MQTT only gives us the IPs; add over HTTP (reuses probe + metadata + storage).
    messages: list[str] = []
    sem = asyncio.Semaphore(_cfg.concurrency)

    async def add(ip: str) -> None:
        async with sem:
            messages.append(await add_device(ip, _cfg.tasmota_user, _cfg.tasmota_password))

    await asyncio.gather(*(add(d.ip) for d in found if d.ip))
    if not messages:
        messages.append("No devices answered via MQTT")
    return True, messages


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #
async def backup_device(device_id: int) -> tuple[bool, str]:
    with session_scope() as s:
        device = s.get(Device, device_id)
        if not device:
            return False, "device not found"
        ip, dtype, name = device.ip, device.type, device.name
        name_custom = device.name_custom
        user, pw = _creds(device)

    # Refresh metadata first so the filename has the current version.
    info = await tasmota.get_info(ip, user, pw, dtype)
    version = info.version if info else ""
    # A user-set name wins; otherwise use the name reported by the device.
    if not name_custom and info and info.name:
        name = info.name
    name = name or ip

    _mark_reachability(device_id, info is not None)

    data = await tasmota.download_backup(ip, user, pw, dtype)
    if not data:
        with session_scope() as s:
            reachable = s.get(Device, device_id).online
        if reachable:
            # Answers our status query but won't hand over its config — a real fault
            # worth its own alert.
            events.record(LEVEL_ERROR, "backup failed — device did not deliver a config",
                          device_id, name, key="backup_failed")
        else:
            # Same root cause as the offline alert; sharing the key keeps the
            # scheduler from re-reporting it every 15 minutes.
            events.record(LEVEL_ERROR, "backup failed — device unreachable",
                          device_id, name, key="unreachable")
        return False, f"{name}: backup failed (offline?)"

    ext = ".zip" if dtype == TYPE_WLED else ".dmp"
    folder = _cfg.backup_dir / _safe(name)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _local_now().strftime("%Y-%m-%d_%H-%M-%S")
    base = f"{_safe(name)}-{stamp}-v{_safe_version(version)}"
    path = folder / f"{base}{ext}"
    # Two devices with the same name share a folder; backed up in the same second
    # (backup_all runs them in parallel) the second would overwrite the first.
    n = 2
    while path.exists():
        path = folder / f"{base}-{n}{ext}"
        n += 1
    path.write_bytes(data)

    with session_scope() as s:
        device = s.get(Device, device_id)
        device.version = version or device.version
        if info and info.name and not device.name_custom:
            device.name = info.name
        if info and info.mac:
            device.mac = info.mac
        device.last_backup = datetime.utcnow()
        s.add(
            Backup(
                device_id=device_id,
                name=name,
                version=version,
                filename=str(path),
                size=len(data),
            )
        )
    _cleanup_backups(device_id)
    events.clear_alert(device_id)
    return True, f"{name}: backup ok"


async def backup_all() -> tuple[int, list[str]]:
    """Back up every device in parallel. Returns (device count, failure messages).

    Sequentially, each offline device cost two HTTP timeouts before the next one
    started, so the button could take minutes.
    """
    with session_scope() as s:
        ids = [d.id for d in s.scalars(select(Device)).all()]
    sem = asyncio.Semaphore(_cfg.concurrency)
    failed: list[str] = []

    async def one(device_id: int) -> None:
        async with sem:
            ok, msg = await backup_device(device_id)
            if not ok:
                failed.append(msg)

    await asyncio.gather(*(one(i) for i in ids))
    return len(ids), failed


def _cleanup_backups(device_id: int) -> None:
    with session_scope() as s:
        max_count = get_int(s, "backup_max_count")
        max_days = get_int(s, "backup_max_days")
        q = select(Backup).where(Backup.device_id == device_id).order_by(Backup.created_at.desc())
        backups = list(s.scalars(q).all())
        to_delete: list[Backup] = []
        if max_count > 0:
            to_delete += backups[max_count:]
        if max_days > 0:
            cutoff = datetime.utcnow() - timedelta(days=max_days)
            to_delete += [b for b in backups if b.created_at < cutoff]
        for b in set(to_delete):
            _remove_backup_file(b.filename)
            s.delete(b)


async def restore_device(device_id: int, backup_id: int) -> tuple[bool, str]:
    with session_scope() as s:
        device = s.get(Device, device_id)
        backup = s.get(Backup, backup_id)
        if not device or not backup:
            return False, "device or backup not found"
        if device.type == TYPE_WLED:
            return False, "restore is only supported for Tasmota devices"
        ip, name = device.ip, device.name
        user, pw = _creds(device)
        path = Path(backup.filename)
    if not path.exists():
        return False, "backup file missing"
    ok = await tasmota.restore_backup(ip, user, pw, path.read_bytes())
    if not ok:
        events.record(LEVEL_ERROR, "restore failed", device_id, name)
    else:
        events.record(LEVEL_INFO, f"restore from {path.name} ok", device_id, name)
    return ok, "restore ok" if ok else "restore failed"


# --------------------------------------------------------------------------- #
# Status refresh (no backup) — used on demand and after a firmware update
# --------------------------------------------------------------------------- #
async def refresh_device(device_id: int) -> tuple[bool, str]:
    """Query a device for current name/version/mac and store it. No backup taken."""
    with session_scope() as s:
        device = s.get(Device, device_id)
        if not device:
            return False, ""
        ip, dtype = device.ip, device.type
        user, pw = _creds(device)
    info = await tasmota.get_info(ip, user, pw, dtype)
    _mark_reachability(device_id, info is not None)
    if not info:
        return False, ""
    with session_scope() as s:
        device = s.get(Device, device_id)
        device.version = info.version or device.version
        if info.name and not device.name_custom:
            device.name = info.name
        if info.mac:
            device.mac = info.mac
        if info.tz_offset is not None:
            device.tz_offset = info.tz_offset
        if info.tz_dst is not None:
            device.tz_dst = info.tz_dst
    return True, info.version


async def sync_timezone(device_id: int) -> tuple[bool, str]:
    """Push the app's time zone onto one device (Timezone/TimeStd/TimeDst).

    Devices left on a fixed offset ignore the DST rules they carry and run an hour
    off for half the year. Writing the rules derived from the configured zone fixes
    that regardless of what the device had before.
    """
    with session_scope() as s:
        device = s.get(Device, device_id)
        if not device:
            return False, "device not found"
        if device.type == TYPE_WLED:
            return False, "time sync is only supported for Tasmota devices"
        ip, name = device.ip, device.name
        user, pw = _creds(device)
        tzname = get_setting(s, "timezone", "Europe/Berlin")

    commands = tz.tasmota_commands(tzname, datetime.utcnow().year)
    if not commands:
        return False, f"unknown time zone '{tzname}'"

    if not await tasmota.apply_commands(ip, user, pw, commands):
        events.record(LEVEL_ERROR, f"time sync failed — device did not accept the commands",
                      device_id, name)
        return False, f"{name}: device did not accept the time settings"

    # Read back so the dashboard reflects reality rather than our intent.
    #
    # Tasmota queues Backlog commands and applies them with a short delay, so reading
    # immediately still returns the OLD zone. Retry a few times before complaining —
    # doing it in one shot produced a "still reports UTC+1" warning for every device
    # even though the change had gone through.
    expected = tz.offset_minutes(tzname)
    new_off = None
    for attempt in range(3):
        await asyncio.sleep(1.5)
        ok, _ = await refresh_device(device_id)
        if not ok:
            continue
        with session_scope() as s:
            new_off = s.get(Device, device_id).tz_offset
        if new_off == expected:
            break

    if new_off is not None and expected is not None and new_off != expected:
        events.record(LEVEL_WARN,
                      f"time sync applied but device still reports UTC{new_off // 60:+d}",
                      device_id, name)
        return False, f"{name}: still off after sync"
    events.record(LEVEL_INFO, f"time zone set to {tzname}", device_id, name)
    return True, f"{name}: time zone synced"


async def refresh_all() -> None:
    with session_scope() as s:
        ids = [d.id for d in s.scalars(select(Device)).all()]
    sem = asyncio.Semaphore(_cfg.concurrency)

    async def one(device_id: int) -> None:
        async with sem:
            await refresh_device(device_id)

    await asyncio.gather(*(one(i) for i in ids))


async def _watch_update(device_id: int, old_version: str, *, delay: float = 90.0,
                        interval: float = 20.0, attempts: int = 90,
                        nudge_after: int = 3) -> None:
    """Follow a device through the flash and push it over the line if it stalls.

    When the full image does not fit into the free program flash, Tasmota upgrades in
    two stages: it flashes tasmota-minimal, reboots, and is then supposed to fetch the
    real image. In practice it can stop right there, sitting on the stripped-down
    build — that has to be re-triggered, which is what `nudge_after` does (once, after
    the device has been idle on the minimal build for a few checks).

    The window is generous on purpose: two downloads plus two reboots over WiFi take
    several minutes, and an earlier 11-minute window expired before the device had
    even reached the minimal stage, producing a misleading "still on the old version".
    """
    await asyncio.sleep(delay)
    with session_scope() as s:
        device = s.get(Device, device_id)
        name = device.name if device else str(device_id)
    saw_minimal = False
    nudged = False
    minimal_rounds = 0
    current = old_version

    for _ in range(attempts):
        ok, version = await refresh_device(device_id)
        if ok and version:
            current = version
            _, tag = github.parse_version(version)
            on_minimal = "minimal" in tag.lower()

            if on_minimal:
                minimal_rounds += 1
                if not saw_minimal:
                    saw_minimal = True
                    log.info("device %s reached the minimal image", device_id)
                # Stalled on stage one — re-trigger it ourselves, once.
                if not nudged and minimal_rounds >= nudge_after:
                    nudged = True
                    with session_scope() as s:
                        d = s.get(Device, device_id)
                        ip = d.ip
                        user, pw = _creds(d)
                    started = await tasmota.upgrade_firmware(ip, user, pw)
                    events.record(
                        LEVEL_WARN if started else LEVEL_ERROR,
                        "stalled on the minimal image — second stage re-triggered"
                        if started else
                        "stalled on the minimal image and would not accept a retry",
                        device_id, name,
                    )
            elif version != old_version:
                # A real build that isn't the one we started from: we're done.
                events.clear_alert(device_id)
                events.record(LEVEL_INFO, f"update finished: {old_version} -> {version}",
                              device_id, name)
                _updating.discard(device_id)
                return
        await asyncio.sleep(interval)

    window = int(delay + attempts * interval)
    _updating.discard(device_id)
    if saw_minimal:
        events.record(
            LEVEL_ERROR,
            f"update stuck — device is still running the minimal image after "
            f"{window // 60} min"
            + (" despite a retry" if nudged else "")
            + ". It is reachable but on a stripped-down build; trigger the update again.",
            device_id, name, key="update_failed",
        )
    else:
        events.record(
            LEVEL_ERROR,
            f"update problem — device reports '{current}' after {window // 60} min "
            f"(started from '{old_version}'). It may have failed to flash.",
            device_id, name, key="update_failed",
        )


# --------------------------------------------------------------------------- #
# Firmware update (opt-in, backup-first)
# --------------------------------------------------------------------------- #
async def _current_ota_image(ip: str, user: str, pw: str) -> str:
    """Read (never write) the OtaUrl the device will flash — for the log/event only.

    Purely informational: it tells the user which image was pulled, which the version
    string cannot (tasmota-DE.bin.gz and tasmota.bin both report '(release-tasmota)').
    """
    return await tasmota.get_ota_url(ip, user, pw)


async def update_device(device_id: int, force: bool = False) -> tuple[bool, str]:
    """Update a single device's firmware. ALWAYS backs up first.

    `force` skips the outdated-check (manual button). The scheduler never forces.
    """
    with session_scope() as s:
        device = s.get(Device, device_id)
        if not device:
            return False, "device not found"
        if device.type == TYPE_WLED:
            return False, "auto-update is only supported for Tasmota devices"
        ip, version, name = device.ip, device.version, device.name

    # Never replace a custom build on our own; only an explicit (warned) click may.
    if not force and github.is_custom_build(version):
        return False, f"{name}: custom firmware {version} — auto-update skipped"

    if not force and not await github.is_outdated(version):
        return False, f"{name}: already up to date"

    # A manual click deserves a fresh answer even if the same thing failed before;
    # the scheduler's repeated attempts stay quiet via the alert key below.
    if force:
        events.clear_alert(device_id)

    with session_scope() as s:
        device = s.get(Device, device_id)
        user, pw = _creds(device)
    image = await _current_ota_image(ip, user, pw)

    # SAFETY: a fresh backup must succeed before we flash anything.
    ok, msg = await backup_device(device_id)
    if not ok:
        events.record(LEVEL_ERROR, f"update aborted — pre-update backup failed ({msg})",
                      device_id, name, key="update_failed")
        return False, f"{name}: aborted — pre-update backup failed ({msg})"

    # Trigger only — the device knows its image and drives the rest itself.
    started = await tasmota.upgrade_firmware(ip, user, pw)
    if not started:
        events.record(LEVEL_ERROR, "update failed — device rejected the OTA command",
                      device_id, name, key="update_failed")
        return False, f"{name}: OTA command failed"
    _updating.add(device_id)
    with session_scope() as s:
        s.get(Device, device_id).last_update = datetime.utcnow()
    # Refresh the stored version once the device finishes flashing and reboots,
    # so the dashboard doesn't keep showing the old firmware until the next backup.
    detail = f" using {image.rsplit('/', 1)[-1]}" if image else ""
    events.record(LEVEL_INFO, f"update started from {version}{detail}", device_id, name)
    _spawn(_watch_update(device_id, version))
    return True, f"{name}: update started{detail}"
