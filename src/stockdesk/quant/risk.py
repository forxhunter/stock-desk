"""Risk levels and position-size guidance. All outputs are upper bounds, not targets."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..i18n import L


@dataclass
class Levels:
    entry: float
    atr: float
    stop: float
    target: float
    risk_per_share: float
    basis: str  # "cost" (anchored at the user's cost basis) or "price"


def atr_levels(entry: float, atr: float, stop_mult: float = 2.0, reward_risk: float = 2.0,
               basis: str = "price") -> Levels | None:
    """ATR stop below entry and a take-profit at ``reward_risk`` times the risk above it."""
    if not (entry and entry > 0 and atr and atr > 0 and math.isfinite(atr)):
        return None
    risk = stop_mult * atr
    stop = max(entry - risk, 0.01)
    return Levels(entry, atr, stop, entry + reward_risk * (entry - stop), entry - stop, basis)


def shares_for_risk(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    """Fixed-fractional sizing: lose at most ``risk_pct`` % of equity if the stop is hit."""
    per_share = entry - stop
    if equity <= 0 or per_share <= 0:
        return 0.0
    return math.floor(equity * risk_pct / 100.0 / per_share)


def vol_budget_weight(ann_vol: float, budget_pct: float = 3.0, cap: float = 0.25) -> float:
    """Weight such that the position contributes about ``budget_pct`` % annualised volatility."""
    if not ann_vol or ann_vol <= 0 or not math.isfinite(ann_vol):
        return 0.0
    return min(cap, budget_pct / 100.0 / ann_vol)


@dataclass
class Kelly:
    full: float            # raw Kelly fraction (can be negative)
    suggested: float       # fractional Kelly after caps, >= 0
    win_rate: float | None
    payoff: float | None   # average win / average loss
    n: int
    note: str = ""


def kelly_from_trades(trade_returns: list[float], fraction: float = 0.25, cap: float = 0.15,
                      min_trades: int = 10) -> Kelly:
    """Kelly f* = W - (1-W)/R from historical round trips, shrunk hard.

    Full Kelly assumes the win rate and payoff are known exactly; they are not, so only a
    fraction is used, the result is capped, and with too few trades it is set to zero.
    """
    n = len(trade_returns)
    wins = [t for t in trade_returns if t > 0]
    losses = [-t for t in trade_returns if t <= 0]
    if n == 0:
        return Kelly(0.0, 0.0, None, None, 0, L("没有历史交易样本", "No historical trades"))
    w = len(wins) / n
    if not losses or sum(losses) == 0:
        return Kelly(0.0, 0.0, w, None, n, L("历史上没有亏损交易，无法估计盈亏比（样本偏差）",
                                                      "No losing trades in history, so the payoff ratio cannot be estimated (sample bias)"))
    if not wins:
        return Kelly(-1.0, 0.0, w, 0.0, n, L("历史上没有盈利交易", "No winning trades in history"))
    payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    full = w - (1 - w) / payoff
    if n < min_trades:
        return Kelly(full, 0.0, w, payoff, n, L(f"样本只有 {n} 笔（<{min_trades}），凯利仓位按 0 处理",
                                                           f"Only {n} trades (<{min_trades}); Kelly position treated as 0"))
    return Kelly(full, max(0.0, min(cap, full * fraction)), w, payoff, n,
                 L("期望为负，凯利仓位为 0", "Negative expectancy; Kelly position is 0") if full <= 0 else "")


@dataclass
class Concentration:
    weights: dict[str, float]
    max_symbol: str = ""
    max_weight: float = 0.0
    top3: float = 0.0
    hhi: float = 0.0
    effective_n: float = 0.0
    warnings: list[str] = field(default_factory=list)


def concentration(values: dict[str, float], max_pct: float = 25.0, top3_pct: float = 60.0) -> Concentration:
    total = sum(v for v in values.values() if v > 0)
    if total <= 0:
        return Concentration({})
    w = {k: v / total for k, v in values.items() if v > 0}
    ordered = sorted(w.items(), key=lambda kv: -kv[1])
    hhi = sum(x * x for x in w.values())
    c = Concentration(w, ordered[0][0], ordered[0][1], sum(x for _, x in ordered[:3]), hhi, 1.0 / hhi)
    for sym, x in ordered:
        if x * 100 > max_pct + 1e-9:
            c.warnings.append(L(f"{sym} 占组合 {x*100:.1f}%，超过单只上限 {max_pct:.0f}%。",
                                f"{sym} is {x*100:.1f}% of the portfolio, above the per-stock cap of {max_pct:.0f}%."))
    if len(ordered) > 3 and c.top3 * 100 > top3_pct + 1e-9:
        c.warnings.append(L(f"前三大持仓合计 {c.top3*100:.1f}%，超过 {top3_pct:.0f}%。",
                            f"The top three holdings total {c.top3*100:.1f}%, above {top3_pct:.0f}%."))
    if len(ordered) >= 3 and c.effective_n < 3:
        c.warnings.append(L(f"有效分散度只相当于 {c.effective_n:.1f} 只等权股票。",
                            f"Effective diversification is only equivalent to {c.effective_n:.1f} equal-weight stocks."))
    return c
