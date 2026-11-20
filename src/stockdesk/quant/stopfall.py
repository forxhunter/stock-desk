"""Stop-falling pattern (止跌形态): a decline that has stopped, by explicit formulas.

Notation for bar t: O, H, L, C, V; ATR = Wilder ATR(14); SMA5 = 5-bar simple average of C. Everything
is causal (bars up to t only). Defaults are in ``DEFAULTS``.

The low of the leg:   t* = the last bar in (t-20, t] with L(t*) = min L over that window.     ("P1")

The pattern is FORMED at t when all four hold:

  D  a decline came first      max H over [t*-20, t*)  -  L(t*)   >=  3 x ATR(t*)
                               and  L(t*) <= min L over [t*-20, t*)      (a new low of the decline when it was
                               made, not a dip during the recovery from an older low)
  S1 the low holds             t - t* >= 3   and   C(t) > L(t*)
                               (by the choice of t*, no later bar has a low at or under L(t*))
  S2 a reversal structure      at least one of the four below
  S3 back above the average    C(t) > SMA5(t)   and   SMA5(t) > SMA5(t-1)

Structures for S2 (each searched on the bars after t*):

  123   1-2-3 bottom (Sperandeo).  P2 = the highest H of the first rally off the low, with
        P2 - L(t*) >= 1 x ATR; then a pull-back of at least 0.5 x ATR below P2 whose low
        P3 > L(t*) (a higher low); confirmed by a close C(s) > P2. Still valid while C(t) > P3.
  2B    failed breakdown / spring (Sperandeo 2B, Wyckoff).  L0 = min L over [t*-40, t*-5];
        L(t*) < L0 (the old low was undercut), some C(s) > L0 with t* <= s <= t*+3 (reclaimed
        within three bars), and C(t) > L0.
  K     reversal candle at the low, confirmed.  Bar k in {t*, t*+1} with range R = H-L >= 0.5 x ATR is
          a hammer:            lower shadow >= 2 x body, lower shadow >= 0.5 R, upper shadow <= 0.25 R
          or a bullish engulfing: C(k-1) < O(k-1), C(k) > O(k), O(k) <= C(k-1), C(k) >= O(k-1)
        and a later close C(s) > H(k).
  FT    follow-through day (O'Neil).  A bar s >= t*+3 (day 4 or later of the attempt, the low being
        day 1) with C(s)/C(s-1) - 1 >= max(1.5 %, 0.8 x ATR/C) on volume V(s) > V(s-1).

Void: a close under L(t*). That makes a new low, t* moves to it, and the count starts again.

None of this predicts that the low is in. It states precisely which signs of a stop are present;
``record`` counts what followed the same signal in the series' own history.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..i18n import L
from . import indicators as ind

DEFAULTS: dict = {"lookback": 20, "leg": 20, "min_drop_atr": 3.0, "hold": 3, "rally_atr": 1.0, "pullback_atr": 0.5,
                  "prior_window": 40, "prior_gap": 5, "reclaim": 3, "candle_min_atr": 0.5, "ft_day": 4, "ft_pct": 1.5,
                  "ft_atr": 0.8, "ma": 5}

NAMES = {"123": L("1-2-3 底（低点抬高后收盘突破反弹高点）", "1-2-3 bottom (higher low, then a close above the rally high)"),
         "2b": L("2B 假跌破（跌破前低后三天内收回）", "2B failed breakdown (old low undercut, reclaimed within three bars)"),
         "candle": L("低点反转 K 线并获确认", "reversal candle at the low, confirmed"),
         "follow": L("放量跟进日", "follow-through day on higher volume")}


def params(over: dict | None = None) -> dict:
    p = dict(DEFAULTS)
    for k, v in (over or {}).items():
        if k in p and v is not None:
            p[k] = type(DEFAULTS[k])(v)
    return p


@dataclass
class StopFall:
    ok: bool = False                 # enough data to judge
    formed: bool = False
    declined: bool = False           # D
    holds: bool = False              # S1
    structure: bool = False          # S2
    above_ma: bool = False           # S3
    structures: list[str] = field(default_factory=list)      # keys of the structures that hold
    low: float = float("nan")        # L(t*)
    low_date: pd.Timestamp | None = None
    low_i: int = -1
    age: int = 0                     # t - t*
    peak: float = float("nan")
    drop_atr: float = float("nan")
    atr: float = float("nan")
    close: float = float("nan")
    asof: pd.Timestamp | None = None
    formed_on: pd.Timestamp | None = None                    # first bar on which this leg's pattern was formed
    info: dict = field(default_factory=dict)                 # numbers behind the structures
    checks: list[tuple[str, bool, str]] = field(default_factory=list)    # (key, passed, detail)

    @property
    def missing(self) -> list[str]:
        return [d for _, ok, d in self.checks if not ok]


def _arrays(bars: pd.DataFrame, p: dict):
    o, h, lo, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = bars["volume"].to_numpy(float) if "volume" in bars else np.zeros(len(c))
    atr = ind.atr(bars["high"], bars["low"], bars["close"]).to_numpy(float)
    sma = ind.sma(bars["close"], int(p["ma"])).to_numpy(float)
    return o, h, lo, c, v, atr, sma


def _eval(a, t: int, p: dict) -> dict | None:
    """The four conditions on the bars up to ``t``; ``None`` when there is not enough data."""
    o, h, lo, c, v, atr, sma = a
    n = int(p["lookback"])
    if t < n + int(p["leg"]) or math.isnan(sma[t]) or math.isnan(sma[t - 1]):
        return None
    w = lo[t - n + 1:t + 1]
    i = t - n + 1 + int(np.flatnonzero(w == w.min())[-1])
    A = atr[i]
    if not (A == A and A > 0):
        return None
    low = lo[i]
    peak = float(h[max(0, i - int(p["leg"])):i].max())
    floor = float(lo[max(0, i - int(p["leg"])):i].min())
    out = {"i": i, "low": low, "age": t - i, "atr": A, "peak": peak, "drop_atr": (peak - low) / A,
           "declined": bool((peak - low) >= p["min_drop_atr"] * A and low <= floor),
           "holds": (t - i) >= int(p["hold"]) and c[t] > low,
           "above_ma": bool(c[t] > sma[t] and sma[t] > sma[t - 1]), "sma": sma[t], "structures": [], "info": {}}

    # 1-2-3 bottom
    R, r_i, phase, p2, p3, conf = -math.inf, -1, 0, math.nan, math.nan, -1
    for s in range(i + 1, t + 1):
        if phase == 0:
            if h[s] > R:
                R, r_i = h[s], s
            elif R - low >= p["rally_atr"] * A and lo[s] <= R - p["pullback_atr"] * A:
                phase, p2, p3 = 1, R, lo[s]
        else:
            p3 = min(p3, lo[s])
            if c[s] > p2:
                conf = s
                break
    if conf >= 0 and p3 > low and c[t] > p3:
        out["structures"].append("123")
        out["info"]["123"] = (p2, p3, conf)

    # 2B: an old low undercut and reclaimed
    a0, b0 = max(0, i - int(p["prior_window"])), i - int(p["prior_gap"])
    if b0 > a0:
        l0 = float(lo[a0:b0 + 1].min())
        back = [s for s in range(i, min(i + int(p["reclaim"]), t) + 1) if c[s] > l0]
        if low < l0 and back and c[t] > l0:
            out["structures"].append("2b")
            out["info"]["2b"] = (l0, back[0])

    # reversal candle at the low, confirmed by a close above its high
    for k in (i, i + 1):
        if k > t or k < 1:
            continue
        rng = h[k] - lo[k]
        if rng < p["candle_min_atr"] * A:
            continue
        body, lower, upper = abs(c[k] - o[k]), min(o[k], c[k]) - lo[k], h[k] - max(o[k], c[k])
        hammer = lower >= 2 * body and lower >= 0.5 * rng and upper <= 0.25 * rng
        engulf = c[k - 1] < o[k - 1] and c[k] > o[k] and o[k] <= c[k - 1] and c[k] >= o[k - 1]
        if hammer or engulf:
            ok = [s for s in range(k + 1, t + 1) if c[s] > h[k]]
            if ok:
                out["structures"].append("candle")
                out["info"]["candle"] = ("hammer" if hammer else "engulf", k, h[k], ok[0])
                break

    # follow-through day
    for s in range(i + int(p["ft_day"]) - 1, t + 1):
        if s < 1 or not c[s - 1]:
            continue
        gain = c[s] / c[s - 1] - 1
        if gain >= max(p["ft_pct"] / 100.0, p["ft_atr"] * A / c[s - 1]) and v[s] > v[s - 1] > 0:
            out["structures"].append("follow")
            out["info"]["follow"] = (s, gain, v[s] / v[s - 1])
            break

    out["structure"] = bool(out["structures"])
    out["formed"] = bool(out["declined"] and out["holds"] and out["structure"] and out["above_ma"])
    return out


def state(bars: pd.DataFrame, over: dict | None = None) -> StopFall:
    """The pattern on the latest bar, with every condition spelled out."""
    p = params(over)
    st = StopFall()
    if bars is None or len(bars) < int(p["lookback"]) + int(p["leg"]) + 16:
        return st
    a = _arrays(bars, p)
    t = len(bars) - 1
    e = _eval(a, t, p)
    if e is None:
        return st
    idx = bars.index
    d = lambda k: f"{idx[k]:%m-%d}"
    st.ok, st.asof, st.close = True, idx[t], float(a[3][t])
    st.low, st.low_i, st.low_date, st.age = float(e["low"]), e["i"], idx[e["i"]], e["age"]
    st.peak, st.drop_atr, st.atr = e["peak"], float(e["drop_atr"]), float(e["atr"])
    st.declined, st.holds, st.structure, st.above_ma, st.formed = e["declined"], e["holds"], e["structure"], e["above_ma"], e["formed"]
    st.structures, st.info = list(e["structures"]), e["info"]
    for s in range(e["i"] + int(p["hold"]), t + 1):          # the first bar on which this leg's pattern stood
        es = _eval(a, s, p)
        if es is not None and es["i"] == e["i"] and es["formed"]:
            st.formed_on = idx[s]
            break
    n = int(p["lookback"])
    st.checks.append(("declined", st.declined, L(
        f"先有一段下跌：低点 {st.low:,.2f}（{d(e['i'])}）比此前 {int(p['leg'])} 日最高 {st.peak:,.2f} 低 {st.drop_atr:.1f} 个 ATR（门槛 {p['min_drop_atr']:g}）",
        f"A decline came first: the low {st.low:,.2f} ({d(e['i'])}) is {st.drop_atr:.1f} ATR under the highest high of the {int(p['leg'])} bars before it, "
        f"{st.peak:,.2f} (threshold {p['min_drop_atr']:g})")))
    st.checks.append(("holds", st.holds, L(
        f"低点守住：{n} 日最低点已过去 {st.age} 个交易日没有再创新低（门槛 {int(p['hold'])} 个），收盘 {st.close:,.2f} 高于低点",
        f"The low holds: {st.age} session(s) since the {n}-bar low without a new low (threshold {int(p['hold'])}), close {st.close:,.2f} above it")))
    if st.structures:
        parts = []
        for key in st.structures:
            i_ = st.info[key]
            if key == "123":
                parts.append(L(f"1-2-3 底：反弹高点 {i_[0]:,.2f}，回踩低点 {i_[1]:,.2f} 高于前低，{d(i_[2])} 收盘突破反弹高点",
                               f"1-2-3 bottom: rally high {i_[0]:,.2f}, pull-back low {i_[1]:,.2f} above the low, close above the rally high on {d(i_[2])}"))
            elif key == "2b":
                parts.append(L(f"2B 假跌破：跌破前低 {i_[0]:,.2f} 后，{d(i_[1])} 收盘收回",
                               f"2B failed breakdown: the earlier low {i_[0]:,.2f} was undercut and reclaimed by the close of {d(i_[1])}"))
            elif key == "candle":
                parts.append(L(f"{'锤子线' if i_[0] == 'hammer' else '看涨吞没'}（{d(i_[1])}），{d(i_[3])} 收盘站上该 K 线高点 {i_[2]:,.2f}",
                               f"{'hammer' if i_[0] == 'hammer' else 'bullish engulfing'} ({d(i_[1])}), confirmed by the close of {d(i_[3])} above its high {i_[2]:,.2f}"))
            else:
                parts.append(L(f"放量跟进日：{d(i_[0])} 上涨 {i_[1] * 100:.1f}%，成交量是前一日的 {i_[2]:.1f} 倍",
                               f"follow-through day: {d(i_[0])} up {i_[1] * 100:.1f}% on {i_[2]:.1f}x the previous day's volume"))
        st.checks.append(("structure", True, L("反转结构：", "Reversal structure: ") + L("；", "; ").join(parts)))
    else:
        st.checks.append(("structure", False, L(
            "反转结构：四种都还没有出现（1-2-3 底、2B 假跌破、低点反转 K 线并获确认、放量跟进日）",
            "Reversal structure: none of the four yet (1-2-3 bottom, 2B failed breakdown, confirmed reversal candle at the low, follow-through day)")))
    sma_t, sma_p = float(a[6][t]), float(a[6][t - 1])
    st.checks.append(("above_ma", st.above_ma, L(
        f"站回短期均线：收盘 {st.close:,.2f} {'高于' if st.close > sma_t else '低于'} {int(p['ma'])} 日均线 {sma_t:,.2f}，均线{'已拐头向上' if sma_t > sma_p else '仍在下行'}",
        f"Back above the short average: close {st.close:,.2f} {'above' if st.close > sma_t else 'below'} the {int(p['ma'])}-bar average {sma_t:,.2f}, "
        f"which is {'turning up' if sma_t > sma_p else 'still falling'}")))
    return st


def signals(bars: pd.DataFrame, over: dict | None = None) -> list[tuple[int, int]]:
    """(bar on which a pattern was first formed, bar of its low), for every leg in ``bars``."""
    p = params(over)
    if bars is None or len(bars) < 60:
        return []
    a = _arrays(bars, p)
    out, last = [], -1
    for t in range(int(p["lookback"]) + int(p["leg"]) + 15, len(bars)):      # the same minimum history as state()
        e = _eval(a, t, p)
        if e is not None and e["formed"] and e["i"] != last:
            out.append((t, e["i"]))
            last = e["i"]
    return out


@dataclass
class Record:
    n: int = 0
    since: pd.Timestamp | None = None
    fwd: dict = field(default_factory=dict)      # horizon -> (count, median, share up)
    base: dict = field(default_factory=dict)     # horizon -> (median, share up) over all bars
    failed: float | None = None                  # share with a close under the leg's low within 21 bars
    last: list = field(default_factory=list)     # [(date, 21-bar return or None)]


def record(bars: pd.DataFrame, over: dict | None = None, horizons=(5, 10, 21), upto: int | None = None) -> Record | None:
    """What followed the earlier signals in ``bars`` (those formed before bar ``upto``; default: all but the last bar)."""
    sig = [x for x in signals(bars, over) if x[0] < (len(bars) - 1 if upto is None else upto)]
    if not sig:
        return None
    c, lo = bars["close"].to_numpy(float), bars["low"].to_numpy(float)
    n = len(c)
    r = Record(n=len(sig), since=bars.index[0])
    for h in horizons:
        got = np.array([c[t + h] / c[t] - 1 for t, _ in sig if t + h < n])
        if len(got):
            r.fwd[h] = (len(got), float(np.median(got)), float((got > 0).mean()))
        every = c[h:] / c[:-h] - 1
        every = every[np.isfinite(every)]
        if len(every):
            r.base[h] = (float(np.median(every)), float((every > 0).mean()))
    fails = [bool((c[t + 1:t + 22] < lo[i]).any()) for t, i in sig if t + 21 < n]
    r.failed = float(np.mean(fails)) if fails else None
    r.last = [(bars.index[t], float(c[t + 21] / c[t] - 1) if t + 21 < n else None) for t, _ in sig[-3:]]
    return r
