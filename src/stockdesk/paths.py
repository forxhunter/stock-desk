"""Filesystem locations. Everything user-specific lives in git-ignored places."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("STOCKDESK_HOME", Path(__file__).resolve().parents[2]))
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "stockdesk.sqlite"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "stockdesk.log"
USER_CONFIG = DATA_DIR / "config.yaml"          # git-ignored, non-secret settings
DEFAULT_CONFIG = ROOT / "config.default.yaml"   # shipped defaults
WATCHLIST = ROOT / "watchlist.yaml"             # git-ignored, user editable
WATCHLIST_SAMPLE = ROOT / "watchlist.sample.yaml"
RH_IMPORT_DIR = DATA_DIR / "imports" / "robinhood"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RH_IMPORT_DIR.mkdir(parents=True, exist_ok=True)
