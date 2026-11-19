"""Free market data via yfinance (unofficial Yahoo Finance wrapper, Apache-2.0).

No account needed. Requests are batched (one call for all symbols) and results are cached by
the engine, so a refresh costs at most two HTTP round trips.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..i18n import L
from .base import AdapterError, AdapterUnavailable, MarketDataAdapter, RateLimited

log = logging.getLogger(__name__)
_COLS = ["open", "high", "low", "close", "volume"]


def _split(raw: pd.DataFrame | None, symbols: list[str], daily: bool) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    for sym in symbols:
        try:
            df = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        df = df.rename(columns=str.lower)
        if not set(_COLS) <= set(df.columns):
            continue
        df = df[_COLS].dropna(subset=["close"])
        if df.empty:
            continue
        idx = pd.DatetimeIndex(df.index)
        if daily:
            idx = idx.tz_localize(None) if idx.tz is not None else idx
            idx = idx.normalize()
        elif idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        df = df.copy()
        df.index = idx
        out[sym] = df[~df.index.duplicated(keep="last")].astype(float)
    return out


class YahooAdapter(MarketDataAdapter):
    name = "yahoo"

    def __init__(self, downloader=None):
        self._download = downloader

    def _dl(self, symbols: list[str], **kw) -> pd.DataFrame | None:
        try:
            import yfinance as yf
            from yfinance import exceptions as yfe
        except ImportError as e:  # pragma: no cover
            raise AdapterUnavailable(L("未安装 yfinance", "yfinance is not installed")) from e
        dl = self._download or yf.download
        try:
            return dl(symbols, group_by="ticker", auto_adjust=True, progress=False,
                      threads=True, timeout=12, **kw)
        except yfe.YFRateLimitError:
            raise RateLimited(L("Yahoo 行情限流", "Yahoo quotes rate-limited")) from None
        except (ConnectionError, TimeoutError, OSError) as e:
            raise AdapterUnavailable(L(f"Yahoo 行情不可用：{type(e).__name__}", f"Yahoo quotes unavailable: {type(e).__name__}")) from None
        except Exception as e:
            name = type(e).__name__
            if "RateLimit" in name or "429" in str(e):
                raise RateLimited(L("Yahoo 行情限流", "Yahoo quotes rate-limited")) from None
            raise AdapterError(L(f"Yahoo 行情错误：{name}", f"Yahoo quotes error: {name}")) from None

    def history(self, symbols: list[str], period: str = "5y") -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        return _split(self._dl(symbols, period=period, interval="1d"), symbols, daily=True)

    def intraday(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        return _split(self._dl(symbols, period="5d", interval="15m"), symbols, daily=False)
