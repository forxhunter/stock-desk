import numpy as np
import pandas as pd
import pytest

from stockdesk.quant import backtest as bt

from conftest import make_bars, random_walk


def test_exact_trade_accounting():
    # close: 10 11 12 13 12 11 ; open[t] = close[t-1]
    b = make_bars([10, 11, 12, 13, 12, 11])
    sig = pd.Series([0, 1, 1, 0, 0, 0], index=b.index, dtype=float)
    # the 30-bar minimum is for real data; call the engine pieces directly
    res = bt.run_backtest(pd.concat([b] * 1), sig.reindex(b.index), cost_bps=0)
    assert res.n_bars == 0          # too short -> refuses
    long_b = make_bars([10] * 30 + [10, 11, 12, 13, 12, 11])
    s = pd.Series(0.0, index=long_b.index)
    s.iloc[31] = 1                   # decided at close of day 31 (price 11)
    s.iloc[32] = 1                   # still long at close of day 32
    res = bt.run_backtest(long_b, s, cost_bps=0)
    # enter at open of day 32 (= close day 31 = 11), exit at open of day 34 (= close day 33 = 13)
    assert res.n_trades == 1
    assert res.trade_returns[0] == pytest.approx(13 / 11 - 1)
    assert res.total_return == pytest.approx(13 / 11 - 1)
    assert res.hit_rate == 1.0


def test_signal_cannot_capture_same_day_move():
    """A signal that 'knows' today's close must not earn today's return."""
    b = random_walk(600, seed=3)
    up_today = (b["close"] > b["close"].shift(1)).astype(float)
    res = bt.run_backtest(b, up_today, cost_bps=0)
    cheat = float(((b["close"].pct_change().clip(lower=0) + 1).prod()) - 1)
    assert res.total_return < cheat / 10        # nowhere near the clairvoyant result
    assert abs(res.sharpe) < 2.0                # on a random walk it is just noise


@pytest.mark.parametrize("key", list(bt.STRATEGIES))
def test_no_lookahead_truncation_invariance(key):
    """Equity up to day T must not change when data after T is altered."""
    b = random_walk(700, seed=11)
    fn = bt.STRATEGIES[key][2]
    cut = 500
    b2 = b.copy()
    b2.iloc[cut:] = b2.iloc[cut:] * 1.7
    e1 = bt.run_backtest(b, fn(b), 10).equity
    e2 = bt.run_backtest(b2, fn(b2), 10).equity
    assert e1 is not None and e2 is not None
    common = e1.index[e1.index < b.index[cut]]
    assert len(common) > 100
    pd.testing.assert_series_equal(e1.loc[common], e2.loc[common])


def test_costs_reduce_returns():
    b = random_walk(800, seed=5)
    sig = bt.strat_macd(b)
    free = bt.run_backtest(b, sig, 0)
    costly = bt.run_backtest(b, sig, 25)
    assert free.n_trades == costly.n_trades > 5
    assert costly.total_return < free.total_return
    # each round trip costs two sides
    assert costly.trade_returns[0] == pytest.approx((1 + free.trade_returns[0]) * (1 - 0.0025) ** 2 - 1)


def test_always_long_matches_buy_and_hold():
    b = random_walk(400, seed=2)
    sig = pd.Series(1.0, index=b.index)
    res = bt.run_backtest(b, sig, 0)
    # first day is entered at the open (= previous close in this fixture) -> identical to B&H
    assert res.total_return == pytest.approx(res.bh_return, rel=1e-9)
    assert res.max_dd == pytest.approx(res.bh_max_dd, rel=1e-9)
    assert res.exposure == 1.0 and res.n_trades == 0 and res.hit_rate is None


def test_latch_and_wilson():
    idx = pd.RangeIndex(6)
    entry = pd.Series([0, 1, 0, 0, 1, 1], index=idx, dtype=bool)
    exit_ = pd.Series([0, 0, 0, 1, 0, 1], index=idx, dtype=bool)
    assert bt.latch(entry, exit_).tolist() == [0, 1, 1, 0, 1, 0]
    lo, hi = bt.wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert bt.wilson_interval(0, 0) is None


def test_small_sample_is_flagged():
    b = random_walk(500, seed=9)
    res = bt.run_backtest(b, bt.strat_ma_cross(b, 50, 200), 10)
    assert res.n_trades < 10
    assert any("不可靠" in n for n in res.notes)


def test_backtest_all_handles_short_history():
    out = bt.backtest_all(random_walk(80))
    assert len(out) == len(bt.STRATEGIES)
    assert all(r.n_bars == 0 or r.years > 0 for r in out)
