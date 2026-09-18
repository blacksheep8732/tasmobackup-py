"""Application configuration via environment variables (Pydantic Settings).

All settings can be set through ENV vars (Docker) or a .env file. Runtime-editable
preferences (backup interval, MQTT, themes, ...) live in the DB `settings` table
instead — see app.models.Setting.
"""
from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TB_", env_file=".env", extra="ignore")

    # --- Storage ---
    data_dir: Path = Path("/data")
    # SQLAlchemy URL. Defaults to SQLite inside data_dir. Another database needs its
    # driver (e.g. pymysql) — the published image ships none, so use a custom build.
    database_url: str | None = None

    # --- Web / security ---
    # Used to sign session cookies AND to derive the device-password encryption key.
    # If unset, a random key is generated once and kept in data_dir/.secret_key
    # (see _resolve_secret_key), so it stays stable across restarts.
    secret_key: str | None = None
    # Initial admin login for the web UI. Password is hashed on first run.
    admin_user: str = "admin"
    admin_password: str = "admin"
    # Set to False to disable the login wall entirely (e.g. behind Traefik auth).
    auth_enabled: bool = True

    # --- Tasmota defaults ---
    tasmota_user: str = "admin"
    tasmota_password: str = ""
    http_timeout: float = 15.0
    # Max concurrent device HTTP requests during scans/backups.
    concurrency: int = 15

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'tasmobackup.db'}"


# Former built-in default. Installs that never set TB_SECRET_KEY encrypted their device
# passwords with it, so it must keep working for them.
LEGACY_SECRET_KEY = "change-me-please-32-bytes-minimum-secret"
SECRET_KEY_FILE = ".secret_key"


def _resolve_secret_key(cfg: Config) -> str:
    """ENV value > persisted key file > legacy default (existing DB) > new random key."""
    if cfg.secret_key:
        return cfg.secret_key
    key_file = cfg.data_dir / SECRET_KEY_FILE
    if key_file.exists():
        return key_file.read_text().strip()
    if cfg.database_url is None and (cfg.data_dir / "tasmobackup.db").exists():
        log.warning("TB_SECRET_KEY not set and database already exists — keeping the "
                    "legacy default key so stored device passwords stay readable.")
        return LEGACY_SECRET_KEY
    key = secrets.token_hex(32)
    key_file.write_text(key + "\n")
    key_file.chmod(0o600)
    log.info("Generated a new secret key in %s", key_file)
    return key


@lru_cache
def get_config() -> Config:
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    cfg.secret_key = _resolve_secret_key(cfg)
    return cfg
