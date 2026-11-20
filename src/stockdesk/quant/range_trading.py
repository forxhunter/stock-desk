"""Phase-based range trading (阶段性震荡区间高抛低吸).

Idea: when a stock has been moving sideways for a while, its recent lows cluster around a
*support* level and its recent highs around a *resistance* level. Inside such a range the
rule buys near support and sells near resistance; once price closes clearly outside the
range (a breakout / breakdown), the range is considered broken and the rule stands aside.

Everything is causal. The range used on day t is measured on the ``lookback`` bars that end
on day t-1, so day t's close can be compared against it (that is how a breakout is seen).
Signals are generated at the close and the backtester fills them at the next open.

A range is "valid" only when all of these hold (defaults in ``DEFAULTS``):
  * support / resistance = the 10th percentile of lows / 90th percentile of highs, so one
    spike does not define the range;
  * width is between 3 and 12 ATR and at least 3 % of price (enough room to trade, but not
    a trend leg);
  * Kaufman efficiency ratio <= 0.30 (price wandered back and forth instead of trending);
  * linear-regression drift over the window <= 50 % of the range width (flat, not tilted);
  * price visited both the bottom zone and the top zone at least twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from ..i18n import L
from . import indicators as ind

DEFAULTS: dict = {
    "lookback": 60,          # bars used to measure the range (~3 months)
    "edge_quantile": 0.10,   # support = 10th pct of lows, resistance = 90th pct of highs
    "min_width_atr": 3.0,
    "max_width_atr": 12.0,
    "min_width_pct": 3.0,    # range must be at least 3 % of price wide
    "max_er": 0.30,          # Kaufman efficiency ratio ceiling (0 = pure chop, 1 = straight line)
    "max_drift": 0.50,       # regression drift over the window, as a fraction of the width
    "min_touches": 2,        # visits to each edge zone
    "zone": 0.20,            # bottom / top 20 % of the range = buy-low / sell-high zones
    "break_atr": 0.5,        # close beyond an edge by 0.5 ATR = range broken
    "max_hold": 40,          # time stop for the backtest (bars)
    "recent": 10,            # how far back to look for a just-broken range
}


def params(over: dict | None = None) -> dict:
    p = dict(DEFAULTS)
    for k, v in (over or {}).items():
        if k in p and v is not None:
            p[k] = type(DEFAULTS[k])(v)
    return p


def range_frame(bars: pd.DataFrame, p: dict | None = None) -> pd.DataFrame:
    """Per-bar range measurements (window ending the previous bar) and validity flags."""
    p = params(p)
    n = int(p["lookback"])
    idx = bars.index
    T = len(bars)
    cols = ["support", "resistance", "width", "width_pct", "width_atr", "atr", "er", "drift",
            "touch_lo", "touch_hi", "valid", "pos"]
    out = pd.DataFrame(np.nan, index=idx, columns=cols)
    out["valid"] = False
    if T < n + 2:
        return out
    hi = bars["high"].to_numpy(float)
    lo = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    atr = ind.atr(bars["high"], bars["low"], bars["close"]).shift(1).to_numpy(float)

    # windows ending at t-1 for t = n .. T-1  ->  window k covers k .. k+n-1, used on bar k+n
    wl = sliding_window_view(lo, n)[: T - n]
    wh = sliding_window_view(hi, n)[: T - n]
    wc = sliding_window_view(c, n)[: T - n]
    q = float(p["edge_quantile"])
    sup = np.quantile(wl, q, axis=1)
    res = np.quantile(wh, 1.0 - q, axis=1)
    width = res - sup
    mid = (res + sup) / 2.0

    z = float(p["zone"])
    in_lo = wl <= (sup + z * width)[:, None]
    in_hi = wh >= (res - z * width)[:, None]
    touch_lo = in_lo[:, 0].astype(int) + (in_lo[:, 1:] & ~in_lo[:, :-1]).sum(axis=1)
    touch_hi = in_hi[:, 0].astype(int) + (in_hi[:, 1:] & ~in_hi[:, :-1]).sum(axis=1)

    x = np.arange(n, dtype=float)
    xm = x - x.mean()
    slope = (wc - wc.mean(axis=1, keepdims=True)) @ xm / (xm @ xm)
    with np.errstate(divide="ignore", invalid="ignore"):
        drift = np.abs(slope * (n - 1)) / width

        # efficiency ratio over the same n bars (needs the close before the window)
        net = np.full(T - n, np.nan)
        path = np.full(T - n, np.nan)
        d = np.abs(np.diff(c))                       # d[i] = |c[i+1] - c[i]|
        wd = sliding_window_view(d, n - 1)           # window k: |moves| inside window k
        path[:] = wd[: T - n].sum(axis=1)
        net[:] = np.abs(wc[:, -1] - wc[:, 0])
        er = net / path

    s = slice(n, T)
    out.iloc[s, out.columns.get_loc("support")] = sup
    out.iloc[s, out.columns.get_loc("resistance")] = res
    out.iloc[s, out.columns.get_loc("width")] = width
    out.iloc[s, out.columns.get_loc("width_pct")] = width / mid * 100.0
    out.iloc[s, out.columns.get_loc("atr")] = atr[n:]
    out.iloc[s, out.columns.get_loc("width_atr")] = width / atr[n:]
    out.iloc[s, out.columns.get_loc("er")] = er
    out.iloc[s, out.columns.get_loc("drift")] = drift
    out.iloc[s, out.columns.get_loc("touch_lo")] = touch_lo
    out.iloc[s, out.columns.get_loc("touch_hi")] = touch_hi
    with np.errstate(divide="ignore", invalid="ignore"):
        out.iloc[s, out.columns.get_loc("pos")] = (c[n:] - sup) / width

    ok = (out["width_atr"].between(p["min_width_atr"], p["max_width_atr"])
          & (out["width_pct"] >= p["min_width_pct"])
          & (out["er"] <= p["max_er"])
          & (out["drift"] <= p["max_drift"])
          & (out["touch_lo"] >= p["min_touches"])
          & (out["touch_hi"] >= p["min_touches"]))
    out["valid"] = ok.fillna(False).astype(bool)
    return out


@dataclass
class RangeEvent:
    i: int            # bar index (signal at that bar's close)
    kind: str         # "buy" | "sell" | "stop" | "time"
    price: float      # close that triggered it
    support: float
    resistance: float


def simulate(bars: pd.DataFrame, p: dict | None = None,
             frame: pd.DataFrame | None = None) -> tuple[pd.Series, list[RangeEvent]]:
    """Long-only state machine. Returns (0/1 signal with NaN warm-up, events)."""
    p = params(p)
    f = frame if frame is not None else range_frame(bars, p)
    c = bars["close"].to_numpy(float)
    valid = f["valid"].to_numpy(bool)
    sup_a, res_a, atr_a = (f[k].to_numpy(float) for k in ("support", "resistance", "atr"))
    z, brk, max_hold = float(p["zone"]), float(p["break_atr"]), int(p["max_hold"])
    sig = np.full(len(c), np.nan)
    events: list[RangeEvent] = []
    start = int(p["lookback"])
    long_ = False
    sup = res = atr = 0.0
    entered = 0
    for t in range(start, len(c)):
        if math.isnan(sup_a[t]) or math.isnan(atr_a[t]):
            sig[t] = np.nan if not long_ else 1.0
            continue
        if long_:
            w = res - sup
            if c[t] < sup - brk * atr:
                long_ = False
                events.append(RangeEvent(t, "stop", c[t], sup, res))
            elif c[t] >= res - z * w:
                long_ = False
                events.append(RangeEvent(t, "sell", c[t], sup, res))
            elif t - entered >= max_hold:
                long_ = False
                events.append(RangeEvent(t, "time", c[t], sup, res))
        elif valid[t]:
            w = res_a[t] - sup_a[t]
            if sup_a[t] - brk * atr_a[t] <= c[t] <= sup_a[t] + z * w:
                long_ = True
                sup, res, atr, entered = sup_a[t], res_a[t], atr_a[t], t
                events.append(RangeEvent(t, "buy", c[t], sup, res))
        sig[t] = 1.0 if long_ else 0.0
    return pd.Series(sig, index=bars.index), events


def strat_range(bars: pd.DataFrame, **over) -> pd.Series:
    return simulate(bars, over)[0]


def edge_record(bars: pd.DataFrame, p: dict | None = None, edge: str = "sell", horizon: int = 10) -> dict | None:
    """What followed the earlier entries into an edge zone of a valid range, within ``horizon`` bars.

    An entry is the first close inside the zone (top ``zone`` share of the range for "sell", bottom for "buy").
    With the range frozen at the entry: "turned" = a close back at or beyond the middle of the range came first;
    "broke" = a close more than ``break_atr`` x ATR outside the edge came first; otherwise "stayed"."""
    p = params(p)
    f = range_frame(bars, p)
    c = bars["close"].to_numpy(float)
    valid, pos = f["valid"].to_numpy(bool), f["pos"].to_numpy(float)
    sup, res, atr = (f[k].to_numpy(float) for k in ("support", "resistance", "atr"))
    z, brk = float(p["zone"]), float(p["break_atr"])
    top = edge == "sell"
    inside = valid & ((pos >= 1 - z) if top else (pos <= z))
    out = {"n": 0, "turned": 0, "broke": 0, "stayed": 0, "moves": []}
    T = len(c)
    for t in range(1, T - horizon):
        if not inside[t] or inside[t - 1]:
            continue
        mid, a = (sup[t] + res[t]) / 2.0, atr[t]
        what = "stayed"
        for u in range(t + 1, t + horizon + 1):
            if (c[u] > res[t] + brk * a) if top else (c[u] < sup[t] - brk * a):
                what = "broke"
                break
            if (c[u] <= mid) if top else (c[u] >= mid):
                what = "turned"
                break
        out["n"] += 1
        out[what] += 1
        out["moves"].append(c[t + horizon] / c[t] - 1)
    if not out["n"]:
        return None
    out["median"] = float(np.median(out["moves"]))
    del out["moves"]
    return out


# ---- current state for the UI --------------------------------------------------------------

@dataclass
class RangeState:
    status: str = "none"     # buy | sell | mid | breakout_up | breakdown | loose | none | short
    label: str = L("数据不足", "No data")
    detail: str = ""
    score: float = 0.0       # colour hint only: +1 near support, -1 near resistance
    support: float = float("nan")
    resistance: float = float("nan")
    width_pct: float = float("nan")
    pos: float = float("nan")
    atr: float = float("nan")
    er: float = float("nan")
    drift: float = float("nan")
    touches: tuple[int, int] = (0, 0)
    days: int = 0            # consecutive bars the range has been valid
    start_i: int = -1        # first bar index of the measured window (for the chart)
    buy_band: tuple[float, float] | None = None
    sell_band: tuple[float, float] | None = None
    stop: float = float("nan")
    breakout: float = float("nan")
    in_trade: bool = False   # the backtest rule is currently long
    reasons: list[str] = field(default_factory=list)
    events: list[RangeEvent] = field(default_factory=list)
    params: dict = field(default_factory=dict)


def _why_not(row: pd.Series, p: dict) -> list[str]:
    out = []
    if row["er"] > p["max_er"]:
        out.append(L(f"走势效率 {row['er']:.2f} > {p['max_er']:.2f}（价格在单边运行，不是来回震荡）",
                     f"Efficiency ratio {row['er']:.2f} > {p['max_er']:.2f} (price is trending one way, not moving back and forth)"))
    if row["drift"] > p["max_drift"]:
        out.append(L(f"区间倾斜：{int(p['lookback'])} 日回归漂移占区间宽度 {row['drift']*100:.0f}% > {p['max_drift']*100:.0f}%",
                     f"Range is tilted: the {int(p['lookback'])}-day regression drift is {row['drift']*100:.0f}% "
                     f"of the range width > {p['max_drift']*100:.0f}%"))
    if row["width_atr"] > p["max_width_atr"]:
        out.append(L(f"区间太宽（{row['width_atr']:.1f}×ATR > {p['max_width_atr']:g}），更像一段趋势",
                     f"Range too wide ({row['width_atr']:.1f}×ATR > {p['max_width_atr']:g}); looks more like a trend leg"))
    if row["width_atr"] < p["min_width_atr"] or row["width_pct"] < p["min_width_pct"]:
        out.append(L(f"区间太窄（{row['width_pct']:.1f}%、{row['width_atr']:.1f}×ATR），扣除成本后没有操作空间",
                     f"Range too narrow ({row['width_pct']:.1f}%, {row['width_atr']:.1f}×ATR); "
                     f"no room to trade after costs"))
    if row["touch_lo"] < p["min_touches"] or row["touch_hi"] < p["min_touches"]:
        out.append(L(f"上下沿测试次数不足（下沿 {int(row['touch_lo'])} 次 / 上沿 {int(row['touch_hi'])} 次，"
                     f"各需 ≥{int(p['min_touches'])}）",
                     f"Too few tests of the edges (lower {int(row['touch_lo'])} / upper {int(row['touch_hi'])}, "
                     f"each needs ≥{int(p['min_touches'])})"))
    return out


def range_state(bars: pd.DataFrame, p: dict | None = None, price: float | None = None) -> RangeState:
    p = params(p)
    st = RangeState(params=p)
    n = int(p["lookback"])
    if bars is None or len(bars) < n + 2:
        st.detail = L(f"需要至少 {n + 2} 个交易日的数据。", f"Needs at least {n + 2} trading days of data.")
        st.status = "short"
        return st
    f = range_frame(bars, p)
    sig, st.events = simulate(bars, p, f)
    st.in_trade = bool(sig.iloc[-1] == 1)
    T = len(bars)
    last = T - 1
    px = float(price) if price else float(bars["close"].iloc[-1])
    valid = f["valid"].to_numpy(bool)

    ref = last if valid[last] else None
    if ref is None:
        back = [t for t in range(max(n, last - int(p["recent"])), last) if valid[t]]
        ref = back[-1] if back else None
    row = f.iloc[ref if ref is not None else last]
    st.support, st.resistance = float(row["support"]), float(row["resistance"])
    st.width_pct, st.atr, st.er, st.drift = (float(row[k]) for k in ("width_pct", "atr", "er", "drift"))
    st.touches = (int(row["touch_lo"]) if not math.isnan(row["touch_lo"]) else 0,
                  int(row["touch_hi"]) if not math.isnan(row["touch_hi"]) else 0)
    w = st.resistance - st.support
    if w > 0:
        st.pos = (px - st.support) / w
    z, brk = float(p["zone"]), float(p["break_atr"])

    if ref is None:
        st.status, st.label, st.score = "none", L("非震荡（不适用）", "Not ranging (n/a)"), 0.0
        st.reasons = _why_not(row, p)
        st.detail = L(f"最近 {n} 个交易日不满足横盘条件，高抛低吸规则不适用。"
                      + ("原因：" + "；".join(st.reasons) + "。" if st.reasons else ""),
                      f"The last {n} trading days do not meet the sideways conditions; "
                      f"the buy-low / sell-high rule does not apply."
                      + (" Reasons: " + "; ".join(st.reasons) + "." if st.reasons else ""))
        return st

    k = ref
    while k >= n and valid[k]:
        k -= 1
    st.days = ref - k
    st.start_i = max(0, k + 1 - n)
    st.buy_band = (st.support, st.support + z * w)
    st.sell_band = (st.resistance - z * w, st.resistance)
    st.stop = st.support - brk * st.atr
    st.breakout = st.resistance + brk * st.atr
    lv = L(f"支撑 {st.support:.2f} / 阻力 {st.resistance:.2f}（宽 {st.width_pct:.1f}%）",
           f"support {st.support:.2f} / resistance {st.resistance:.2f} (width {st.width_pct:.1f}%)")

    if px > st.breakout:
        st.status, st.label, st.score = "breakout_up", L("向上突破区间", "Breakout above range"), 0.3
        st.detail = L(f"收盘 {px:.2f} 已高于阻力 {st.resistance:.2f} 超过 {brk:g}×ATR，原区间（{lv}）失效。"
                      "高抛低吸规则停止使用；突破是否有效需要看后续几天能否站稳，回落到区间内则视为假突破。",
                      f"Close {px:.2f} is above resistance {st.resistance:.2f} by more than {brk:g}×ATR; "
                      f"the original range ({lv}) is no longer valid. "
                      "The buy-low / sell-high rule stops being used; whether the breakout holds depends on whether "
                      "price stays above in the next few days, and a fall back into the range counts as a false breakout.")
    elif px < st.stop:
        st.status, st.label, st.score = "breakdown", L("向下跌破区间", "Breakdown below range"), -0.8
        st.detail = L(f"收盘 {px:.2f} 已低于支撑 {st.support:.2f} 超过 {brk:g}×ATR，原区间（{lv}）失效。"
                      "按这条规则，持有的低吸仓位应在此止损，且不再低吸。",
                      f"Close {px:.2f} is below support {st.support:.2f} by more than {brk:g}×ATR; "
                      f"the original range ({lv}) is no longer valid. "
                      "Under this rule, a position bought low would be stopped out here, with no further buying low.")
    elif ref != last:
        st.status, st.label, st.score = "loose", L("区间松动", "Range loosening"), 0.0
        why = L("；", "; ").join(_why_not(f.iloc[last], p)) or L("指标在临界值附近", "indicators are near their thresholds")
        st.detail = L(f"{last - ref} 个交易日前还满足横盘条件（{lv}），现在价格仍在区间内但条件已不满足"
                      f"（{why}）。暂停新的低吸，已持有的按原区间管理。",
                      f"{last - ref} trading days ago the sideways conditions were still met ({lv}); price is still "
                      f"inside the range but the conditions no longer hold ({why}). "
                      f"New buy-low entries are paused; positions already held are managed against the original range.")
    elif st.pos <= z:
        st.status, st.label, st.score = "buy", L("接近支撑（低吸区）", "Near support (buy zone)"), 0.6
        st.detail = L(f"横盘已持续 {st.days} 天，{lv}。现价 {px:.2f} 位于区间 {st.pos*100:.0f}% 处，"
                      f"处于下沿 {z*100:.0f}% 的低吸区 {st.buy_band[0]:.2f}–{st.buy_band[1]:.2f}。"
                      f"规则的失效位（止损参考）{st.stop:.2f}，目标是高抛区 {st.sell_band[0]:.2f} 以上。",
                      f"Sideways for {st.days} days, {lv}. Price {px:.2f} is at {st.pos*100:.0f}% of the range, "
                      f"in the buy zone {st.buy_band[0]:.2f}–{st.buy_band[1]:.2f} (bottom {z*100:.0f}%). "
                      f"The rule's invalidation level (stop reference) is {st.stop:.2f}; "
                      f"the target is the sell zone from {st.sell_band[0]:.2f} up.")
    elif st.pos >= 1 - z:
        st.status, st.label, st.score = "sell", L("接近阻力（高抛区）", "Near resistance (sell zone)"), -0.6
        st.detail = L(f"横盘已持续 {st.days} 天，{lv}。现价 {px:.2f} 位于区间 {st.pos*100:.0f}% 处，"
                      f"处于上沿 {z*100:.0f}% 的高抛区 {st.sell_band[0]:.2f}–{st.sell_band[1]:.2f}。"
                      f"若收盘站上 {st.breakout:.2f}（阻力 + {brk:g}×ATR）则视为向上突破，区间作废。",
                      f"Sideways for {st.days} days, {lv}. Price {px:.2f} is at {st.pos*100:.0f}% of the range, "
                      f"in the sell zone {st.sell_band[0]:.2f}–{st.sell_band[1]:.2f} (top {z*100:.0f}%). "
                      f"A close above {st.breakout:.2f} (resistance + {brk:g}×ATR) counts as an upward breakout "
                      f"and voids the range.")
    else:
        st.status, st.label, st.score = "mid", L("区间中部（观望）", "Mid-range (wait)"), 0.0
        st.detail = L(f"横盘已持续 {st.days} 天，{lv}。现价 {px:.2f} 位于区间 {st.pos*100:.0f}% 处，"
                      f"不在两端，高抛低吸规则没有动作。低吸区 {st.buy_band[0]:.2f}–{st.buy_band[1]:.2f}，"
                      f"高抛区 {st.sell_band[0]:.2f}–{st.sell_band[1]:.2f}。",
                      f"Sideways for {st.days} days, {lv}. Price {px:.2f} is at {st.pos*100:.0f}% of the range, "
                      f"not near either edge, so the buy-low / sell-high rule takes no action. "
                      f"Buy zone {st.buy_band[0]:.2f}–{st.buy_band[1]:.2f}, "
                      f"sell zone {st.sell_band[0]:.2f}–{st.sell_band[1]:.2f}.")
    return st
