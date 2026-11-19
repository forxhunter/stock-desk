"""Non-secret configuration: shipped defaults deep-merged with data/config.yaml."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .i18n import L


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Dotted-path access over a nested dict, e.g. ``cfg.get("quant.cost_bps")``."""

    def __init__(self, data: dict | None = None, user_path: Path | None = None):
        self.data = data or {}
        self.user_path = user_path
        self._user: dict = {}

    @classmethod
    def load(cls, default_path: Path | None = None, user_path: Path | None = None) -> "Config":
        default_path = default_path or paths.DEFAULT_CONFIG
        user_path = user_path or paths.USER_CONFIG
        base = yaml.safe_load(default_path.read_text(encoding="utf-8")) or {}
        user: dict = {}
        if user_path.exists():
            try:
                user = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                user = {}
        cfg = cls(_deep_merge(base, user), user_path)
        cfg._user = user
        return cfg

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        """Set a value in memory and in the user override file (never secrets)."""
        for target in (self.data, self._user):
            cur = target
            parts = dotted.split(".")
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = value

    def save(self) -> None:
        if self.user_path is None:
            return
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        header = L("# Stock Desk 用户配置（覆盖 config.default.yaml）。不要在这里写密码或 token。\n",
                   "# Stock Desk user settings (override config.default.yaml). Do not put passwords or tokens here.\n")
        self.user_path.write_text(
            header + yaml.safe_dump(self._user, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
