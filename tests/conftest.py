import numpy as np
import pandas as pd
import pytest


def make_bars(close, start="2021-01-04", spread=0.01) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.bdate_range(start, periods=len(close))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(len(close), 1e6)}, index=idx)


def random_walk(n=900, seed=7, mu=0.0004, sigma=0.02, start=100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(mu, sigma, n)))
    return make_bars(close)


@pytest.fixture
def bars():
    return random_walk()
