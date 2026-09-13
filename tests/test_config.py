"""Secret-key resolution: ENV > key file > legacy default (existing DB) > generated."""
from app.config import LEGACY_SECRET_KEY, SECRET_KEY_FILE, Config, _resolve_secret_key


def _cfg(tmp_path, **kw):
    return Config(data_dir=tmp_path, **{"secret_key": None, **kw})


def test_env_value_wins(tmp_path):
    (tmp_path / SECRET_KEY_FILE).write_text("from-file")
    assert _resolve_secret_key(_cfg(tmp_path, secret_key="from-env")) == "from-env"


def test_generates_and_persists_key(tmp_path):
    key = _resolve_secret_key(_cfg(tmp_path))
    assert len(key) == 64
    assert (tmp_path / SECRET_KEY_FILE).stat().st_mode & 0o777 == 0o600
    # Second start reads the same key back.
    assert _resolve_secret_key(_cfg(tmp_path)) == key


def test_existing_db_without_key_keeps_legacy_default(tmp_path):
    (tmp_path / "tasmobackup.db").write_bytes(b"")
    assert _resolve_secret_key(_cfg(tmp_path)) == LEGACY_SECRET_KEY
    assert not (tmp_path / SECRET_KEY_FILE).exists()
