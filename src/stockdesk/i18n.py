"""Interface language: Chinese or English.

Every user-visible string is written once in both languages at the place it is used::

    from .i18n import L
    title = L("每日简报", "Daily briefing")
    line = L(f"现价 {px:,.2f}", f"Price {px:,.2f}")

The language is fixed when the program starts (changing it needs a restart), so module-level
tables can use ``L`` too. Order of precedence: the ``STOCKDESK_LANG`` environment variable, then
``app.language`` in ``data/config.yaml`` (``zh`` / ``en`` / ``auto``), then the system locale
(Chinese locale -> ``zh``, anything else -> ``en``).
"""
from __future__ import annotations

import locale
import os

from . import paths


def _from_config() -> str:
    try:
        import yaml
        data = yaml.safe_load(paths.USER_CONFIG.read_text(encoding="utf-8")) or {}
        return str((data.get("app") or {}).get("language") or "").strip().lower()
    except Exception:
        return ""


def _from_locale() -> str:
    try:
        name = (locale.getlocale()[0] or "") + " " + (os.environ.get("LANG") or "")
        if os.name == "nt":
            import ctypes
            name += f" lcid{ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF:02x}"
    except Exception:
        name = ""
    low = name.lower()
    return "zh" if ("zh" in low or "chinese" in low or "lcid04" in low) else "en"


def detect() -> str:
    for choice in (os.environ.get("STOCKDESK_LANG", "").strip().lower(), _from_config()):
        if choice in ("zh", "en"):
            return choice
    return _from_locale()


LANG = detect()


def L(zh: str, en: str) -> str:
    """The Chinese or the English wording, by the interface language."""
    return en if LANG == "en" else zh
