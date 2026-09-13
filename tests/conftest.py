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

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create tables + default settings once for the whole test session."""
    from app.db import init_db

    init_db()
