"""Technical indicators on pandas Series. All are causal: value at t uses data <= t only."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """Standard EMA, alpha = 2/(n+1), seeded with the first value (adjust=False)."""
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (RMA): alpha = 1/n."""
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    # diff() makes the first row NaN; skip it so smoothing starts on real changes
    avg_up = wilder(up.iloc[1:], n).reindex(close.index)
    avg_dn = wilder(dn.iloc[1:], n).reindex(close.index)
    rs = avg_up / avg_dn.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_dn != 0.0, 100.0)          # no losses in the window -> 100
    return out.where(avg_up.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder ATR."""
    return wilder(true_range(high, low, close), n)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower)
    pct_b = (close - lower) / width.replace(0.0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "pct_b": pct_b, "bandwidth": width / mid})


def drawdown(close: pd.Series, window: int | None = None) -> pd.Series:
    """Drawdown from the running (or rolling ``window``) peak, as a negative fraction."""
    peak = close.cummax() if window is None else close.rolling(window, min_periods=1).max()
    return close / peak - 1.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float(drawdown(equity).min())


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    """Annualised close-to-close volatility of log returns."""
    r = np.log(close / close.shift(1))
    return r.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(TRADING_DAYS)


def pct_change_n(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1.0


def relative_strength(close: pd.Series, bench: pd.Series, n: int) -> float | None:
    """Excess return over ``n`` bars versus the benchmark (aligned on common dates)."""
    df = pd.concat([close, bench], axis=1, join="inner").dropna()
    if len(df) <= n:
        return None
    a = df.iloc[-1, 0] / df.iloc[-1 - n, 0] - 1.0
    b = df.iloc[-1, 1] / df.iloc[-1 - n, 1] - 1.0
    return float(a - b)
