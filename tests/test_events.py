"""Tests for the online-status tracking, event log and notification plumbing."""
from app import events, service, tasmota
from app.db import session_scope, set_setting
from app.models import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, Device


def test_migration_added_columns():
    """The additive migration must give existing installs the new columns."""
    from sqlalchemy import inspect
    from app.db import engine

    cols = {c["name"] for c in inspect(engine).get_columns("devices")}
    assert {"online", "last_seen", "fail_count"} <= cols


def test_first_contact_does_not_announce_recovery(device_id):
    """A brand-new device coming online must not produce a 'back online' event."""
    before = len(events.recent(500))
    service._mark_reachability(device_id, True)
    with session_scope() as s:
        d = s.get(Device, device_id)
        assert d.online is True
        assert d.last_seen is not None
        assert d.fail_count == 0
    assert len(events.recent(500)) == before, "unexpected event on first contact"


def test_offline_is_debounced(device_id):
    """One dropped request must not raise an alert; the second one must."""
    service._mark_reachability(device_id, True)   # establish "online" + last_seen

    service._mark_reachability(device_id, False)  # first miss
    with session_scope() as s:
        d = s.get(Device, device_id)
        assert d.online is True, "a single miss must not flip the device offline"
        assert d.fail_count == 1

    service._mark_reachability(device_id, False)  # second miss -> offline
    with session_scope() as s:
        d = s.get(Device, device_id)
        assert d.online is False
    latest = events.recent(1)[0]
    assert latest.level == LEVEL_WARN
    assert "no longer reachable" in latest.message
    assert latest.device_id == device_id


def test_recovery_is_announced(device_id):
    service._mark_reachability(device_id, True)
    service._mark_reachability(device_id, False)
    service._mark_reachability(device_id, False)
    service._mark_reachability(device_id, True)
    latest = events.recent(1)[0]
    assert latest.level == LEVEL_INFO
    assert "reachable again" in latest.message


def test_unseen_count_ignores_info(device_id):
    events.clear_all()
    events.record(LEVEL_INFO, "just so you know", device_id, "Testgerät")
    assert events.unseen_count() == 0, "info events must not raise the warning badge"
    events.record(LEVEL_ERROR, "something broke", device_id, "Testgerät")
    assert events.unseen_count() == 1
    events.mark_all_seen()
    assert events.unseen_count() == 0


def test_notification_threshold_skips_low_levels(device_id, monkeypatch):
    """With min level 'warn', an info event must not trigger a delivery attempt."""
    sent = []

    def fake_spawn(coro):
        sent.append(coro)
        coro.close()

    monkeypatch.setattr(events, "_spawn", fake_spawn)
    with session_scope() as s:
        set_setting(s, "notify_url", "https://example.invalid/topic")
        set_setting(s, "notify_min_level", LEVEL_WARN)

    events.record(LEVEL_INFO, "quiet", device_id, "Testgerät")
    assert sent == [], "info must be filtered out by the warn threshold"

    events.record(LEVEL_ERROR, "loud", device_id, "Testgerät")
    assert len(sent) == 1, "error must be delivered"

    with session_scope() as s:
        set_setting(s, "notify_url", "")


def test_pages_render(client, device_id):
    """The dashboard, event log and settings pages must render after the changes."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Testgerät" in r.text
    # Online column present in one state or the other.
    assert ("Online" in r.text) or ("Offline" in r.text)

    r = client.get("/events")
    assert r.status_code == 200

    r = client.get("/settings")
    assert r.status_code == 200
    assert "notify_url" in r.text
    assert "online_check_minutes" in r.text
    assert "admin_password_hash" not in r.text


def test_flash_message_survives_redirect(client, device_id):
    """A failing action must leave a visible message on the next page render."""
    events.clear_all()
    # The device IP is unroutable, so the backup fails -> flash + error event.
    r = client.post(f"/devices/{device_id}/backup", follow_redirects=True)
    assert r.status_code == 200
    assert "Backup fehlgeschlagen" in r.text or "backup failed" in r.text
    assert any(e.level == LEVEL_ERROR for e in events.recent(5))


def test_htmx_poll_does_not_swallow_flash(client, device_id):
    """The 30 s htmx refresh must not consume a pending flash message."""
    events.clear_all()
    client.post(f"/devices/{device_id}/backup", follow_redirects=False)
    # Simulate the dashboard's background poll first...
    poll = client.get("/", headers={"HX-Request": "true"})
    assert poll.status_code == 200
    assert "Backup fehlgeschlagen" not in poll.text
    # ...the message must still be waiting for the real page load.
    full = client.get("/")
    assert "Backup fehlgeschlagen" in full.text


def test_header_safe_survives_umlauts():
    """Regression: an umlaut in a device name used to raise UnicodeEncodeError in
    httpx and silently kill the whole notification."""
    import base64

    assert events._header_safe("TasmoBackup") == "TasmoBackup"
    encoded = events._header_safe("TasmoBackup: Küchenlampe")
    assert encoded.startswith("=?UTF-8?B?") and encoded.endswith("?=")
    encoded.encode("ascii")  # must not raise — this is the actual bug
    payload = encoded[len("=?UTF-8?B?"):-2]
    assert base64.b64decode(payload).decode("utf-8") == "TasmoBackup: Küchenlampe"


async def test_send_test_reports_failure_instead_of_raising():
    """A dead endpoint must come back as a readable message, not an exception."""
    ok, msg = await events.send_test("http://127.0.0.1:9/nope", "ntfy")
    assert ok is False
    assert "failed" in str(msg).lower()

    ok, msg = await events.send_test("", "ntfy")
    assert ok is False


async def test_upgrade_sends_only_the_trigger(monkeypatch):
    """The whole point: we trigger, Tasmota does the rest.

    Setting OtaUrl would overwrite the device's own image choice — including the
    language variant (tasmota-DE.bin.gz), which the version string does not expose —
    and would interfere with the two-stage upgrade Tasmota drives on its own.
    """
    sent = []

    async def fake_cmnd(client, ip, user, password, cmnd):
        sent.append(cmnd)
        return {"Upgrade": "Version 15.6.0 from http://ota.tasmota.com/tasmota/release/tasmota-DE.bin.gz"}

    monkeypatch.setattr(tasmota, "_cmnd", fake_cmnd)
    assert await tasmota.upgrade_firmware("10.0.0.1", "admin", "") is True
    assert sent == ["Upgrade 1"], f"exactly one command expected, got {sent}"


async def test_upgrade_detects_a_refusal(monkeypatch):
    """A device that declines must not be reported as 'update started'."""
    async def refuse(client, ip, user, password, cmnd):
        return {"Upgrade": "Option 1 unknown"}

    monkeypatch.setattr(tasmota, "_cmnd", refuse)
    assert await tasmota.upgrade_firmware("10.0.0.1", "admin", "") is False

    async def dead(client, ip, user, password, cmnd):
        return None

    monkeypatch.setattr(tasmota, "_cmnd", dead)
    assert await tasmota.upgrade_firmware("10.0.0.1", "admin", "") is False


async def test_ota_image_is_only_read(monkeypatch):
    """Reading OtaUrl for the log must not write anything."""
    sent = []

    async def fake_cmnd(client, ip, user, password, cmnd):
        sent.append(cmnd)
        return {"OtaUrl": "http://ota.tasmota.com/tasmota/release/tasmota-DE.bin.gz"}

    monkeypatch.setattr(tasmota, "_cmnd", fake_cmnd)
    image = await service._current_ota_image("10.0.0.1", "admin", "")
    assert image.endswith("tasmota-DE.bin.gz")
    assert sent == ["OtaUrl"], "reading must not pass a value"


async def test_watch_update_sits_through_the_minimal_stage(monkeypatch, device_id):
    """Tasmota flashes tasmota-minimal first and reboots, then fetches the real
    image. The intermediate '(minimal)' build must not be reported as success."""
    versions = ["15.5.0(release-tasmota)",   # not rebooted yet
                "15.6.0(minimal)",           # stage one
                "15.6.0(minimal)",
                "15.6.0(release-tasmota)"]   # stage two done
    seen = []

    async def fake_refresh(did):
        v = versions[min(len(seen), len(versions) - 1)]
        seen.append(v)
        return True, v

    recorded = []
    monkeypatch.setattr(service, "refresh_device", fake_refresh)
    monkeypatch.setattr(service.events, "record",
                        lambda level, msg, *a, **k: recorded.append((level, msg)))

    await service._watch_update(device_id, "15.5.0(release-tasmota)",
                                delay=0, interval=0, attempts=10)

    assert len(recorded) == 1, f"expected exactly one final event, got {recorded}"
    level, msg = recorded[0]
    assert level == LEVEL_INFO
    assert "update finished" in msg
    assert "15.6.0(release-tasmota)" in msg
    assert "minimal" not in msg


async def test_no_offline_alert_while_flashing(device_id):
    """A device reboots (twice) during an update — that must not raise an alert."""
    service._mark_reachability(device_id, True)   # establish online + last_seen
    events.clear_all()

    service._updating.add(device_id)
    try:
        service._mark_reachability(device_id, False)
        service._mark_reachability(device_id, False)
    finally:
        service._updating.discard(device_id)

    assert events.recent(5) == [], "reboot during update must not alert"

    # Outside an update the same sequence does alert.
    service._mark_reachability(device_id, True)
    service._mark_reachability(device_id, False)
    service._mark_reachability(device_id, False)
    assert any("no longer reachable" in e.message for e in events.recent(5))


def test_timezone_rules_for_europe():
    """The Berlin rules must match Tasmota's documented EU values."""
    from app import tz as tzmod

    assert tzmod.tasmota_commands("Europe/Berlin", 2026) == [
        "TimeDst 0,0,3,1,2,120",   # last Sunday in March, 02:00 -> UTC+2
        "TimeStd 0,0,10,1,3,60",   # last Sunday in October, 03:00 -> UTC+1
        "Timezone 99",
    ]


def test_timezone_rules_other_zones():
    from app import tz as tzmod

    # US rules differ (2nd Sunday March / 1st Sunday November).
    assert tzmod.tasmota_commands("America/New_York", 2026) == [
        "TimeDst 0,2,3,1,2,-240", "TimeStd 0,1,11,1,2,-300", "Timezone 99"]
    # Southern hemisphere flips the leading flag.
    assert tzmod.tasmota_commands("Australia/Sydney", 2026)[0].startswith("TimeDst 1,")
    # A zone without DST needs no rules at all.
    assert tzmod.tasmota_commands("Asia/Tokyo", 2026) == ["Timezone 9"]
    assert tzmod.tasmota_commands("Not/AZone", 2026) is None


def test_tz_offset_is_read_from_status():
    """Local minus UTC, because the Timezone field reads '99' once DST is active."""
    from app.tasmota import _tz_offset_from_status

    # Device stuck on a fixed +1 while Berlin is on +2.
    assert _tz_offset_from_status({
        "Local": "2026-09-02T22:31:02", "UTC": "2026-09-02T21:31:02Z"}) == 60
    # Correctly configured device.
    assert _tz_offset_from_status({
        "Local": "2026-09-02T23:31:03", "UTC": "2026-09-02T21:31:03Z"}) == 120
    # One second apart must still round to a clean offset.
    assert _tz_offset_from_status({
        "Local": "2026-09-02T23:31:04", "UTC": "2026-09-02T21:31:03Z"}) == 120
    assert _tz_offset_from_status({}) is None


async def test_sync_timezone_sends_backlog(monkeypatch, device_id):
    """Syncing must send exactly the derived commands, in one Backlog call."""
    sent = []

    async def fake_cmnd(client, ip, user, password, cmnd):
        sent.append(cmnd)
        return {"Backlog": "Done"}

    async def fake_get_info(ip, user, pw, dtype=0):
        from app.tasmota import DeviceInfo
        return DeviceInfo(name="Testgerät", version="15.6.0(release-tasmota)", tz_offset=120)

    monkeypatch.setattr(tasmota, "_cmnd", fake_cmnd)
    monkeypatch.setattr(tasmota, "get_info", fake_get_info)

    ok, msg = await service.sync_timezone(device_id)
    assert ok, msg
    assert len(sent) == 1, f"expected a single Backlog call, got {sent}"
    assert sent[0] == ("Backlog TimeDst 0,0,3,1,2,120; TimeStd 0,0,10,1,3,60; Timezone 99")


def test_fixed_offset_is_detected_even_when_it_matches_today():
    """A device on a fixed +02:00 looks fine in summer and breaks in October.

    Comparing offsets alone misses it, so we also check whether the device runs
    Timezone 99 at all.
    """
    from app import tz as tzmod
    from app.tasmota import _tz_dst_from_status

    assert tzmod.has_dst("Europe/Berlin", 2026) is True
    assert tzmod.has_dst("Asia/Tokyo", 2026) is False

    # Tasmota reports "99" only when the rules are active.
    assert _tz_dst_from_status({"Timezone": "99"}) is True
    assert _tz_dst_from_status({"Timezone": "+02:00"}) is False
    assert _tz_dst_from_status({"Timezone": "+01:00"}) is False
    assert _tz_dst_from_status({}) is None


async def test_dashboard_flags_both_kinds_of_mismatch(client):
    """Wrong-now and correct-by-accident must both show up."""
    import uuid
    from datetime import datetime as dt

    with session_scope() as s:
        made = []
        for name, off, dst in [("Falsch", 60, False),    # an hour behind
                               ("Fest", 120, False),      # right today, fixed offset
                               ("Korrekt", 120, True)]:   # rule-based, fine
            d = Device(name=name, ip="10.0.0.9", mac=uuid.uuid4().hex[:12].upper(),
                       version="15.6.0(release-tasmota)", online=True,
                       last_seen=dt.utcnow(), tz_offset=off, tz_dst=dst)
            s.add(d)
            s.flush()
            made.append(d.id)

    html = client.get("/").text
    try:
        assert "Uhrzeit -1 h" in html, "device that is an hour behind must be flagged"
        assert "🕒 feste Zeitzone" in html, "fixed-offset device must be flagged too"
        # Exactly two warnings — the rule-based device must not raise one.
        assert html.count("🕒") == 2, "one warning per affected device, no global banner"
        # The overview only informs; changing happens on the device's edit page.
        assert 'action="/sync-time"' not in html
        for did in made[:2]:
            assert f'href="/devices/{did}/edit"' in html

        # The edit page carries the controls and the exact commands.
        page = client.get(f"/devices/{made[0]}/edit").text
        assert "Zeit &amp; Zeitzone" in page or "Zeit & Zeitzone" in page
        assert f'action="/devices/{made[0]}/sync-time"' in page
        assert "Timezone 99" in page, "the page should show what will be sent"
        assert "UTC+1" in page, "device's own offset must be shown"

        # A correctly configured device shows the all-clear instead of a warning.
        ok_page = client.get(f"/devices/{made[2]}/edit").text
        assert "stimmt mit dem Server" in ok_page
    finally:
        with session_scope() as s:
            for did in made:
                d = s.get(Device, did)
                if d:
                    s.delete(d)


async def test_sync_waits_before_reading_back(monkeypatch, device_id):
    """Regression: Tasmota applies Backlog commands with a delay.

    Reading the zone back immediately returned the OLD offset, so a successful sync
    was reported as 'still reports UTC+1' for every device.
    """
    reads = {"n": 0}

    async def fake_cmnd(client, ip, user, password, cmnd):
        return {"Backlog": "Done"}

    async def fake_get_info(ip, user, pw, dtype=0):
        from app.tasmota import DeviceInfo
        # First read still shows the old fixed offset, then the new one appears.
        reads["n"] += 1
        offset = 60 if reads["n"] == 1 else 120
        return DeviceInfo(name="Testgerät", version="15.6.0(release-tasmota)",
                          tz_offset=offset, tz_dst=offset == 120)

    recorded = []
    monkeypatch.setattr(tasmota, "_cmnd", fake_cmnd)
    monkeypatch.setattr(tasmota, "get_info", fake_get_info)
    monkeypatch.setattr(service.asyncio, "sleep", lambda *_a, **_k: asyncio_noop())
    monkeypatch.setattr(service.events, "record",
                        lambda level, msg, *a, **k: recorded.append((level, msg)))

    ok, msg = await service.sync_timezone(device_id)
    assert ok, f"a delayed but successful sync must not be reported as failure: {msg}"
    assert reads["n"] >= 2, "must read back more than once"
    assert all("still reports" not in m for _, m in recorded), recorded


async def asyncio_noop():
    return None


def test_lasting_fault_is_reported_once(device_id):
    """Regression: a device that stayed offline produced an error every 15 minutes.

    The scheduler retries a device whose last backup is old, so a permanently
    unreachable device generated one alert per run — 52 over a single night.
    """
    events.clear_all()
    with session_scope() as s:
        s.get(Device, device_id).alert_state = ""

    for _ in range(10):   # ten scheduler runs
        events.record(LEVEL_ERROR, "backup failed — device unreachable",
                      device_id, "Testgerät", key="unreachable")

    assert len(events.recent(50)) == 1, "a lasting fault must be reported exactly once"


def test_alert_repeats_after_recovery(device_id):
    """Once the device was healthy again, the next fault must be reported."""
    events.clear_all()
    with session_scope() as s:
        s.get(Device, device_id).alert_state = ""

    events.record(LEVEL_ERROR, "backup failed — device unreachable",
                  device_id, "Testgerät", key="unreachable")
    events.record(LEVEL_ERROR, "backup failed — device unreachable",
                  device_id, "Testgerät", key="unreachable")
    assert len(events.recent(50)) == 1

    events.clear_alert(device_id)          # device came back
    events.record(LEVEL_ERROR, "backup failed — device unreachable",
                  device_id, "Testgerät", key="unreachable")
    assert len(events.recent(50)) == 2, "after recovery the next fault must be reported"


def test_different_faults_are_not_swallowed(device_id):
    """A new kind of problem must be reported even while another one is active."""
    events.clear_all()
    with session_scope() as s:
        s.get(Device, device_id).alert_state = ""

    events.record(LEVEL_ERROR, "backup failed — device unreachable",
                  device_id, "Testgerät", key="unreachable")
    events.record(LEVEL_ERROR, "update failed — device rejected the OTA command",
                  device_id, "Testgerät", key="update_failed")
    assert len(events.recent(50)) == 2

    # ...but that new one is itself deduplicated from now on.
    events.record(LEVEL_ERROR, "update failed — device rejected the OTA command",
                  device_id, "Testgerät", key="update_failed")
    assert len(events.recent(50)) == 2


def test_events_without_a_key_still_repeat(device_id):
    """User-triggered messages must not be swallowed by the dedup."""
    events.clear_all()
    for _ in range(3):
        events.record(LEVEL_INFO, "time zone set to Europe/Berlin", device_id, "Testgerät")
    assert len(events.recent(50)) == 3


async def test_successful_backup_clears_the_alert(device_id, monkeypatch):
    """A working backup must reset the state so later faults are reported again."""
    with session_scope() as s:
        d = s.get(Device, device_id)
        d.alert_state = "unreachable"
        d.online = True

    async def fake_get_info(ip, user, pw, dtype=0):
        from app.tasmota import DeviceInfo
        return DeviceInfo(name="Testgerät", version="15.6.0(release-tasmota)")

    async def fake_download(ip, user, pw, dtype=0):
        return b"dummy-config"

    monkeypatch.setattr(tasmota, "get_info", fake_get_info)
    monkeypatch.setattr(tasmota, "download_backup", fake_download)

    ok, _ = await service.backup_device(device_id)
    assert ok
    with session_scope() as s:
        assert s.get(Device, device_id).alert_state == "", "success must clear the alert"


async def test_stalled_minimal_is_re_triggered(monkeypatch, device_id):
    """The device sat on the minimal image and had to be pushed manually.

    After a few checks without progress the app must send Upgrade 1 itself — once —
    and then recognise the finished update.
    """
    versions = ["15.6.0(minimal)"] * 5 + ["15.6.0(release-tasmota)"]
    seen, upgrades, recorded = [], [], []

    async def fake_refresh(did):
        v = versions[min(len(seen), len(versions) - 1)]
        seen.append(v)
        return True, v

    async def fake_upgrade(ip, user, pw):
        upgrades.append(ip)
        return True

    monkeypatch.setattr(service, "refresh_device", fake_refresh)
    monkeypatch.setattr(service.tasmota, "upgrade_firmware", fake_upgrade)
    monkeypatch.setattr(service.events, "record",
                        lambda level, msg, *a, **k: recorded.append((level, msg)))

    await service._watch_update(device_id, "15.5.0(release-tasmota)",
                                delay=0, interval=0, attempts=20, nudge_after=3)

    assert len(upgrades) == 1, f"exactly one retry expected, got {len(upgrades)}"
    assert any("re-triggered" in m for _, m in recorded), recorded
    assert any("update finished" in m for _, m in recorded), recorded
    assert not any("stuck" in m for _, m in recorded), "must not report failure"


async def test_stuck_on_minimal_is_reported_distinctly(monkeypatch, device_id):
    """If it never leaves the minimal image, say so — not 'still on the old version'."""
    recorded = []

    async def always_minimal(did):
        return True, "15.6.0(minimal)"

    async def fake_upgrade(ip, user, pw):
        return True

    monkeypatch.setattr(service, "refresh_device", always_minimal)
    monkeypatch.setattr(service.tasmota, "upgrade_firmware", fake_upgrade)
    monkeypatch.setattr(service.events, "record",
                        lambda level, msg, *a, **k: recorded.append((level, msg)))

    await service._watch_update(device_id, "15.5.0(release-tasmota)",
                                delay=0, interval=0, attempts=6, nudge_after=3)

    final = recorded[-1][1]
    assert "minimal image" in final and "despite a retry" in final, final


async def test_timeout_names_the_version_the_device_reports(monkeypatch, device_id):
    """Regression: the timeout message quoted the old version even when the device
    reported something else, which made a stalled two-stage update unreadable."""
    recorded = []

    async def stuck(did):
        return True, "15.5.0(release-tasmota)"

    monkeypatch.setattr(service, "refresh_device", stuck)
    monkeypatch.setattr(service.events, "record",
                        lambda level, msg, *a, **k: recorded.append((level, msg)))

    await service._watch_update(device_id, "15.5.0(release-tasmota)",
                                delay=0, interval=0, attempts=3)
    final = recorded[-1][1]
    assert "reports '15.5.0(release-tasmota)'" in final
    assert "started from" in final
