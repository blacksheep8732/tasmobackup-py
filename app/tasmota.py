"""Async HTTP client for Tasmota and WLED devices.

Consolidates everything the original PHP did across many near-identical curl blocks
(status 0/2/5, scan, /dl backup, /u2 restore) into one small, typed module, plus the
firmware-update flow (OtaUrl + Upgrade) the original never implemented.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import get_config
from .models import TYPE_TASMOTA, TYPE_WLED

_cfg = get_config()
USER_AGENT = "TasmoBackup-py/0.1"


@dataclass
class DeviceInfo:
    """Normalised device metadata, regardless of Tasmota/WLED."""

    name: str = ""
    mac: str = ""
    version: str = ""
    type: int = TYPE_TASMOTA
    # UTC offset the device applies, in minutes (None if it didn't report a time).
    tz_offset: int | None = None
    # True when the device runs `Timezone 99`, i.e. applies DST rules. A fixed offset
    # looks correct for half the year and is wrong for the other half.
    tz_dst: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _client(ip: str) -> httpx.AsyncClient:
    # Tasmota's CSRF protection requires a matching Referer/Origin header.
    return httpx.AsyncClient(
        timeout=_cfg.http_timeout,
        follow_redirects=False,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"http://{ip}/",
            "Origin": f"http://{ip}",
        },
    )


async def _cmnd(c: httpx.AsyncClient, ip: str, user: str, password: str, cmnd: str) -> dict[str, Any] | None:
    """Run a Tasmota console command via /cm and return parsed JSON."""
    try:
        r = await c.get(
            f"http://{ip}/cm",
            params={"user": user, "password": password, "cmnd": cmnd},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


async def probe(ip: str, user: str, password: str) -> int | None:
    """Detect whether an IP hosts a Tasmota or WLED device. Returns type or None."""
    async with _client(ip) as c:
        try:
            r = await c.get(f"http://{ip}/", auth=(user, password))
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        body = r.text
        if "Tasmota" in body:
            return TYPE_TASMOTA
        if "WLED" in body:
            return TYPE_WLED
    return None


def _tz_offset_from_status(tim: dict[str, Any]) -> int | None:
    """Derive the device's UTC offset in minutes from a StatusTIM block.

    We compute Local - UTC rather than reading the "Timezone" field: with DST rules
    active that field reads "99" instead of an offset.
    """
    local, utc = str(tim.get("Local", "")), str(tim.get("UTC", ""))
    if not local or not utc:
        return None
    try:
        lt = datetime.fromisoformat(local)
        ut = datetime.fromisoformat(utc.rstrip("Z"))
    except ValueError:
        return None
    # Round to the nearest minute: the two timestamps can be a second apart.
    return int(round((lt - ut).total_seconds() / 60))


def _tz_dst_from_status(tim: dict[str, Any]) -> bool | None:
    """Whether the device applies DST rules — Tasmota reports "99" in that case."""
    raw = tim.get("Timezone")
    if raw is None:
        return None
    return str(raw).strip() == "99"


async def get_info(ip: str, user: str, password: str, dtype: int = TYPE_TASMOTA) -> DeviceInfo | None:
    """Fetch name/mac/version. Returns None if the device is unreachable."""
    if dtype == TYPE_WLED:
        async with _client(ip) as c:
            try:
                r = await c.get(f"http://{ip}/json", auth=(user, password))
                data = r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None
        if not data or "info" not in data:
            return None
        info = data["info"]
        mac = info.get("mac", "")
        mac = ":".join(mac[i : i + 2] for i in range(0, len(mac), 2)) if mac else ""
        return DeviceInfo(
            name=str(info.get("name", "")).strip(),
            mac=mac.upper(),
            version=str(info.get("ver", "")).strip(),
            type=TYPE_WLED,
            raw=data,
        )

    # Tasmota: Status 0 returns everything in one call on modern firmware.
    async with _client(ip) as c:
        data = await _cmnd(c, ip, user, password, "status 0")
        if not data:
            return None
        status = data.get("Status", {})
        fwr = data.get("StatusFWR", {})
        net = data.get("StatusNET", {})
        # Fallback for very old firmware that splits the responses.
        if not fwr:
            d2 = await _cmnd(c, ip, user, password, "status 2")
            fwr = (d2 or {}).get("StatusFWR", {})
        if not net:
            d5 = await _cmnd(c, ip, user, password, "status 5")
            net = (d5 or {}).get("StatusNET", {})

    name = (
        status.get("DeviceName")
        or (status.get("FriendlyName") or [""])[0]
        or status.get("Topic", "")
    )
    return DeviceInfo(
        name=str(name).strip(),
        mac=str(net.get("Mac", "")).upper(),
        version=str(fwr.get("Version", "")),
        type=TYPE_TASMOTA,
        tz_offset=_tz_offset_from_status(data.get("StatusTIM", {})),
        tz_dst=_tz_dst_from_status(data.get("StatusTIM", {})),
        raw=data,
    )


async def download_backup(ip: str, user: str, password: str, dtype: int = TYPE_TASMOTA) -> bytes | None:
    """Return the raw backup payload (.dmp for Tasmota, .zip bytes for WLED)."""
    async with _client(ip) as c:
        if dtype == TYPE_TASMOTA:
            try:
                r = await c.get(f"http://{ip}/dl", auth=(user, password))
            except httpx.HTTPError:
                return None
            if r.status_code != 200 or not r.content:
                return None
            return r.content

        # WLED: bundle cfg.json + presets.json into a zip.
        files: dict[str, bytes] = {}
        for fname, path in (("cfg.json", "/cfg.json?download"), ("presets.json", "/presets.json?download")):
            try:
                r = await c.get(f"http://{ip}{path}", auth=(user, password))
            except httpx.HTTPError:
                return None
            if r.status_code != 200:
                return None
            files[fname] = r.content
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fname, content in files.items():
                z.writestr(fname, content)
        return buf.getvalue()


# Only a successful upload makes Tasmota's /u2 page reload itself (it restarts).
# The status is 200 either way and the "Successful"/"Failed" words are translated
# (tasmota-DE says "Erfolgreich"), so this script is the language-independent tell.
# See HandleUploadDone() in xdrv_01_9_webserver.ino.
_UPLOAD_OK_MARKER = "setTimeout("


async def restore_backup(ip: str, user: str, password: str, data: bytes) -> bool:
    """Upload a .dmp config back to a Tasmota device (/rs then /u2)."""
    async with _client(ip) as c:
        try:
            # Arm the upload type as a settings restore. If this fails, /u2 ignores
            # the file but still reports success — so it must be checked.
            armed = await c.get(f"http://{ip}/rs", auth=(user, password))
            if armed.status_code != 200:
                return False
            r = await c.post(
                f"http://{ip}/u2",
                auth=(user, password),
                files={"u2": ("config.dmp", data, "application/octet-stream")},
            )
        except httpx.HTTPError:
            return False
        return r.status_code == 200 and _UPLOAD_OK_MARKER in r.text


async def get_ota_url(ip: str, user: str, password: str) -> str:
    """Read the device's configured OtaUrl, or "" if unavailable.

    This is the authoritative source for which image the device should flash: it
    carries the language variant (tasmota-DE.bin.gz) and the compression that the
    version string does not expose.
    """
    async with _client(ip) as c:
        res = await _cmnd(c, ip, user, password, "OtaUrl")
    if not res:
        return ""
    url = str(res.get("OtaUrl", "")).strip()
    return url if url.startswith("http") else ""


async def apply_commands(ip: str, user: str, password: str, commands: list[str]) -> bool:
    """Send several console commands in one Backlog call. True if the device answered."""
    if not commands:
        return False
    async with _client(ip) as c:
        res = await _cmnd(c, ip, user, password, "Backlog " + "; ".join(commands))
    return res is not None


async def upgrade_firmware(ip: str, user: str, password: str) -> bool:
    """Trigger an OTA update with `Upgrade 1` — nothing else.

    We deliberately do NOT set OtaUrl: the device already knows the correct image,
    including its language variant and compression, and Tasmota handles the whole
    procedure on its own (fetching tasmota-minimal first and rebooting when the full
    image does not fit into the free program flash). Any extra command from us can
    only get in the way.

    Caller MUST take a fresh backup before invoking this.
    """
    async with _client(ip) as c:
        res = await _cmnd(c, ip, user, password, "Upgrade 1")
        if res is None:
            return False
        # Tasmota answers {"Upgrade": "Version 15.6.0 from http://..."} when it
        # accepted the job, and a short refusal ("Option 1 unknown", "already
        # ...") otherwise. Treat anything without a source URL as a refusal.
        answer = str(res.get("Upgrade", ""))
        return "from" in answer.lower() or "http" in answer.lower()
