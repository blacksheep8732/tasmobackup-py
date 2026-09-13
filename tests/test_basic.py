"""Smoke tests that don't need a real device."""
import os
import tempfile

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TB_SECRET_KEY", "test-secret-key-deterministic-please")

from app import github  # noqa: E402
from app.security import decrypt, encrypt, hash_password, verify_password  # noqa: E402


def test_password_roundtrip():
    token = encrypt("hunter2")
    assert token != "hunter2"
    assert decrypt(token) == "hunter2"
    assert decrypt("") == ""


def test_auth_hash():
    h = hash_password("secret")
    assert verify_password("secret", h)
    assert not verify_password("wrong", h)


def test_parse_version():
    assert github.parse_version("14.2.0(tasmota)") == ("14.2.0", "(tasmota)")
    assert github.parse_version("14.2.0") == ("14.2.0", "")


def test_semver_ordering():
    assert github._semver("14.2.0") < github._semver("14.3.0")
    assert github._semver("9.5.0") < github._semver("14.0.0")
