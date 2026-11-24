"""Key levels of an index, their breaks, the past record, and the alert that is raised from them."""
import numpy as np
import pandas as pd
import pytest

from stockdesk import insight
from stockdesk.quant import keylevels as kl

from conftest import make_bars


def bars_of(close):
    return make_bars(close, spread=0.002)


def rising(n=320, start=100.0, step=0.1):
    return [start + step * i for i in range(n)]


def test_break_of_an_average_needs_a_hold_and_fires_once():
    close = rising() + [118.0, 117.0, 131.5, 117.5]            # falls through the 50-day average, bounces, falls again
    c = pd.Series(close, index=pd.bdate_range("2024-01-01", periods=len(close)))
    ev, lvl, held = kl.series(c, "ma", 50, "down", 10)
    assert bool(ev.iloc[320]) and int(held.iloc[320]) >= 10    # first close below after a long stretch above
    assert not ev.iloc[321] and not ev.iloc[323]               # still below / back below after one day above: no repeat
    up, _, _ = kl.series(c, "ma", 50, "up", 10)
    assert not up.iloc[322]                                    # two closes below are not a "hold" to reclaim from


def test_state_lists_levels_breaks_and_the_record():
    saw = []                                                   # several earlier breaks so that there is a record
    for k in range(6):
        saw += rising(120, 100 + 8 * k, 0.12) + [100 + 8 * k + 6.0] * 3
    bars = make_bars(saw + rising(150, 150, 0.1) + [146.0], spread=0.002)
    st = kl.state("^GSPC", bars, user_levels=[160, 90])
    keys = [x.key for x in st.broken]
    assert st.name == "标普 500" and {"sma50", "sma100", "sma200", "low20", "low60", "dd10", "user160"} <= set(keys)
    assert keys[0] == "user160" and st.broken[0].weight == 3   # most important first
    assert st.level("sma50").held >= 10 and st.day_pct < -5
    rec = st.records["sma50:down"]
    assert rec.n >= 3 and 21 in rec.fwd and 21 in rec.base and rec.back5 is not None and rec.dip21 is not None
    assert all(x.value < st.price for x in st.below()) and all(x.kind != "low" for x in st.above())
    assert st.reclaimed == [] and not st.live
    again = kl.state("^GSPC", make_bars(saw + rising(150, 150, 0.1) + [146.0, 145.0], spread=0.002), user_levels=[160, 90])
    assert "sma50" not in [x.key for x in again.broken]        # the day after: no new break ...
    assert again.inforce is not None and again.inforce[0].key == "sma200" and again.inforce[2] == 1
    up = kl.state("^GSPC", make_bars(rising(280) + [120.0] * 15 + [140.0], spread=0.002))
    assert {"high252", "sma50"} <= {x.key for x in up.reclaimed} and up.broken == []
    steady = kl.state("^GSPC", make_bars(rising(300), spread=0.002))
    assert steady.reclaimed == [] and steady.broken == []          # a new high every day is not an event


def test_support_zone_break_and_short_history():
    wave = [100 + 10 * np.sin(i / 6.0) for i in range(300)]
    bars = make_bars(wave + [80.0], spread=0.002)
    st = kl.state("^IXIC", bars)
    zones = [x for x in st.broken if x.kind == "zone"]
    assert zones and zones[0].zone is not None and st.price < zones[0].value
    assert kl.state("^IXIC", bars.tail(100)) is None           # too little history: no state, no alert


def test_session_clock_and_beta():
    ny = lambda s: pd.Timestamp(s, tz="America/New_York")
    assert kl.session_live(pd.Timestamp("2026-11-20 15:30"), ny("2026-11-20 10:45"))          # Friday, mid-session
    assert not kl.session_live(pd.Timestamp("2026-11-20 20:45"), ny("2026-11-20 16:05"))      # after the close
    assert not kl.session_live(pd.Timestamp("2026-11-19 20:45"), ny("2026-11-20 09:00"))      # yesterday's bar
    assert not kl.session_live(None)
    idx = pd.bdate_range("2025-01-01", periods=200)
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.01, 200)
    a, b = pd.Series(100 * np.exp(np.cumsum(r)), index=idx), pd.Series(100 * np.exp(np.cumsum(2 * r)), index=idx)
    assert kl.beta(b, a) == pytest.approx(2.0, abs=0.1) and kl.beta(b.tail(20), a) is None


def test_index_alert_text_has_every_section_and_no_advice():
    saw = []
    for k in range(6):
        saw += rising(120, 100 + 8 * k, 0.12) + [100 + 8 * k + 6.0] * 3
    bars = make_bars(saw + rising(150, 150, 0.1) + [146.0], spread=0.002)
    st = kl.state("^GSPC", bars)
    other = kl.state("^IXIC", make_bars(rising(300), spread=0.002))
    title, body = insight.index_alert(st, "down", st.broken, others={"^GSPC": st, "^IXIC": other},
                                      vix={"price": 31.0, "chg": 4.0, "pct": 14.8}, index_bars=bars)
    assert title.startswith("标普 500 收盘跌破") and "等" in title
    for section in ("【发生了什么】", "【现在的位置】", "【下方的关键位】", "【历史上同类情况】", "【接下来看什么】"):
        assert section in body
    assert "上涨的比例" in body and "5 个交易日内收回" in body and "恐慌水平" in body and "纳斯达克综合指数" in body
    assert "不是预测，也不是买卖建议" in body and "建议买" not in body and "应该卖" not in body
    assert body.split("\n")[0].startswith("标普 500（^GSPC）") and insight.is_head("【发生了什么】")
    assert insight.tidy("跌破200日均线") == "跌破 200 日均线"


# ---- in the engine: alerts, mail, the other alert kinds ------------------------------------------------

from test_engine_misc import env  # noqa: E402,F401  (fixture)


def _index_market(market, last=146.0):
    """Give the fake market an S&P that breaks down on its last bar, a calm Nasdaq and a VIX."""
    idx = market.daily["SPY"].index
    saw = []
    for k in range(6):
        saw += rising(120, 100 + 8 * k, 0.12) + [100 + 8 * k + 6.0] * 3
    for sym, close in (("^GSPC", (saw + rising(150, 150, 0.1) + [last])[-len(idx):]), ("^IXIC", rising(len(idx))),
                       ("^VIX", [18.0] * (len(idx) - 1) + [27.0])):
        df = make_bars(close, spread=0.002)
        df.index = idx[-len(df):]
        market.daily[sym] = df


def test_engine_raises_index_alert_intraday_then_at_the_close_and_mails_once(env, monkeypatch):
    from stockdesk import engine as engine_mod, headless, notify
    eng, market, store, _ = env
    _index_market(market)
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, subject, body, **k: sent.append((subject, body)))
    eng.cfg.set("notify.email", {"enabled": True, "to": "me@example.com", "smtp_host": "h", "smtp_port": 587,
                                 "username": "bot@example.com"})
    monkeypatch.setattr(engine_mod.keylevels, "session_live", lambda *a, **k: True)
    snap = eng.refresh(force=True)
    assert set(snap.indices) == {"^GSPC", "^IXIC"} and snap.vix["price"] == 27.0 and snap.indices["^GSPC"].live
    al = [a for a in snap.alerts if a.kind == "index_level"]
    assert len(al) == 1 and al[0].symbol == "^GSPC" and al[0].severity == "warn" and ":live:" in al[0].id
    assert "盘中跌破" in al[0].title and "还没有收盘确认" in al[0].body and "【对你的自选" in al[0].body
    assert "AAA" in al[0].body and "β" in al[0].body and "VIX 27.0" in al[0].body
    eng.build_snapshot()
    assert len(sent) == 1 and "盘中跌破" in sent[0][0]                      # one mail for the intraday break
    for word in ("市值", "盈亏", "成本", "股 ·", "10 股"):                    # quotes and levels only
        assert word not in sent[0][1]

    monkeypatch.setattr(engine_mod.keylevels, "session_live", lambda *a, **k: False)
    snap = eng.build_snapshot()
    al = [a for a in snap.alerts if a.kind == "index_level"]
    assert len(al) == 1 and al[0].id.endswith(":close") and al[0].severity == "high" and "收盘确认" in al[0].body
    eng.build_snapshot()
    assert len(sent) == 2 and "收盘跌破" in sent[1][0]                      # and one when the close confirms it
    digest = "\n".join(headless.digest(snap))
    assert "【大盘关键位】" in digest and "标普 500" in digest and "下方关键位" in digest

    eng.cfg.set("alerts.index_levels.enabled", False)
    snap = eng.refresh(force=True)
    assert snap.indices == {} and not [a for a in snap.alerts if a.kind == "index_level"]


def test_quiet_index_raises_nothing_and_upward_moves_stay_off_the_mail(env, monkeypatch):
    from stockdesk import engine as engine_mod, notify
    eng, market, store, _ = env
    _index_market(market, last=165.5)                                       # keeps rising: no break
    idx = market.daily["SPY"].index
    up = make_bars((rising(len(idx) - 16) + [rising(len(idx) - 16)[-1] - 8] * 15 + [rising(len(idx))[-1] + 5]), spread=0.002)
    up.index = idx[-len(up):]
    market.daily["^IXIC"] = up                                              # dips, then closes at a new high
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, subject, body, **k: sent.append(subject))
    eng.cfg.set("notify.email", {"enabled": True, "to": "me@example.com", "smtp_host": "h", "smtp_port": 587,
                                 "username": "bot@example.com"})
    monkeypatch.setattr(engine_mod.keylevels, "session_live", lambda *a, **k: False)
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "index_level"]
    assert [a.symbol for a in al] == ["^IXIC"] and al[0].severity == "info" and ":up:" in al[0].id
    assert "【上方的关键位】" in al[0].body or "【历史上同类情况】" in al[0].body
    assert sent == []                                                       # shown in the panel, never mailed


def test_plan_alert_carries_a_full_write_up_without_holdings(env, monkeypatch):
    from stockdesk import engine as engine_mod, plans
    eng, market, store, _ = env
    _index_market(market)
    monkeypatch.setattr(engine_mod.keylevels, "session_live", lambda *a, **k: False)
    px = float(market.daily["CCC"]["close"].iloc[-1])
    eng.cfg.set("alerts.stop_fall.require_for_buy", False)         # this test is about the write-up, not about the wait
    monkeypatch.setattr(plans, "load", lambda path=None: {"CCC": {
        "start": "2026-01-01", "end": "2027-06-01", "buy_below": [px * 1.1, px * 0.5], "target_price": px * 2,
        "exit_below": px * 0.4, "max_weight_pct": 10}})
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "plan_level"]
    assert len(al) == 1 and al[0].data["key"] == "buy"
    body = al[0].body
    assert body.split("\n")[0].startswith("CCC 现价")                        # the toast still shows the plain sentence
    for section in ("【这次触发】", "【CCC 现在的位置】", "【同一时间的大盘】", "【可以核对的】"):
        assert section in body
    assert "下一档买入价" in body and "目标价" in body and "底线价" in body and "均线：" in body and "RSI(14)" in body
    assert "相对大盘" in body and "标普 500" in body and body.rstrip().endswith("不会下单。")
    for word in ("市值", "盈亏", "占组合", "成本"):
        assert word not in body


def test_named_levels_with_sources_and_the_cta_trend_estimate():
    close = rising(320) + [128.0]                    # a long rise, then a drop under the close of 21 sessions earlier
    st = kl.state("^GSPC", bars_of(close), user_levels=[{"level": 130, "name": "130 支撑", "source": "某报道 2026-11-20"}, 90, {"bad": 1}])
    u = st.level("user130")
    assert u is not None and u.name == "130 支撑" and u.source == "某报道 2026-11-20" and st.level("user90").name == "你设的关键位 90"
    assert u in st.broken                             # crossed from above on the last bar
    m21 = st.level("mom21")
    assert m21 is not None and m21.value == close[-22] and "1 个月动量翻转价" in m21.name and m21 in st.broken
    assert st.level("mom252") is not None and "mom252" not in [x.key for x in st.broken]
    title, body = insight.index_alert(st, "down", st.broken, index_bars=bars_of(close))
    assert "来源：某报道 2026-11-20" in body and "不公开" in body
    assert insight.label(u) == "130 支撑"


def test_scan_reports_a_crossed_key_level_with_its_source():
    from stockdesk import flow
    idx = pd.bdate_range("2026-06-01", periods=80)
    calm = [7300 + 2 * (i % 2) for i in range(79)]
    df = pd.DataFrame({"close": calm + [7190.0], "volume": [1000.0] * 80}, index=idx)
    items, _ = flow.scan_futures({"^GSPC": df}, {"symbols": {"^GSPC": "标普 500 指数"}}, None,
                                 {"^GSPC": [("7200 支撑", 7200.0, "某报道"), ("7625（CTA）", 7625.0, "")]})
    assert len(items) == 1 and any("跌破关键位 7200 支撑" in f for f in items[0].facts)
    assert any(d.startswith("关键位：") and "7625（CTA）" in d for d in items[0].detail) and any("某报道" in d for d in items[0].detail)
    quiet = pd.DataFrame({"close": calm + [7301.0], "volume": [1000.0] * 80}, index=idx)
    assert flow.scan_futures({"^GSPC": quiet}, {"symbols": {"^GSPC": "x"}}, None, {"^GSPC": [("7200", 7200.0, "")]})[0] == []
