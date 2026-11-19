"""Long-only portfolio optimisation (own implementation on scipy SLSQP).

Methods: minimum volatility, maximum Sharpe, risk parity, mean-variance utility.
Covariance uses Ledoit-Wolf shrinkage towards a scaled identity; expected returns are
historical means shrunk 50% towards the cross-sectional average, because sample means
are extremely noisy. Max-Sharpe / mean-variance inherit that noise - treat with care.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize

TRADING_DAYS = 252


def ledoit_wolf(returns: np.ndarray) -> tuple[np.ndarray, float]:
    """Ledoit & Wolf (2004) shrinkage of the sample covariance towards mu*I.

    Returns (shrunk covariance, shrinkage intensity in [0, 1]).
    """
    x = np.asarray(returns, dtype=float)
    t, n = x.shape
    x = x - x.mean(axis=0)
    s = x.T @ x / t
    mu = np.trace(s) / n
    d2 = np.sum((s - mu * np.eye(n)) ** 2) / n
    if d2 <= 0:
        return s, 0.0
    b2 = 0.0
    for k in range(t):
        row = x[k][:, None]
        b2 += np.sum((row @ row.T - s) ** 2) / n
    b2 = min(b2 / (t * t), d2)
    shrink = b2 / d2
    return shrink * mu * np.eye(n) + (1 - shrink) * s, float(shrink)


def shrunk_mean(returns: pd.DataFrame, intensity: float = 0.5) -> pd.Series:
    mu = returns.mean() * TRADING_DAYS
    return (1 - intensity) * mu + intensity * mu.mean()


@dataclass
class OptResult:
    symbols: list[str]
    mu: pd.Series
    cov: pd.DataFrame
    shrinkage: float
    n_obs: int
    weights: dict[str, pd.Series] = field(default_factory=dict)   # method -> weights
    stats: dict[str, dict] = field(default_factory=dict)          # method -> ret/vol/sharpe
    notes: list[str] = field(default_factory=list)


def _solve(fun, n: int, max_w: float, x0: np.ndarray | None = None) -> np.ndarray:
    x0 = np.full(n, 1.0 / n) if x0 is None else x0
    res = minimize(fun, x0, method="SLSQP", bounds=[(0.0, max_w)] * n,
                   constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
                   options={"maxiter": 500, "ftol": 1e-12})
    w = np.clip(res.x, 0.0, max_w)
    return w / w.sum()


def min_vol_weights(cov: np.ndarray, max_w: float = 1.0) -> np.ndarray:
    return _solve(lambda w: w @ cov @ w, len(cov), max_w)


def max_sharpe_weights(mu: np.ndarray, cov: np.ndarray, rf: float = 0.0, max_w: float = 1.0) -> np.ndarray:
    def neg_sharpe(w):
        vol = np.sqrt(max(w @ cov @ w, 1e-18))
        return -(w @ mu - rf) / vol
    best, best_val = None, np.inf
    n = len(mu)
    starts = [np.full(n, 1.0 / n), min_vol_weights(cov, max_w)]
    for x0 in starts:  # the ratio is not convex in w; try two starts
        w = _solve(neg_sharpe, n, max_w, x0)
        v = neg_sharpe(w)
        if v < best_val:
            best, best_val = w, v
    return best


def risk_parity_weights(cov: np.ndarray, max_w: float = 1.0) -> np.ndarray:
    """Equal risk contribution: each asset contributes the same share of portfolio variance."""
    n = len(cov)

    def obj(w):
        port_var = w @ cov @ w
        rc = w * (cov @ w) / port_var
        return np.sum((rc - 1.0 / n) ** 2)
    inv_vol = 1.0 / np.sqrt(np.diag(cov))
    return _solve(obj, n, max_w, inv_vol / inv_vol.sum())


def mean_variance_weights(mu: np.ndarray, cov: np.ndarray, risk_aversion: float = 3.0,
                          max_w: float = 1.0) -> np.ndarray:
    return _solve(lambda w: -(w @ mu) + 0.5 * risk_aversion * (w @ cov @ w), len(mu), max_w)


METHODS = {
    "risk_parity": "风险平价",
    "min_vol": "最小波动",
    "max_sharpe": "最大夏普",
    "mean_variance": "均值-方差",
}


def optimize(prices: pd.DataFrame, max_weight: float = 0.35, lookback: int = 504, rf: float = 0.0) -> OptResult | None:
    """``prices``: daily closes, one column per symbol."""
    px = prices.dropna(axis=1, how="all").tail(lookback + 1)
    rets = px.pct_change().dropna(how="any")
    rets = rets.loc[:, rets.std() > 0]
    n = rets.shape[1]
    if n < 2 or len(rets) < 60:
        return None
    max_w = max(max_weight, 1.0 / n + 1e-6)          # keep the problem feasible
    cov_d, shrink = ledoit_wolf(rets.to_numpy())
    cov = cov_d * TRADING_DAYS
    mu = shrunk_mean(rets)
    syms = list(rets.columns)
    res = OptResult(syms, mu, pd.DataFrame(cov, index=syms, columns=syms), shrink, len(rets))
    mu_a = mu.to_numpy()
    raw = {
        "risk_parity": risk_parity_weights(cov, max_w),
        "min_vol": min_vol_weights(cov, max_w),
        "max_sharpe": max_sharpe_weights(mu_a, cov, rf, max_w),
        "mean_variance": mean_variance_weights(mu_a, cov, 3.0, max_w),
    }
    for k, w in raw.items():
        res.weights[k] = pd.Series(w, index=syms)
        vol = float(np.sqrt(w @ cov @ w))
        ret = float(w @ mu_a)
        res.stats[k] = {"ret": ret, "vol": vol, "sharpe": (ret - rf) / vol if vol > 0 else 0.0}
    res.notes = [
        f"基于最近 {len(rets)} 个交易日、{n} 只股票；协方差做了 Ledoit-Wolf 收缩（强度 {shrink:.2f}）。",
        "预期收益来自历史均值并向平均值收缩 50%，误差仍然很大；最大夏普 / 均值-方差对它非常敏感，"
        "容易过拟合历史。风险平价和最小波动只依赖协方差，相对稳健。",
        f"单只权重上限 {max_w*100:.0f}%，只做多、不加杠杆。结果是参考，不是指令。",
    ]
    return res


def rebalance_deltas(current_values: dict[str, float], target_weights: pd.Series,
                     prices: dict[str, float], total_value: float | None = None,
                     min_trade_pct: float = 1.0) -> pd.DataFrame:
    """Difference between target and current holdings, in dollars and (approximate) shares."""
    total = total_value if total_value else sum(current_values.values())
    rows = []
    for sym in sorted(set(current_values) | set(target_weights.index)):
        cur = float(current_values.get(sym, 0.0))
        tw = float(target_weights.get(sym, 0.0))
        tgt = tw * total
        delta = tgt - cur
        px = prices.get(sym)
        small = abs(delta) < total * min_trade_pct / 100.0
        rows.append({
            "symbol": sym, "current_value": cur, "current_weight": cur / total if total else 0.0,
            "target_weight": tw, "target_value": tgt, "delta_value": 0.0 if small else delta,
            "delta_shares": 0.0 if small or not px else float(np.trunc(delta / px)),
        })
    return pd.DataFrame(rows).set_index("symbol")
