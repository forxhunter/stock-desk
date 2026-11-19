"""Common adapter interfaces.

Every adapter is READ-ONLY by construction: the interfaces below expose no method that
could create, modify or cancel an order, and none may be added.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Callable

import pandas as pd

from ..i18n import L
from ..models import BrokerData, MarketOdds, NewsItem

USER_AGENT = "StockDesk/0.1 (personal desktop reader)"


class AdapterError(Exception):
    """Something went wrong talking to a source. Message is safe to show (no secrets)."""


class AdapterUnavailable(AdapterError):
    """Source not reachable / not running / not logged in. Expected, handled gracefully."""


class RateLimited(AdapterError):
    """The source asked us to slow down."""


class BrokerAdapter(ABC):
    """Read-only account snapshot: positions (+ recent executions if available)."""
    name: str = "broker"
    label: str = "Broker"
    official: bool = True

    @abstractmethod
    def fetch(self) -> BrokerData: ...


class MarketDataAdapter(ABC):
    name: str = "market"

    @abstractmethod
    def history(self, symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
        """Daily OHLCV per symbol; columns open/high/low/close/volume, tz-naive index."""

    @abstractmethod
    def intraday(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Recent intraday bars per symbol (for last price, day change and sparkline)."""


class ResearchAdapter(ABC):
    """Public research / news feed."""
    name: str = "research"

    @abstractmethod
    def fetch(self) -> list[NewsItem]: ...


class OddsAdapter(ABC):
    """Public prediction-market probabilities (context only)."""
    name: str = "odds"

    @abstractmethod
    def fetch(self, watch: dict[str, list[str]]) -> list[MarketOdds]: ...


# ---- tiny HTTP GET helper (GET only, by design) ----------------------------------------------

HttpGet = Callable[..., tuple[int, dict, bytes]]


def http_get(url: str, headers: dict | None = None, timeout: float = 15.0,
             insecure_localhost: bool = False) -> tuple[int, dict, bytes]:
    """HTTP GET returning (status, headers, body). Never sends a request body.

    Error messages never include the URL, because URLs may carry tokens.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method="GET")
    ctx = None
    if insecure_localhost:
        host = urllib.request.urlparse(url).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise AdapterError(L("只允许对本机地址关闭证书校验", "Certificate checks may only be turned off for local addresses"))
        ctx = ssl._create_unverified_context()  # the local IBKR gateway uses a self-signed cert
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, dict(e.headers.items()), b""
        if e.code == 429:
            raise RateLimited(L("HTTP 429：请求过于频繁", "HTTP 429: too many requests")) from None
        raise AdapterError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise AdapterUnavailable(L(f"网络不可用：{type(reason).__name__}", f"Network unavailable: {type(reason).__name__}")) from None


def get_json(getter: HttpGet, url: str, **kw):
    status, _, body = getter(url, **kw)
    if status != 200:
        raise AdapterError(f"HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AdapterError(L("返回内容不是 JSON", "The response is not JSON")) from None
