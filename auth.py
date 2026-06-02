"""Auth helpers — load and verify credentials from /etc/openwrt-monitor/auth.json.

The JSON file holds:
    {
      "username": "<str>",
      "password_hash": "<argon2id hash>",
      "secret_key": "<hex, used as Flask session secret>"
    }

Bootstrapped by scripts/init_auth.py. Rotated in-place from the running
Flask app via change_password(). Mode 0600, root-owned.
"""

import json
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

AUTH_PATH = Path("/etc/openwrt-monitor/auth.json")

_hasher = PasswordHasher()


def load() -> dict:
    """Read auth.json. Raises FileNotFoundError if missing."""
    with open(AUTH_PATH, "r") as f:
        return json.load(f)


def _save(data: dict) -> None:
    """Atomic write with 0600 perms."""
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_PATH)


def verify(username: str, password: str) -> bool:
    """Constant-time username check plus Argon2 password verify."""
    try:
        data = load()
    except FileNotFoundError:
        return False

    if not secrets.compare_digest(str(username or ""), data.get("username", "")):
        return False

    try:
        _hasher.verify(data["password_hash"], password or "")
    except Argon2Error:
        return False
    return True


def change_password(current: str, new: str) -> tuple[bool, str]:
    """Verify current pw, rehash new, persist. Returns (ok, message)."""
    try:
        data = load()
    except FileNotFoundError:
        return False, "Auth not configured — run scripts/init_auth.py"

    try:
        _hasher.verify(data["password_hash"], current or "")
    except Argon2Error:
        return False, "Current password is incorrect"

    if not new or len(new) < 12:
        return False, "New password must be at least 12 characters"

    data["password_hash"] = _hasher.hash(new)
    _save(data)
    return True, "Password changed"


def get_secret_key() -> str:
    """Return the stable Flask session secret. Raises if missing."""
    return load()["secret_key"]


def bootstrap(username: str, password: str) -> None:
    """Write a fresh auth.json. Overwrites any existing file."""
    _save({
        "username": username,
        "password_hash": _hasher.hash(password),
        "secret_key": secrets.token_hex(32),
    })
