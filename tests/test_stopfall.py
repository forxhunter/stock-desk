"""The stop-falling pattern by its formulas, and the alerts that wait for it (buy levels, range edges, indices)."""
import numpy as np
import pandas as pd

from stockdesk.quant import range_trading as rt
from stockdesk.quant import stopfall as sf

from conftest import make_bars
from test_engine_misc import env  # noqa: F401  (fixture)
from test_range_trading import oscillating

FLAT = [100 + 0.5 * (i % 2) for i in range(70)]
FALL = [100 - 1.5 * k for k in range(1, 11)]              # ten sessions down to 85


def ohlc(rows, volume=None):
    """Bars from explicit (open, high, low, close) rows."""
    idx = pd.bdate_range("2025-01-01", periods=len(rows))
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = volume if volume is not None else 1e6
    return df


def bars(close, spread=0.004):
    return make_bars(close, start="2025-01-01", spread=spread)


def test_a_falling_price_is_not_a_stop_and_a_1_2_3_bottom_is():
    falling = sf.state(bars(FLAT + FALL))
    assert falling.ok and falling.declined and not falling.holds and not falling.formed and falling.age == 0
    assert [k for k, ok, _ in falling.checks if not ok] == ["holds", "structure", "above_ma"] and len(falling.missing) == 3
    rally = FLAT + FALL + [86.0, 87.5, 89.0, 89.5]           # up a few days: the low holds, but no structure yet
    st = sf.state(bars(rally))
    assert st.holds and st.above_ma and not st.structure and not st.formed
    done = sf.state(bars(rally + [87.6, 86.9, 90.2]))        # pull-back above the low, then a close over the rally high
    assert done.formed and "123" in done.structures and done.low_date == bars(rally).index[80] and done.age == 6
    p2, p3, _ = done.info["123"]
    assert p3 > done.low and done.close > p2 and done.drop_atr >= 3 and done.formed_on == done.asof
    assert all(ok for _, ok, _ in done.checks) and "1-2-3 底" in done.checks[2][2]
    lower = sf.state(bars(rally + [87.6, 86.9, 90.2, 84.0]))  # a new low: the count starts again
    assert not lower.formed and lower.age == 0 and lower.low < done.low
    assert not sf.state(bars(FLAT)).declined and not sf.state(bars(FLAT[:30])).ok


def test_failed_breakdown_reversal_candle_and_follow_through():
    dip = FLAT[:30] + [97, 94, 91, 90, 92, 94, 96] + [96 + 0.4 * (i % 2) for i in range(14)]
    two_b = sf.state(bars(dip + [94.5, 93, 91.5, 90, 88.6, 90.6, 91.4, 92.3, 93.1]))    # undercuts 90, back above it at once
    assert two_b.formed and "2b" in two_b.structures and two_b.info["2b"][0] > two_b.low

    rows = [(c, c + 0.4, c - 0.4, c) for c in FLAT + FALL[:-1]]
    rows += [(86.0, 86.2, 84.0, 86.1),                       # a hammer at the low
             (86.2, 87.0, 86.0, 86.8), (86.8, 87.6, 86.6, 87.4), (87.4, 88.2, 87.2, 88.0), (88.0, 88.9, 87.8, 88.7)]
    hammer = sf.state(ohlc(rows))
    assert hammer.formed and hammer.structures == ["candle"] and hammer.info["candle"][0] == "hammer" and "锤子线" in hammer.checks[2][2]

    slow = [(c, c + 0.4, c - 0.4, c) for c in FLAT + FALL] + [(85.0 + 0.2 * k, 85.6 + 0.2 * k, 84.9 + 0.2 * k, 85.3 + 0.2 * k) for k in range(1, 4)]
    vol = [1e6] * (len(slow) - 1) + [1e6]
    quiet = sf.state(ohlc(slow, vol))
    assert quiet.holds and not quiet.structure                # drifting up without any of the four structures
    ft = slow + [(86.0, 88.9, 85.9, 88.7)]                    # day 4 of the attempt: +3 % on twice the volume
    follow = sf.state(ohlc(ft, vol + [2e6]))
    assert follow.formed and "follow" in follow.structures and follow.info["follow"][2] == 2.0
    assert "follow" not in sf.state(ohlc(ft, vol + [0.9e6])).structures      # the same day on lower volume does not count


def test_signals_are_causal_and_the_record_counts_failures():
    wave = FLAT[:30]
    for k in range(8):                                       # eight declines, each followed by a 1-2-3 bottom
        wave += [100 + 0.5 * (i % 2) for i in range(30)] + FALL + [86.0, 87.5, 89.0, 89.5, 87.6, 86.9, 90.2, 92, 94, 96, 98, 100]
    b = bars(wave + FLAT[:30])
    sig = sf.signals(b)
    assert len(sig) == 8 and all(t > i for t, i in sig)
    for t, i in sig[:3]:                                     # the same answer with only the bars up to that day
        cut = sf.state(b.iloc[:t + 1])
        assert cut.formed and cut.low_i == i and not sf.state(b.iloc[:t]).formed
    rec = sf.record(b)
    assert rec.n == 8 and rec.fwd[5][2] == 1.0 and rec.fwd[21][1] > 0 and rec.failed == 0.0 and len(rec.last) == 3
    assert sf.record(bars(FLAT)) is None and sf.record(b, upto=sig[2][0]).n == 2


def test_range_edge_record_counts_turns_and_breaks():
    b = make_bars(oscillating(400), spread=0.003)
    top, bottom = rt.edge_record(b, None, "sell"), rt.edge_record(b, None, "buy")
    assert top["n"] >= 5 and top["turned"] >= top["n"] - 1 and top["broke"] == 0 and top["median"] < 0
    assert bottom["n"] >= 5 and bottom["turned"] >= bottom["n"] - 1 and bottom["median"] > 0
    assert rt.edge_record(make_bars(np.linspace(100, 200, 300), spread=0.003), None, "sell") is None


# ---- in the engine ------------------------------------------------------------------------------------

def _set(market, sym, close):
    idx = market.daily["SPY"].index
    df = make_bars(close[-len(idx):], spread=0.004)
    df.index = idx[-len(df):]
    market.daily[sym] = df
    return df


def test_buy_level_waits_for_the_pattern(env, monkeypatch):
    from stockdesk import plans
    eng, market, store, _ = env
    base = [100 + 0.5 * (i % 2) for i in range(400)]
    df = _set(market, "CCC", base + FALL)                    # at 85, still falling
    plan = {"start": "2026-01-01", "end": "2027-06-01", "buy_below": [90.0, 70.0], "target_price": 140.0}
    monkeypatch.setattr(plans, "load", lambda path=None: {"CCC": plan})
    snap = eng.refresh(force=True)
    assert not [a for a in snap.alerts if a.kind == "plan_level"]             # the level is broken, nothing is announced
    st = snap.plans[0]
    assert st.buy_reached == [90.0] and st.events == []
    assert any("止跌形态还没有成立" in t and "低点守住" in t for _, t in st.flags)
    from stockdesk import headless
    assert "止跌形态还没有成立" in "\n".join(headless.digest(snap))

    df = _set(market, "CCC", base + FALL + [86.0, 87.5, 89.0, 89.5, 87.6, 86.9, 90.2])     # the 1-2-3 bottom completes above the level
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "plan_level"]
    low_day = df.index[-7]                                   # the session after the last down day shares its low
    assert len(al) == 1 and al[0].id == f"plan:CCC:buy:90:{low_day:%Y%m%d}" and al[0].data["key"] == "buy"
    assert "买入价位出现止跌形态" in al[0].title and "【止跌形态】" in al[0].body and "四项条件成立 4/4" in al[0].body
    assert "1-2-3 底" in al[0].body and "作废条件" in al[0].body and "不会下单" in al[0].body
    assert not any("还没有成立" in t for _, t in snap.plans[0].flags)

    eng.cfg.set("alerts.stop_fall.require_for_buy", False)   # the old behaviour, if wanted: the level alone
    _set(market, "CCC", base + FALL)
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "plan_level"]
    assert [a.id for a in al] == ["plan:CCC:buy:90"] and "到达你的买入价位" in al[0].title


def test_range_top_reminds_and_range_bottom_waits(env, monkeypatch):
    from stockdesk import notify
    eng, market, store, _ = env
    n = len(market.daily["SPY"].index)
    wave = oscillating(n + 40, period=20)
    top = int(np.argmax(wave[-25:])) + len(wave) - 25
    _set(market, "CCC", wave[:top + 1])                      # ends at a crest
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, subject, body, **k: sent.append((subject, body)))
    eng.cfg.set("notify.email", {"enabled": True, "to": "me@example.com", "smtp_host": "h", "smtp_port": 587, "username": "bot@example.com"})
    snap = eng.refresh(force=True)
    assert snap.get("CCC").analysis.range.status == "sell"
    al = [a for a in snap.alerts if a.kind == "range"]
    assert len(al) == 1 and al[0].symbol == "CCC" and al[0].id.startswith("range:CCC:sell:") and al[0].severity == "warn"
    assert "高抛提醒" in al[0].title and "到达震荡区间上沿" in al[0].title
    for part in ("【震荡区间】", "高抛区", "区间作废位", "过去每次进入高抛区", "【CCC 现在的位置】", "不会下单", "不是预测，也不是买卖建议"):
        assert part in al[0].body
    for word in ("市值", "盈亏", "成本"):
        assert word not in al[0].body
    first = al[0].id
    assert [a.id for a in eng.build_snapshot().alerts if a.kind == "range"] == [first] and len(sent) == 1      # one reminder per visit

    trough = int(np.argmin(wave[-25:])) + len(wave) - 25
    _set(market, "CCC", wave[:trough + 1])                   # ends at a trough: at the bottom, still falling
    snap = eng.refresh(force=True)
    assert snap.get("CCC").analysis.range.status == "buy" and not [a for a in snap.alerts if a.kind == "range"]
    eng.cfg.set("alerts.range.enabled", False)
    _set(market, "CCC", wave[:top + 1])
    assert not [a for a in eng.refresh(force=True).alerts if a.kind == "range"]


def test_index_stop_alert_and_break_switch(env, monkeypatch):
    from stockdesk import engine as engine_mod
    from test_keylevels import _index_market
    eng, market, store, _ = env
    _index_market(market)
    monkeypatch.setattr(engine_mod.keylevels, "session_live", lambda *a, **k: False)
    eng.cfg.set("alerts.index_levels.break_alerts", False)
    snap = eng.refresh(force=True)
    assert snap.indices["^GSPC"].broken and not [a for a in snap.alerts if a.kind == "index_level"]     # breaks are muted on request
    idx = market.daily["SPY"].index
    up = [100 + 0.1 * i for i in range(len(idx) - 16)]
    peak = up[-1]
    leg = [peak - 0.02 * peak * k for k in range(1, 11)]                                              # -20 % in ten sessions
    low = leg[-1]
    tail = [low * 1.012, low * 1.03, low * 1.047, low * 1.031, low * 1.022, low * 1.06]
    df = make_bars(up + leg + tail, spread=0.004)
    df.index = idx[-len(df):]
    market.daily["^GSPC"] = df
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "index_level"]
    assert len(al) == 1 and ":stop:" in al[0].id and "出现止跌形态" in al[0].title and al[0].severity == "warn"
    for part in ("【止跌形态】", "四项条件成立 4/4", "作废条件", "【现在的位置】", "不是预测，也不是买卖建议"):
        assert part in al[0].body
