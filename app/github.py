"""Fetch the latest Tasmota firmware release info from GitHub, cached on disk.

Used to show whether a device is outdated. Note that we deliberately do NOT derive
an OTA image URL from this: the device's own OtaUrl is authoritative (it carries the
language variant, which the version string does not expose) and Tasmota runs the
whole upgrade itself once triggered.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import get_config

_cfg = get_config()
_RELEASES_URL = "https://api.github.com/repos/arendst/Tasmota/releases/latest"
_CACHE_FILE = _cfg.data_dir / "tasmota-release.json"
_CACHE_TTL = 6 * 3600  # seconds
# After a failed lookup, don't ask again for this long. The dashboard polls every
# 30 s; without a pause each poll would wait for the full timeout while offline.
_RETRY_AFTER = 10 * 60  # seconds
_last_failure = 0.0


def _read_cache() -> dict[str, Any] | None:
    try:
        return json.loads(_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return None


async def latest_release() -> dict[str, Any] | None:
    """Return the cached/fresh 'latest' release JSON, or None if unavailable."""
    global _last_failure
    if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _CACHE_TTL:
        cached = _read_cache()
        if cached is not None:
            return cached
    if time.time() - _last_failure < _RETRY_AFTER:
        return _read_cache()  # stale is better than waiting on a network that's down
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "TasmoBackup-py"}) as c:
            r = await c.get(_RELEASES_URL)
            if r.status_code != 200:
                raise httpx.HTTPError("bad status")
            data = r.json()
        _CACHE_FILE.write_text(json.dumps(data))
        return data
    except (httpx.HTTPError, ValueError, OSError):
        # Fall back to a stale cache if the network is down.
        _last_failure = time.time()
        return _read_cache()


def parse_version(version: str) -> tuple[str, str]:
    """Split a Tasmota version string like '14.2.0(tasmota)' into (number, tag)."""
    if "(" in version:
        num, _, rest = version.partition("(")
        return num.strip(), f"({rest.rstrip(')')})"
    return version.strip(), ""


def is_custom_build(version: str) -> bool:
    """True for firmware that did not come from an official Tasmota release.

    Official release images report their build as '(release-<variant>)'. Anything
    else with a build tag — '(gas)', '(ee1d867-scripting)', a self-compiled
    '(tasmota)' — is someone's own build. An OTA 'update' would replace it with the
    official image and lose whatever made it special, so it is never auto-updated.
    """
    _, tag = parse_version(version)
    return bool(tag) and not tag.startswith("(release-")


async def latest_version() -> str:
    rel = await latest_release()
    if not rel:
        return ""
    return str(rel.get("tag_name", "")).lstrip("v")


async def is_outdated(version: str) -> bool:
    num, _ = parse_version(version)
    latest = await latest_version()
    if not num or not latest:
        return False
    return _semver(num) < _semver(latest)


def _semver(v: str) -> tuple[int, ...]:
    parts = []
    for chunk in v.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)
