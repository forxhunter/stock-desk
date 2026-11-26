"""Market depth for the S&P 500 from free public data (read-only).

A full order book for the S&P is not free (exchange depth-of-book feeds are paid, and Cboe's public
book viewer refuses automated clients). What free data does show:

  activity   SPY volume so far today against its average daily volume (Yahoo)
  top of book  SPY best bid / ask and their sizes, and the spread (Yahoo, one venue's quote)
  option walls   the strikes with the most SPY open interest near the price: the largest put open
                 interest below the price ("put wall") and call open interest above it ("call wall"),
                 over the nearest expiries and the next two monthlies, converted to S&P points with the
                 current SPX / SPY ratio. Dealers' hedging tends to concentrate around these strikes;
                 they are where liquidity sits, not levels the price must respect.
"""
from __future__ import annotations

import logging

import pandas as pd

from .i18n import L

log = logging.getLogger(__name__)


def option_walls(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, band: float = 0.08) -> dict:
    """Largest open interest below (puts) and above (calls) ``spot``, within ``band`` of it."""
    c = calls.groupby("strike")["openInterest"].sum() if len(calls) else pd.Series(dtype=float)
    p = puts.groupby("strike")["openInterest"].sum() if len(puts) else pd.Series(dtype=float)
    c = c[(c.index > spot) & (c.index <= spot * (1 + band))].dropna()
    p = p[(p.index < spot) & (p.index >= spot * (1 - band))].dropna()
    out = {}
    if len(c) and c.max() > 0:
        out["call"] = (float(c.idxmax()), float(c.max()))
    if len(p) and p.max() > 0:
        out["put"] = (float(p.idxmax()), float(p.max()))
    return out


def fetch(spx: float | None = None, expiries: int = 5) -> dict:
    """Depth figures for the S&P; empty when Yahoo does not answer."""
    import yfinance as yf
    out: dict = {}
    try:
        tk = yf.Ticker("SPY")
        exps = list(tk.options or [])
        monthly = [e for e in exps if pd.Timestamp(e).weekday() == 4 and 15 <= pd.Timestamp(e).day <= 21][:2]
        calls, puts, spot = [], [], None
        for e in dict.fromkeys(exps[:expiries] + monthly):
            oc = tk.option_chain(e)
            calls.append(oc.calls)
            puts.append(oc.puts)
            spot = (getattr(oc, "underlying", None) or {}).get("regularMarketPrice") or spot
        info = tk.info or {}
        spot = spot or info.get("regularMarketPrice")
        if spot:
            ratio = (spx / spot) if spx else 10.0
            walls = option_walls(pd.concat(calls) if calls else pd.DataFrame(), pd.concat(puts) if puts else pd.DataFrame(), float(spot))
            out["walls"] = {k: (v[0], v[0] * ratio, v[1]) for k, v in walls.items()}       # (SPY strike, S&P points, contracts)
            out["spot"], out["ratio"] = float(spot), ratio
        vol, avg = info.get("regularMarketVolume"), info.get("averageVolume")
        if vol and avg:
            out["activity"] = (float(vol), float(avg), float(vol) / float(avg))
        if info.get("bid") and info.get("ask"):
            out["book"] = (float(info["bid"]), int(info.get("bidSize") or 0), float(info["ask"]), int(info.get("askSize") or 0))
    except Exception as e:                     # no chain, rate limit, network: no depth this time
        log.info("depth unavailable: %s", type(e).__name__)
    return out


def line(d: dict | None) -> str:
    """One line for mail: activity, spread, option walls in S&P points."""
    if not d:
        return ""
    parts = []
    if d.get("activity"):
        v, a, r = d["activity"]
        parts.append(L(f"SPY 今日成交 {v / 1e6:.0f}M 股，为平均的 {r:.1f} 倍（{'活跃' if r >= 1.3 else '清淡' if r < 0.8 else '平常'}）",
                       f"SPY volume today {v / 1e6:.0f}M shares, {r:.1f}x its average ({'busy' if r >= 1.3 else 'thin' if r < 0.8 else 'ordinary'})"))
    if d.get("book"):
        b, bs, a, as_ = d["book"]
        parts.append(L(f"买卖价差 {a - b:.2f} 美元", f"bid-ask spread ${a - b:.2f}"))
    w = d.get("walls") or {}
    if w.get("put") or w.get("call"):
        bits = []
        if w.get("put"):
            bits.append(L(f"下方看跌持仓最集中 ≈{w['put'][1]:,.0f} 点（SPY {w['put'][0]:g}，{w['put'][2] / 1e3:.0f}k 张）",
                          f"largest put open interest below ≈{w['put'][1]:,.0f} (SPY {w['put'][0]:g}, {w['put'][2] / 1e3:.0f}k contracts)"))
        if w.get("call"):
            bits.append(L(f"上方看涨持仓最集中 ≈{w['call'][1]:,.0f} 点（SPY {w['call'][0]:g}，{w['call'][2] / 1e3:.0f}k 张）",
                          f"largest call open interest above ≈{w['call'][1]:,.0f} (SPY {w['call'][0]:g}, {w['call'][2] / 1e3:.0f}k contracts)"))
        parts.append(L("；", "; ").join(bits))
    return L("交易深度：", "Market depth: ") + L("；", "; ").join(parts) if parts else ""
