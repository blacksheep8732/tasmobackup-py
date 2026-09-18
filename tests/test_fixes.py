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
