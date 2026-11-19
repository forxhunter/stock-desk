"""A small, honest long-only daily backtester.

Timing convention (no look-ahead):
  * a strategy produces ``signal[t]`` in {0, 1} using data up to and including the close of day t;
  * the position is changed at the OPEN of day t+1;
  * a one-way cost (commission + slippage, in basis points) is charged on every change.

Strategy parameters are textbook defaults and are NOT optimised per ticker, which limits
(but does not remove) overfitting. Results are descriptive statistics of the past only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import indicators as ind
from . import range_trading as rt

TRADING_DAYS = 252


@dataclass
class BacktestResult:
    key: str
    label: str
    rule: str
    n_bars: int = 0
    years: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    ann_vol: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0
    exposure: float = 0.0
    n_trades: int = 0
    hit_rate: float | None = None
    hit_ci: tuple[float, float] | None = None
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    bh_return: float = 0.0
    bh_cagr: float = 0.0
    bh_max_dd: float = 0.0
    first_half_return: float = 0.0
    second_half_return: float = 0.0
    in_position_now: bool = False
    trade_returns: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    equity: pd.Series | None = None


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def latch(entry: pd.Series, exit_: pd.Series) -> pd.Series:
    """Stateful 0/1 signal: becomes 1 on ``entry``, 0 on ``exit_`` (exit wins on ties)."""
    s = pd.Series(np.nan, index=entry.index)
    s[entry.fillna(False).astype(bool)] = 1.0
    s[exit_.fillna(False).astype(bool)] = 0.0
    return s.ffill().fillna(0.0)


# ---- strategies: each returns a 0/1 signal with NaN during indicator warm-up -------------

def _mask_warmup(sig: pd.Series, *needed: pd.Series) -> pd.Series:
    ok = pd.Series(True, index=sig.index)
    for s in needed:
        ok &= s.notna()
    return sig.where(ok)


def strat_ma_cross(bars: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.Series:
    f, s = ind.sma(bars["close"], fast), ind.sma(bars["close"], slow)
    return _mask_warmup((f > s).astype(float), f, s)


def strat_macd(bars: pd.DataFrame) -> pd.Series:
    m = ind.macd(bars["close"])
    return _mask_warmup((m["macd"] > m["signal"]).astype(float), m["signal"])


def strat_rsi_reversion(bars: pd.DataFrame, lo: float = 30, exit_level: float = 55) -> pd.Series:
    r = ind.rsi(bars["close"])
    return _mask_warmup(latch(r < lo, r > exit_level), r)


def strat_bollinger_reversion(bars: pd.DataFrame) -> pd.Series:
    b = ind.bollinger(bars["close"])
    return _mask_warmup(latch(bars["close"] < b["lower"], bars["close"] > b["mid"]), b["mid"])


def strat_trend_filter(bars: pd.DataFrame) -> pd.Series:
    s200 = ind.sma(bars["close"], 200)
    m = ind.macd(bars["close"])
    sig = ((bars["close"] > s200) & (m["macd"] > m["signal"])).astype(float)
    return _mask_warmup(sig, s200, m["signal"])


STRATEGIES: dict[str, tuple[str, str, Callable[[pd.DataFrame], pd.Series]]] = {
    "ma_50_200": ("均线金叉 50/200", "50日均线在200日均线上方时持有", strat_ma_cross),
    "ma_20_50": ("均线金叉 20/50", "20日均线在50日均线上方时持有",
                 lambda b: strat_ma_cross(b, 20, 50)),
    "macd": ("MACD 金叉", "MACD 线在信号线上方时持有", strat_macd),
    "trend_filter": ("趋势过滤", "收盘价高于200日均线且 MACD 金叉时持有", strat_trend_filter),
    "rsi_reversion": ("RSI 超卖反弹", "RSI(14)<30 买入，回到 55 以上卖出", strat_rsi_reversion),
    "boll_reversion": ("布林下轨反弹", "收盘跌破布林下轨买入，回到中轨卖出", strat_bollinger_reversion),
    "range_trading": ("震荡区间高抛低吸", "横盘区间内跌到支撑附近（下沿20%）买入，涨到阻力附近（上沿20%）卖出；"
                      "跌破支撑 0.5×ATR 止损，40 天未到目标离场", rt.strat_range),
}


# ---- engine --------------------------------------------------------------------------------

def run_backtest(bars: pd.DataFrame, signal: pd.Series, cost_bps: float = 10.0,
                 key: str = "", label: str = "", rule: str = "") -> BacktestResult:
    res = BacktestResult(key=key, label=label, rule=rule)
    valid = signal.dropna()
    if len(valid) < 30:
        res.notes.append("历史数据不足，无法回测。")
        return res
    start = valid.index[0]
    bars = bars.loc[start:]
    sig = signal.loc[start:].fillna(0.0)
    o, c = bars["open"].astype(float), bars["close"].astype(float)

    w = sig.shift(1).fillna(0.0)            # position held during day t (decided at close t-1)
    pw = w.shift(1).fillna(0.0)
    r_cc = c / c.shift(1) - 1.0             # hold through
    r_oc = c / o - 1.0                      # enter at today's open
    r_co = o / c.shift(1) - 1.0             # exit at today's open
    gross = np.where((w == 1) & (pw == 1), r_cc,
             np.where((w == 1) & (pw == 0), r_oc,
              np.where((w == 0) & (pw == 1), r_co, 0.0)))
    gross = pd.Series(gross, index=bars.index).fillna(0.0)
    cost = cost_bps / 1e4
    net = (1.0 + gross) * (1.0 - cost * (w - pw).abs()) - 1.0
    # day 0 only provides the first signal; performance starts on day 1
    net = net.iloc[1:]
    if net.empty:
        return res
    equity = (1.0 + net).cumprod()

    n = len(net)
    years = n / TRADING_DAYS
    res.n_bars, res.years, res.equity = n, years, equity
    res.total_return = float(equity.iloc[-1] - 1.0)
    res.cagr = float(equity.iloc[-1] ** (1 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else -1.0
    res.ann_vol = float(net.std(ddof=1) * math.sqrt(TRADING_DAYS)) if n > 1 else 0.0
    res.sharpe = float(net.mean() / net.std(ddof=1) * math.sqrt(TRADING_DAYS)) if n > 1 and net.std(ddof=1) > 0 else 0.0
    res.max_dd = ind.max_drawdown(equity)
    res.exposure = float(w.iloc[1:].mean())
    res.in_position_now = bool(sig.iloc[-1] == 1)

    bh = c.iloc[1:] / c.iloc[0]
    res.bh_return = float(bh.iloc[-1] - 1.0)
    res.bh_cagr = float(bh.iloc[-1] ** (1 / years) - 1.0) if years > 0 else 0.0
    res.bh_max_dd = ind.max_drawdown(bh)

    half = n // 2
    res.first_half_return = float(equity.iloc[half - 1] - 1.0) if half >= 1 else 0.0
    res.second_half_return = float(equity.iloc[-1] / equity.iloc[half - 1] - 1.0) if half >= 1 else 0.0

    # round-trip trades (entry at open, exit at open); a still-open trade is not counted
    entries = list(np.flatnonzero(((w == 1) & (pw == 0)).to_numpy()))
    exits = list(np.flatnonzero(((w == 0) & (pw == 1)).to_numpy()))
    ov = o.to_numpy()
    trades: list[float] = []
    for i, e in enumerate(entries):
        x = next((x for x in exits if x > e), None)
        if x is None:
            break
        trades.append(float(ov[x] / ov[e] * (1 - cost) ** 2 - 1.0))
    res.trade_returns = trades
    res.n_trades = len(trades)
    if trades:
        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t <= 0]
        res.hit_rate = len(wins) / len(trades)
        res.hit_ci = wilson_interval(len(wins), len(trades))
        res.avg_win = float(np.mean(wins)) if wins else 0.0
        res.avg_loss = float(np.mean(losses)) if losses else 0.0
        if losses and sum(losses) < 0:
            res.profit_factor = float(sum(wins) / -sum(losses))

    if res.n_trades < 10:
        res.notes.append(f"只有 {res.n_trades} 笔完整交易，胜率在统计上不可靠。")
    if res.first_half_return * res.second_half_return < 0:
        res.notes.append("前后两段历史表现方向相反，规则不稳定。")
    if res.total_return < res.bh_return:
        res.notes.append("同期跑输买入持有。")
    return res


def backtest_all(bars: pd.DataFrame, cost_bps: float = 10.0,
                 range_params: dict | None = None) -> list[BacktestResult]:
    out = []
    for key, (label, rule, fn) in STRATEGIES.items():
        if key == "range_trading" and range_params:
            fn = lambda b, _p=range_params: rt.strat_range(b, **_p)
        try:
            out.append(run_backtest(bars, fn(bars), cost_bps, key, label, rule))
        except Exception as exc:  # a broken series must not take the whole refresh down
            r = BacktestResult(key=key, label=label, rule=rule)
            r.notes.append(f"回测失败：{type(exc).__name__}")
            out.append(r)
    return out
