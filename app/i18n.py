"""Lightweight i18n: each language is a flat JSON "language pack" in app/locales/.

Add a new language by dropping e.g. `fr.json` into the locales folder (copy an
existing file and translate the values) and restarting the app — it will appear
in the language selector automatically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANG = "de"

# Native display names for known language codes (fallback: the code itself).
_LANG_NAMES = {"de": "Deutsch", "en": "English"}


@lru_cache
def _packs() -> dict[str, dict[str, str]]:
    packs: dict[str, dict[str, str]] = {}
    for f in sorted(LOCALES_DIR.glob("*.json")):
        try:
            packs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return packs


def available_languages() -> list[tuple[str, str]]:
    """Return [(code, display_name), ...] for the language selector."""
    return [(code, _LANG_NAMES.get(code, code)) for code in _packs()]


@dataclass(frozen=True)
class Msg:
    """A user-facing message that is translated when shown, not when created.

    The service layer has no idea which language the UI uses, so it returns a key
    plus parameters. str() gives English — that is what ends up in the logs — and
    the web layer calls .text(lang). A parameter may itself be a Msg.
    """

    key: str
    params: dict[str, object] = field(default_factory=dict)

    def text(self, lang: str) -> str:
        params = {k: v.text(lang) if isinstance(v, Msg) else v for k, v in self.params.items()}
        return translate(lang, self.key, **params)

    def __str__(self) -> str:
        return self.text("en")


def msg(key: str, **params: object) -> Msg:
    return Msg(key, params)


def translate(lang: str, key: str, **kwargs: object) -> str:
    packs = _packs()
    text = (packs.get(lang) or {}).get(key)
    if text is None:  # fall back to default lang, then English, then the key itself
        text = (packs.get(DEFAULT_LANG) or {}).get(key)
    if text is None:
        text = (packs.get("en") or {}).get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text
