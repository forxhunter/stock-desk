import pandas as pd

from stockdesk import flow

NOW = 1_800_000_000.0


def trade(cash, price=0.5, side="BUY", idx=0, wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", age_min=5, cid="0xc1", slug="ev1",
          title="Will TSMC report record revenue?"):
    return {"proxy_wallet": wallet, "side": side, "condition_id": cid, "size": cash / price, "price": price,
            "timestamp": NOW - age_min * 60, "title": title, "event_slug": slug, "outcome": ["Yes", "No"][idx],
            "outcome_index": idx, "pseudonym": "Calm-Otter"}


def event(slug="ev1", cid="0xc1", tags=("Business",), vol24=400000.0, hour=None, closed=False):
    return {slug: {"slug": slug, "title": "Event", "tags": [{"label": t} for t in tags],
                   "markets": [{"conditionId": cid, "outcomes": '["Yes", "No"]', "outcomePrices": '["0.55", "0.45"]',
                                "volume24hr": vol24, "oneHourPriceChange": hour, "closed": closed}]}}


WATCH = {"TSM": ["TSMC", "Taiwan Semiconductor"], "NVDA": ["Nvidia"]}


def scan(trades, events, **cfg):
    return flow.scan_polymarket(trades, events, WATCH, cfg, NOW)


def test_big_trade_on_watchlist_market_is_flagged():
    items, _ = scan([trade(80000)], event())
    assert len(items) == 1
    i = items[0]
    assert i.tier == "watch" and i.tickers == ["TSM"] and i.severity in ("warn", "high")
    assert any(f.startswith("大额单") for f in i.facts) and "Yes" in i.headline
    assert i.key.startswith("pm:0xc1:a:")


def test_small_and_old_trades_are_ignored():
    assert scan([trade(20000)], event())[0] == []                       # below big_usd, 5 % of volume
    assert scan([trade(80000, age_min=300)], event())[0] == []          # outside the recent window


def test_near_certain_buys_are_low_information():
    items, notes = scan([trade(300000, price=0.98)], event())
    assert items == [] and any("信息量低" in n for n in notes)
    items, _ = scan([trade(300000, price=0.03, side="SELL")], event())
    assert items == []


def test_direction_is_normalised_to_first_outcome():
    items, _ = scan([trade(80000, idx=1), trade(70000, side="SELL", idx=0, wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")], event())
    assert "净流向「No」" in items[0].headline and items[0].key.split(":")[2] == "b"
    assert any(f.startswith("单边资金") for f in items[0].facts) is False   # only two trades


def test_wallet_concentration_onesided_and_share():
    rows = [trade(40000, age_min=m) for m in (5, 200, 400)]             # one wallet, 120k over 24 h
    items, _ = scan(rows, event())
    assert any(f.startswith("同一钱包集中下注") for f in items[0].facts)
    rows = [trade(25000, wallet=f"0x{n}" * 10) for n in "123"]
    items, _ = scan(rows, event(vol24=200000.0))
    kinds = [f.split("：")[0] for f in items[0].facts]
    assert "单边资金" in kinds and "相对成交异常" in kinds


def test_jump_sports_and_closed_markets():
    items, _ = scan([trade(15000)], event(hour=0.08))
    assert any(f.startswith("价格跳变") for f in items[0].facts)
    items, notes = scan([trade(500000, title="Lakers vs. Celtics")], event(tags=("Sports", "NBA")))
    assert items == [] and any("体育" in n for n in notes)
    assert scan([trade(500000)], event(closed=True))[0] == []
    assert scan([trade(500000)], {})[0] == []                           # event not found


def test_tiers_and_alert_selection():
    macro, _ = scan([trade(80000, title="Fed cuts rates in December?")], event(tags=("Fed",)))
    other, _ = scan([trade(80000, title="Who wins the song contest?")], event(tags=("Culture",)))
    assert macro[0].tier == "macro" and other[0].tier == "other"
    rep = flow.FlowReport(ts=NOW, items=macro + other)
    assert [i.tier for i in flow.alert_items(rep)] == ["macro"]
    title, body = flow.alert_text(macro[0])
    assert title.startswith("Polymarket 异动") and "不是买卖建议" in body
    again = flow.FlowReport.from_dict(rep.to_dict())
    assert again.items[0].key == rep.items[0].key and again.by("polymarket")


def test_fetch_uses_v2_and_batches_events():
    urls = []

    def getter(url, **kw):
        urls.append(url)
        body = b'{"data": [{"condition_id": "0x1"}]}' if "/v2/trades" in url else b'[{"slug": "a"}]'
        return 200, {}, body

    assert flow.fetch_trades(getter, 10000) == [{"condition_id": "0x1"}]
    assert "filterAmount=10000" in urls[0] and "/v2/trades" in urls[0]
    flow.fetch_events([f"s{i}" for i in range(45)], getter)
    assert len(urls) == 4 and urls[1].count("slug=") == 20


def test_options_volume_vs_open_interest():
    calls = pd.DataFrame([
        {"contractSymbol": "TSM261218C00400000", "strike": 400.0, "lastPrice": 5.0, "volume": 4000, "openInterest": 500,
         "impliedVolatility": 0.5, "inTheMoney": False},
        {"contractSymbol": "TSM261218C00410000", "strike": 410.0, "lastPrice": 6.0, "volume": 4000, "openInterest": 5,
         "impliedVolatility": 0.5, "inTheMoney": False},
        {"contractSymbol": "TSM261218C00380000", "strike": 380.0, "lastPrice": 9.0, "volume": 4000, "openInterest": 9000,
         "impliedVolatility": 0.4, "inTheMoney": True},
        {"contractSymbol": "TSM261218C00500000", "strike": 500.0, "lastPrice": 0.02, "volume": 3000, "openInterest": 10,
         "impliedVolatility": 0.9, "inTheMoney": False}])
    puts = pd.DataFrame([{"contractSymbol": "TSM261218P00300000", "strike": 300.0, "lastPrice": 2.0, "volume": float("nan"),
                          "openInterest": float("nan"), "impliedVolatility": 0.6, "inTheMoney": False}])
    day = lambda d: f"{pd.Timestamp(NOW, unit='s') + pd.Timedelta(days=d):%Y-%m-%d}"
    far = day(30)
    items, notes = flow.scan_options({"TSM": {day(0): (calls, puts), far: (calls, puts)}}, None, {"TSM"}, NOW)
    assert [i.title for i in items] == [f"TSM {far} 400 看涨"]   # same-day expiry, high / tiny OI, tiny premium: out
    assert items[0].tier == "watch" and "8.0 倍" in items[0].headline and "看不出是主动买还是主动卖" in items[0].reading[0]
    assert notes and "看跌看涨比 0.00" in notes[0] and "近 2 个到期日" in notes[0]


def test_futures_z_score_and_volume():
    idx = pd.bdate_range("2026-06-01", periods=80)
    calm = [100 + 0.2 * (i % 2) for i in range(79)]
    quiet = pd.DataFrame({"close": calm + [100.1], "volume": [1000.0] * 80}, index=idx)
    shock = pd.DataFrame({"close": calm + [95.0], "volume": [1000.0] * 78 + [2500.0, 400.0]}, index=idx)
    items, notes = flow.scan_futures({"ES=F": shock, "NQ=F": quiet}, None, NOW)
    assert [i.key.split(":")[1] for i in items] == ["ES=F"] and len(notes) == 2
    kinds = " ".join(items[0].facts)
    assert "标准差" in kinds and "20 日均量的 2.5 倍" in kinds and items[0].severity == "high"


def test_alerts_put_bets_first_and_cap_other_sources():
    mk = lambda src, n, sev="high": flow.FlowItem(src, f"{src}{n}", "t", severity=sev, tier="macro", score=5 - n * 0.1)
    rep = flow.FlowReport(ts=NOW, items=[mk("options", n) for n in range(6)] + [mk("futures", 0), mk("polymarket", 0, "warn"),
                                                                              mk("polymarket", 1, "info")])
    assert [i.key for i in flow.alert_items(rep)] == ["polymarket0", "options0", "options1", "futures0"]


def test_polymarket_details_spell_out_the_trades_and_the_pay_off():
    ev = event()
    ev["ev1"]["markets"][0].update({"oneDayPriceChange": 0.04, "volumeNum": 2_000_000, "liquidityNum": 150000,
                                    "endDateIso": "2027-03-01"})
    items, _ = scan([trade(80000, price=0.4)], ev)
    d = "\n".join(items[0].detail)
    assert "市场：现在的价格「Yes」55.0% /「No」45.0%" in d.replace(" 「", "「") and "2027-03-01 到期" in d
    assert "Calm-Otter" in d and "买入「Yes」$80k @ 40.0%" in d
    assert "兑现可收回 $200k（净赚 $120k，1.5 倍）" in d and "高于 40%" in d
    assert "成交价 40.0% → 现在 55.0%（+15.0 个百分点），成交之后价格继续同方向走" in d
    assert "24 小时成交的 20%" in d
    title, body = flow.alert_text(items[0], ["【额外】", "· x"])
    assert "【触发的规则】" in body and "【具体情况】" in body and "【怎么看】" in body and "【额外】" in body
    assert body.index("【额外】") < body.index("不是买卖建议")
    sold, _ = scan([trade(80000, price=0.4, side="SELL")], ev)
    assert any("最大一笔是卖出" in x for x in sold[0].detail)
    cashing, notes = scan([trade(300000, price=0.97, side="SELL")], ev)       # selling a near-certain holding: no information
    assert cashing == [] and any("信息量低" in n for n in notes)


def test_option_details_break_even_implied_move_and_other_legs():
    calls = pd.DataFrame([
        {"contractSymbol": "TSM1C400", "strike": 400.0, "lastPrice": 5.0, "volume": 4000, "openInterest": 500,
         "impliedVolatility": 0.5, "inTheMoney": False},
        {"contractSymbol": "TSM1C420", "strike": 420.0, "lastPrice": 2.0, "volume": 3900, "openInterest": 9000,
         "impliedVolatility": 0.5, "inTheMoney": False}])
    puts = pd.DataFrame([{"contractSymbol": "TSM1P300", "strike": 300.0, "lastPrice": 2.0, "volume": 100.0,
                          "openInterest": 5000.0, "impliedVolatility": 0.6, "inTheMoney": False}])
    far = f"{pd.Timestamp(NOW, unit='s') + pd.Timedelta(days=30):%Y-%m-%d}"
    spot = {"TSM": {"price": 350.0, "vol20": 0.3, "high252": 390.0, "low252": 250.0}}
    items, _ = flow.scan_options({"TSM": {far: (calls, puts)}}, None, {"TSM"}, NOW, spot)
    d = "\n".join(items[0].detail)
    assert "正股 350.00，行权价 400 高于现价 14.3%（价外）" in d and "还有 30 天" in d
    assert "涨到 405.00（距现价 +15.7%）才不亏，这个价位在 52 周收盘高点 390.00 之上" in d
    assert "一个标准差约 ±14.3%" in d and "行权价在这个范围之内" in d and "隐含波动明显高于实际" in d
    assert "同到期日的 420 看涨也成交 3,900 张" in d and "价差" in d and "看跌看涨比 0.01" in d
    bare, _ = flow.scan_options({"TSM": {far: (calls.iloc[:1], puts)}}, None, {"TSM"}, NOW)      # no stock price known
    assert any("更像单腿交易" in x for x in bare[0].detail) and not any("盈亏平衡" in x for x in bare[0].detail)


def test_futures_details_levels_other_contracts_and_pattern():
    idx = pd.bdate_range("2025-06-01", periods=300)
    calm = [100 + 0.2 * (i % 2) for i in range(299)]
    mk = lambda last, bump=None: pd.DataFrame({"close": [c if i != bump else c * 0.97 for i, c in enumerate(calm)] + [last],
                                               "volume": [1000.0] * 300}, index=idx)
    bars = {"ES=F": mk(95.0, bump=150), "NQ=F": mk(94.0), "^VIX": mk(130.0), "ZN=F": mk(101.5), "GC=F": mk(100.1)}
    items, _ = flow.scan_futures(bars, None, NOW)
    es = next(i for i in items if i.key.startswith("fut:ES=F"))
    d = "\n".join(es.detail)
    assert "20 日均线" in d and "区间的 0% 位置" in d and "近 5 日" in d
    assert "同一时间的其它品种" in d and "纳指 100 期货" in d and "VIX 波动率指数 +" in d
    assert "典型的避险组合" in d
    assert "同方向超过 2σ 的单日变动出现过 1 次" in d and "之后 5 日涨跌的中位数" in d
