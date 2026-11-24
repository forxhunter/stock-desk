"""Key price levels of a market index and the breaks of them (破位), by fixed rules.

A level is "broken" on the latest bar when the close (or the live price while the session is open)
is on the far side of it after having held the near side:

  ma      close below the n-day average after at least ``min_hold`` closes at or above it
  low     close below the lowest close of the previous n sessions, the first such close in ``min_hold``
  dd      close ``d`` % or more below the highest close of the last 252 sessions, the first in ``min_hold``
  zone    close below a support zone (cluster of swing highs / lows of the past year, quant/levels.py)
  user    close below a level the user wrote in the config (optionally with a name and the source it came from)
  mom     trend-model estimate: the n-session return turns negative, i.e. the close falls under the close of
          n sessions earlier (n = 21 / 63 / 252, the one-, three- and twelve-month lookbacks of time-series
          momentum). Banks' CTA "trigger levels" come from proprietary models and are not published; this is a
          transparent public proxy for the same kind of signal, computed from closes only.
and, mirrored, "reclaimed": close back above an average, a new 252-session closing high, or a close
above a user level.

For the rule-based kinds (ma / low / dd / high) the same definition is run over the index's own
history, so an alert can say what followed the earlier breaks: forward returns, how often the level
was regained within five sessions, how much lower it went first. That is a record of the past on one
index, not a forecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..i18n import L
from . import indicators as ind
from .levels import Zone, support_zones

DEFAULTS: dict = {"ma": [50, 100, 200], "lows": [20, 60], "drawdowns": [10.0, 20.0], "high": 252, "min_hold": 10,
                  "momentum": [21, 63, 252],
                  "zones": True, "zone_tol_atr": 0.5, "stats_years": 30, "horizons": [5, 10, 21, 63]}

NAMES = {"^GSPC": L("标普 500", "S&P 500"), "^IXIC": L("纳斯达克综合指数", "Nasdaq Composite"),
         "^NDX": L("纳斯达克 100", "Nasdaq 100"), "^DJI": L("道琼斯工业指数", "Dow Jones Industrial Average"),
         "^RUT": L("罗素 2000", "Russell 2000"), "^SOX": L("费城半导体指数", "PHLX Semiconductor index")}
SYMBOLS = ["^GSPC", "^IXIC"]


def params(over: dict | None = None) -> dict:
    p = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for k, v in (over or {}).items():
        if k in p and v is not None:
            p[k] = v
    return p


@dataclass
class Level:
    key: str                         # "sma50" | "low20" | "dd10" | "high252" | "zone0" | "user6800"
    kind: str                        # "ma" | "low" | "dd" | "high" | "zone" | "user"
    name: str
    value: float
    weight: int = 1                  # 1 minor .. 3 major
    n: float = 0                     # window (ma / low / high) or percentage (dd)
    held: int = 0                    # closes on the near side before the break (rule-based kinds)
    zone: Zone | None = None
    source: str = ""                 # where a level the user entered comes from


@dataclass
class Record:
    """What followed the earlier breaks of the same kind on the same index."""
    n: int = 0
    since: pd.Timestamp | None = None
    fwd: dict = field(default_factory=dict)      # horizon -> (count, median, share up, 10th pct, 90th pct)
    base: dict = field(default_factory=dict)     # horizon -> (median, share up, 10th pct) over all sessions
    back5: float | None = None                   # share that closed back across the level within 5 sessions
    dip21: tuple | None = None                   # (median, 10th pct) of the lowest close in the next 21 sessions
    last: list = field(default_factory=list)     # [(date, 21-session return or None)], most recent last


@dataclass
class IndexState:
    symbol: str
    name: str
    asof: pd.Timestamp | None = None
    live: bool = False               # the session is still open: the last bar is unfinished
    price: float = float("nan")
    prev_close: float = float("nan")
    day_pct: float = 0.0
    ind: dict = field(default_factory=dict)
    levels: list[Level] = field(default_factory=list)       # every key level, highest first
    broken: list[Level] = field(default_factory=list)       # broken downwards on this bar, most important first
    reclaimed: list[Level] = field(default_factory=list)    # taken back / broken upwards on this bar
    records: dict[str, Record] = field(default_factory=dict)   # "<key>:<down|up>" -> record
    inforce: tuple | None = None     # (Level as of the break, date, sessions since, Record): the latest rule-based
    #                                  break of an earlier session that the price is still on the far side of
    recent: list = field(default_factory=list)   # [(date, Level)]: breaks of the last sessions that still stand

    def below(self) -> list[Level]:
        """Levels under the price, nearest first (a closing high is not a support; equal values are listed once)."""
        return self._distinct([x for x in self.levels if x.value < self.price and x.kind != "high"])

    def above(self) -> list[Level]:
        """Levels over the price, nearest first (a closing low is not something to regain)."""
        return self._distinct([x for x in reversed(self.levels) if x.value > self.price and x.kind != "low"])

    @staticmethod
    def _distinct(rows: list[Level]) -> list[Level]:
        out: list[Level] = []
        for x in rows:
            if out and abs(x.value / out[-1].value - 1) < 1e-4:
                if x.weight > out[-1].weight:
                    out[-1] = x
                continue
            out.append(x)
        return out

    def level(self, key: str) -> Level | None:
        return next((x for x in self.levels if x.key == key), None)


def _weight(kind: str, n: float) -> int:
    if kind == "ma":
        return 3 if n >= 200 else 2 if n >= 50 else 1
    if kind == "low":
        return 2 if n >= 60 else 1
    if kind == "dd":
        return 3 if n >= 20 else 2
    if kind == "mom":
        return 3 if n >= 252 else 2 if n >= 63 else 1
    return 3 if kind == "user" else 2


def _name(kind: str, n: float) -> str:
    if kind == "ma":
        return L(f"{n:g} 日均线", f"{n:g}-day average")
    if kind == "low":
        return L(f"{n:g} 日收盘低点", f"{n:g}-day closing low")
    if kind == "high":
        return L("52 周收盘高点", "52-week closing high") if n == 252 else L(f"{n:g} 日收盘高点", f"{n:g}-day closing high")
    if kind == "dd":
        return L(f"回撤 {n:g}% 线", f"{n:g}% drawdown line")
    if kind == "mom":
        span = {21: L("1 个月", "1-month"), 63: L("3 个月", "3-month"), 252: L("12 个月", "12-month")}.get(int(n), L(f"{n:g} 日", f"{n:g}-session"))
        return L(f"CTA 趋势估算（{span}动量翻转价）", f"CTA trend estimate ({span} momentum flip)")
    return ""


def _run_before(flag: pd.Series) -> pd.Series:
    """For each bar: how many consecutive bars right before it had ``flag`` true."""
    f = flag.fillna(False).astype(bool)
    run = f.groupby((~f).cumsum()).cumsum()
    return run.shift(1).fillna(0).astype(int)


def series(c: pd.Series, kind: str, n: float, direction: str, min_hold: int = 10):
    """(event, level, held) Series for one rule: the same definition for today and for the history."""
    if kind == "ma":
        lvl = ind.sma(c, int(n))
        ok = lvl.notna()
        far = ((c < lvl) if direction == "down" else (c > lvl)) & ok
        held = _run_before(~far & ok)
    elif kind in ("low", "high"):
        prior = c.shift(1).rolling(int(n), min_periods=int(n))
        lvl = prior.min() if kind == "low" else prior.max()
        far = (c < lvl) if kind == "low" else (c > lvl)
        held = _run_before(~far & lvl.notna())
    elif kind == "dd":
        lvl = c.rolling(252, min_periods=120).max() * (1 - float(n) / 100.0)
        far = c <= lvl
        held = _run_before(~far & lvl.notna())
    elif kind == "mom":
        lvl = c.shift(int(n))
        ok = lvl.notna()
        far = ((c < lvl) if direction == "down" else (c > lvl)) & ok
        held = _run_before(~far & ok)
    else:
        raise ValueError(kind)
    return far & (held >= int(min_hold)), lvl, held


def record(c: pd.Series, event: pd.Series, level: pd.Series, kind: str, direction: str,
           horizons=(5, 10, 21, 63), upto: int | None = None) -> Record | None:
    """The outcome of every earlier event in ``c``: those before bar ``upto`` (default: the latest bar)."""
    arr = c.to_numpy(float)
    lv = level.to_numpy(float)
    n = len(arr)
    idx = [int(i) for i in np.flatnonzero(event.to_numpy(bool)[:(n - 1 if upto is None else upto)])]
    if not idx:
        return None
    r = Record(n=len(idx), since=c.index[0])
    for h in horizons:
        got = np.array([arr[i + h] / arr[i] - 1 for i in idx if i + h < n])
        if len(got):
            r.fwd[h] = (len(got), float(np.median(got)), float((got > 0).mean()),
                        float(np.percentile(got, 10)), float(np.percentile(got, 90)))
        every = arr[h:] / arr[:-h] - 1
        every = every[np.isfinite(every)]
        if len(every):
            r.base[h] = (float(np.median(every)), float((every > 0).mean()), float(np.percentile(every, 10)))
    back, dips = [], []
    for i in idx:
        if i + 5 < n:
            nxt = arr[i + 1:i + 6]
            ref = lv[i + 1:i + 6] if kind in ("ma", "mom") else np.full(5, lv[i])
            back.append(bool(((nxt > ref) if direction == "down" else (nxt < ref)).any()))
        if i + 21 < n:
            dips.append(float(arr[i + 1:i + 22].min() / arr[i] - 1))
    r.back5 = float(np.mean(back)) if back else None
    r.dip21 = (float(np.median(dips)), float(np.percentile(dips, 10))) if dips else None
    r.last = [(c.index[i], float(arr[i + 21] / arr[i] - 1) if i + 21 < n else None) for i in idx[-3:]]
    return r


def join(long: pd.DataFrame | None, bars: pd.DataFrame) -> pd.DataFrame:
    """The long history with the recent bars laid over it."""
    if long is None or not len(long):
        return bars
    out = pd.concat([long[long.index < bars.index[0]], bars])
    return out[~out.index.duplicated(keep="last")].sort_index()


def session_live(last_bar_utc: pd.Timestamp | None, now: pd.Timestamp | None = None) -> bool:
    """True while the regular US session of the last intraday bar's day is still open."""
    if last_bar_utc is None:
        return False
    ny = now if now is not None else pd.Timestamp.now(tz="America/New_York")
    bar = pd.Timestamp(last_bar_utc)
    bar = (bar.tz_localize("UTC") if bar.tzinfo is None else bar).tz_convert("America/New_York")
    return bar.date() == ny.date() and ny.weekday() < 5 and (9, 30) <= (ny.hour, ny.minute) < (16, 0)


def state(symbol: str, bars: pd.DataFrame, cfg: dict | None = None, *, name: str = "", live: bool = False,
          user_levels: list | tuple = (), long: pd.DataFrame | None = None) -> IndexState | None:
    """Key levels of ``symbol`` and what the latest bar did to them. ``bars`` ends with the current session."""
    p = params(cfg)
    if bars is None or len(bars) < 130:
        return None
    full = join(long, bars)
    cut = full.index[-1] - pd.DateOffset(years=int(p["stats_years"]))
    full = full[full.index >= cut]
    c = full["close"].astype(float)
    px, prev = float(c.iloc[-1]), float(c.iloc[-2])
    hold = int(p["min_hold"])
    st = IndexState(symbol, name or NAMES.get(symbol, symbol), asof=full.index[-1], live=bool(live), price=px,
                    prev_close=prev, day_pct=(px / prev - 1) * 100 if prev else 0.0)

    standing: list[tuple] = []           # (bar of the break, level then, series) for breaks still in force

    def rule(kind: str, n: float, directions: tuple[str, ...]) -> None:
        lv = None
        for d in directions:
            ev, lvl, held = series(c, kind, n, d, hold)
            val = float(lvl.iloc[-1])
            if math.isnan(val):
                return
            if lv is None:
                lv = Level(f"{'sma' if kind == 'ma' else kind}{n:g}", kind, _name(kind, n), val, _weight(kind, n), n)
                st.levels.append(lv)
            if bool(ev.iloc[-1]):
                lv.held = int(held.iloc[-1])
                (st.broken if d == "down" else st.reclaimed).append(lv)
                rec = record(c, ev, lvl, kind, d, tuple(p["horizons"]))
                if rec is not None:
                    st.records[f"{lv.key}:{d}"] = rec
            elif d == "down" and kind in ("ma", "dd"):
                far = (c < lvl) if kind == "ma" else (c <= lvl)
                earlier = np.flatnonzero(ev.to_numpy(bool)[:-1])
                if bool(far.iloc[-1]) and len(earlier) and len(c) - 1 - int(earlier[-1]) <= 60:
                    start = int(earlier[-1])
                    then = Level(lv.key, kind, lv.name, float(lvl.iloc[start]), lv.weight, n, int(held.iloc[start]))
                    standing.append((start, then, ev, lvl))

    for n in p["ma"]:
        rule("ma", int(n), ("down", "up"))
    for n in p["lows"]:
        rule("low", int(n), ("down",))
    if p["high"]:
        rule("high", int(p["high"]), ("up",))
    for d in p["drawdowns"]:
        rule("dd", float(d), ("down",))
    for n in p["momentum"] or []:
        rule("mom", int(n), ("down", "up"))
    if p["zones"]:
        try:
            zones = support_zones(full.iloc[:-1].tail(500), prev, tol_atr=float(p["zone_tol_atr"]))[:3]
        except Exception:
            zones = []
        for k, z in enumerate(zones):
            single = z.year_low and z.touches == 1
            lv = Level(f"zone{z.low:.0f}", "zone",
                       L(f"一年低点 {z.low:,.0f}", f"1-year low {z.low:,.0f}") if single
                       else L(f"支撑区 {z.low:,.0f}–{z.high:,.0f}", f"support zone {z.low:,.0f}–{z.high:,.0f}"),
                       float(z.low), 2, zone=z)
            st.levels.append(lv)
            if px < z.low:
                st.broken.append(lv)
    for raw in user_levels or ():
        given, src = "", ""
        if isinstance(raw, dict):
            given, src = str(raw.get("name") or "").strip(), str(raw.get("source") or "").strip()
            raw = raw.get("level")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        lv = Level(f"user{val:g}", "user", given or L(f"你设的关键位 {val:,.0f}", f"your level {val:,.0f}"), val, 3, source=src)
        st.levels.append(lv)
        if prev >= val > px:
            st.broken.append(lv)
        elif prev <= val < px:
            st.reclaimed.append(lv)

    if standing:
        start, then, ev, lvl = max(standing, key=lambda x: (x[0], x[1].weight))
        st.inforce = (then, c.index[start], len(c) - 1 - start,
                      record(c, ev, lvl, then.kind, "down", tuple(p["horizons"]), upto=start))
    st.levels.sort(key=lambda x: -x.value)
    st.broken.sort(key=lambda x: (-x.weight, -x.value))
    st.reclaimed.sort(key=lambda x: (-x.weight, x.value))

    h, lo = full["high"].astype(float), full["low"].astype(float)
    tail = c.tail(252)
    chg = c.diff()
    down = int(_run_before(chg < 0).iloc[-1]) + 1 if chg.iloc[-1] < 0 else 0
    up = int(_run_before(chg > 0).iloc[-1]) + 1 if chg.iloc[-1] > 0 else 0
    atr = float(ind.atr(h, lo, c).iloc[-1])
    v = full["volume"].astype(float) if "volume" in full else None
    vr = None
    if not live and v is not None and len(v) > 22 and float(v.iloc[-21:-1].mean()) > 0 and float(v.iloc[-1]) > 0:
        vr = float(v.iloc[-1]) / float(v.iloc[-21:-1].mean())
    mas = {}
    for n in (20, 50, 100, 200):
        m = ind.sma(c, n)
        if not math.isnan(float(m.iloc[-1])):
            under = c < m
            side = under if bool(under.iloc[-1]) else ~under & m.notna()
            mas[n] = (float(m.iloc[-1]), int(_run_before(side).iloc[-1]) + 1)     # value, closes on this side
    st.ind = {"mas": mas,
              "rsi": float(ind.rsi(c).iloc[-1]), "atr": atr, "atr_pct": atr / px * 100 if px else float("nan"),
              "high252": float(tail.max()), "high252_date": tail.idxmax(), "low252": float(tail.min()),
              "dd": (px / float(tail.max()) - 1) * 100, "chg5": (px / float(c.iloc[-6]) - 1) * 100,
              "chg21": (px / float(c.iloc[-22]) - 1) * 100, "down_days": down, "up_days": up, "vol_ratio": vr,
              "day_low": float(lo.iloc[-1]), "day_high": float(h.iloc[-1])}
    return st


def beta(stock: pd.Series, index: pd.Series, n: int = 120) -> float | None:
    """Slope of the stock's daily returns on the index's over the last ``n`` common sessions."""
    df = pd.concat([stock, index], axis=1, join="inner").dropna().pct_change().dropna().tail(n)
    if len(df) < 40:
        return None
    var = float(df.iloc[:, 1].var())
    return float(df.iloc[:, 0].cov(df.iloc[:, 1]) / var) if var > 0 else None
