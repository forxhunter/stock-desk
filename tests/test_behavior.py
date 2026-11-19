"""Behavioural detectors on synthetic price paths and trade histories."""
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from stockdesk.behavior import detectors as det
from stockdesk.behavior.demo import make_demo_trades
from stockdesk.models import Position, Trade

from conftest import make_bars, random_walk

NY = ZoneInfo("America/New_York")


def ts(day, hour=11):
    return datetime.combine(pd.Timestamp(day).date(), dtime(hour), NY).timestamp()


def path(*segments, start=100.0):
    """segments: (n_days, daily_return)."""
    out, p = [], start
    for n, r in segments:
        for _ in range(n):
            p *= 1 + r
            out.append(p)
    return make_bars(out, start="2025-01-06")


def run(trades, bars, positions=None, prices=None, plans=None, wl=None, cfg=None, now=None, equity=0.0):
    last = max(b.index[-1] for b in bars.values())
    return det.detect(trades, bars, positions or {}, prices or {}, plans or {}, wl or [], cfg, equity,
                      now or ts(last, 18))


def kinds(alerts):
    return sorted(a.kind for a in alerts)


def test_chase_buy_after_spike_is_flagged_and_calm_buy_is_not():
    b = path((200, 0.0005), (6, 0.035), (3, 0.0))            # flat, then +23% in 6 days
    spike_day, calm_day = b.index[205], b.index[150]
    hot = Trade("t1", "XYZ", "BUY", 10, float(b["close"].iloc[205]), ts(spike_day))
    alerts = run([hot], {"XYZ": b})
    assert kinds(alerts) == ["chase"]
    a = alerts[0]
    assert "追涨" in a.title and "5 日已上涨" in a.body and "RSI" in a.body and a.data["chg"] > 10
    calm = Trade("t0", "XYZ", "BUY", 10, float(b["close"].iloc[150]), ts(calm_day))
    assert run([calm], {"XYZ": b}, now=ts(calm_day, 18)) == []


def test_panic_sell_after_drop_is_flagged():
    b = path((200, 0.0005), (6, -0.035), (2, 0.0))
    buy = Trade("b", "XYZ", "BUY", 10, 100.0, ts(b.index[100]))
    sell = Trade("s", "XYZ", "SELL", 10, float(b["close"].iloc[205]), ts(b.index[205]))
    alerts = run([buy, sell], {"XYZ": b})
    assert "panic" in kinds(alerts)
    p = next(a for a in alerts if a.kind == "panic")
    assert "杀跌" in p.title and "已下跌" in p.body
    # a sell in a quiet market is fine
    quiet = Trade("s2", "XYZ", "SELL", 10, float(b["close"].iloc[190]), ts(b.index[190]))
    assert "panic" not in kinds(run([buy, quiet], {"XYZ": b}, now=ts(b.index[190], 18)))


def test_history_note_reports_how_similar_past_trades_ended():
    # two spikes; after the first one the price falls back -> the user's past chase lost money
    b = path((120, 0.0), (5, 0.04), (25, -0.012), (60, 0.0), (5, 0.04), (2, 0.0))
    first = Trade("old", "XYZ", "BUY", 10, float(b["close"].iloc[124]), ts(b.index[124]))
    second = Trade("new", "XYZ", "BUY", 10, float(b["close"].iloc[214]), ts(b.index[214]))
    alerts = run([first, second], {"XYZ": b})
    chase = [a for a in alerts if a.kind == "chase"]
    assert [a.id for a in chase] == ["chase:new"]            # the old one is outside the look-back window
    assert "过去 1 次类似的追涨买入" in chase[0].body and "1 次是亏的" in chase[0].body
    assert chase[0].data["history"]["avg_fwd"] < 0


def test_fomo_watchlist_add():
    b = path((200, 0.0), (5, 0.03), (1, 0.0))
    wl = [{"symbol": "XYZ", "action": "add", "ts": ts(b.index[-1]), "price": None},
          {"symbol": "XYZ", "action": "remove", "ts": ts(b.index[-1]), "price": None}]
    alerts = run([], {"XYZ": b}, wl=wl)
    assert kinds(alerts) == ["fomo_watch"]
    assert run([], {"XYZ": path((206, 0.0))}, wl=wl) == []


def test_disposition_effect():
    b = random_walk(500, seed=1)
    d = b.index
    trades = []
    for i in range(3):       # winners sold after 3 days, losers held 60 days
        trades += [Trade(f"wb{i}", "W", "BUY", 1, 100, ts(d[10 + i * 20])), Trade(f"ws{i}", "W", "SELL", 1, 105, ts(d[13 + i * 20]))]
        trades += [Trade(f"lb{i}", "L", "BUY", 1, 100, ts(d[100 + i * 70])), Trade(f"ls{i}", "L", "SELL", 1, 90, ts(d[160 + i * 70]))]
    alerts = run(trades, {"W": b, "L": b})
    disp = [a for a in alerts if a.kind == "disposition"]
    assert len(disp) == 1 and "处置效应" in disp[0].title and disp[0].data["loss_days"] > disp[0].data["win_days"]
    # balanced holding periods -> no alert
    even = []
    for i in range(3):
        even += [Trade(f"wb{i}", "W", "BUY", 1, 100, ts(d[10 + i * 20])), Trade(f"ws{i}", "W", "SELL", 1, 105, ts(d[15 + i * 20]))]
        even += [Trade(f"lb{i}", "L", "BUY", 1, 100, ts(d[100 + i * 20])), Trade(f"ls{i}", "L", "SELL", 1, 95, ts(d[105 + i * 20]))]
    assert "disposition" not in kinds(run(even, {"W": b, "L": b}))


def test_round_trips_fifo():
    t = [Trade("1", "A", "BUY", 10, 100, 0), Trade("2", "A", "BUY", 10, 120, 86400),
         Trade("3", "A", "SELL", 15, 130, 3 * 86400)]
    trips = det.round_trips(t)
    assert [(r["qty"], r["buy_price"]) for r in trips] == [(10, 100), (5, 120)]
    assert abs(trips[0]["ret"] - 0.3) < 1e-12 and trips[0]["days"] == 3 and trips[1]["days"] == 2


def test_averaging_down_in_downtrend():
    b = path((150, 0.0), (60, -0.006))
    d = b.index
    buys = [Trade(f"b{i}", "XYZ", "BUY", 10, float(b["close"].iloc[k]), ts(d[k])) for i, k in enumerate((190, 198, 206))]
    alerts = run(buys, {"XYZ": b})
    assert "avg_down" in kinds(alerts)
    assert "连续 3 次买入" in next(a for a in alerts if a.kind == "avg_down").body
    assert "avg_down" not in kinds(run(buys[:2], {"XYZ": b}))                 # only two buys
    up = path((150, 0.0), (60, 0.004))
    rising = [Trade(f"b{i}", "XYZ", "BUY", 10, float(up["close"].iloc[k]), ts(up.index[k])) for i, k in enumerate((190, 198, 206))]
    assert "avg_down" not in kinds(run(rising, {"XYZ": up}))


def test_overtrading_and_revenge():
    b = path((260, 0.0002))
    d = b.index
    many = [Trade(f"o{i}", "XYZ", "BUY" if i % 2 == 0 else "SELL", 1, 100.0, ts(d[-1], 10) + i * 60) for i in range(14)]
    assert "overtrade" in kinds(run(many, {"XYZ": b}))
    assert "overtrade" not in kinds(run(many[:6], {"XYZ": b}))

    loss = [Trade("b", "XYZ", "BUY", 10, 100.0, ts(d[-10])), Trade("s", "XYZ", "SELL", 10, 90.0, ts(d[-2], 10)),
            Trade("r", "ABC", "BUY", 30, 50.0, ts(d[-2], 14))]
    alerts = run(loss, {"XYZ": b})
    rev = [a for a in alerts if a.kind == "revenge"]
    assert len(rev) == 1 and rev[0].symbol == "ABC" and "-10.0%" in rev[0].body
    later = loss[:2] + [Trade("r", "ABC", "BUY", 30, 50.0, ts(d[-2], 10) + 3 * 86400)]
    assert "revenge" not in kinds(run(later, {"XYZ": b}, now=ts(d[-2], 10) + 4 * 86400))


def test_plan_breach_and_oversize():
    b = path((260, 0.0))
    pos = {"AAA": Position("AAA", 100, 120.0), "BBB": Position("BBB", 10, 50.0), "CCC": Position("CCC", 10, 50.0)}
    prices = {"AAA": 90.0, "BBB": 80.0, "CCC": 50.0}
    plans = {"AAA": {"stop": 95.0, "target": 150.0, "max_pct": None}, "BBB": {"stop": None, "target": 75.0, "max_pct": None},
             "CCC": {"max_pct": 2.0}}
    alerts = run([], {"AAA": b}, pos, prices, plans)
    by = {a.id.split(":")[0] + ":" + a.symbol: a for a in alerts}
    assert "已跌破你设定的止损" in by["stop_breach:AAA"].title and by["stop_breach:AAA"].severity == "high"
    assert "目标价" in by["target_hit:BBB"].title
    assert "oversize:AAA" in by and "默认的" in by["oversize:AAA"].body           # 9000 of 10300
    assert "oversize:CCC" in by and "你自己设定的" in by["oversize:CCC"].body     # 4.9% > own 2% limit
    assert "oversize:BBB" not in by


def test_thresholds_are_configurable():
    b = path((200, 0.0005), (6, 0.035), (3, 0.0))
    hot = Trade("t1", "XYZ", "BUY", 10, float(b["close"].iloc[205]), ts(b.index[205]))
    strict = {"runup_pct": 60.0, "rsi_overbought": 101, "above_ma_pct": 90.0}
    assert run([hot], {"XYZ": b}, cfg=strict) == []
    assert run([hot], {"XYZ": b}, cfg={"lookback_days": 1}) == []


def test_pretrade_check_buy_and_sell():
    b = path((200, 0.0005), (6, 0.035))
    px = float(b["close"].iloc[-1])
    now = ts(b.index[-1], 15)
    pos = {"XYZ": Position("XYZ", 50, 100.0), "LOSER": Position("LOSER", 10, 100.0)}
    prices = {"XYZ": px, "LOSER": 70.0}
    checks = det.pretrade_check("XYZ", "BUY", 500, px, b, [], pos, prices, {}, None, 0.0, now)
    titles = {c.title: c for c in checks}
    assert titles["这看起来像追涨"].level == "high"
    assert titles["仓位占比"].level == "high"
    assert titles["这笔交易的风险"].level == "warn" and "最多约" in titles["这笔交易的风险"].detail
    assert "还没有计划" in titles

    flat = make_bars(100 + np.sin(np.arange(260) / 3.0), start="2025-01-06")     # quiet, range-bound
    fpx = float(flat["close"].iloc[-1])
    ok = det.pretrade_check("XYZ", "BUY", 1, fpx, flat, [], {"A": Position("A", 100, 100), "B": Position("B", 100, 100),
                            "C": Position("C", 100, 100), "D": Position("D", 100, 100)},
                            {"A": 100, "B": 100, "C": 100, "D": 100}, {"stop": fpx * 0.95, "target": fpx * 1.2}, None, 0.0, now)
    assert {c.level for c in ok} == {"ok"}

    sell = det.pretrade_check("XYZ", "SELL", 20, px, b, [], pos, prices, {"stop": 90.0, "target": 200.0}, None, 0.0, now)
    st = {c.title: c for c in sell}
    assert "计划并未触发" in st and "卖盈利、留亏损？" in st and "LOSER" in st["卖盈利、留亏损？"].detail
    crash = path((200, 0.0005), (6, -0.04))
    cpx = float(crash["close"].iloc[-1])
    panic = det.pretrade_check("XYZ", "SELL", 20, cpx, crash, [], pos, {"XYZ": cpx}, {}, None, 0.0, now)
    assert panic[0].title == "这看起来像杀跌" and panic[0].level == "high"
    none = det.pretrade_check("NEW", "SELL", 1, 10.0, None, [], {}, {}, {}, None, 0.0, now)
    assert {c.title for c in none} >= {"行情数据不足", "未持有"}


def test_demo_trades_are_flagged_and_deterministic():
    bars = {s: random_walk(700, seed=i, sigma=0.03) for i, s in enumerate(["A", "B", "C"])}
    t1 = make_demo_trades(bars, ["A", "B", "C"])
    t2 = make_demo_trades(bars, ["A", "B", "C"])
    assert t1 and all(t.demo and t.id.startswith("demo:") for t in t1)
    assert [t.id for t in t1] == [t.id for t in t2]
    assert make_demo_trades({}, ["A"]) == []
    alerts = run(t1, bars)
    assert all(a.demo for a in alerts if a.kind in ("chase", "panic", "disposition"))
