"""Plain data containers shared by adapters, analytics and UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float
    source: str = "manual"
    currency: str = "USD"
    market_price: float | None = None


@dataclass
class Trade:
    id: str
    symbol: str
    side: str            # "BUY" | "SELL"
    qty: float
    price: float
    ts: float            # epoch seconds (UTC)
    source: str = ""
    fees: float = 0.0
    demo: bool = False


@dataclass
class BrokerData:
    """What a broker adapter returns from one read-only sync."""
    positions: list[Position] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    cash: float | None = None
    net_liquidation: float | None = None


@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    summary: str
    link: str
    published: float
    tickers: list[str] = field(default_factory=list)


@dataclass
class MarketOdds:
    """One prediction-market outcome (read-only context, never a trade trigger)."""
    id: str
    question: str
    prob: float                      # probability of "Yes" (0..1)
    change_1d: float | None
    change_1w: float | None
    change_1m: float | None
    volume_24h: float
    volume: float
    end_date: str
    url: str
    label: str = ""                  # keyword / group label
    tickers: list[str] = field(default_factory=list)
    history: list[float] = field(default_factory=list)


@dataclass
class Alert:
    id: str                          # stable de-duplication key
    kind: str
    symbol: str
    severity: str                    # "info" | "warn" | "high"
    title: str
    body: str                        # Chinese explanation incl. data and the user's own history
    data: dict[str, Any] = field(default_factory=dict)
    created: float = 0.0
    snoozed_until: float = 0.0
    dismissed: bool = False
    notified: bool = False
    demo: bool = False
