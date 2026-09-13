"""SQLAlchemy ORM models."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Device types
TYPE_TASMOTA = 0
TYPE_WLED = 1


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("mac", name="uq_device_mac"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    ip: Mapped[str] = mapped_column(String(64), index=True)
    mac: Mapped[str] = mapped_column(String(32), default="")
    type: Mapped[int] = mapped_column(Integer, default=TYPE_TASMOTA)
    version: Mapped[str] = mapped_column(String(64), default="")
    # Per-device HTTP credentials. Password stored encrypted (see security.py).
    username: Mapped[str] = mapped_column(String(64), default="admin")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    # When True the name was set by the user and is not overwritten from the device.
    name_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    # Opt-in firmware auto-update for THIS device. Off by default for safety.
    auto_update: Mapped[bool] = mapped_column(Boolean, default=False)
    # Reachability, updated by every get_info() call (backup, refresh, online check).
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # UTC offset the device applies, in minutes. Compared against the configured
    # display time zone to spot devices stuck on a fixed offset (no DST).
    tz_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Whether the device uses DST rules (Timezone 99) rather than a fixed offset.
    tz_dst: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Key of the problem last reported for this device ("" = all clear). Recurring
    # checks compare against it so a lasting fault is reported once, not every run.
    alert_state: Mapped[str] = mapped_column(String(32), default="")
    # Consecutive failed reachability checks. Used to debounce flaky WiFi so a single
    # dropped request does not raise an offline alert.
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_backup: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_update: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[str] = mapped_column(String(64), default="")
    filename: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# Event levels, ordered by severity so notifications can filter on a threshold.
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"
LEVEL_ORDER = {LEVEL_INFO: 0, LEVEL_WARN: 1, LEVEL_ERROR: 2}


class Event(Base):
    """Notable things that happened: failed backups, aborted updates, devices going
    offline. Written by the service layer so both the web UI and the scheduler
    produce the same visible history."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable: some events (scheduler runs, webhook failures) are not device-specific.
    device_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # Denormalised so the history stays readable after a device is deleted.
    device_name: Mapped[str] = mapped_column(String(128), default="")
    level: Mapped[str] = mapped_column(String(16), default=LEVEL_INFO, index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    # False until the user opens the events page; drives the dashboard warning banner.
    seen: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Setting(Base):
    """Simple key/value store for runtime-editable preferences."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
