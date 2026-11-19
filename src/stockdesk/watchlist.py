"""User watchlist ("my picks") stored as YAML, plus merging of broker positions."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import paths
from .models import Position

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")


@dataclass
class WatchItem:
    symbol: str
    shares: float | None = None
    cost: float | None = None
    target: float | None = None
    stop: float | None = None
    max_pct: float | None = None
    note: str = ""
    auto: bool = False        # added automatically because a broker holds it

    def to_dict(self) -> dict:
        d: dict = {"symbol": self.symbol}
        for k in ("shares", "cost", "target", "stop", "max_pct"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.note:
            d["note"] = self.note
        return d


def normalize_symbol(raw: str) -> str | None:
    s = (raw or "").strip().upper().replace(" ", "")
    return s if _SYMBOL_RE.match(s) else None


def _num(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


class Watchlist:
    def __init__(self, path: Path | None = None, sample: Path | None = None):
        self.path = path or paths.WATCHLIST
        self.sample = sample or paths.WATCHLIST_SAMPLE
        self.items: list[WatchItem] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists() and self.sample.exists():
            shutil.copyfile(self.sample, self.path)
        self.items = []
        if not self.path.exists():
            return
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            raw = {}
        seen = set()
        for entry in raw.get("tickers", []) or []:
            if isinstance(entry, str):
                entry = {"symbol": entry}
            sym = normalize_symbol(str(entry.get("symbol", "")))
            if not sym or sym in seen:
                continue
            seen.add(sym)
            self.items.append(WatchItem(
                symbol=sym, shares=_num(entry.get("shares")), cost=_num(entry.get("cost")),
                target=_num(entry.get("target")), stop=_num(entry.get("stop")),
                max_pct=_num(entry.get("max_pct")), note=str(entry.get("note", "") or "")))

    def save(self) -> None:
        header = ("# Stock Desk 自选股（本文件不进 git）。字段说明见 watchlist.sample.yaml。\n")
        body = yaml.safe_dump({"tickers": [i.to_dict() for i in self.items if not i.auto]},
                              allow_unicode=True, sort_keys=False)
        self.path.write_text(header + body, encoding="utf-8")

    def symbols(self) -> list[str]:
        return [i.symbol for i in self.items]

    def get(self, symbol: str) -> WatchItem | None:
        return next((i for i in self.items if i.symbol == symbol), None)

    def add(self, raw_symbol: str) -> WatchItem | None:
        sym = normalize_symbol(raw_symbol)
        if not sym:
            return None
        item = self.get(sym)
        if item:
            if item.auto:
                item.auto = False
                self.save()
            return item
        item = WatchItem(sym)
        self.items.append(item)
        self.save()
        return item

    def remove(self, symbol: str) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items if i.symbol != symbol]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def update(self, symbol: str, **fields) -> None:
        item = self.get(symbol)
        if not item:
            return
        for k, v in fields.items():
            if hasattr(item, k):
                setattr(item, k, v)
        if not item.auto:
            self.save()

    def has_sample_holdings(self) -> bool:
        """True while the manual holdings are still the shipped demo numbers from the sample file."""
        mine = {(p.symbol, p.qty, p.avg_cost) for p in self.manual_positions()}
        if not mine or not self.sample.exists():
            return False
        try:
            raw = yaml.safe_load(self.sample.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return False
        demo = set()
        for e in raw.get("tickers", []) or []:
            if isinstance(e, dict) and _num(e.get("shares")):
                demo.add((normalize_symbol(str(e.get("symbol", ""))), _num(e.get("shares")), _num(e.get("cost")) or 0.0))
        return bool(mine & demo)

    # ---- positions -------------------------------------------------------
    def manual_positions(self) -> list[Position]:
        return [Position(i.symbol, i.shares, i.cost or 0.0, "manual")
                for i in self.items if i.shares]

    def merge_positions(self, broker_positions: list[Position]) -> dict[str, Position]:
        """Combine broker positions (summed across brokers, cost averaged) with manual ones.

        Broker data wins: a manual position is only used for symbols no broker reports.
        Symbols held at a broker but missing from the watchlist are added as ``auto`` items.
        """
        merged: dict[str, Position] = {}
        for p in broker_positions:
            if not p.qty:
                continue
            cur = merged.get(p.symbol)
            if cur is None:
                merged[p.symbol] = Position(p.symbol, p.qty, p.avg_cost, p.source, p.currency, p.market_price)
            else:
                total = cur.qty + p.qty
                cost = ((cur.qty * cur.avg_cost + p.qty * p.avg_cost) / total) if total else 0.0
                src = cur.source if p.source in cur.source.split("+") else f"{cur.source}+{p.source}"
                merged[p.symbol] = Position(p.symbol, total, cost, src, cur.currency, p.market_price or cur.market_price)
        # once a broker reports real holdings, the shipped demo numbers must not be mixed in
        if not (merged and self.has_sample_holdings()):
            for p in self.manual_positions():
                merged.setdefault(p.symbol, p)
        known = set(self.symbols())
        for sym in merged:
            if sym not in known:
                self.items.append(WatchItem(sym, auto=True))
        # drop auto items that no longer have a position
        self.items = [i for i in self.items if not i.auto or i.symbol in merged]
        return merged
