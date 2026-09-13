"""Translate an IANA time zone into Tasmota's Timezone/TimeStd/TimeDst commands.

Tasmota does not know zone names. It either applies a fixed UTC offset, or — with
`Timezone 99` — switches between two offsets using explicit rules. A device left on
a fixed offset silently drifts by an hour for half the year, which is exactly the
state most devices were found in.

We derive the rules from the zone configured in the app settings, so the devices
follow the same clock as the server without anyone hand-copying DST dates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def offset_minutes(tzname: str, at: datetime | None = None) -> int | None:
    """Current UTC offset of a zone in minutes, or None for an unknown zone."""
    try:
        zone = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    moment = (at or datetime.now(dt_timezone.utc)).astimezone(zone)
    off = moment.utcoffset()
    return None if off is None else int(off.total_seconds() // 60)


def _transitions(zone: ZoneInfo, year: int) -> list[tuple[datetime, int, int]]:
    """Find offset changes in a year as (utc_moment, old_minutes, new_minutes).

    Hour-by-hour scan: a year is 8760 steps, the result is cached, and it avoids
    depending on tzdata internals that differ between Python versions.
    """
    out: list[tuple[datetime, int, int]] = []
    t = datetime(year, 1, 1, tzinfo=dt_timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=dt_timezone.utc)
    prev = int(t.astimezone(zone).utcoffset().total_seconds() // 60)
    while t < end:
        t += timedelta(hours=1)
        cur = int(t.astimezone(zone).utcoffset().total_seconds() // 60)
        if cur != prev:
            out.append((t, prev, cur))
            prev = cur
    return out


def _rule(moment: datetime, old_off: int, new_off: int) -> tuple[int, int, int, int, int]:
    """Turn one transition into Tasmota's (week, month, dow, hour, offset).

    The hour Tasmota expects is the local wall-clock time *before* the jump, so we
    convert the UTC moment using the offset that was still in effect.
    """
    local_before = moment + timedelta(minutes=old_off)
    day, month = local_before.day, local_before.month
    # Days in this month, without calendar imports.
    nxt = (local_before.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_in_month = (nxt - timedelta(days=1)).day
    # week: 0 means "last", otherwise the 1st..4th occurrence of that weekday.
    week = 0 if day + 7 > days_in_month else (day - 1) // 7 + 1
    # Tasmota counts 1=Sunday..7=Saturday; Python has Monday=0..Sunday=6.
    dow = (local_before.weekday() + 1) % 7 + 1
    return week, month, dow, local_before.hour, new_off


@lru_cache(maxsize=32)
def has_dst(tzname: str, year: int) -> bool:
    """True if the zone switches offsets during the year."""
    try:
        zone = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return len(_transitions(zone, year)) == 2


@lru_cache(maxsize=32)
def tasmota_commands(tzname: str, year: int) -> list[str] | None:
    """Commands that make a device follow `tzname`, or None if the zone is unknown.

    Returns either a single fixed-offset `Timezone <h>` (zones without DST) or the
    rule-based trio. TimeDst/TimeStd are sent before `Timezone 99` so the rules are
    in place when the device starts applying them.
    """
    try:
        zone = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        return None

    changes = _transitions(zone, year)
    if len(changes) != 2:
        # No DST (or something exotic like a one-off change) — use a fixed offset.
        off = offset_minutes(tzname)
        if off is None:
            return None
        if off % 60 == 0 and -13 * 60 <= off <= 13 * 60:
            return [f"Timezone {off // 60}"]
        # Fractional offsets can't be expressed by `Timezone <h>`; emulate them with
        # two identical rules under Timezone 99.
        rule = f"0,1,1,1,0,{off}"
        return [f"TimeDst {rule}", f"TimeStd {rule}", "Timezone 99"]

    first, second = changes
    # The transition that increases the offset starts DST.
    dst_change, std_change = (first, second) if first[2] > first[1] else (second, first)
    dst = _rule(*dst_change)
    std = _rule(*std_change)
    # Southern hemisphere zones start DST later in the year than they end it.
    hemisphere = 0 if dst[1] < std[1] else 1

    def fmt(r: tuple[int, int, int, int, int]) -> str:
        week, month, dow, hour, off = r
        return f"{hemisphere},{week},{month},{dow},{hour},{off}"

    return [f"TimeDst {fmt(dst)}", f"TimeStd {fmt(std)}", "Timezone 99"]
