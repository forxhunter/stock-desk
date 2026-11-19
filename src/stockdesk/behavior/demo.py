"""Synthetic sample executions for demo mode (clearly flagged ``demo=True``).

They are built on real price history so the behavioural detectors have something to show
before any broker is connected. They disappear as soon as real executions are synced.
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from ..i18n import L
from ..models import Trade

_NY = ZoneInfo("America/New_York")


def _ts(day: pd.Timestamp, hour: int = 11) -> float:
    return datetime.combine(day.date(), dtime(hour, 0), _NY).timestamp()


def make_demo_trades(bars: dict[str, pd.DataFrame], symbols: list[str], runup_days: int = 5) -> list[Trade]:
    syms = [s for s in symbols if s in bars and len(bars[s]) > 150][:4]
    out: list[Trade] = []
    if not syms:
        return out

    def add(sym: str, i: int, side: str, qty: float, hour: int = 11) -> None:
        b = bars[sym]
        day, px = b.index[i], float(b["close"].iloc[i])
        out.append(Trade(f"demo:{sym}:{day.date()}:{side}:{hour}", sym, side, qty, round(px, 2),
                         _ts(day, hour), L("演示", "Demo"), 0.0, True))

    # 1) a buy on the day with the strongest recent run-up, 2) a sell on the weakest day
    best = max(syms, key=lambda s: (bars[s]["close"].pct_change(runup_days).iloc[-30:]).max())
    chg = bars[best]["close"].pct_change(runup_days)
    add(best, int(chg.iloc[-30:].to_numpy().argmax()) - 30, "BUY", 10)
    worst = min(syms, key=lambda s: (bars[s]["close"].pct_change(runup_days).iloc[-30:]).min())
    chg = bars[worst]["close"].pct_change(runup_days)
    i_sell = int(chg.iloc[-30:].to_numpy().argmin()) - 30
    add(worst, i_sell - 80, "BUY", 15)
    add(worst, i_sell, "SELL", 15)
    # 3) older round trips: winners closed quickly, losers held for a long time
    for k, sym in enumerate(syms[:3]):
        c = bars[sym]["close"]
        if len(c) < 460:            # the sample round trips reach back ~420 bars
            continue
        base = -420 + k * 15
        for j in range(2):
            start = base + j * 110
            win_exit = next((i for i in range(start + 2, start + 15) if c.iloc[i] > c.iloc[start] * 1.02), None)
            if win_exit:
                add(sym, start, "BUY", 5, 10)
                add(sym, win_exit, "SELL", 5, 10)
            s2 = start + 20
            lose_exit = next((i for i in range(s2 + 40, s2 + 80) if c.iloc[i] < c.iloc[s2] * 0.97), None)
            if lose_exit:
                add(sym, s2, "BUY", 5, 14)
                add(sym, lose_exit, "SELL", 5, 14)
    return sorted(out, key=lambda t: t.ts)
