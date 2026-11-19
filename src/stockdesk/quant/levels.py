"""Support zones below the current price, found by a fixed rule (no forecasting).

Method:
  1. take the last ``lookback`` daily bars (default one year);
  2. find the turning points: swing lows, and swing highs (an old peak often acts as support once
     price is above it) - a bar whose low / high is the extreme of the ``pivot`` bars on each side;
  3. keep the ones below the current price and merge neighbours that are within ``tol_atr`` x ATR
     of each other into one zone;
  4. a zone counts when at least two turning points fall into it; the lowest swing low of the period
     is always kept.
Each zone is reported with its range, the number of turning points and one representative level
(the median). Other reasonable methods (volume profile, round numbers, moving averages) give
different levels; these are reference points, not predictions that price will hold there.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class Zone:
    level: float               # representative price: the median of the turning points in the zone
    low: float
    high: float
    touches: int
    last: pd.Timestamp         # date of the most recent turning point in the zone
    year_low: bool = False     # contains the lowest swing low of the period


def _pivots(arr: np.ndarray, k: int, low: bool) -> list[int]:
    out = []
    for i in range(k, len(arr) - k):
        w = arr[i - k:i + k + 1]
        if arr[i] == (w.min() if low else w.max()):
            out.append(i)
    return out


def support_zones(bars: pd.DataFrame, price: float | None = None, lookback: int = 252, pivot: int = 5,
                  tol_atr: float = 1.0, min_touches: int = 2) -> list[Zone]:
    """Support zones below ``price`` (default: the last close), nearest first."""
    if bars is None or len(bars) < max(60, pivot * 4):
        return []
    sub = bars.tail(lookback)
    lo, hi = sub["low"].to_numpy(float), sub["high"].to_numpy(float)
    px = float(price) if price else float(sub["close"].iloc[-1])
    atr = float(ind.atr(bars["high"], bars["low"], bars["close"]).iloc[-1])
    if not atr or atr != atr:
        return []
    lows = [(float(lo[i]), sub.index[i]) for i in _pivots(lo, pivot, True)]
    pts = [p for p in lows if p[0] < px] + \
          [(float(hi[i]), sub.index[i]) for i in _pivots(hi, pivot, False) if hi[i] < px]
    if not pts:
        return []
    floor = min((p[0] for p in lows if p[0] < px), default=None)
    pts.sort(key=lambda p: -p[0])
    groups: list[list[tuple[float, pd.Timestamp]]] = [[pts[0]]]
    for p in pts[1:]:
        if groups[-1][-1][0] - p[0] <= tol_atr * atr:
            groups[-1].append(p)
        else:
            groups.append([p])
    zones = []
    for g in groups:
        vals = [p[0] for p in g]
        is_floor = floor is not None and floor in vals
        if len(g) >= min_touches or is_floor:
            zones.append(Zone(float(np.median(vals)), min(vals), max(vals), len(g), max(p[1] for p in g), is_floor))
    return zones
