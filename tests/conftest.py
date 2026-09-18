"""Shared test setup: point the app at a throwaway data dir before it is imported."""
import os
import tempfile

# Must happen before any `app.*` import — config is read (and cached) at import time.
os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TB_SECRET_KEY", "test-secret-key-deterministic-please")
os.environ.setdefault("TB_ADMIN_USER", "admin")
os.environ.setdefault("TB_ADMIN_PASSWORD", "testpw")
# Tests point at unroutable IPs on purpose; don't wait 15 s for each dead request.
os.environ.setdefault("TB_HTTP_TIMEOUT", "1")

import uuid  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create tables + default settings once for the whole test session."""
    from app.db import init_db

    init_db()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import _ensure_admin, app

    _ensure_admin()
    # No `with` block: we don't want the lifespan to start the real scheduler.
    c = TestClient(app)
    r = c.post("/login", data={"username": "admin", "password": "testpw"}, follow_redirects=False)
    assert r.status_code == 303, "login failed"
    return c


@pytest.fixture
def device_id():
    """A fresh device per test — unique MAC, since the column is unique."""
    from app.db import session_scope
    from app.models import Device

    with session_scope() as s:
        d = Device(name="Testgerät", ip="10.0.0.1", mac=uuid.uuid4().hex[:12].upper())
        s.add(d)
        s.flush()
        new_id = d.id
    yield new_id
    with session_scope() as s:
        d = s.get(Device, new_id)
        if d:
            s.delete(d)
