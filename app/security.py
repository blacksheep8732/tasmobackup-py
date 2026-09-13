"""Symmetric encryption for stored device passwords + web-UI auth helpers.

Device passwords are encrypted at rest with a Fernet key derived from the app
SECRET_KEY. The web login uses a bcrypt-hashed admin password kept in the
settings table so it can be changed at runtime.
"""
from __future__ import annotations

import base64
import hashlib

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from .config import get_config

_cfg = get_config()


def _fernet() -> Fernet:
    # Derive a stable 32-byte key from the configured secret.
    digest = hashlib.sha256(_cfg.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # Wrong/changed SECRET_KEY — treat as empty rather than crashing a backup run.
        return ""


def hash_password(password: str) -> str:
    # bcrypt only considers the first 72 bytes; truncate explicitly to avoid errors.
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except (ValueError, TypeError):
        return False
