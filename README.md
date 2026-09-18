# TasmoBackup-py

A modern Python rewrite of [TasmoBackup](https://github.com/danmed/TasmoBackupV1) —
backup all your Tasmota & WLED devices, and **optionally** auto-update Tasmota
firmware (opt-in per device, always backing up first).

## Why a rewrite?

The original is PHP on an end-of-life PHP 7.3 base image, has Google Analytics baked
in, stores device passwords in plaintext, generates HTML by string concatenation, and
— despite the assumption — **never actually updated firmware**. This rewrite is a
single FastAPI container with an in-process scheduler (no external cron), encrypted
credentials, a login wall, and a real (safe) firmware-update flow.

## Features

- Scheduled + on-demand backups (Tasmota `.dmp`, WLED `cfg.json`+`presets.json` zip)
- Restore a Tasmota backup with one click — a rejected upload is reported as failed
  (Tasmota answers HTTP 200 either way, so the response itself is checked)
- **Online status per device** with a periodic reachability check and "last seen" age
- **Event log + notifications**: failed backups, aborted or stuck updates and devices
  going offline are recorded and pushed to ntfy/Gotify or a JSON webhook
- Add devices manually, scan an IP subnet (up to a /22), or **discover via MQTT**
  (parallel async); deleting a device also deletes its backups
- German/English UI with a simple JSON language-pack system (`app/locales/`)
- Firmware version check against the latest GitHub release
- **Opt-in firmware auto-update**: per-device toggle + global master switch; a fresh
  backup is **always** taken immediately before flashing, and aborts if it fails
- Login-protected web UI, encrypted device and MQTT passwords, no tracking
- SQLite by default (MySQL/Postgres via `TB_DATABASE_URL`)

## Unraid

Install **TasmoBackup-py** from Community Applications, or add the template manually:
`https://raw.githubusercontent.com/blacksheep8732/tasmobackup-py/main/templates/tasmobackup-py.xml`

## Quick start (Docker)

Prebuilt image: `ghcr.io/blacksheep8732/tasmobackup-py:latest`

```bash
cp .env.example .env
# generate a stable secret (encrypts stored passwords + signs sessions):
echo "TB_SECRET_KEY=$(openssl rand -hex 32)" >> .env   # then edit/remove the placeholder line
docker compose up -d --build
```

Open http://SERVER:8259 and log in with `TB_ADMIN_USER` / `TB_ADMIN_PASSWORD`.

> ⚠️ Keep `TB_SECRET_KEY` stable. If it changes, previously stored device passwords
> can no longer be decrypted (you'd just re-enter them).

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
TB_DATA_DIR=./data TB_SECRET_KEY=dev uvicorn app.main:app --reload --port 8259
pytest -q
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `TB_SECRET_KEY` | *(generated)* | Signs sessions + encrypts device passwords. If unset, a random key is created once in `data/.secret_key`. |
| `TB_ADMIN_USER` / `TB_ADMIN_PASSWORD` | `admin` / `admin` | Initial web login |
| `TB_AUTH_ENABLED` | `true` | Set `false` if behind external auth (e.g. Traefik) |
| `TB_DATA_DIR` | `/data` | Where DB + backups live |
| `TB_DATABASE_URL` | *(SQLite in data dir)* | e.g. `mysql+pymysql://user:pass@host/db` |
| `TB_TASMOTA_USER` / `TB_TASMOTA_PASSWORD` | `admin` / *(empty)* | Default device credentials |
| `TB_CONCURRENCY` | `15` | Max parallel device requests during scans/backups |
| `PUID` / `PGID` | `1000` / `1000` | User/group the app runs as; owns the data dir (Unraid: `99` / `100`) |

Runtime preferences (backup interval, retention, auto-update master switch, theme,
admin password) are editable in the **Settings** page.

> Note: `TB_ADMIN_PASSWORD` only seeds the login on the **very first start**. After
> that the password lives as a bcrypt hash in the database and is changed in Settings
> — editing the env var has no effect.

## Time zone sync

Tasmota does not know zone names: it either applies a fixed UTC offset or, with
`Timezone 99`, switches between two offsets using explicit rules. Devices left on a
fixed offset silently run an hour off for half the year — the DST rules they carry
are ignored.

The dashboard therefore compares each device's clock (`Local` minus `UTC` from the
`status 0` it already fetches — no extra request) against the configured display zone
and flags any difference, linking to that device's edit page. Two cases are told apart:
the clock is wrong *now*, or it is right today but the device runs a fixed offset in a
zone that switches — that one breaks at the next changeover.

Applying happens per device, in **Edit → Time & time zone**, which shows what the
device reports, what the server expects, and the exact commands before sending them in
one `Backlog` call, e.g. for `Europe/Berlin`:

```
Backlog TimeDst 0,0,3,1,2,120; TimeStd 0,0,10,1,3,60; Timezone 99
```

Rules are derived from the zone database (`app/tz.py`), so US, southern-hemisphere and
DST-free zones come out correctly too. Nothing is written without pressing the button,
and afterwards the device is read back so the page shows what actually took effect.

## Online status & notifications

Every device row shows a green/red status pill plus how long ago the device last
answered. A background job contacts all devices every *n* minutes (Settings →
*Reachability*, `0` disables it); a device is only reported offline after two
consecutive failed checks, so a single dropped WiFi request causes no false alarm.

Problems are written to an **event log** (nav bar → *Events*, with an unread badge and
a dashboard banner) and, if a target URL is configured, pushed out over HTTP:

| Setting | Meaning |
|---|---|
| *Target URL* | e.g. `https://ntfy.sh/your-secret-topic`, `https://gotify.example/message?token=<app token>`, or any webhook |
| *Format* | `ntfy` (plain-text body, `Title`/`Priority`/`Tags` headers), `gotify` (`{title, message, priority}`) or `json` (`{level, device, message, time}`) |
| *Notify from level* | `info`, `warn` or `error` — defaults to `warn` |
| *Keep events* | prune the log after n days |

Alerts are **state-based, not periodic**: each device stores the key of the problem
last reported, and an identical one is dropped until the device is healthy again
(successful backup, or it comes back online). A device that stays offline is therefore
reported once — before this, the scheduler's 15-minute retries produced an error every
run. A manual update click resets the state first, so it always answers.

What raises an event: backup failed, pre-update backup failed (update aborted), device
rejected the `Upgrade` command, **device still reporting the old version ~11 min after
a flash** (the "update went wrong" case), **device stuck on the minimal image** (stage
two never completed), device unreachable, device back online, restore failed.

Non-ASCII device names are RFC 2047 encoded in the `Title` header, so umlauts survive
the trip to ntfy. Delivery failures are logged and never abort a backup run.

## How auto-update stays safe

1. Disabled by default. Requires **both** the global master switch (Settings) **and**
   the per-device "Auto-update" toggle to be ON.
2. Before any flash, a fresh backup is taken; if it fails, the update is aborted.
3. We only ever send `Upgrade 1` — we never set `OtaUrl`. The device already knows
   which image belongs to it, and that choice cannot be reconstructed from the
   outside: `15.5.0(release-tasmota)` is reported by both `tasmota.bin` and the
   German `tasmota-DE.bin.gz`, so writing our own URL would silently flash the
   wrong language variant.
4. Tasmota then drives the flash itself. When the full image does not fit into the
   free program flash (typical: 1 MB program area, ~650 KB in use), it upgrades in two
   stages: `tasmota-minimal` first, reboot, then the real image, reboot again. We only
   watch — the intermediate `(minimal)` build is not mistaken for a finished update,
   and the reboots raise no offline alert.

   **It does not always complete stage two.** A device can stop on the minimal build
   and stay there. If that state persists for a few checks, the app re-sends
   `Upgrade 1` **once** and says so; the device's OtaUrl survives the minimal flash,
   so it fetches the right image. Should it still not move, the event names that
   specific state instead of claiming the old version is unchanged.
5. Scheduled updates only fire for genuinely outdated devices; the manual "Update"
   button forces it (still backing up first).
6. **Custom builds are never auto-updated.** Only images that report their build as
   `(release-…)` are official releases. Anything else — a scripting build such as
   `15.6.0(gas)`, a self-compiled `(tasmota)` — is shown as *custom build* instead of
   *outdated*, skipped by the scheduler, and the manual button asks with an explicit
   warning, because an OTA would replace it with the official image.

WLED firmware updates are **not** automated (no safe headless OTA path); WLED is
backup/restore only.

## License

MIT — see [LICENSE](LICENSE). This project is a rewrite of
[TasmoBackup](https://github.com/danmed/TasmoBackupV1) by danmed, which is also
released under the MIT License.

## Project layout

```
app/
  config.py     env-based config
  db.py         engine, sessions, settings store
  models.py     SQLAlchemy models (Device, Backup, Event, Setting)
  events.py     event log + outbound notifications (ntfy / webhook)
  security.py   Fernet password encryption + bcrypt login
  tasmota.py    async device client (status/backup/restore/OTA)
  github.py     latest-release lookup (version comparison only)
  service.py    business logic (add/scan/backup/restore/update)
  scheduler.py  APScheduler hourly job (interval-gated)
  main.py       FastAPI routes + UI
  templates/    Jinja2 + htmx
tests/
```
