"""Engine with a fake market adapter, store, watchlist, config and the read-only guard."""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from stockdesk import credentials
from stockdesk.adapters.base import AdapterUnavailable, MarketDataAdapter, RateLimited
from stockdesk.config import Config
from stockdesk.engine import Engine
from stockdesk.models import Alert, NewsItem, Position, Trade
from stockdesk.store import Store
from stockdesk.watchlist import Watchlist, normalize_symbol

from conftest import random_walk

ROOT = Path(__file__).resolve().parents[1]


class FakeMarket(MarketDataAdapter):
    def __init__(self, symbols):
        self.daily = {s: random_walk(700, seed=i + 1) for i, s in enumerate(symbols)}
        end = pd.Timestamp.now().normalize()
        for s, df in self.daily.items():          # history ending "today"
            df.index = pd.bdate_range(end=end, periods=len(df))
        self.fail: Exception | None = None
        self.calls = 0

    def history(self, symbols, period="5y"):
        self.calls += 1
        if self.fail:
            raise self.fail
        return {s: self.daily[s] for s in symbols if s in self.daily}

    def intraday(self, symbols):
        if self.fail:
            raise self.fail
        out = {}
        for s in symbols:
            if s not in self.daily:
                continue
            d = self.daily[s]
            idx = pd.DatetimeIndex([d.index[-2] + pd.Timedelta(hours=20), d.index[-1] + pd.Timedelta(hours=15),
                                    d.index[-1] + pd.Timedelta(hours=20)])
            c = [float(d["close"].iloc[-2]), float(d["close"].iloc[-1]) * 0.99, float(d["close"].iloc[-1])]
            out[s] = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    import stockdesk.paths as paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "RH_IMPORT_DIR", tmp_path / "data" / "imports" / "robinhood")
    wl_path = tmp_path / "watchlist.yaml"
    wl_path.write_text(yaml.safe_dump({"tickers": [
        {"symbol": "AAA", "shares": 10, "cost": 50.0, "stop": 1.0, "target": 100000.0},
        {"symbol": "BBB", "shares": 5, "cost": 80.0}, {"symbol": "CCC"}, "bad symbol!!"]}), encoding="utf-8")
    cfg = Config.load(ROOT / "config.default.yaml", tmp_path / "data" / "config.yaml")
    cfg.set("research.semianalysis.enabled", False)
    cfg.set("research.polymarket.enabled", False)
    cfg.set("congress.enabled", False)            # no network in tests; tests/test_congress.py feeds it fixtures
    market = FakeMarket(["AAA", "BBB", "CCC", "SPY"])
    store = Store(tmp_path / "data" / "t.sqlite")
    eng = Engine(cfg, store, Watchlist(wl_path, tmp_path / "none.yaml"), market)
    return eng, market, store, tmp_path


def test_engine_snapshot_numbers(env):
    eng, market, store, _ = env
    snap = eng.refresh()
    assert [t.symbol for t in snap.tickers] == ["AAA", "BBB", "CCC"]
    a = snap.get("AAA")
    last, prev = float(market.daily["AAA"]["close"].iloc[-1]), float(market.daily["AAA"]["close"].iloc[-2])
    assert a.price == pytest.approx(last) and a.prev_close == pytest.approx(prev)
    assert a.day_pct == pytest.approx((last / prev - 1) * 100)
    assert a.value == pytest.approx(10 * last) and a.upl == pytest.approx(10 * (last - 50))
    assert a.levels_src == "user" and 0 < a.progress < 1
    b = snap.get("BBB")
    assert b.levels_src == "auto" and b.stop < 80 < b.target
    assert snap.get("CCC").qty == 0 and snap.get("CCC").progress is None
    assert snap.total_value == pytest.approx(a.value + b.value)
    assert snap.day_pnl == pytest.approx(a.day_chg * 10 + b.day_chg * 5)
    assert snap.mode == "演示" and snap.market_ok
    assert all(t.analysis and t.analysis.ok for t in snap.tickers)
    assert snap.opt is not None and snap.conc.weights
    assert snap.demo_trades and snap.n_trades > 0            # sample executions, flagged as demo


def test_engine_offline_and_rate_limit_fall_back_to_cache(env):
    eng, market, store, tmp = env
    first = eng.refresh()
    market.fail = AdapterUnavailable("offline")
    snap = eng.refresh(force=True)
    assert not snap.market_ok and "缓存" in snap.market_msg
    assert snap.get("AAA").price == pytest.approx(first.get("AAA").price)

    # a brand-new engine (app restart while offline) still shows cached prices
    eng2 = Engine(eng.cfg, store, eng.watchlist, market)
    snap2 = eng2.refresh()
    assert snap2.get("AAA").price == pytest.approx(first.get("AAA").price)
    assert snap2.get("AAA").analysis.ok

    market.fail = RateLimited("限流")
    eng2.refresh(force=True)
    market.fail = None
    calls = market.calls
    snap3 = eng2.refresh()                                   # inside the back-off window: no request
    assert market.calls == calls and "限流" in snap3.market_msg


def test_engine_broker_failure_is_graceful_and_positions_merge(env):
    eng, market, store, tmp = env
    with pytest.MonkeyPatch.context() as mp:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        eng.cfg.set("sources.ibkr_tws.enabled", True)
        eng.cfg.set("sources.ibkr_tws.port", port)
        eng.cfg.set("sources.ibkr_tws.timeout", 1)
        snap = eng.refresh(force=True)
        state, msg = snap.sources["ibkr_tws"]
        assert state == "off" and "TWS" in msg
        assert snap.mode == "券商离线" and snap.get("AAA").qty == 10      # manual positions still shown

    merged = eng.watchlist.merge_positions([Position("AAA", 4, 60.0, "IBKR"), Position("AAA", 6, 40.0, "Robinhood"),
                                            Position("ZZZ", 1, 5.0, "IBKR")])
    assert merged["AAA"].qty == 10 and merged["AAA"].avg_cost == pytest.approx(48.0)
    assert merged["AAA"].source == "IBKR+Robinhood"
    assert merged["BBB"].source == "manual"
    assert eng.watchlist.get("ZZZ").auto
    eng.watchlist.merge_positions([])
    assert eng.watchlist.get("ZZZ") is None                               # auto item vanishes with the position


def test_engine_alert_lifecycle_and_snooze(env):
    eng, market, store, _ = env
    eng.cfg.set("quant.max_position_pct", 10.0)
    snap = eng.refresh()
    over = [a for a in snap.alerts if a.kind == "oversize"]
    assert over and all(a in snap.new_alerts for a in over)
    again = eng.refresh()
    assert not [a for a in again.new_alerts if a.kind == "oversize"]      # toast only once
    store.snooze_alert(over[0].id, 7)
    assert over[0].id not in [a.id for a in eng.refresh().alerts]
    store.dismiss_alert(over[-1].id)
    assert over[-1].id not in [a.id for a in eng.refresh().alerts]
    assert over[0].id in [a.id for a in store.load_alerts(include_hidden=True)]

    sym = eng.add_symbol(" spy ")
    assert sym == "SPY" and eng.add_symbol("not a ticker!") is None
    assert store.load_wl_events()[-1]["action"] == "add"
    eng.set_plan("CCC", 10.0, 20.0, 5.0)
    assert Watchlist(eng.watchlist.path, eng.watchlist.sample).get("CCC").stop == 10.0
    eng.remove_symbol("SPY")
    assert "SPY" not in eng.watchlist.symbols()
    checks, px = eng.pretrade("AAA", "BUY", 1000, None)
    assert px == pytest.approx(snap.get("AAA").price) and any(c.level == "high" for c in checks)


def test_store_roundtrip(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    bars = random_walk(50)
    st.save_bars("X", "1d", bars)
    pd.testing.assert_frame_equal(st.load_bars("X", "1d"), bars, check_freq=False)
    assert st.load_bars("NOPE", "1d").empty
    st.set_meta("k", {"a": 1})
    assert st.get_meta("k") == {"a": 1} and st.get_meta("k", max_age=-1) is None and st.get_meta("zz", 5) == 5
    t = Trade("id1", "X", "BUY", 1, 2.0, 3.0, "IBKR")
    assert st.upsert_trades([t, t]) == 1 and st.upsert_trades([t]) == 0
    st.upsert_trades([Trade("d", "X", "BUY", 1, 2.0, 1.0, "演示", demo=True)])
    assert len(st.load_trades()) == 2 and len(st.load_trades(include_demo=False)) == 1
    st.delete_demo_trades()
    assert [x.id for x in st.load_trades()] == ["id1"]
    st.upsert_news([NewsItem("l", "S", "T", "sum", "l", 10.0, ["X"])])
    assert st.load_news()[0].tickers == ["X"]
    a = Alert("a1", "chase", "X", "warn", "t", "b", {"x": 1})
    assert len(st.add_alerts([a])) == 1 and st.add_alerts([a]) == []
    st.snooze_alert("a1", 1)
    assert st.load_alerts() == []
    later = Alert("a2", "chase", "X", "warn", "t", "b")
    st.add_alerts([later])                                   # same kind+symbol inherits the snooze
    assert st.load_alerts() == []


def test_watchlist_and_config(tmp_path):
    assert normalize_symbol(" brk-b ") == "BRK-B" and normalize_symbol("^GSPC") is None
    assert normalize_symbol("a b;c") is None and normalize_symbol("") is None
    sample = tmp_path / "sample.yaml"
    sample.write_text("tickers:\n  - symbol: nvda\n    shares: 3\n    cost: 10\n  - AAPL\n  - symbol: NVDA\n", encoding="utf-8")
    wl = Watchlist(tmp_path / "wl.yaml", sample)
    assert wl.symbols() == ["NVDA", "AAPL"] and (tmp_path / "wl.yaml").exists()
    assert wl.add("msft").symbol == "MSFT" and wl.add("???") is None
    assert wl.remove("AAPL") and not wl.remove("AAPL")
    assert Watchlist(tmp_path / "wl.yaml", sample).symbols() == ["NVDA", "MSFT"]

    cfg = Config.load(ROOT / "config.default.yaml", tmp_path / "user.yaml")
    assert cfg.get("sources.ibkr_tws.enabled") is False
    assert cfg.get("sources.robinhood_unofficial.enabled") is False      # unofficial source is opt-in
    cfg.set("sources.ibkr_tws.port", 4002)
    cfg.save()
    again = Config.load(ROOT / "config.default.yaml", tmp_path / "user.yaml")
    assert again.get("sources.ibkr_tws.port") == 4002 and again.get("app.benchmark") == "SPY"
    assert again.get("no.such.key", "d") == "d"
    assert credentials.redact("token=abc123 failed", "abc123") == "token=*** failed"


# ---- hard rule: there is no order-placement code anywhere ----------------------------------

FORBIDDEN = [
    r"placeOrder", r"cancelOrder", r"modifyOrder", r"whatIfOrder", r"bracketOrder", r"reqGlobalCancel",
    r"\bMarketOrder\b", r"\bLimitOrder\b", r"\bStopOrder\b", r"order_buy", r"order_sell", r"\border\(",
    r"cancel_stock_order", r"cancel_all", r"order_option", r"order_crypto", r"/iserver/account/[^\s\"']*/orders?\b",
    r"/iserver/reply", r"method\s*=\s*[\"'](POST|PUT|DELETE|PATCH)[\"']", r"requests\.(post|put|delete|patch)",
    r"create_order", r"submit_order", r"post_order", r"clob\.polymarket\.com/order",
]


def test_no_order_code_anywhere_in_source():
    pat = re.compile("|".join(FORBIDDEN))
    hits = []
    for f in (ROOT / "src").rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert hits == []


def test_no_secrets_or_plain_credential_files_in_repo():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for needed in ("data/", "watchlist.yaml", ".env", "*.pickle"):
        assert needed in gi
    default = yaml.safe_load((ROOT / "config.default.yaml").read_text(encoding="utf-8"))
    flat = str(default).lower()
    assert "password" not in flat and "token:" not in flat
    assert default["sources"]["robinhood_unofficial"]["username"] == ""


# ---- foreign listings, FX, daily briefing, privacy mode ----------------------------------

class _Broker:
    name, label = "ibkr_flex", "IBKR Flex"

    def __init__(self, positions):
        self.positions = positions

    def fetch(self):
        from stockdesk.models import BrokerData
        return BrokerData(positions=self.positions)


def test_symbol_map_and_fx_conversion(env, monkeypatch):
    eng, market, store, _ = env
    market.daily["EUR1.F"] = market.daily["CCC"].copy()                 # the data source's own symbol
    fx = market.daily["SPY"].copy()
    fx[["open", "high", "low", "close"]] = 1.25
    market.daily["EURUSD=X"] = fx
    eng.cfg.set("market.symbol_map", {"EUR1": "EUR1.F"})
    monkeypatch.setattr(eng, "_adapters", lambda: [_Broker([Position("EUR1", 10, 100.0, "IBKR", "EUR")])])
    snap = eng.refresh(force=True)
    v = snap.get("EUR1")
    assert v is not None and v.bars is not None and v.currency == "EUR" and v.fx == 1.25
    assert v.value == pytest.approx(10 * v.price * 1.25)                # USD
    assert v.upl == pytest.approx((v.price - 100.0) * 10 * 1.25)
    assert v.analysis is not None and v.analysis.ok                     # signals work for the foreign listing
    assert snap.total_value == pytest.approx(sum(t.value for t in snap.tickers))


def test_daily_briefing_flags_and_changes(env):
    import time as _t
    from stockdesk import briefing
    eng, market, store, _ = env
    snap = eng.refresh(force=True)
    b = snap.briefing
    assert b is not None and b.prev_day is None and {i.symbol for i in b.held} == {"AAA", "BBB"}
    assert [i.symbol for i in b.watch] == ["CCC"]
    aaa = next(i for i in b.held if i.symbol == "AAA")
    assert aaa.levels_src == "user" and aaa.stop == 1.0 and aaa.stop_dist_pct > 0
    assert any("超过" in t for _, t in aaa.flags) == (aaa.weight * 100 > aaa.cap_pct)

    # "yesterday" looked different -> today's briefing lists what changed
    hist = store.get_meta("briefing:states")
    today = b.day
    y = dict(hist[today])
    y["AAA"] = {**y["AAA"], "stance": "偏空" if y["AAA"]["stance"] != "偏空" else "偏多", "below_stop": True}
    store.set_meta("briefing:states", {"2000-01-01": y, today: hist[today]})
    b2 = eng.build_snapshot().briefing
    assert b2.prev_day == "2000-01-01"
    texts = [t for s, t in b2.changes if s == "AAA"]
    assert any(t.startswith("综合信号") for t in texts) and "回到止损参考位上方" in texts

    # a sale inside the cool-off window and a breached stop are flagged
    store.upsert_trades([Trade("s1", "BBB", "SELL", 1, 80.0, _t.time() - 3600, "IBKR")])
    eng.watchlist.update("BBB", stop=1e9)
    b3 = eng.build_snapshot().briefing
    bbb = next(i for i in b3.held if i.symbol == "BBB")
    assert any("冷静期" in t for _, t in bbb.flags) and any(l == "high" for l, _ in bbb.flags)
    assert briefing.diff(None, y["AAA"]) == ["新出现在列表中"]


def test_privacy_mode_masks_amounts(env):
    from stockdesk.ui import render, theme as T
    eng, *_ = env
    snap = eng.refresh(force=True)
    pal = T.Palette()
    html = render.briefing_html(snap.briefing, pal)
    assert "每日简报" in html and "AAA" in html
    try:
        T.PRIVATE = True
        assert T.money(1234.5) == "$" + T.MASK and T.qty(10) == T.MASK
        v = snap.get("AAA")
        text = render.analysis_html(v, pal)
        assert "10 股" not in text and "隐私模式下已隐藏" in text
        assert "隐私模式" in render.private_html("每日简报")
    finally:
        T.PRIVATE = False
    assert "10 股" in render.analysis_html(snap.get("AAA"), pal)


def test_reference_comparison_and_plan_tracking(env, monkeypatch, tmp_path):
    from datetime import date
    from stockdesk import plans
    from stockdesk.ui import render, theme as T
    eng, market, store, _ = env
    home = market.daily["CCC"].copy()
    home[["open", "high", "low", "close"]] *= 1000.0                    # home share quoted in KRW
    market.daily["HOME.KS"] = home
    market.daily["EUR1.F"] = market.daily["CCC"].copy() * 1.0
    for sym, rate in (("EURUSD=X", 1.25), ("KRWUSD=X", 0.00125)):       # 1000 KRW = 1 EUR
        fx = market.daily["SPY"].copy()
        fx[["open", "high", "low", "close"]] = rate
        market.daily[sym] = fx
    eng.cfg.set("market.symbol_map", {"EUR1": "EUR1.F"})
    eng.cfg.set("market.reference", {"EUR1": {"symbol": "HOME.KS", "currency": "KRW", "ratio": 1, "name": "Home"}})
    monkeypatch.setattr(eng, "_adapters", lambda: [_Broker([Position("EUR1", 10, 100.0, "IBKR", "EUR")])])
    eng.refresh(force=True)                 # first pass adds EUR1 to the watchlist, second loads its reference
    snap = eng.refresh(force=True)
    r = snap.refs["EUR1"]
    last = float(market.daily["CCC"]["close"].iloc[-1])
    assert r.implied == pytest.approx(last, rel=1e-6)
    assert abs(r.premium_avg) < 1e-6 and r.corr == pytest.approx(1.0) and r.ref_stance
    assert abs(r.premium_pct) < 2.0         # live price vs last close differ only by the fake intraday tick

    # plan template: numbers stay empty until the user fills them in
    path = tmp_path / "plans.yaml"
    assert plans.ensure_template("EUR1", 60.0, "EUR", path=path, today=date(2026, 1, 1))
    assert not plans.ensure_template("EUR1", 60.0, path=path)          # never overwrites
    plan = plans.load(path)["EUR1"]
    st = plans.status("EUR1", plan, price=90.0, weight_pct=60.0, qty=10, trades=[], today=date(2026, 4, 2))
    assert set(st.missing) == {"目标占比", "底线价", "分批减仓价位"} and st.days_left > 0 and not st.flags
    plan.update(target_weight_pct=20, exit_below=95, reduce_above=[80, 120])
    buy = Trade("b", "EUR1", "BUY", 1, 88.0, pd.Timestamp("2026-02-01").timestamp(), "IBKR")
    st = plans.status("EUR1", plan, price=90.0, weight_pct=60.0, qty=10, trades=[buy], today=date(2026, 4, 2))
    assert st.path_weight_pct == pytest.approx(60 - 40 * 91 / st.days_total)
    text = " ".join(t for _, t in st.flags)
    assert "底线价" in text and "减仓价位 80" in text and "不加仓" in text and "高于按时间均匀推进" in text
    assert st.levels_reached == [80.0] and st.next_level == 120.0 and not st.missing
    html = render.briefing_html(snap.briefing, T.Palette(), snap.refs, [st])
    assert "对应的本土股票" in html and "我的计划" in html


def test_plan_save_and_reference_levels(env, tmp_path):
    from datetime import date
    from stockdesk import plans
    eng, *_ = env
    snap = eng.refresh(force=True)
    path = tmp_path / "plans.yaml"
    plans.save("AAA", {"target_weight_pct": 20.0, "reduce_above": [120.0, 100.0], "start_weight_pct": 55.0},
               path=path, today=date(2026, 1, 1))
    plans.save("BBB", {"exit_below": 5.0}, path=path, today=date(2026, 1, 1))
    plans.save("AAA", {"exit_below": 40.0}, path=path)                  # update keeps earlier fields
    got = plans.load(path)
    assert got["AAA"]["target_weight_pct"] == 20.0 and got["AAA"]["exit_below"] == 40.0 and "BBB" in got
    assert got["AAA"]["start"] == "2026-01-01" and got["AAA"]["end"] == "2026-07-03"
    labels = [k for k, _ in plans.reference_levels(snap.get("AAA"))]
    assert "现价" in labels and "你的平均成本" in labels and "50 日均线" in labels


def test_bottoming_alert_and_email(env, monkeypatch):
    from stockdesk import notify
    from stockdesk.ui import render, theme as T
    from test_range_trading import _fall_then_base
    eng, market, store, _ = env
    base = _fall_then_base(True)
    base.index = market.daily["SPY"].index[-len(base):]
    market.daily["CCC"] = base
    eng.cfg.set("alerts.bottoming", {"enabled": True, "symbols": ["CCC", "AAA"], "min_score": 5})
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, subject, body, **k: sent.append((s["to"], subject, body)))
    snap = eng.refresh(force=True)
    assert snap.bottoms["CCC"].triggered and "AAA" in snap.bottoms
    al = [a for a in snap.alerts if a.kind == "bottoming"]
    assert len(al) == 1 and al[0].symbol == "CCC" and "不是预测" in al[0].body and al[0] in snap.new_alerts
    assert sent == []                                            # e-mail is off until the user enables it
    assert "筑底观察" in render.briefing_html(snap.briefing, T.Palette(), bottoms=snap.bottoms)

    eng.cfg.set("notify.email", {"enabled": True, "to": "me@example.com", "smtp_host": "smtp.example.com",
                                 "smtp_port": 587, "username": "bot@example.com"})
    eng.build_snapshot()
    eng.build_snapshot()
    assert len(sent) == 1 and sent[0][0] == "me@example.com" and "筑底信号" in sent[0][1]   # once per base
    assert "AAA" not in sent[0][2].split("CCC")[0] and "股 ·" not in sent[0][2]             # no holdings in the mail


def test_notify_send_uses_only_the_configured_account(monkeypatch):
    from stockdesk import notify

    class FakeSMTP:
        log = []

        def __init__(self, host, port):
            self.log.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, secret):
            self.log.append(("login", user))

        def send_message(self, msg):
            self.log.append(("send", msg["To"], msg["From"], msg["Subject"]))

    s = notify.settings({"enabled": True, "to": "me@example.com", "smtp_host": "h", "smtp_port": 587,
                         "username": "bot@example.com"})
    notify.send(s, "标题", "正文", smtp_factory=FakeSMTP, secret="app-code")
    assert FakeSMTP.log == [("connect", "h", 587), ("login", "bot@example.com"),
                            ("send", "me@example.com", "bot@example.com", "标题")]
    with pytest.raises(notify.NotifyError):
        notify.send(notify.settings({"enabled": True}), "x", "y", smtp_factory=FakeSMTP, secret="z")
    assert notify.settings({})["enabled"] is False and notify.settings({})["kinds"] == ["bottoming", "plan_level", "flow", "index_level", "congress"]


def test_build_plan_levels_raise_alerts(env, monkeypatch, tmp_path):
    from datetime import date
    from stockdesk import plans
    eng, market, store, _ = env
    px = float(market.daily["CCC"]["close"].iloc[-1])
    plan = {"start": "2026-01-01", "end": "2026-07-01", "buy_below": [px * 1.1, px * 0.5],
            "target_price": px * 0.9, "max_weight_pct": 10}
    st = plans.status("CCC", plan, price=px, weight_pct=12.0, qty=0, trades=[], today=date(2026, 2, 3))
    assert st.buy_reached == [px * 1.1] and st.next_buy == px * 0.5 and not st.missing
    assert {e[0] for e in st.events} == {"buy", "target"} and any("仓位上限" in t for _, t in st.flags)
    assert not any("不加仓" in t for _, t in st.flags)              # a build-up plan does not imply "no adding"

    monkeypatch.setattr(plans, "load", lambda path=None: {"CCC": plan})
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "plan_level"]
    assert {a.data["key"] for a in al} == {"buy", "target"} and all(a.severity == "warn" for a in al)
    assert all("不会下单" in a.body for a in al) and [p.symbol for p in snap.plans] == ["CCC"]
    monkeypatch.setattr(plans, "load", lambda path=None: {"CCC": {**plan, "buy_below": [], "target_price": px * 5}})
    assert not [a for a in eng.build_snapshot().alerts if a.kind == "plan_level"]     # gone once no level is hit


def test_plan_can_follow_another_stock(env, monkeypatch):
    from datetime import date
    from stockdesk import plans
    from stockdesk.ui import render, theme as T
    plan = {"start": "2026-01-01", "end": "2026-07-01", "follow": "000660.KS", "exit_below": 1118000,
            "reduce_above": [1300000], "target_weight_pct": 30, "start_weight_pct": 70}
    kw = dict(price=730.0, weight_pct=70.0, qty=40, trades=[], today=date(2026, 1, 2), ccy="EUR",
              follow_name="SK Hynix", follow_ccy="KRW", to_local=0.00068)
    st = plans.status("HY9H", plan, follow_price=1096000.0, **kw)
    assert st.follow == "000660.KS" and st.check_price == 1096000.0 and st.level_ccy == "KRW"
    assert [e[0] for e in st.events] == ["exit"]
    assert "1,096,000 KRW" in st.events[0][3] and "1,118,000" in st.events[0][3] and "HY9H 计划" in st.events[0][3]
    # the listing's own price (730 EUR) is never compared with the KRW levels
    calm = plans.status("HY9H", plan, follow_price=1200000.0, **kw)
    assert calm.events == [] and calm.next_level == 1300000
    assert st.chart_levels("000660.KS") == {"exit": 1118000, "reduce": [1300000], "buy": [], "target": None, "approx": False}
    own = st.chart_levels("HY9H")
    assert own["approx"] and own["exit"] == pytest.approx(760.24) and own["reduce"] == [pytest.approx(884.0)]
    blind = plans.status("HY9H", plan, follow_price=None, **kw)
    assert blind.events == [] and any("没有取到" in t for _, t in blind.flags)
    html = render.plans_html([st], T.Palette())
    assert "价位按" in html and "1,118,000 KRW" in html and "≈ 760 EUR" in html

    # an empty build-up plan: nothing is pre-filled, and buying is not flagged as breaking the plan
    buy = Trade("b", "TSM", "BUY", 1, 350.0, pd.Timestamp("2026-02-01").timestamp(), "IBKR")
    shell = plans.status("TSM", {"start": "2026-01-01", "end": "2026-07-01", "kind": "build"}, price=360.0,
                         weight_pct=5.0, qty=10, trades=[buy], today=date(2026, 3, 3))
    assert shell.building and shell.missing == ["分批买入价位", "目标价", "仓位上限"] and shell.events == []
    assert not any("不加仓" in t for _, t in shell.flags)
    assert "未填写" in render.plans_html([shell], T.Palette())

    # in the engine: AAA's plan written in BBB's prices
    eng, market, store, _ = env
    bbb = float(market.daily["BBB"]["close"].iloc[-1])
    monkeypatch.setattr(plans, "load", lambda path=None: {"AAA": {"start": "2026-01-01", "end": "2026-07-01",
                                                                    "follow": "BBB", "exit_below": bbb * 2}})
    snap = eng.refresh(force=True)
    al = [a for a in snap.alerts if a.kind == "plan_level"]
    assert len(al) == 1 and al[0].symbol == "AAA" and "BBB 现价" in al[0].body and "按 BBB" in al[0].title
    assert snap.plans[0].check_price == pytest.approx(snap.get("BBB").price) and snap.plans[0].to_local == 1.0
