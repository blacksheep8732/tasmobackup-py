"""Database engine, session factory and typed settings helpers."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_config
from .models import Base, Setting

_cfg = get_config()
_connect_args = {"check_same_thread": False} if _cfg.sqlalchemy_url.startswith("sqlite") else {}
engine = create_engine(_cfg.sqlalchemy_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Default runtime settings, created on first run.
DEFAULT_SETTINGS: dict[str, str] = {
    "backup_interval_hours": "24",   # how often the scheduler backs up each device
    "backup_max_count": "0",         # keep at most N backups per device (0 = unlimited)
    "backup_max_days": "0",          # delete backups older than N days (0 = unlimited)
    "auto_update_global": "N",       # master switch; even when 'Y', only opt-in devices update
    "scan_subnet": "",               # e.g. 192.168.178.0/24 for IP-range scan
    "mqtt_host": "",
    "mqtt_port": "1883",
    "mqtt_user": "",
    "mqtt_password": "",
    "mqtt_topic": "tasmotas",
    "mqtt_autoscan": "N",            # run MQTT discovery before each scheduled backup
    "theme": "auto",
    "language": "de",
    "timezone": "Europe/Berlin",   # for displaying timestamps (stored in UTC)
    # --- Reachability ---
    "online_check_minutes": "5",     # how often to ping devices (0 = disabled)
    # --- Notifications ---
    "notify_url": "",                # ntfy topic URL or generic webhook (empty = off)
    "notify_format": "ntfy",         # ntfy (plain body + headers) | json (generic webhook)
    "notify_min_level": "warn",      # info | warn | error
    "events_max_days": "30",         # prune the event log after N days (0 = keep all)
}


def _migrate() -> None:
    """Lightweight, additive migrations for columns added after initial release."""
    insp = inspect(engine)
    if "devices" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("devices")}
    with engine.begin() as conn:
        if "name_custom" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN name_custom BOOLEAN DEFAULT 0"))
        if "online" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN online BOOLEAN DEFAULT 0"))
        if "last_seen" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN last_seen DATETIME"))
        if "fail_count" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN fail_count INTEGER DEFAULT 0"))
        if "tz_offset" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN tz_offset INTEGER"))
        if "tz_dst" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN tz_dst BOOLEAN"))
        if "alert_state" not in cols:
            conn.execute(text("ALTER TABLE devices ADD COLUMN alert_state VARCHAR(32) DEFAULT ''"))
            # Devices already known to be offline count as "reported" — otherwise the
            # upgrade itself would emit a fresh alert for a state that hasn't changed.
            conn.execute(text("UPDATE devices SET alert_state='unreachable' WHERE online=0"))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate()
    with SessionLocal() as s:
        existing = {row.key for row in s.scalars(select(Setting)).all()}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                s.add(Setting(key=key, value=value))
        s.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_setting(s: Session, key: str, default: str = "") -> str:
    row = s.get(Setting, key)
    return row.value if row else default


def set_setting(s: Session, key: str, value: str) -> None:
    row = s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))


def all_settings(s: Session) -> dict[str, str]:
    return {row.key: row.value for row in s.scalars(select(Setting)).all()}
