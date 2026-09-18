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


# --- Point 3: custom firmware is never auto-updated ------------------------------ #

def test_custom_build_detection():
    from app.github import is_custom_build

    assert not is_custom_build("15.6.0(release-tasmota)")
    assert not is_custom_build("15.6.0(release-tasmota32)")
    assert not is_custom_build("15.6.0")              # no tag: nothing to go on
    assert is_custom_build("15.6.0(gas)")
    assert is_custom_build("15.1.0.1(ee1d867-scripting)")
    assert is_custom_build("15.6.0(tasmota)")         # self-compiled default name


async def test_scheduled_update_never_touches_a_custom_build(monkeypatch, device_id):
    from app import github, service, tasmota

    with session_scope() as s:
        s.get(Device, device_id).version = "15.6.0(gas)"

    async def must_not_run(*a, **k):
        raise AssertionError("device was contacted for a custom build")

    async def newer(*a, **k):
        return "99.0.0"

    monkeypatch.setattr(github, "latest_version", newer)
    monkeypatch.setattr(tasmota, "upgrade_firmware", must_not_run)
    monkeypatch.setattr(tasmota, "get_ota_url", must_not_run)
    ok, msg = await service.update_device(device_id, force=False)
    assert not ok and "custom" in msg


def test_dashboard_marks_custom_build_instead_of_outdated(client, device_id, monkeypatch):
    from app import github

    async def newer():
        return "99.0.0"

    monkeypatch.setattr(github, "latest_version", newer)
    with session_scope() as s:
        d = s.get(Device, device_id)
        d.version, d.name = "15.6.0(gas)", "Gaszähler-Test"
    page = client.get("/").text
    row = page[page.index("Gaszähler-Test"):].split("</tr>")[0]
    assert "eigene Firmware" in row or "custom build" in row
    assert "veraltet" not in row and "outdated" not in row
    assert "ACHTUNG" in html.unescape(row) or "WARNING" in html.unescape(row)


# --- Point 4: deleting a device removes its backups ----------------------------- #

def test_delete_device_removes_its_backups_but_nothing_outside(client, tmp_path):
    from app.config import get_config

    folder = get_config().backup_dir / "Loeschtest"
    folder.mkdir(parents=True, exist_ok=True)
    inside = [folder / "a.dmp", folder / "b.dmp"]
    outside = tmp_path / "keep-me.dmp"
    for f in inside + [outside]:
        f.write_bytes(b"x")
    with session_scope() as s:
        d = Device(name="Loeschtest", ip="10.8.0.1", mac="DD0000000001")
        s.add(d)
        s.flush()
        did = d.id
        for f in inside + [outside]:
            s.add(Backup(device_id=did, name="Loeschtest", filename=str(f)))

    client.post(f"/devices/{did}/delete")

    with session_scope() as s:
        assert s.get(Device, did) is None
        assert s.query(Backup).filter(Backup.device_id == did).count() == 0
    assert not any(f.exists() for f in inside)
    assert not folder.exists(), "empty device folder should be removed"
    assert outside.exists(), "a file outside the backup folder must never be deleted"


# --- Point 5: an unreachable GitHub is not asked on every dashboard poll --------- #

async def test_github_failure_pauses_retries(monkeypatch, tmp_path):
    import httpx

    from app import github

    attempts = 0

    async def offline(self, *a, **k):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(github, "_CACHE_FILE", tmp_path / "none.json")
    monkeypatch.setattr(github, "_last_failure", 0.0)
    monkeypatch.setattr(httpx.AsyncClient, "get", offline)

    assert await github.latest_release() is None
    assert await github.latest_release() is None
    assert attempts == 1, "the second call must not hit the network again"
