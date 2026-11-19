import numpy as np
import pandas as pd
import pytest

from stockdesk.quant import optimizer as opt
from stockdesk.quant import risk
from stockdesk.quant.analysis import analyze

from conftest import random_walk


# ---- risk ------------------------------------------------------------------------------

def test_atr_levels():
    lv = risk.atr_levels(100, 2.5, 2.0, 2.0)
    assert lv.stop == 95 and lv.target == 110 and lv.risk_per_share == 5
    assert risk.atr_levels(100, 0) is None and risk.atr_levels(100, float("nan")) is None


def test_shares_for_risk():
    # 1% of 50k = 500 at risk; 5 per share -> 100 shares
    assert risk.shares_for_risk(50_000, 1.0, 100, 95) == 100
    assert risk.shares_for_risk(50_000, 1.0, 100, 100) == 0
    assert risk.shares_for_risk(0, 1.0, 100, 95) == 0


def test_vol_budget_weight():
    assert risk.vol_budget_weight(0.30, 3.0, 0.25) == pytest.approx(0.10)
    assert risk.vol_budget_weight(0.05, 3.0, 0.25) == 0.25          # capped
    assert risk.vol_budget_weight(0.0) == 0.0


def test_kelly():
    # W=0.6, payoff=2 -> f* = 0.6 - 0.4/2 = 0.4 ; quarter Kelly 0.1
    trades = [0.10] * 6 + [-0.05] * 4
    k = risk.kelly_from_trades(trades, 0.25, 0.15, min_trades=10)
    assert k.full == pytest.approx(0.4) and k.suggested == pytest.approx(0.10)
    assert risk.kelly_from_trades(trades, 1.0, 0.15).suggested == 0.15       # cap
    assert risk.kelly_from_trades(trades[:5], 0.25, 0.15).suggested == 0.0   # too few trades
    neg = risk.kelly_from_trades([0.02] * 3 + [-0.05] * 7)
    assert neg.full < 0 and neg.suggested == 0.0
    assert risk.kelly_from_trades([0.1] * 12).suggested == 0.0               # no losses: not trusted
    assert risk.kelly_from_trades([]).suggested == 0.0


def test_concentration():
    c = risk.concentration({"A": 50, "B": 30, "C": 10, "D": 10}, 25, 60)
    assert c.max_symbol == "A" and c.max_weight == pytest.approx(0.5)
    assert c.top3 == pytest.approx(0.9)
    assert c.effective_n == pytest.approx(1 / (0.25 + 0.09 + 0.01 + 0.01))
    assert len(c.warnings) >= 3
    assert risk.concentration({"A": 1, "B": 1, "C": 1, "D": 1, "E": 1}).warnings == []
    assert risk.concentration({}).weights == {}


# ---- optimiser -------------------------------------------------------------------------

def test_min_vol_two_assets_analytic():
    s1, s2, rho = 0.2, 0.3, 0.25
    cov = np.array([[s1**2, rho * s1 * s2], [rho * s1 * s2, s2**2]])
    w1 = (s2**2 - rho * s1 * s2) / (s1**2 + s2**2 - 2 * rho * s1 * s2)
    w = opt.min_vol_weights(cov)
    assert w == pytest.approx([w1, 1 - w1], abs=1e-5)


def test_risk_parity_uncorrelated_is_inverse_vol():
    vols = np.array([0.1, 0.2, 0.4])
    w = opt.risk_parity_weights(np.diag(vols**2))
    inv = (1 / vols) / (1 / vols).sum()
    assert w == pytest.approx(inv, abs=1e-4)
    cov = np.diag(vols**2)
    rc = w * (cov @ w) / (w @ cov @ w)
    assert rc == pytest.approx([1 / 3] * 3, abs=1e-4)


def test_max_sharpe_matches_tangency():
    mu = np.array([0.08, 0.12, 0.10])
    cov = np.array([[0.04, 0.006, 0.0], [0.006, 0.09, 0.01], [0.0, 0.01, 0.0625]])
    raw = np.linalg.solve(cov, mu)
    expected = raw / raw.sum()                     # all positive here -> long-only solution
    assert (expected > 0).all()
    assert opt.max_sharpe_weights(mu, cov) == pytest.approx(expected, abs=1e-4)


def test_bounds_and_budget():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(6, 6))
    cov = a @ a.T / 100 + np.eye(6) * 0.01
    mu = rng.normal(0.08, 0.05, 6)
    for w in (opt.min_vol_weights(cov, 0.3), opt.max_sharpe_weights(mu, cov, 0.0, 0.3),
              opt.risk_parity_weights(cov, 0.3), opt.mean_variance_weights(mu, cov, 3.0, 0.3)):
        assert w.sum() == pytest.approx(1.0)
        assert (w >= -1e-9).all() and (w <= 0.3 + 1e-6).all()


def test_ledoit_wolf():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(60, 10)) * 0.01
    s, k = opt.ledoit_wolf(x)
    assert 0 < k <= 1
    sample = np.cov(x.T, bias=True)
    # shrinkage pulls off-diagonals towards zero and keeps the trace
    assert np.trace(s) == pytest.approx(np.trace(sample))
    off = ~np.eye(10, dtype=bool)
    assert np.abs(s[off]).sum() < np.abs(sample[off]).sum()
    assert np.all(np.linalg.eigvalsh(s) > 0)


def test_optimize_and_rebalance():
    px = pd.DataFrame({f"S{i}": random_walk(400, seed=i, sigma=0.01 * (i + 1))["close"] for i in range(4)})
    res = opt.optimize(px, max_weight=0.5)
    assert res is not None and set(res.weights) == set(opt.METHODS)
    for w in res.weights.values():
        assert w.sum() == pytest.approx(1.0)
    assert res.weights["min_vol"]["S0"] > res.weights["min_vol"]["S3"]    # low vol asset favoured
    assert opt.optimize(px[["S0"]]) is None

    target = pd.Series({"A": 0.5, "B": 0.5})
    d = opt.rebalance_deltas({"A": 8000, "B": 2000, "C": 0.0}, target, {"A": 100, "B": 50, "C": 10}, min_trade_pct=1)
    assert d.loc["A", "delta_value"] == pytest.approx(-3000) and d.loc["A", "delta_shares"] == -30
    assert d.loc["B", "delta_value"] == pytest.approx(3000) and d.loc["B", "delta_shares"] == 60
    assert d["delta_value"].sum() == pytest.approx(0)
    small = opt.rebalance_deltas({"A": 5040, "B": 4960}, target, {"A": 100, "B": 50}, min_trade_pct=1)
    assert (small["delta_value"] == 0).all()                              # below the 1% threshold


# ---- analysis glue ---------------------------------------------------------------------

def test_analyze_outputs_reasoning(bars):
    bench = random_walk(900, seed=99)["close"]
    a = analyze("TEST", bars, bench, qty=10, avg_cost=float(bars["close"].iloc[-1]) * 0.9,
                equity=100_000, position_value=10 * float(bars["close"].iloc[-1]))
    assert a.ok and a.stance in ("偏多", "中性", "偏空")
    assert -1 <= a.composite <= 1
    assert {s.key for s in a.signals} == {"trend", "momentum", "rel_strength", "drawdown", "volatility"}
    assert all(s.detail and s.inputs for s in a.signals)
    assert a.levels.basis == "cost" and a.levels.stop < a.levels.entry < a.levels.target
    assert 0 <= a.sizing.suggested_weight <= 0.25
    assert any("历史检验" in r for r in a.rationale) and any("仓位上限" in r for r in a.rationale)
    assert any("不构成投资建议" in c for c in a.caveats)
    assert not analyze("X", bars.iloc[:30]).ok
