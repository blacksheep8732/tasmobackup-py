"""Regression tests for the issues found in the 2026-09 code review."""
import html
import re

from app.db import session_scope
from app.models import Backup, Device

HOSTILE_NAME = "Oma's \"Lampe\" </script><b>x"


def _onsubmit_handlers(page: str) -> list[str]:
    """The JavaScript a browser would run, i.e. the attribute after entity decoding."""
    return [html.unescape(m) for m in re.findall(r"onsubmit='([^']*)'", page)]


def test_confirm_dialogs_survive_quotes_in_device_name(client, device_id):
    with session_scope() as s:
        s.get(Device, device_id).name = HOSTILE_NAME
        s.add(Backup(device_id=device_id, name=HOSTILE_NAME, filename="/nonexistent.dmp"))

    for url in ("/", f"/devices/{device_id}/backups"):
        page = client.get(url).text
        handlers = [h for h in _onsubmit_handlers(page) if "Lampe" in h]
        assert handlers, f"no confirm dialog with the device name on {url}"
        for js in handlers:
            # The name must end up inside ONE JSON string literal: no raw quote that
            # could close it, no tag that could end a script block.
            assert re.fullmatch(r'return confirm\("(?:[^"\\]|\\.)*"\);', js), js
            assert "'" not in js and "<" not in js


# --- Point 2: numeric settings are validated and read defensively --------------- #

def test_invalid_number_is_refused_but_valid_fields_are_saved(client):
    from app.db import get_setting

    with session_scope() as s:
        before = get_setting(s, "backup_interval_hours")
    r = client.post("/settings", data={"backup_interval_hours": "1,5", "backup_max_count": "7"},
                    follow_redirects=True)
    assert "nicht gespeichert" in r.text or "not saved" in r.text
    with session_scope() as s:
        assert get_setting(s, "backup_interval_hours") == before  # refused
        assert get_setting(s, "backup_max_count") == "7"          # accepted
    client.post("/settings", data={"backup_max_count": "0"})


def test_garbage_in_the_database_falls_back_to_the_default():
    from app.db import get_int, get_setting, set_setting

    with session_scope() as s:
        old = get_setting(s, "backup_interval_hours")
        set_setting(s, "backup_interval_hours", "abc")
    try:
        with session_scope() as s:
            assert get_int(s, "backup_interval_hours") == 24
    finally:
        with session_scope() as s:
            set_setting(s, "backup_interval_hours", old)


async def test_scheduler_keeps_going_after_one_device_fails(monkeypatch):
    from app import scheduler, service

    with session_scope() as s:
        a, b = Device(name="A", ip="10.9.0.1", mac="AA0000000001"), Device(name="B", ip="10.9.0.2", mac="AA0000000002")
        s.add_all([a, b])
        s.flush()
        ids = [a.id, b.id]
    called: list[int] = []

    async def fake_backup(device_id):
        called.append(device_id)
        if device_id == ids[0]:
            raise RuntimeError("boom")
        return True, "ok"

    monkeypatch.setattr(service, "backup_device", fake_backup)
    try:
        await scheduler.run_scheduled_backups()
        assert ids[1] in called, "the device after the failing one was skipped"
    finally:
        with session_scope() as s:
            for i in ids:
                s.delete(s.get(Device, i))
