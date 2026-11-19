"""IBKR Client Portal Web API (official) through the locally running Client Portal Gateway.

The Web API itself is trading-capable and has no read-only scope, so this adapter enforces
read-only on the client side: it can only issue HTTP GET, and only to the allow-listed
portfolio/trade-history paths below. The user logs in to the gateway in their own browser;
this app never sees the credentials.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import BrokerData, Position, Trade
from .base import AdapterError, AdapterUnavailable, BrokerAdapter, HttpGet, get_json, http_get

_ALLOWED = (
    re.compile(r"^/portfolio/accounts$"),
    re.compile(r"^/portfolio/[A-Za-z0-9]+/positions/\d+$"),
    re.compile(r"^/portfolio/[A-Za-z0-9]+/ledger$"),
    re.compile(r"^/iserver/account/trades(\?days=\d)?$"),
)


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class IbkrClientPortalAdapter(BrokerAdapter):
    name = "ibkr_cp"
    label = "IBKR (Client Portal)"
    official = True

    def __init__(self, base_url: str = "https://localhost:5000/v1/api", getter: HttpGet = http_get):
        self.base_url = base_url.rstrip("/")
        self._get = getter

    def _read(self, path: str):
        if not any(p.match(path) for p in _ALLOWED):
            raise AdapterError(f"路径不在只读白名单内：{path}")
        try:
            return get_json(self._get, self.base_url + path, timeout=8.0, insecure_localhost=True)
        except AdapterUnavailable:
            raise AdapterUnavailable("连不上 Client Portal Gateway（默认 https://localhost:5000）。"
                                     "请先启动网关并在浏览器里登录。") from None
        except AdapterError as e:
            if "401" in str(e) or "403" in str(e):
                raise AdapterUnavailable("Client Portal Gateway 未登录或会话已过期，请在浏览器重新登录。") from None
            raise

    def fetch(self) -> BrokerData:
        data = BrokerData()
        accounts = self._read("/portfolio/accounts") or []
        for acct in accounts:
            aid = str(acct.get("accountId") or acct.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9]+", aid):
                continue
            page = 0
            while page < 20:
                rows = self._read(f"/portfolio/{aid}/positions/{page}") or []
                for r in rows:
                    if r.get("assetClass") != "STK" or not _num(r.get("position")):
                        continue
                    sym = str(r.get("ticker") or r.get("contractDesc") or "").upper().replace(" ", "-")
                    if sym:
                        data.positions.append(Position(sym, _num(r.get("position")), _num(r.get("avgCost")),
                                                       "IBKR", r.get("currency", "USD"),
                                                       _num(r.get("mktPrice")) or None))
                if len(rows) < 100:
                    break
                page += 1
            try:
                ledger = self._read(f"/portfolio/{aid}/ledger") or {}
                base = ledger.get("BASE") or ledger.get("USD") or {}
                if base:
                    data.net_liquidation = (data.net_liquidation or 0.0) + _num(base.get("netliquidationvalue"))
                    data.cash = (data.cash or 0.0) + _num(base.get("cashbalance"))
            except AdapterError:
                pass
        try:
            for t in self._read("/iserver/account/trades?days=7") or []:
                if t.get("sec_type") not in (None, "STK"):
                    continue
                ms = _num(t.get("trade_time_r"))
                ts = ms / 1000.0 if ms else datetime.now(timezone.utc).timestamp()
                data.trades.append(Trade(
                    id=f"ibkr:{t.get('execution_id')}", symbol=str(t.get("symbol", "")).upper(),
                    side="BUY" if str(t.get("side", "")).upper().startswith("B") else "SELL",
                    qty=abs(_num(t.get("size"))), price=_num(t.get("price")), ts=ts, source="IBKR",
                    fees=abs(_num(t.get("commission")))))
        except AdapterError:
            pass  # trade history is optional
        return data
