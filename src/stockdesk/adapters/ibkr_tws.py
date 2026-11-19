"""IBKR via the official TWS API (TWS or IB Gateway), using ib_async in READ-ONLY mode.

* ``readonly=True`` is always passed to ``connectAsync`` (not configurable).
* Only account/position/execution *queries* are used. This module contains no order calls.
* The user should additionally tick "Read-Only API" in TWS / IB Gateway
  (Global Configuration -> API -> Settings), so the gateway itself rejects any order.
"""
from __future__ import annotations

import asyncio

from ..models import BrokerData, Position, Trade
from .base import AdapterError, AdapterUnavailable, BrokerAdapter


class IbkrTwsAdapter(BrokerAdapter):
    name = "ibkr_tws"
    label = "IBKR (TWS API)"
    official = True

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 47,
                 timeout: float = 4.0, ib_factory=None):
        self.host, self.port, self.client_id, self.timeout = host, int(port), int(client_id), float(timeout)
        self._ib_factory = ib_factory

    def fetch(self) -> BrokerData:
        # Runs in a worker thread: give it its own event loop.
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._fetch())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    async def _fetch(self) -> BrokerData:
        if self._ib_factory is not None:
            ib = self._ib_factory()
        else:
            try:
                from ib_async import IB
            except ImportError as e:  # pragma: no cover
                raise AdapterUnavailable("未安装 ib_async") from e
            ib = IB()
        try:
            try:
                await ib.connectAsync(self.host, self.port, clientId=self.client_id,
                                      timeout=self.timeout, readonly=True)
            except (ConnectionRefusedError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                raise AdapterUnavailable(
                    f"连不上 TWS / IB Gateway（{self.host}:{self.port}）。请确认它已启动并登录，"
                    f"且已在 API 设置里启用 Socket 客户端。[{type(e).__name__}]") from None
            except Exception as e:
                raise AdapterError(f"IBKR 连接失败：{type(e).__name__}") from None

            data = BrokerData()
            for p in ib.positions():
                c = p.contract
                if getattr(c, "secType", "") != "STK" or not p.position:
                    continue
                data.positions.append(Position(
                    symbol=str(c.symbol).upper().replace(" ", "-"), qty=float(p.position),
                    avg_cost=float(p.avgCost), source="IBKR", currency=getattr(c, "currency", "USD")))
            prices = {}
            for item in ib.portfolio():
                c = item.contract
                if getattr(c, "secType", "") == "STK":
                    prices[str(c.symbol).upper().replace(" ", "-")] = float(item.marketPrice)
            for pos in data.positions:
                mp = prices.get(pos.symbol)
                if mp and mp > 0:
                    pos.market_price = mp
            for av in ib.accountValues():
                if av.currency in ("USD", "BASE") and av.tag in ("NetLiquidation", "TotalCashValue"):
                    try:
                        val = float(av.value)
                    except ValueError:
                        continue
                    if av.tag == "NetLiquidation" and (data.net_liquidation is None or av.currency == "BASE"):
                        data.net_liquidation = val
                    if av.tag == "TotalCashValue" and (data.cash is None or av.currency == "BASE"):
                        data.cash = val
            try:  # today's executions (a read query); history beyond that comes from Flex
                fills = await asyncio.wait_for(ib.reqExecutionsAsync(), timeout=self.timeout + 4)
            except Exception:
                fills = []
            for f in fills or []:
                c, e = f.contract, f.execution
                if getattr(c, "secType", "") != "STK":
                    continue
                ts = e.time.timestamp() if hasattr(e.time, "timestamp") else 0.0
                fee = getattr(getattr(f, "commissionReport", None), "commission", 0.0) or 0.0
                data.trades.append(Trade(
                    id=f"ibkr:{e.execId}", symbol=str(c.symbol).upper().replace(" ", "-"),
                    side="BUY" if str(e.side).upper() in ("BOT", "BUY") else "SELL",
                    qty=abs(float(e.shares)), price=float(e.price), ts=ts, source="IBKR",
                    fees=abs(float(fee)) if fee == fee else 0.0))
            return data
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
