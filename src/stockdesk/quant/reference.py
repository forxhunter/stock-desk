"""Compare a foreign listing (e.g. a GDR) with its home-market underlying share.

Example: HY9H (SK Hynix GDR, Frankfurt, EUR) vs 000660.KS (SK Hynix, Seoul, KRW).
The home market usually trades first, so its move and trend are context for the listing, and the
gap between the listing and the currency-converted home price shows whether it trades rich or cheap.
Descriptive only; nothing here is a forecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass
class RefState:
    symbol: str                 # the listing the user holds
    ref_symbol: str
    name: str = ""
    ref_currency: str = ""
    currency: str = ""
    ref_price: float = float("nan")
    ref_asof: pd.Timestamp | None = None
    ref_day_pct: float = float("nan")
    ref_chg_5d: float = float("nan")
    ref_chg_21d: float = float("nan")
    ref_chg_126d: float = float("nan")
    ref_dd_252: float = float("nan")      # home share: distance from its 52-week high
    implied: float = float("nan")         # home close converted into the listing's currency
    premium_pct: float = float("nan")     # listing price vs implied
    premium_avg: float = float("nan")     # 60-day average of the daily premium
    premium_std: float = float("nan")
    corr: float = float("nan")            # 120-day correlation of daily returns
    ref_stance: str = ""
    ref_composite: float = 0.0
    ref_range: str = ""
    ref_below_falling_sma50: bool = False
    stale_days: int = 0                   # listing has traded this many days past the last home close


def _chg(c: pd.Series, n: int) -> float:
    return float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n else float("nan")


def compare(symbol: str, bars: pd.DataFrame, ref_symbol: str, ref_bars: pd.DataFrame,
            fx_local: pd.Series | None, fx_ref: pd.Series | None, *, ratio: float = 1.0,
            price: float | None = None, name: str = "", currency: str = "", ref_currency: str = "",
            analysis=None) -> RefState | None:
    """``fx_local`` / ``fx_ref``: daily USD value of one unit of each currency (None = already USD)."""
    if bars is None or ref_bars is None or len(bars) < 30 or len(ref_bars) < 30:
        return None
    st = RefState(symbol, ref_symbol, name or ref_symbol, ref_currency, currency)
    rc = ref_bars["close"].dropna()
    st.ref_price, st.ref_asof = float(rc.iloc[-1]), rc.index[-1]
    st.ref_day_pct, st.ref_chg_5d, st.ref_chg_21d, st.ref_chg_126d = (_chg(rc, n) for n in (1, 5, 21, 126))
    st.ref_dd_252 = float(rc.iloc[-1] / rc.tail(252).max() - 1) * 100

    one = pd.Series(1.0, index=rc.index)
    fl = fx_local if fx_local is not None else one
    fr = fx_ref if fx_ref is not None else one
    df = pd.concat([bars["close"], rc, fl, fr], axis=1, keys=["h", "r", "fl", "fr"]).sort_index()
    df[["fl", "fr"]] = df[["fl", "fr"]].ffill()
    last_fx = df[["fl", "fr"]].dropna().iloc[-1] if len(df[["fl", "fr"]].dropna()) else None
    both = df.dropna()
    if last_fx is None or len(both) < 20:
        return st
    implied = both["r"] * ratio * both["fr"] / both["fl"]
    prem = (both["h"] / implied - 1) * 100
    st.premium_avg, st.premium_std = float(prem.tail(60).mean()), float(prem.tail(60).std(ddof=1))
    st.implied = float(st.ref_price * ratio * last_fx["fr"] / last_fx["fl"])
    px = price if price and not math.isnan(price) else float(bars["close"].iloc[-1])
    st.premium_pct = (px / st.implied - 1) * 100
    ret = both[["h", "r"]].pct_change().dropna().tail(120)
    if len(ret) >= 30:
        st.corr = float(ret["h"].corr(ret["r"]))
    st.stale_days = int((bars.index > st.ref_asof).sum())
    if analysis is not None and getattr(analysis, "ok", False):
        st.ref_stance, st.ref_composite = analysis.stance, analysis.composite
        st.ref_range = analysis.range.label if analysis.range is not None else ""
        i = analysis.ind
        s50, s50p = i.get("sma50"), i.get("sma50_prev")
        st.ref_below_falling_sma50 = bool(s50 and s50p and not math.isnan(s50) and not math.isnan(s50p)
                                          and st.ref_price < s50 < s50p)
    return st
