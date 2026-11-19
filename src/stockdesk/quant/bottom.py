"""Rule-based "bottoming" check (筑底信号) for a stock that has fallen a lot.

It does not predict a bottom. It counts how many classic signs of a base are present at the latest
close; when enough of them line up at once the program raises an alert. All conditions are causal
(they use data up to the latest bar only) and each one is reported with its numbers.

Precondition: the stock is at least ``min_drawdown_pct`` below its 52-week high.
Conditions (defaults in ``DEFAULTS``):
  1. low_holds     the lowest low of the last 60 bars is at least 5 bars old and has not been broken
  2. higher_low    the latest confirmed swing low is above the one before it
  3. above_sma20   two consecutive closes above the 20-day average
  4. rsi_recovery  RSI(14) was below 30 within the last 30 bars and is now above 45
  5. macd_cross    MACD line above its signal line, having crossed up within the last 15 bars
  6. rebound       the close is more than 2 x ATR above that 60-bar low
Volume confirmation (a base without volume behind it is much weaker):
  7. vol_climax    a selling climax: within 3 bars of that low, a day traded >= 2 x its 50-day average volume
  8. vol_follow    a follow-through day: 4+ bars after the low, a close up >= 1.5 % on volume above both the
                   50-day average and the previous day
  9. vol_accum     accumulation: since the low, volume on up days is >= 1.3 x the volume on down days
A base is "clear" when at least ``min_score`` of the six price conditions AND at least
``min_volume_score`` of the three volume conditions hold together. Without usable volume data the
check never fires.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..i18n import L
from . import indicators as ind

DEFAULTS: dict = {
    "min_drawdown_pct": 20.0,
    "lookback": 60,
    "low_age": 5,
    "pivot": 5,            # bars on each side that make a swing low
    "rsi_low": 30.0,
    "rsi_now": 45.0,
    "rsi_window": 30,
    "macd_window": 15,
    "rebound_atr": 2.0,
    "min_score": 5,
    "climax_mult": 2.0,
    "follow_pct": 1.5,
    "accum_ratio": 1.3,
    "min_volume_score": 2,
}

LABELS = {
    "low_holds": L("低点守住", "Low holds"), "higher_low": L("低点抬高", "Higher low"),
    "above_sma20": L("站上 20 日线", "Above 20-day MA"),
    "rsi_recovery": L("RSI 从超卖回升", "RSI recovering from oversold"), "macd_cross": L("MACD 金叉", "MACD bullish cross"),
    "rebound": L("脱离低点", "Off the low"),
    "vol_climax": L("放量见底", "Volume climax at the low"), "vol_follow": L("放量上涨确认", "Volume-confirmed rally"),
    "vol_accum": L("涨时量大于跌时量", "Volume higher on up days than down days"),
}
VOLUME_KEYS = ("vol_climax", "vol_follow", "vol_accum")


@dataclass
class BottomState:
    ok: bool = False                 # enough data
    eligible: bool = False           # fallen far enough for "bottoming" to mean anything
    triggered: bool = False
    score: int = 0                   # price conditions that hold (of 6)
    min_score: int = 5
    vol_score: int = 0               # volume conditions that hold (of 3)
    min_vol_score: int = 2
    has_volume: bool = True
    drawdown_pct: float = float("nan")
    base_low: float = float("nan")
    base_low_date: pd.Timestamp | None = None
    close: float = float("nan")
    asof: pd.Timestamp | None = None
    checks: list[tuple[str, bool, str]] = field(default_factory=list)   # (key, passed, detail)

    @property
    def passed(self) -> list[str]:
        return [LABELS[k] for k, ok, _ in self.checks if ok]

    @property
    def summary(self) -> str:
        return L(f"价格 {self.score}/6、成交量 {self.vol_score}/3", f"Price {self.score}/6, volume {self.vol_score}/3")


def params(over: dict | None = None) -> dict:
    p = dict(DEFAULTS)
    for k, v in (over or {}).items():
        if k in p and v is not None:
            p[k] = type(DEFAULTS[k])(v)
    return p


def swing_lows(low: np.ndarray, k: int) -> list[int]:
    """Indices of confirmed swing lows: the lowest low within ``k`` bars on both sides."""
    out = []
    for i in range(k, len(low) - k):
        w = low[i - k:i + k + 1]
        if low[i] == w.min() and (not out or i - out[-1] > k):
            out.append(i)
    return out


def bottom_state(bars: pd.DataFrame, over: dict | None = None) -> BottomState:
    p = params(over)
    st = BottomState(min_score=int(p["min_score"]), min_vol_score=int(p["min_volume_score"]))
    n = int(p["lookback"])
    if bars is None or len(bars) < max(n + 30, 120):
        return st
    c, h, l = bars["close"], bars["high"], bars["low"]
    lo = l.to_numpy(float)
    st.ok, st.asof, st.close = True, bars.index[-1], float(c.iloc[-1])
    st.drawdown_pct = float(c.iloc[-1] / c.tail(252).max() - 1) * 100
    st.eligible = st.drawdown_pct <= -p["min_drawdown_pct"]

    win = lo[-n:]
    i_low = int(np.argmin(win))
    age = n - 1 - i_low
    st.base_low, st.base_low_date = float(win[i_low]), bars.index[len(lo) - n + i_low]
    holds = age >= p["low_age"]
    st.checks.append(("low_holds", holds, L(f"{n} 日最低 {st.base_low:,.2f}（{st.base_low_date:%m-%d}），"
                                            f"已 {age} 个交易日没有再创新低",
                                            f"{n}-day low {st.base_low:,.2f} ({st.base_low_date:%m-%d}), "
                                            f"no new low for {age} trading days")))

    piv = swing_lows(lo[-(n * 2):], int(p["pivot"]))
    base = len(lo) - min(len(lo), n * 2)
    if len(piv) >= 2:
        a, b = lo[-(n * 2):][piv[-2]], lo[-(n * 2):][piv[-1]]
        st.checks.append(("higher_low", bool(b > a),
                          L(f"最近两个波段低点 {a:,.2f}（{bars.index[base + piv[-2]]:%m-%d}）→ "
                            f"{b:,.2f}（{bars.index[base + piv[-1]]:%m-%d}）",
                            f"Last two swing lows {a:,.2f} ({bars.index[base + piv[-2]]:%m-%d}) → "
                            f"{b:,.2f} ({bars.index[base + piv[-1]]:%m-%d})")))
    else:
        st.checks.append(("higher_low", False, L("还没有形成两个可比较的波段低点",
                                                 "No two comparable swing lows have formed yet")))

    s20 = ind.sma(c, 20)
    above = bool(c.iloc[-1] > s20.iloc[-1] and c.iloc[-2] > s20.iloc[-2])
    st.checks.append(("above_sma20", above, L(f"收盘 {st.close:,.2f}，20 日均线 {float(s20.iloc[-1]):,.2f}",
                                              f"Close {st.close:,.2f}, 20-day average {float(s20.iloc[-1]):,.2f}")))

    rsi = ind.rsi(c)
    rmin = float(rsi.tail(int(p["rsi_window"])).min())
    rnow = float(rsi.iloc[-1])
    st.checks.append(("rsi_recovery", bool(rmin < p["rsi_low"] and rnow > p["rsi_now"]),
                      L(f"RSI 近 {int(p['rsi_window'])} 日最低 {rmin:.0f}，现在 {rnow:.0f}",
                        f"RSI low over the last {int(p['rsi_window'])} days {rmin:.0f}, now {rnow:.0f}")))

    m = ind.macd(c)
    diff = (m["macd"] - m["signal"]).tail(int(p["macd_window"]) + 1)
    crossed = bool(diff.iloc[-1] > 0 and (diff.iloc[:-1] <= 0).any())
    st.checks.append(("macd_cross", crossed,
                      L(f"MACD {float(m['macd'].iloc[-1]):+.2f} / 信号线 {float(m['signal'].iloc[-1]):+.2f}",
                        f"MACD {float(m['macd'].iloc[-1]):+.2f} / signal line {float(m['signal'].iloc[-1]):+.2f}")))

    atr = float(ind.atr(h, l, c).iloc[-1])
    dist = (st.close - st.base_low) / atr if atr and not math.isnan(atr) else 0.0
    st.checks.append(("rebound", bool(dist > p["rebound_atr"]),
                      L(f"收盘高于低点 {dist:.1f} × ATR（ATR {atr:,.2f}）",
                        f"Close is {dist:.1f} × ATR above the low (ATR {atr:,.2f})")))

    st.score = sum(1 for k, ok, _ in st.checks if ok and k not in VOLUME_KEYS)

    # ---- volume confirmation
    v = bars["volume"].astype(float) if "volume" in bars else pd.Series(0.0, index=bars.index)
    avg50 = v.rolling(50, min_periods=30).mean()
    st.has_volume = bool((v.tail(n) > 0).mean() > 0.8 and avg50.iloc[-1] > 0)
    if not st.has_volume:
        for key in VOLUME_KEYS:
            st.checks.append((key, False, L("没有可用的成交量数据", "No usable volume data")))
    else:
        pos = len(lo) - n + i_low                       # bar index of the base low
        ratio = (v / avg50).to_numpy()
        w = slice(max(pos - 3, 0), min(pos + 4, len(v)))
        peak = float(np.nanmax(ratio[w])) if len(ratio[w]) else 0.0
        st.checks.append(("vol_climax", bool(peak >= p["climax_mult"]),
                          L(f"低点前后 3 天内最大成交量是 50 日均量的 {peak:.1f} 倍（门槛 {p['climax_mult']:g} 倍）",
                            f"Peak volume within 3 days of the low was {peak:.1f}x the 50-day average "
                            f"(threshold {p['climax_mult']:g}x)")))
        chg = c.pct_change().to_numpy() * 100
        vv = v.to_numpy()
        ft = [i for i in range(pos + 4, len(v)) if chg[i] >= p["follow_pct"] and ratio[i] > 1.0 and vv[i] > vv[i - 1]]
        st.checks.append(("vol_follow", bool(ft),
                          L(f"{bars.index[ft[-1]]:%m-%d} 上涨 {chg[ft[-1]]:.1f}%，成交量是均量的 {ratio[ft[-1]]:.1f} 倍",
                            f"{bars.index[ft[-1]]:%m-%d} up {chg[ft[-1]]:.1f}%, volume {ratio[ft[-1]]:.1f}x the average") if ft
                          else L(f"低点后第 4 天起，还没有出现涨幅 ≥{p['follow_pct']:g}% 且放量的一天",
                                 f"From the 4th day after the low, no day yet with a gain of ≥{p['follow_pct']:g}% "
                                 f"on higher volume")))
        since = slice(pos + 1, len(v))
        up = float(vv[since][chg[since] > 0].sum())
        dn = float(vv[since][chg[since] < 0].sum())
        days = len(vv[since])
        ud = up / dn if dn > 0 else (float("inf") if up > 0 else 0.0)
        st.checks.append(("vol_accum", bool(days >= 5 and ud >= p["accum_ratio"]),
                          L(f"低点以来 {days} 天，上涨日成交量 / 下跌日成交量 = {ud:.2f}（门槛 {p['accum_ratio']:g}）",
                            f"{days} days since the low, up-day volume / down-day volume = {ud:.2f} "
                            f"(threshold {p['accum_ratio']:g})")
                          if days >= 5 else L(f"低点以来只有 {days} 天，样本不足",
                                              f"Only {days} days since the low; sample too small")))
    st.vol_score = sum(1 for k, ok, _ in st.checks if ok and k in VOLUME_KEYS)
    st.triggered = (st.eligible and holds and st.score >= st.min_score
                    and st.has_volume and st.vol_score >= st.min_vol_score)
    return st


def history(bars: pd.DataFrame, over: dict | None = None, step: int = 1) -> pd.Series:
    """True on the days the check would have fired (for sanity checks; slow, not used by the UI)."""
    out = {}
    start = max(int(params(over)["lookback"]) + 30, 120)
    for i in range(start, len(bars) + 1, step):
        out[bars.index[i - 1]] = bottom_state(bars.iloc[:i], over).triggered
    return pd.Series(out, dtype=bool)
