"""Phase-based range trading (高抛低吸): detection, signals, breakout handling, wiring."""
import numpy as np
import pandas as pd
import pytest

from stockdesk.quant import backtest as bt
from stockdesk.quant import range_trading as rt
from stockdesk.quant.analysis import analyze

from conftest import make_bars


def oscillating(n=260, lo=95.0, hi=105.0, period=20, seed=1, noise=0.15):
    """A clean sideways market bouncing between ~lo and ~hi."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    mid, amp = (hi + lo) / 2, (hi - lo) / 2
    close = mid + amp * np.sin(2 * np.pi * t / period) + rng.normal(0, noise, n)
    return close


def trending(n=260, start=100.0, step=0.006, seed=2):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(step + rng.normal(0, 0.004, n)))


def test_sideways_market_is_detected_with_sensible_levels():
    b = make_bars(oscillating(), spread=0.003)
    f = rt.range_frame(b)
    assert f["valid"].iloc[100:].mean() > 0.9
    st = rt.range_state(b)
    assert st.status in ("buy", "sell", "mid")
    assert 94 < st.support < 98 and 102 < st.resistance < 106
    assert st.buy_band[0] == pytest.approx(st.support) and st.sell_band[1] == pytest.approx(st.resistance)
    assert st.stop < st.support < st.resistance < st.breakout


def test_trend_is_not_a_range():
    b = make_bars(trending(), spread=0.003)
    f = rt.range_frame(b)
    assert f["valid"].iloc[80:].mean() < 0.05
    st = rt.range_state(b)
    assert st.status == "none" and st.reasons


def test_zones_buy_near_support_sell_near_resistance():
    close = oscillating()
    b = make_bars(close, spread=0.003)
    sig, events = rt.simulate(b)
    buys = [e for e in events if e.kind == "buy"]
    sells = [e for e in events if e.kind == "sell"]
    assert len(buys) >= 5 and len(sells) >= 5
    for e in buys:
        assert e.price <= e.support + 0.2 * (e.resistance - e.support) + 1e-9
    for e in sells:
        assert e.price >= e.resistance - 0.2 * (e.resistance - e.support) - 1e-9
    res = bt.run_backtest(b, sig, 10)
    assert res.n_trades >= 5 and res.hit_rate > 0.7 and res.total_return > 0


def test_breakdown_stops_out_and_breakout_flags():
    base = list(oscillating(200))
    # price ends at support, then collapses well below it
    down = make_bars(base + list(np.linspace(base[-1], 80, 8)), spread=0.003)
    st = rt.range_state(down)
    assert st.status == "breakdown" and st.score < 0
    _, ev = rt.simulate(down)
    last_buy = max((e.i for e in ev if e.kind == "buy"), default=-1)
    assert any(e.kind == "stop" and e.i > last_buy for e in ev) or not rt.simulate(down)[0].iloc[-1]
    up = make_bars(base + list(np.linspace(base[-1], 125, 8)), spread=0.003)
    st = rt.range_state(up)
    assert st.status == "breakout_up"
    assert rt.simulate(up)[0].iloc[-1] == 0          # no range position after the breakout


def test_live_price_override_and_short_history():
    b = make_bars(oscillating(), spread=0.003)
    st = rt.range_state(b, price=float(rt.range_state(b).support))
    assert st.status in ("buy", "loose")
    short = rt.range_state(make_bars(oscillating(40)))
    assert short.status == "short"


def test_params_override_and_unknown_keys_ignored():
    p = rt.params({"lookback": "40", "zone": 0.1, "enabled": True, "nonsense": 3})
    assert p["lookback"] == 40 and p["zone"] == 0.1 and "nonsense" not in p
    b = make_bars(oscillating(), spread=0.003)
    a = rt.simulate(b, {"zone": 0.1})[1]
    c = rt.simulate(b, {"zone": 0.3})[1]
    assert len(c) >= len(a)


def test_registered_in_backtests_and_analysis():
    assert "range_trading" in bt.STRATEGIES
    b = make_bars(oscillating(400), spread=0.003)
    a = analyze("XYZ", b, cfg={"range": {"enabled": True, "lookback": 60}})
    assert a.range is not None and a.range.status in ("buy", "sell", "mid")
    assert any(x.key == "range_trading" and x.n_trades > 0 for x in a.backtests)
    assert any(line.startswith("【震荡区间】") for line in a.rationale)
    off = analyze("XYZ", b, cfg={"range": {"enabled": False}})
    assert off.range is None and all(x.key != "range_trading" for x in off.backtests)


def test_range_html_renders_every_status():
    from stockdesk.ui import render, theme as T
    pal = T.Palette()
    for close in (oscillating(), trending(), list(oscillating(200)) + list(np.linspace(100, 80, 8))):
        st = rt.range_state(make_bars(close, spread=0.003))
        html = render.range_html(st, pal)
        assert "震荡区间" in html and st.label in html


def test_sample_holdings_detection(tmp_path):
    """The badge says 演示模式 only while the shipped demo holdings are still in watchlist.yaml."""
    import shutil
    from pathlib import Path
    from stockdesk.watchlist import Watchlist
    sample = Path(__file__).resolve().parents[1] / "watchlist.sample.yaml"
    wl_path = tmp_path / "watchlist.yaml"
    shutil.copyfile(sample, wl_path)
    wl = Watchlist(wl_path, sample)
    assert wl.has_sample_holdings()
    wl_path.write_text("tickers:\n  - {symbol: NVDA, shares: 3, cost: 120}\n  - AMD\n", encoding="utf-8")
    wl.load()
    assert not wl.has_sample_holdings()


def test_sample_holdings_ignored_once_broker_reports(tmp_path):
    import shutil
    from pathlib import Path
    from stockdesk.models import Position
    from stockdesk.watchlist import Watchlist
    sample = Path(__file__).resolve().parents[1] / "watchlist.sample.yaml"
    wl_path = tmp_path / "watchlist.yaml"
    shutil.copyfile(sample, wl_path)
    wl = Watchlist(wl_path, sample)
    assert "AAPL" in wl.merge_positions([])                     # demo mode still shows the sample
    merged = wl.merge_positions([Position("TSM", 9, 383.0, "IBKR")])
    assert set(merged) == {"TSM"} and merged["TSM"].qty == 9   # fake AAPL / NVDA / MSFT dropped


# ---- bottoming check ------------------------------------------------------------------------

def _fall_then_base(recover=True, n_flat=200, volume=True):
    """Long slide, then (optionally) a base with a higher low and a push back above the averages."""
    rng = np.random.default_rng(4)
    top = np.full(n_flat, 200.0) + rng.normal(0, 0.5, n_flat)
    slide = np.linspace(200, 100, 80) + rng.normal(0, 0.6, 80)
    if not recover:
        return make_bars(np.r_[top, slide], spread=0.004)
    base = np.r_[np.linspace(100, 110, 9), np.linspace(110, 104, 7), np.linspace(104, 118, 9)]
    b = make_bars(np.r_[top, slide, base + rng.normal(0, 0.3, len(base))], spread=0.004)
    if volume:                               # selling climax at the low, then heavier volume on up days
        i = int(np.argmin(b["low"].to_numpy()))
        up = b["close"].diff().to_numpy() > 0
        vol = b["volume"].to_numpy().copy()
        vol[i - 1:i + 2] = 3e6
        vol[i + 2:] = np.where(up[i + 2:], 1.8e6, 0.7e6)
        vol[i + 4:] *= np.linspace(1.0, 1.3, len(vol) - i - 4)
        b["volume"] = vol
    return b


def test_bottoming_fires_on_a_base_not_on_a_falling_knife():
    from stockdesk.quant import bottom
    st = bottom.bottom_state(_fall_then_base(True))
    assert st.eligible and st.score >= 5 and st.vol_score >= 2 and st.triggered
    quiet = bottom.bottom_state(_fall_then_base(True, volume=False))      # same prices, no volume behind them
    assert quiet.score == st.score and quiet.vol_score < 2 and not quiet.triggered
    novol = _fall_then_base(True)
    novol["volume"] = 0.0
    assert not bottom.bottom_state(novol).has_volume and not bottom.bottom_state(novol).triggered
    assert 95 < st.base_low < 104 and {"低点守住", "低点抬高", "站上 20 日线"} <= set(st.passed)
    falling = bottom.bottom_state(_fall_then_base(False))
    assert falling.eligible and not falling.triggered and falling.score <= 2
    # near its highs a stock is not "bottoming", whatever the indicators say
    high = bottom.bottom_state(make_bars(np.linspace(100, 200, 300), spread=0.004))
    assert high.ok and not high.eligible and not high.triggered
    assert not bottom.bottom_state(make_bars(np.linspace(100, 90, 50))).ok


def test_bottoming_is_causal():
    from stockdesk.quant import bottom
    b = _fall_then_base(True)
    cut = len(b) - 15
    b2 = b.copy()
    b2.iloc[cut:] = b2.iloc[cut:] * 0.5
    a, c = bottom.bottom_state(b.iloc[:cut]), bottom.bottom_state(b2.iloc[:cut])
    assert (a.score, a.vol_score, a.triggered, a.base_low) == (c.score, c.vol_score, c.triggered, c.base_low)


def test_support_zones_cluster_turning_points_below_price():
    from stockdesk.quant.levels import support_zones
    # bounces three times near 100, twice near 120, then trades at 140
    seg = lambda a, b, n=12: list(np.linspace(a, b, n))
    close = seg(130, 100) + seg(100, 125) + seg(125, 101) + seg(101, 135) + seg(135, 120) + seg(120, 138) + \
        seg(138, 99.5) + seg(99.5, 150) + seg(150, 121) + seg(121, 140)
    b = make_bars(close, spread=0.002)
    zones = support_zones(b)
    assert [round(z.level) for z in zones][-1] in (99, 100, 101) and zones[-1].year_low and zones[-1].touches >= 3
    assert any(118 <= z.level <= 123 and z.touches >= 2 for z in zones)
    assert all(z.level < 140 for z in zones) and [z.level for z in zones] == sorted((z.level for z in zones), reverse=True)
    assert support_zones(b, price=90.0) == []            # nothing below the price -> no support to report
    assert support_zones(make_bars([100] * 20)) == []
