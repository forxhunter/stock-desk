import numpy as np
import pandas as pd
import pytest

from stockdesk.quant import indicators as ind

from conftest import make_bars


def test_sma_hand():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ind.sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_ema_hand():
    # alpha = 2/(3+1) = 0.5, seeded with the first value
    s = pd.Series([10, 20, 30, 40], dtype=float)
    assert ind.ema(s, 3).iloc[-1] == pytest.approx(((10 * .5 + 20 * .5) * .5 + 30 * .5) * .5 + 40 * .5)


def test_rsi_hand_wilder():
    """Explicit Wilder recursion written out as a loop."""
    close = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
                       46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64])
    n = 5
    d = close.diff().dropna().to_numpy()
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[0], dn[0]
    for i in range(1, len(d)):
        au = au + (up[i] - au) / n
        ad = ad + (dn[i] - ad) / n
    expected = 100 - 100 / (1 + au / ad)
    assert ind.rsi(close, n).iloc[-1] == pytest.approx(expected, rel=1e-12)


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(up).iloc[-1] == 100.0
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert ind.rsi(down).iloc[-1] == pytest.approx(0.0)
    assert np.isnan(ind.rsi(up).iloc[5])


def test_atr_hand():
    b = make_bars([10, 11, 10.5, 12, 11.5, 13, 12.5, 14], spread=0.02)
    tr = []
    for i in range(len(b)):
        h, l = b["high"].iloc[i], b["low"].iloc[i]
        if i == 0:
            tr.append(h - l)
        else:
            pc = b["close"].iloc[i - 1]
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    n = 3
    a = tr[0]
    for x in tr[1:]:
        a = a + (x - a) / n
    assert ind.atr(b["high"], b["low"], b["close"], n).iloc[-1] == pytest.approx(a)


def test_bollinger_hand():
    s = pd.Series([2, 4, 4, 4, 5, 5, 7, 9], dtype=float)     # mean 5, population sd 2
    bb = ind.bollinger(s, 8, 2.0).iloc[-1]
    assert bb["mid"] == pytest.approx(5.0)
    assert bb["upper"] == pytest.approx(9.0) and bb["lower"] == pytest.approx(1.0)
    assert bb["pct_b"] == pytest.approx(1.0)


def test_drawdown_hand():
    s = pd.Series([100, 120, 90, 110, 60, 130], dtype=float)
    assert ind.max_drawdown(s) == pytest.approx(60 / 120 - 1)
    assert ind.drawdown(s).iloc[-1] == 0.0


def test_relative_strength():
    idx = pd.bdate_range("2024-01-01", periods=11)
    a = pd.Series(np.linspace(100, 120, 11), index=idx)
    b = pd.Series(np.linspace(100, 105, 11), index=idx)
    assert ind.relative_strength(a, b, 10) == pytest.approx(0.20 - 0.05)
    assert ind.relative_strength(a, b, 11) is None


def test_causal(bars):
    """Changing future data must not change past indicator values."""
    c = bars["close"]
    cut = 500
    mod = c.copy()
    mod.iloc[cut:] *= 3.0
    for fn in (lambda s: ind.rsi(s), lambda s: ind.macd(s)["hist"], lambda s: ind.bollinger(s)["pct_b"],
               lambda s: ind.sma(s, 50), lambda s: ind.realized_vol(s)):
        pd.testing.assert_series_equal(fn(c).iloc[:cut], fn(mod).iloc[:cut])


# ---- cross-check against the independent `ta` library ---------------------------------

ta = pytest.importorskip("ta")


def test_against_ta_library(bars):
    c, h, l = bars["close"], bars["high"], bars["low"]
    # compare after a long warm-up so different seeding conventions have decayed
    k = slice(300, None)
    np.testing.assert_allclose(ind.rsi(c)[k], ta.momentum.RSIIndicator(c, 14).rsi()[k], rtol=1e-6)
    m = ta.trend.MACD(c)
    mine = ind.macd(c)
    np.testing.assert_allclose(mine["macd"][k], m.macd()[k], rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(mine["signal"][k], m.macd_signal()[k], rtol=1e-6, atol=1e-8)
    bb = ta.volatility.BollingerBands(c, 20, 2)
    np.testing.assert_allclose(ind.bollinger(c)["upper"][k], bb.bollinger_hband()[k], rtol=1e-9)
    np.testing.assert_allclose(ind.bollinger(c)["lower"][k], bb.bollinger_lband()[k], rtol=1e-9)
    np.testing.assert_allclose(ind.sma(c, 50)[k], ta.trend.SMAIndicator(c, 50).sma_indicator()[k], rtol=1e-12)
    np.testing.assert_allclose(ind.atr(h, l, c)[k], ta.volatility.AverageTrueRange(h, l, c, 14).average_true_range()[k],
                               rtol=1e-4)
