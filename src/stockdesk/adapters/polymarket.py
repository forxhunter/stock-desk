"""Polymarket public market data (read-only context).

Uses only unauthenticated GET endpoints:
  * Gamma API   https://gamma-api.polymarket.com/public-search?q=...   (find events/markets)
  * Gamma API   https://gamma-api.polymarket.com/markets?slug=...      (pinned markets)
  * CLOB API    https://clob.polymarket.com/prices-history?market=<tokenId>&interval=1m&fidelity=1440
No wallet, no account, no API key, and no trading / betting functionality of any kind.
Probabilities are shown as context next to suggestions; they never trigger anything.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..models import MarketOdds
from .base import AdapterError, HttpGet, OddsAdapter, get_json, http_get

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _opt(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _jlist(v) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def parse_market(m: dict, event: dict | None = None, label: str = "",
                 tickers: list[str] | None = None) -> tuple[MarketOdds, str | None] | None:
    """Convert one Gamma market dict; returns None for closed / non-binary / empty markets."""
    if m.get("closed") or m.get("archived") or m.get("active") is False:
        return None
    outcomes, prices = _jlist(m.get("outcomes")), _jlist(m.get("outcomePrices"))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    yes = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
    prob = _f(prices[yes], -1.0)
    if not 0.0 <= prob <= 1.0:
        return None
    sign = 1.0 if yes == 0 else -1.0     # price-change fields refer to the first outcome
    question = str(m.get("question") or "").strip()
    if str(outcomes[yes]).lower() != "yes":
        question = f"{question} → {outcomes[yes]}"
    ev_slug = (event or {}).get("slug") or next((e.get("slug") for e in m.get("events") or [] if e.get("slug")), None)
    url = f"https://polymarket.com/event/{ev_slug}" if ev_slug else f"https://polymarket.com/market/{m.get('slug', '')}"

    def chg(key):
        v = _opt(m.get(key))
        return None if v is None else sign * v

    return MarketOdds(
        id=str(m.get("id") or m.get("slug")), question=question, prob=prob,
        change_1d=chg("oneDayPriceChange"), change_1w=chg("oneWeekPriceChange"),
        change_1m=chg("oneMonthPriceChange"),
        volume_24h=_f(m.get("volume24hr")), volume=_f(m.get("volumeNum", m.get("volume"))),
        end_date=str(m.get("endDateIso") or (m.get("endDate") or "")[:10]), url=url, label=label,
        tickers=list(tickers or []), history=[],
    ), (_jlist(m.get("clobTokenIds")) or [None])[yes]


def _mentions(name: str, text: str) -> bool:
    """Whole-word match; an all-caps ticker must appear in caps (so "ARM" does not match "farm")."""
    if name.isupper() and len(name) <= 5:
        return re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text) is not None
    return re.search(rf"(?<![A-Za-z]){re.escape(name.lower())}(?![A-Za-z])", text.lower()) is not None


def for_watchlist(odds: list[MarketOdds], symbols: list[str], macro: bool = True) -> list[MarketOdds]:
    """Odds relevant to the current watchlist: stock-linked ones first, then general topics.

    Tickers of removed stocks are dropped straight away, without waiting for the next search."""
    sym = set(symbols)
    stock, general = [], []
    for o in odds:
        if o.tickers:
            keep = [t for t in o.tickers if t in sym]
            if keep:
                stock.append(MarketOdds(**{**o.__dict__, "tickers": keep}))
        elif macro:
            general.append(o)
    stock.sort(key=lambda o: -o.volume_24h)
    return stock + general


class PolymarketAdapter(OddsAdapter):
    name = "polymarket"

    def __init__(self, keywords: list[dict], slugs: list[str] | None = None, getter: HttpGet = http_get,
                 per_keyword: int = 3, min_volume: float = 10000.0, auto_ticker_search: bool = True,
                 max_markets: int = 24, with_history: bool = True, min_volume_24h: float = 100.0,
                 per_ticker: int = 2, exclude: list[str] | None = None, macro: bool = True):
        self.keywords, self.slugs = keywords or [], slugs or []
        self._get = getter
        self.per_keyword, self.min_volume = per_keyword, min_volume
        self.auto, self.max_markets, self.with_history = auto_ticker_search, max_markets, with_history
        self.min_volume_24h, self.per_ticker = min_volume_24h, per_ticker
        # markets that merely mention a company name (e.g. "#1 searched on Google") are noise
        self.exclude = [str(x).lower() for x in (exclude or []) if str(x).strip()]
        self.macro = macro          # keep keyword topics that are not tied to any stock (Fed, recession...)
        self.errors: list[str] = []

    def _search(self, q: str) -> list[tuple[dict, dict]]:
        data = get_json(self._get, f"{GAMMA}/public-search?q={quote(q)}&limit_per_type=8&events_status=active")
        pairs = []
        for ev in (data or {}).get("events") or []:
            if ev.get("closed") or ev.get("archived"):
                continue
            for m in ev.get("markets") or []:
                pairs.append((m, ev))
        return pairs

    def fetch(self, watch: dict[str, list[str]]) -> list[MarketOdds]:
        """``watch``: symbol -> aliases (first alias is used for automatic search)."""
        self.errors = []
        queries: list[tuple[str, str, list[str], bool]] = []     # (query, label, tickers, auto)
        for kw in self.keywords:
            if isinstance(kw, str):
                kw = {"q": kw}
            if not kw.get("q"):
                continue
            tickers = list(kw.get("tickers") or [])
            if tickers and not any(t in watch for t in tickers):
                continue            # pinned to stocks that are no longer on the watchlist
            if not tickers and not self.macro:
                continue
            queries.append((kw["q"], kw.get("label") or kw["q"], [t for t in tickers if t in watch], False))
        if self.auto:
            known = {q.lower() for q, _, _, _ in queries}
            for sym, aliases in list(watch.items())[:20]:
                name = next((a for a in aliases if len(a) >= 4), None)
                if name is None and len(sym) >= 2 and sym.isalpha():
                    name = sym      # no company name configured: search the ticker itself
                if name and name.lower() not in known:
                    queries.append((name, name, [sym], True))
                    known.add(name.lower())

        found: dict[str, tuple[MarketOdds, str | None]] = {}
        ok = 0
        for q, label, tickers, auto in queries:
            try:
                pairs = self._search(q)
                ok += 1
            except AdapterError as e:
                self.errors.append(f"{q}: {e}")
                continue
            parsed = []
            for m, ev in pairs:
                r = parse_market(m, ev, label, tickers)
                if not r or r[0].volume < self.min_volume or r[0].volume_24h < self.min_volume_24h:
                    continue
                text = f"{r[0].question} {ev.get('title', '')}"
                if auto and not _mentions(q, text):
                    continue        # automatic company searches must actually name the company
                if any(x in text.lower() for x in self.exclude):
                    continue
                parsed.append(r)
            parsed.sort(key=lambda r: -r[0].volume_24h)
            for odds, token in parsed[: self.per_ticker if auto else self.per_keyword]:
                if odds.id in found:
                    for t in tickers:
                        if t not in found[odds.id][0].tickers:
                            found[odds.id][0].tickers.append(t)
                else:
                    found[odds.id] = (odds, token)
        for slug in self.slugs:
            try:
                rows = get_json(self._get, f"{GAMMA}/markets?slug={quote(str(slug))}")
                ok += 1
            except AdapterError as e:
                self.errors.append(f"{slug}: {e}")
                continue
            for m in rows or []:
                r = parse_market(m, None, "已固定")
                if r:
                    found.setdefault(r[0].id, r)
        if queries and not ok and not found:
            raise AdapterError(self.errors[0] if self.errors else "Polymarket 不可用")

        out = sorted(found.values(), key=lambda r: -r[0].volume_24h)[: self.max_markets]
        if self.with_history:
            for odds, token in out:
                if not token:
                    continue
                try:
                    h = get_json(self._get, f"{CLOB}/prices-history?market={token}&interval=1m&fidelity=1440")
                    odds.history = [_f(p.get("p")) for p in (h or {}).get("history") or []][-40:]
                except AdapterError:
                    pass                  # the sparkline is optional
        return [o for o, _ in out]
