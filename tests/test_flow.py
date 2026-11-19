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
