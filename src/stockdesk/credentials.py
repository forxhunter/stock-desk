"""Secrets live only in the OS credential vault (Windows Credential Manager via keyring).

Nothing here is ever written to disk by this app or passed to the logger.

The one exception is a headless server, which has no credential vault: when the environment
variable ``STOCKDESK_SECRETS_FILE`` names a file, secrets are read from that JSON file instead
(owner-only permissions; the user puts it there with ``scripts/push_secrets.py``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE = "StockDesk"

KEY_FLEX_TOKEN = "ibkr_flex_token"
KEY_RH_PASSWORD = "robinhood_password"


def _file() -> Path | None:
    p = os.environ.get("STOCKDESK_SECRETS_FILE")
    return Path(p).expanduser() if p else None


def _file_secrets() -> dict:
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def get_secret(name: str) -> str | None:
    if _file() is not None:
        return _file_secrets().get(name) or None
    try:
        import keyring
        return keyring.get_password(SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str) -> bool:
    if _file() is not None:
        return False          # the server never writes secrets; they are pushed from the desktop
    try:
        import keyring
        keyring.set_password(SERVICE, name, value)
        return True
    except Exception:
        return False


def delete_secret(name: str) -> None:
    if _file() is not None:
        return
    try:
        import keyring
        keyring.delete_password(SERVICE, name)
    except Exception:
        pass


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def redact(text: str, *secrets: str | None) -> str:
    """Remove secret values from a message before it is shown or logged."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
