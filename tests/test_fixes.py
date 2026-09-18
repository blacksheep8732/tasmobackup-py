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
    assert not ok and "custom" in str(msg)


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


# --- Point 6: "back up all" runs in parallel, same-named devices don't collide --- #

async def test_backup_all_is_parallel_and_same_names_do_not_collide(monkeypatch):
    import asyncio
    import time

    from app import service, tasmota
    from app.tasmota import DeviceInfo

    async def slow_info(ip, *a, **k):
        await asyncio.sleep(0.3)
        return DeviceInfo(name="Zwilling", version="15.6.0(release-tasmota)", mac="")

    async def slow_dump(ip, *a, **k):
        return f"config of {ip}".encode()

    monkeypatch.setattr(tasmota, "get_info", slow_info)
    monkeypatch.setattr(tasmota, "download_backup", slow_dump)

    with session_scope() as s:
        made = [Device(name="Zwilling", ip=f"10.7.0.{i}", mac=f"EE00000000{i:02d}") for i in range(5)]
        s.add_all(made)
        s.flush()
        ids = [d.id for d in made]
    try:
        start = time.monotonic()
        total, failed = await service.backup_all()
        elapsed = time.monotonic() - start
        assert not failed and total >= 5
        assert elapsed < 1.0, f"took {elapsed:.2f}s — looks sequential"
        with session_scope() as s:
            files = [b.filename for b in s.query(Backup).filter(Backup.device_id.in_(ids))]
        assert len(files) == 5 and len(set(files)) == 5, "a same-named backup was overwritten"
        from pathlib import Path
        contents = {Path(f).read_bytes() for f in files}
        assert len(contents) == 5
    finally:
        for i in ids:
            service.delete_device(i)


# --- Point 7: the version string cannot break the backup file name --------------- #

def test_version_is_made_safe_for_file_names():
    from app.service import _safe_version

    assert _safe_version("15.6.0(release-tasmota)") == "15.6.0(release-tasmota)"
    assert _safe_version("15.1.0.1(ee1d867-scripting)") == "15.1.0.1(ee1d867-scripting)"
    assert "/" not in _safe_version("1.0/../../etc") and "\\" not in _safe_version("a\\b")
    assert len(_safe_version("9" * 500)) == 64


async def test_backup_with_slash_in_version_succeeds(monkeypatch, device_id):
    from app import service, tasmota
    from app.tasmota import DeviceInfo

    async def info(*a, **k):
        return DeviceInfo(name="Slash", version="1.0/evil", mac="")

    async def dump(*a, **k):
        return b"cfg"

    monkeypatch.setattr(tasmota, "get_info", info)
    monkeypatch.setattr(tasmota, "download_backup", dump)
    ok, msg = await service.backup_device(device_id)
    assert ok, msg
    service.delete_device(device_id)


# --- Point 8: a rejected restore is reported as failed --------------------------- #
# Response bodies follow HandleUploadDone() in Tasmota 15.6.0 (xdrv_01_9_webserver.ino).
_U2_OK = ("<script>setTimeout(function(){location.href='.';},15000);</script>"
          "<div style='text-align:center;'><b>Upload <font color='#008000'>Erfolgreich</font></b>")
_U2_FAIL = ("<div style='text-align:center;'><b>Upload <font color='#ff5661'>Fehlgeschlagen</font>"
            "</b><br><br>Ungültige Datei-Signatur")


def _fake_device(monkeypatch, rs_status: int, u2_body: str):
    import httpx

    from app import tasmota

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rs":
            return httpx.Response(rs_status, text="restore page")
        return httpx.Response(200, text=u2_body)

    monkeypatch.setattr(tasmota, "_client",
                        lambda ip: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_restore_success_is_recognised(monkeypatch):
    from app import tasmota

    _fake_device(monkeypatch, 200, _U2_OK)
    assert await tasmota.restore_backup("10.0.0.9", "admin", "", b"cfg") is True


async def test_restore_failure_page_is_not_success(monkeypatch):
    from app import tasmota

    _fake_device(monkeypatch, 200, _U2_FAIL)  # HTTP 200, but the upload failed
    assert await tasmota.restore_backup("10.0.0.9", "admin", "", b"cfg") is False


async def test_restore_not_armed_is_not_success(monkeypatch):
    from app import tasmota

    _fake_device(monkeypatch, 401, _U2_OK)  # /u2 would claim success for nothing
    assert await tasmota.restore_backup("10.0.0.9", "admin", "", b"cfg") is False


# --- Point 9: the MQTT password is stored encrypted and never sent to the browser - #

def _mqtt_pw_raw() -> str:
    from app.db import get_setting

    with session_scope() as s:
        return get_setting(s, "mqtt_password", "")


def test_mqtt_password_encrypted_hidden_kept_and_clearable(client):
    from app.security import decrypt

    client.post("/settings", data={"mqtt_password": "geheim123"})
    raw = _mqtt_pw_raw()
    assert raw != "geheim123" and decrypt(raw) == "geheim123"
    assert "geheim123" not in client.get("/settings").text

    client.post("/settings", data={"mqtt_password": ""})          # blank keeps it
    assert decrypt(_mqtt_pw_raw()) == "geheim123"

    client.post("/settings", data={"mqtt_password_clear": "1"})   # checkbox clears it
    assert _mqtt_pw_raw() == ""


def test_legacy_plaintext_mqtt_password_is_migrated_once():
    from app.db import set_setting
    from app.main import _encrypt_legacy_mqtt_password
    from app.security import decrypt

    with session_scope() as s:
        set_setting(s, "mqtt_password", "altes-pw")
    _encrypt_legacy_mqtt_password()
    first = _mqtt_pw_raw()
    _encrypt_legacy_mqtt_password()                 # a second start changes nothing
    assert _mqtt_pw_raw() == first and decrypt(first) == "altes-pw"
    with session_scope() as s:
        set_setting(s, "mqtt_password", "")


async def test_mqtt_discovery_gets_the_decrypted_password(monkeypatch):
    from app import mqtt, service
    from app.db import set_setting
    from app.security import encrypt

    seen = {}

    async def fake_discover(host, port, user, password, group):
        seen["password"] = password
        return []

    monkeypatch.setattr(mqtt, "discover", fake_discover)
    with session_scope() as s:
        set_setting(s, "mqtt_host", "10.0.0.2")
        set_setting(s, "mqtt_password", encrypt("broker-pw"))
    try:
        await service.mqtt_discover()
        assert seen["password"] == "broker-pw"
    finally:
        with session_scope() as s:
            set_setting(s, "mqtt_host", "")
            set_setting(s, "mqtt_password", "")


# --- Point 10: user-facing messages follow the UI language ----------------------- #

def test_msg_translates_and_stays_english_in_logs():
    from app.i18n import msg

    m = msg("msg.update_backup_failed", name="Dach", reason=msg("msg.backup_failed", name="Dach"))
    assert m.text("de") == "Dach: Update abgebrochen — Backup vorher fehlgeschlagen (Dach: Backup fehlgeschlagen (offline?))"
    assert str(m) == "Dach: aborted — pre-update backup failed (Dach: backup failed (offline?))"


def test_every_message_key_exists_in_all_languages():
    import json
    import re
    from pathlib import Path

    app_dir = Path(__file__).parent.parent / "app"
    used = set()
    for f in app_dir.glob("*.py"):
        used |= set(re.findall(r'"(msg\.[a-z_]+)"', f.read_text()))
    assert len(used) > 30
    for pack in (app_dir / "locales").glob("*.json"):
        missing = used - set(json.loads(pack.read_text(encoding="utf-8")))
        assert not missing, f"{pack.name} lacks {sorted(missing)}"


def test_flash_follows_the_ui_language(client, device_id):
    from app.db import set_setting

    # unroutable IP -> fails; the redirect target shows (and consumes) the message
    r = client.post(f"/devices/{device_id}/backup", follow_redirects=True)
    assert "Backup fehlgeschlagen" in r.text
    with session_scope() as s:
        set_setting(s, "language", "en")
    try:
        r = client.post(f"/devices/{device_id}/backup", follow_redirects=True)
        assert "backup failed (offline?)" in r.text
    finally:
        with session_scope() as s:
            set_setting(s, "language", "de")


def test_scan_reports_bad_or_huge_subnets_as_errors(client):
    page = client.post("/scan", data={"subnet_base": "999.1.1.1", "subnet_cidr": "24"},
                       follow_redirects=True).text
    assert "Ungültiges Netz" in page and "gefunden:" not in page
    page = client.post("/scan", data={"subnet_base": "10.0.0.0", "subnet_cidr": "16"},
                       follow_redirects=True).text
    assert "zu groß" in page
