"""Drawdown scenarios for an index: the rungs below, what followed each rung in the past, and how the
holdings would move with the index.

An EPISODE starts on the first close that is ``first`` % (default 10) or more under the highest close
of the previous 252 sessions (the peak P), and ends on the first close back at or above P. Its depth
is the lowest close of the episode against P. A RUNG r (10, 15, 20 ... %) is "reached" on the first
close at or under P x (1 - r/100).

For every rung the finished episodes give: how many reached it, the share of those that went on to
the next rung, how much further the index fell from the day the rung was reached to the low of the
episode, how long that took, and the returns from that day. This is the record of one index. The
episodes are few (a few dozen in seven decades) and differ widely: a count, not a forecast.

The stress estimate is arithmetic: a holding's beta to the index (slope of its returns on the
index's) times a further move of the index. Betas are measured in ordinary times and usually rise
in a sell-off, so the estimate is more likely too small than too large.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

RUNGS = (10, 15, 20, 25, 30, 40, 50)
HORIZONS = (21, 63, 126, 252)


@dataclass
class Episode:
    start: int                       # bar of the first close at or under the first rung
    peak: float
    peak_i: int
    trough: float
    trough_i: int
    end: int | None                  # bar of the first close back at the peak; None = still open
    cross: dict = field(default_factory=dict)       # rung -> bar on which it was reached

    @property
    def depth(self) -> float:
        return (self.trough / self.peak - 1) * 100


@dataclass
class RungStat:
    rung: int
    n: int = 0                       # finished episodes that reached this rung
    next_rung: int | None = None
    deeper: float | None = None      # share of them that went on to the next rung
    more: tuple | None = None        # (median, worst decile) further fall from the rung day to the low, in %
    to_low: float | None = None      # median sessions from the rung day to the low
    to_peak: float | None = None     # median sessions from the rung day back to the old peak
    fwd: dict = field(default_factory=dict)         # horizon -> (count, median, share up, 10th percentile)


@dataclass
class Study:
    since: pd.Timestamp | None = None
    n: int = 0                       # finished episodes
    rungs: dict = field(default_factory=dict)       # rung -> RungStat
    current: Episode | None = None   # the open episode, if the index is in one
    dates: dict = field(default_factory=dict)       # rung -> date it was reached in the open episode
    depths: list = field(default_factory=list)      # depth of every finished episode, in %


def episodes(close: pd.Series, first: float = 10.0, rungs=RUNGS) -> list[Episode]:
    c = close.to_numpy(float)
    peak = close.rolling(252, min_periods=120).max().to_numpy(float)
    out: list[Episode] = []
    i, n = 0, len(c)
    while i < n:
        p = peak[i]
        if not (p == p) or c[i] > p * (1 - first / 100.0):
            i += 1
            continue
        lo = max(0, i - 252)
        ep = Episode(i, float(p), lo + int(np.argmax(c[lo:i + 1])), float(c[i]), i, None)
        j = i
        while j < n and c[j] < p:
            if c[j] < ep.trough:
                ep.trough, ep.trough_i = float(c[j]), j
            for r in rungs:
                if r not in ep.cross and c[j] <= p * (1 - r / 100.0):
                    ep.cross[r] = j
            j += 1
        ep.end = j if j < n else None
        out.append(ep)
        i = j if j > i else i + 1
    return out


def study(close: pd.Series, first: float = 10.0, rungs=RUNGS, horizons=HORIZONS) -> Study:
    close = close.dropna()
    st = Study(since=close.index[0] if len(close) else None)
    if len(close) < 400:
        return st
    c = close.to_numpy(float)
    eps = episodes(close, first, rungs)
    done = [e for e in eps if e.end is not None]
    st.n, st.depths = len(done), [e.depth for e in done]
    if eps and eps[-1].end is None:
        st.current = eps[-1]
        st.dates = {r: close.index[k] for r, k in eps[-1].cross.items()}
    for k, r in enumerate(rungs):
        hit = [e for e in done if r in e.cross]
        rs = RungStat(r, len(hit), rungs[k + 1] if k + 1 < len(rungs) else None)
        if hit:
            if rs.next_rung is not None:
                rs.deeper = float(np.mean([rs.next_rung in e.cross for e in hit]))
            more = np.array([(e.trough / c[e.cross[r]] - 1) * 100 for e in hit])
            rs.more = (float(np.median(more)), float(np.percentile(more, 10)))
            rs.to_low = float(np.median([e.trough_i - e.cross[r] for e in hit]))
            rs.to_peak = float(np.median([e.end - e.cross[r] for e in hit]))
            for h in horizons:
                got = np.array([c[e.cross[r] + h] / c[e.cross[r]] - 1 for e in hit if e.cross[r] + h < len(c)])
                if len(got):
                    rs.fwd[h] = (len(got), float(np.median(got)), float((got > 0).mean()), float(np.percentile(got, 10)))
        st.rungs[r] = rs
    return st


def ladder(depth_pct: float, rungs=RUNGS, count: int = 4) -> list[int]:
    """The rungs worth showing for an index that is ``depth_pct`` % under its peak: the one it is on and the next ones."""
    d = abs(min(depth_pct, 0.0))
    start = max((r for r in rungs if r <= d), default=rungs[0])
    k = rungs.index(start)
    return list(rungs[k:k + count])


@dataclass
class Exposure:
    symbol: str
    weight: float                    # share of the holdings' value
    beta: float | None
    kind: str                        # "daily" | "weekly": how the beta was measured
    price: float
    currency: str = "USD"


def beta(stock: pd.Series, index: pd.Series, weekly: bool = False, n: int = 120) -> float | None:
    """Slope of the stock's returns on the index's: daily over ``n`` sessions, or weekly over two years
    (for a stock that trades in other hours, where same-day returns do not line up)."""
    df = pd.concat([stock, index], axis=1, join="inner").dropna()
    if weekly:
        df = df.resample("W").last().dropna().pct_change().dropna().tail(104)
        need = 30
    else:
        df = df.pct_change().dropna().tail(n)
        need = 40
    if len(df) < need:
        return None
    var = float(df.iloc[:, 1].var())
    return float(df.iloc[:, 0].cov(df.iloc[:, 1]) / var) if var > 0 else None


def exposures(views, index_bars: pd.DataFrame | None) -> list[Exposure]:
    held = [v for v in views or [] if getattr(v, "value", 0) > 0 and v.bars is not None and v.price == v.price]
    total = sum(v.value for v in held)
    out = []
    for v in held:
        weekly = v.currency != "USD"
        b = beta(v.bars["close"], index_bars["close"], weekly) if index_bars is not None else None
        out.append(Exposure(v.symbol, v.value / total if total else 0.0, b, "weekly" if weekly else "daily", float(v.price), v.currency))
    out.sort(key=lambda e: -e.weight)
    return out


def stress(exps: list[Exposure], move: float) -> tuple[float | None, list[tuple[str, float | None, float | None]]]:
    """(estimated change of the holdings, [(symbol, estimated change, estimated price)]) for an index move (a fraction)."""
    rows, total, covered = [], 0.0, 0.0
    for e in exps:
        if e.beta is None:
            rows.append((e.symbol, None, None))
            continue
        m = max(e.beta * move, -0.95)
        rows.append((e.symbol, m, e.price * (1 + m)))
        total += e.weight * m
        covered += e.weight
    return (total if covered > 0 else None), rows
