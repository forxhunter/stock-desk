"""Broker / data adapters against fakes. No test logs in to anything or needs credentials."""
import json
import socket
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from stockdesk.adapters import base
from stockdesk.adapters.ibkr_cp import IbkrClientPortalAdapter
from stockdesk.adapters.ibkr_flex import IbkrFlexAdapter, parse_statement
from stockdesk.adapters.ibkr_tws import IbkrTwsAdapter
from stockdesk.adapters.polymarket import PolymarketAdapter, parse_market
from stockdesk.adapters.robinhood_csv import RobinhoodCsvAdapter, parse_activity_csv, positions_from_trades
from stockdesk.adapters.robinhood_unofficial import RobinhoodUnofficialAdapter
from stockdesk.adapters.semianalysis import SemiAnalysisAdapter, build_matchers, match_tickers, parse_feed
from stockdesk.adapters.yahoo import YahooAdapter


# ---- Yahoo ------------------------------------------------------------------------------

def _yf_frame(symbols, tz="America/New_York"):
    idx = pd.date_range("2026-01-05", periods=5, freq="B", tz=tz)
    cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]])
    data = np.arange(len(idx) * len(cols), dtype=float).reshape(len(idx), len(cols)) + 1
    return pd.DataFrame(data, index=idx, columns=cols)


def test_yahoo_split_and_missing_symbol():
    frame = _yf_frame(["AAA", "BBB"])
    frame["BBB"] = np.nan                        # delisted / unknown symbol
    calls = []
    ad = YahooAdapter(downloader=lambda syms, **kw: calls.append(kw) or frame)
    out = ad.history(["AAA", "BBB"], "1y")
    assert list(out) == ["AAA"]
    df = out["AAA"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None and len(df) == 5
    assert calls[0]["period"] == "1y" and calls[0]["interval"] == "1d" and calls[0]["auto_adjust"] is True


def test_yahoo_errors_are_mapped():
    def boom(*a, **k):
        raise ConnectionError("no network")
    with pytest.raises(base.AdapterUnavailable):
        YahooAdapter(downloader=boom).intraday(["AAA"])

    class YFRateLimitError(Exception):
        pass

    def limited(*a, **k):
        raise YFRateLimitError("Too Many Requests")
    with pytest.raises(base.RateLimited):
        YahooAdapter(downloader=limited).history(["AAA"])
    assert YahooAdapter(downloader=lambda *a, **k: None).history(["AAA"]) == {}


# ---- IBKR TWS ---------------------------------------------------------------------------

class FakeIB:
    def __init__(self):
        self.connect_kwargs = None
        self.disconnected = False
        self.calls = []

    async def connectAsync(self, host, port, **kw):
        self.connect_kwargs = kw

    def positions(self):
        stk = NS(secType="STK", symbol="NVDA", currency="USD")
        opt = NS(secType="OPT", symbol="NVDA", currency="USD")
        brk = NS(secType="STK", symbol="BRK B", currency="USD")
        return [NS(account="U1", contract=stk, position=40.0, avgCost=118.5),
                NS(account="U1", contract=opt, position=1.0, avgCost=300.0),
                NS(account="U1", contract=brk, position=3.0, avgCost=410.0)]

    def portfolio(self):
        return [NS(contract=NS(secType="STK", symbol="NVDA"), marketPrice=130.25)]

    def accountValues(self):
        return [NS(tag="NetLiquidation", currency="USD", value="50000.5"),
                NS(tag="TotalCashValue", currency="USD", value="1200"),
                NS(tag="Leverage-S", currency="", value="abc")]

    async def reqExecutionsAsync(self):
        ex = NS(execId="0001", time=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc), side="BOT", shares=10, price=120.0)
        return [NS(contract=NS(secType="STK", symbol="NVDA"), execution=ex, commissionReport=NS(commission=1.0))]

    def disconnect(self):
        self.disconnected = True

    def __getattr__(self, name):          # any other call would be a bug -> fail loudly
        raise AssertionError(f"unexpected IB call: {name}")


def test_tws_adapter_is_readonly_and_parses():
    fake = FakeIB()
    data = IbkrTwsAdapter(ib_factory=lambda: fake).fetch()
    assert fake.connect_kwargs["readonly"] is True
    assert fake.disconnected
    assert [(p.symbol, p.qty, p.avg_cost) for p in data.positions] == [("NVDA", 40.0, 118.5), ("BRK-B", 3.0, 410.0)]
    assert data.positions[0].market_price == 130.25
    assert data.net_liquidation == 50000.5 and data.cash == 1200
    t = data.trades[0]
    assert (t.id, t.side, t.qty, t.price, t.fees) == ("ibkr:0001", "BUY", 10, 120.0, 1.0)


def test_tws_adapter_fails_gracefully_when_tws_not_running():
    """Real ib_async against a port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    ad = IbkrTwsAdapter(port=free_port, timeout=2)
    with pytest.raises(base.AdapterUnavailable) as e:
        ad.fetch()
    assert "TWS" in str(e.value)
    with pytest.raises(base.AdapterUnavailable):     # and again: no leaked loop / state
        ad.fetch()


# ---- IBKR Flex --------------------------------------------------------------------------

FLEX_SEND_OK = b"""<FlexStatementResponse timestamp="x"><Status>Success</Status>
<ReferenceCode>987654</ReferenceCode><Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>
</FlexStatementResponse>"""
FLEX_WAIT = b"""<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage></FlexStatementResponse>"""
FLEX_STATEMENT = b"""<FlexQueryResponse queryName="sd" type="AF"><FlexStatements count="1"><FlexStatement accountId="U1">
<OpenPositions>
 <OpenPosition symbol="NVDA" assetCategory="STK" currency="USD" position="40" costBasisPrice="118.5" markPrice="131" levelOfDetail="SUMMARY"/>
 <OpenPosition symbol="NVDA" assetCategory="STK" currency="USD" position="40" costBasisPrice="118.5" markPrice="131" levelOfDetail="LOT"/>
 <OpenPosition symbol="SPY 261218C00700000" assetCategory="OPT" position="1" costBasisPrice="5"/>
</OpenPositions>
<Trades>
 <Trade symbol="NVDA" assetCategory="STK" buySell="BUY" quantity="10" tradePrice="120.5" dateTime="20261102;103000" tradeID="111" ibCommission="-1.0"/>
 <Trade symbol="NVDA" assetCategory="STK" buySell="SELL" quantity="-5" tradePrice="125" dateTime="20261105;150000" tradeID="112" ibCommission="-1.0"/>
 <Trade symbol="ES" assetCategory="FUT" buySell="BUY" quantity="1" tradePrice="6000" dateTime="20261105;150000" tradeID="113"/>
</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_flex_parse_statement():
    d = parse_statement(FLEX_STATEMENT)
    assert [(p.symbol, p.qty, p.avg_cost, p.market_price) for p in d.positions] == [("NVDA", 40.0, 118.5, 131.0)]
    assert [(t.id, t.side, t.qty, t.price) for t in d.trades] == [("ibkr:111", "BUY", 10, 120.5), ("ibkr:112", "SELL", 5, 125)]
    assert datetime.fromtimestamp(d.trades[0].ts, timezone.utc).hour == 15     # 10:30 New York = 15:30 UTC
    with pytest.raises(base.AdapterError):
        parse_statement(b"not xml")


def test_flex_flow_polls_and_never_leaks_token():
    token = "SECRETTOKEN123"
    urls, replies = [], [FLEX_SEND_OK, FLEX_WAIT, FLEX_STATEMENT]

    def getter(url, **kw):
        urls.append(url)
        return 200, {}, replies.pop(0)
    ad = IbkrFlexAdapter(token, "42", getter=getter, sleep=lambda s: None)
    data = ad.fetch()
    assert len(data.positions) == 1 and len(data.trades) == 2
    assert urls[0].endswith(f"SendRequest?t={token}&q=42&v=3")
    assert "q=987654" in urls[1] and len(urls) == 3

    def failing(url, **kw):
        raise base.AdapterError(f"boom while calling {url}")
    with pytest.raises(base.AdapterError) as e:
        IbkrFlexAdapter(token, "42", getter=failing, sleep=lambda s: None).fetch()
    assert token not in str(e.value)

    rejected = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
    with pytest.raises(base.AdapterError) as e:
        IbkrFlexAdapter(token, "42", getter=lambda u, **k: (200, {}, rejected), sleep=lambda s: None).fetch()
    assert "1012" in str(e.value)
    with pytest.raises(base.AdapterUnavailable):
        IbkrFlexAdapter(None, "42").fetch()


# ---- IBKR Client Portal -----------------------------------------------------------------

def test_client_portal_get_only_whitelist():
    seen = []

    def getter(url, **kw):
        seen.append((url, kw))
        path = url.split("/v1/api", 1)[1]
        body = {
            "/portfolio/accounts": [{"accountId": "U1234567"}],
            "/portfolio/U1234567/positions/0": [
                {"ticker": "AAPL", "position": 25, "avgCost": 201.0, "mktPrice": 230.1, "assetClass": "STK", "currency": "USD"},
                {"ticker": "AAPL", "position": 1, "avgCost": 3.0, "assetClass": "OPT"}],
            "/portfolio/U1234567/ledger": {"BASE": {"netliquidationvalue": 9000.0, "cashbalance": 500.0}},
            "/iserver/account/trades?days=7": [
                {"execution_id": "e1", "symbol": "AAPL", "side": "B", "size": 5, "price": "229.5",
                 "trade_time_r": 1794000000000, "sec_type": "STK", "commission": "1.0"}],
        }[path]
        return 200, {}, json.dumps(body).encode()
    data = IbkrClientPortalAdapter(getter=getter).fetch()
    assert [(p.symbol, p.qty, p.market_price) for p in data.positions] == [("AAPL", 25.0, 230.1)]
    assert data.net_liquidation == 9000.0 and data.cash == 500.0
    assert data.trades[0].side == "BUY" and data.trades[0].ts == 1794000000.0
    assert all(kw.get("insecure_localhost") for _, kw in seen)

    ad = IbkrClientPortalAdapter(getter=getter)
    for bad in ("/iserver/account/U1/orders", "/iserver/reply/abc", "/portfolio/U1/../orders", "/iserver/account/trades?days=7&x=1"):
        with pytest.raises(base.AdapterError):
            ad._read(bad)

    def down(url, **kw):
        raise base.AdapterUnavailable("refused")
    with pytest.raises(base.AdapterUnavailable) as e:
        IbkrClientPortalAdapter(getter=down).fetch()
    assert "Gateway" in str(e.value)


def test_http_get_refuses_insecure_non_localhost():
    with pytest.raises(base.AdapterError):
        base.http_get("https://example.com/x", insecure_localhost=True)


# ---- Robinhood CSV ----------------------------------------------------------------------

RH_CSV = '''"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code","Quantity","Price","Amount"
"11/05/2026","11/05/2026","11/06/2026","NVDA","NVIDIA\nCUSIP: 67066G104","Sell","5","$130.00","$650.00"
"10/01/2026","10/01/2026","10/02/2026","NVDA","NVIDIA","Buy","10","$120.00","($1,200.00)"
"09/01/2026","09/01/2026","09/02/2026","NVDA","NVIDIA","Buy","10","$100.00","($1,000.00)"
"09/01/2026","09/01/2026","09/02/2026","NVDA","NVIDIA","Buy","10","$100.00","($1,000.00)"
"08/15/2026","08/15/2026","08/15/2026","","ACH Deposit","ACH","","","$5,000.00"
"08/20/2026","08/20/2026","08/20/2026","AAPL","Cash Div: R/D 2026-08-11","CDIV","","","$6.00"
"08/21/2026","08/21/2026","08/22/2026","AAPL 9/18/2026 Call $250.00","AAPL call","BTO","1","$3.00","($300.00)"
"07/01/2026","07/01/2026","07/02/2026","TSLA","Tesla","Sell","3","$300.00","$900.00"
'''


def test_robinhood_csv_parse_and_positions(tmp_path):
    trades, splits = parse_activity_csv(RH_CSV)
    assert [(t.symbol, t.side, t.qty, t.price) for t in trades] == [
        ("TSLA", "SELL", 3, 300.0), ("NVDA", "BUY", 10, 100.0), ("NVDA", "BUY", 10, 100.0),
        ("NVDA", "BUY", 10, 120.0), ("NVDA", "SELL", 5, 130.0)]
    assert len({t.id for t in trades}) == 5            # identical same-day fills stay distinct
    assert trades == parse_activity_csv(RH_CSV)[0]     # ids are stable across imports
    pos = positions_from_trades(trades, splits)
    assert len(pos) == 1                               # TSLA sold before the report started: clipped
    assert pos[0].symbol == "NVDA" and pos[0].qty == 25
    assert pos[0].avg_cost == pytest.approx(3200 / 30)  # average cost is unchanged by a sale

    (tmp_path / "a.csv").write_text(RH_CSV, encoding="utf-8")
    (tmp_path / "overlap.csv").write_text(RH_CSV, encoding="utf-8")      # overlapping report: no dupes
    data = RobinhoodCsvAdapter(tmp_path).fetch()
    assert len(data.trades) == 5 and data.positions[0].qty == 25
    with pytest.raises(base.AdapterUnavailable):
        RobinhoodCsvAdapter(tmp_path / "none").fetch()


# ---- Robinhood unofficial (fake API) ----------------------------------------------------

class FakeRH:
    def __init__(self):
        self.calls = []

    def login(self, **kw):
        self.calls.append(("login", kw))
        return {"access_token": "x"}

    def get_open_stock_positions(self):
        return [{"instrument": "u/nvda", "quantity": "12.5", "average_buy_price": "110.0"}, None,
                {"instrument": "u/zero", "quantity": "0.0", "average_buy_price": "1"}]

    def get_symbol_by_url(self, url):
        return {"u/nvda": "NVDA", "u/zero": "ZERO"}[url]

    def get_all_stock_orders(self):
        return [{"id": "o1", "state": "filled", "side": "buy", "cumulative_quantity": "2", "average_price": "100.5",
                 "instrument": "u/nvda", "last_transaction_at": "2026-11-03T15:00:00Z", "fees": "0.02"},
                {"id": "o2", "state": "cancelled", "side": "sell", "quantity": "2", "instrument": "u/nvda"}]

    def load_account_profile(self):
        return {"cash": "321.5"}

    def order_buy_market(self, *a, **k):     # exists on the real module; must be unreachable
        raise AssertionError("order function reached")


def test_robinhood_unofficial_reads_only():
    fake = FakeRH()
    ad = RobinhoodUnofficialAdapter("me@example.com", lambda: "pw", api=fake)
    data = ad.fetch()
    assert [(p.symbol, p.qty, p.avg_cost) for p in data.positions] == [("NVDA", 12.5, 110.0)]
    assert [(t.id, t.side, t.qty, t.price) for t in data.trades] == [("rh:o1", "BUY", 2.0, 100.5)]
    assert data.cash == 321.5
    assert fake.calls[0][1]["store_session"] is False
    ad.fetch()
    assert len(fake.calls) == 1                         # logged in once per session
    with pytest.raises(base.AdapterError):
        ad._rh().order_buy_market("NVDA", 1)            # blocked by the allow-list proxy
    with pytest.raises(base.AdapterUnavailable):
        RobinhoodUnofficialAdapter("me", lambda: None, api=FakeRH()).fetch()
    with pytest.raises(base.AdapterUnavailable):
        RobinhoodUnofficialAdapter("", lambda: "pw", api=FakeRH()).fetch()


# ---- SemiAnalysis RSS -------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0"><channel>
<item><title><![CDATA[TSMC's N2 ramp &amp; what it means]]></title><link>https://example.com/p/n2</link>
<description><![CDATA[<p>Capacity, pricing, customers.</p>]]></description><pubDate>Thu, 12 Nov 2026 18:06:26 GMT</pubDate>
<content:encoded><![CDATA[<p>Apple is first. AMD later. AMD again. AMD a third time. Members only below.</p>]]></content:encoded></item>
<item><title>Datacenter power update</title><link>https://example.com/p/power</link>
<description>Grid constraints for hyperscalers such as Microsoft.</description><pubDate>bad date</pubDate></item>
<item><title>No link item</title><description>x</description></item>
</channel></rss>"""
WATCH = {"TSM": ["TSMC"], "AMD": ["AMD"], "AAPL": ["Apple"], "MSFT": ["Microsoft"], "ON": [], "ARM": ["Arm Holdings"]}


def test_rss_parse_and_ticker_mapping():
    items = parse_feed(RSS, "SemiAnalysis", build_matchers(WATCH))
    assert len(items) == 2
    a, b = items
    assert a.title == "TSMC's N2 ramp & what it means" and a.summary == "Capacity, pricing, customers."
    assert a.published == datetime(2026, 11, 12, 18, 6, 26, tzinfo=timezone.utc).timestamp()
    assert a.tickers == ["TSM", "AMD"]          # title mention; AMD 3x in body; Apple only once -> not tagged
    assert b.tickers == ["MSFT"] and b.published == 0.0
    assert "Members only" not in a.summary      # body text is never stored
    m = build_matchers(WATCH)
    assert match_tickers("Turn it ON and wave an arm", m) == []          # short / lowercase words don't match
    assert match_tickers("Buy $ON now, says ARM", m) == ["ON", "ARM"]
    with pytest.raises(base.AdapterError):
        parse_feed(b"<html>", "x", m)


def test_rss_adapter_conditional_get_and_fallback():
    calls = []

    def getter(url, headers=None, **kw):
        calls.append((url, headers))
        if "first" in url:
            raise base.AdapterUnavailable("down")
        return 200, {"ETag": 'W/"abc"', "Last-Modified": "Thu, 12 Nov 2026 18:06:26 GMT"}, RSS
    ad = SemiAnalysisAdapter(["https://first/feed", "https://second/feed"], WATCH, getter=getter)
    assert len(ad.fetch()) == 2
    assert ad.validators["https://second/feed"]["etag"] == 'W/"abc"'

    def not_modified(url, headers=None, **kw):
        assert headers["If-None-Match"] == 'W/"abc"'
        return 304, {}, b""
    ad2 = SemiAnalysisAdapter(["https://second/feed"], WATCH, getter=not_modified, validators=ad.validators)
    assert ad2.fetch() == []
    with pytest.raises(base.AdapterError):
        SemiAnalysisAdapter(["https://first/feed"], WATCH, getter=getter).fetch()


# ---- Polymarket -------------------------------------------------------------------------

def _market(mid, question, yes, vol24=5000.0, vol=50000.0, **kw):
    m = {"id": mid, "question": question, "outcomes": '["Yes", "No"]',
         "outcomePrices": json.dumps([str(yes), str(1 - yes)]), "volume24hr": vol24, "volumeNum": vol,
         "active": True, "closed": False, "endDateIso": "2026-12-31", "oneDayPriceChange": 0.01,
         "oneWeekPriceChange": -0.05, "oneMonthPriceChange": 0.1,
         "clobTokenIds": json.dumps([f"tok{mid}y", f"tok{mid}n"]), "slug": f"m{mid}"}
    m.update(kw)
    return m


def test_polymarket_parse_market():
    odds, token = parse_market(_market("1", "Fed cuts in December?", 0.62), {"slug": "fed-dec"}, "Fed", ["SPY"])
    assert odds.prob == 0.62 and odds.change_1w == -0.05 and token == "tok1y"
    assert odds.url == "https://polymarket.com/event/fed-dec" and odds.tickers == ["SPY"]
    assert parse_market(_market("2", "closed", 0.5, closed=True)) is None
    assert parse_market(_market("3", "multi", 0.5, outcomes='["A","B","C"]')) is None
    assert parse_market(_market("4", "bad", 0.5, outcomePrices="oops")) is None
    flipped, token = parse_market(_market("5", "Q", 0.3, outcomes='["No", "Yes"]', outcomePrices='["0.3", "0.7"]'))
    assert flipped.prob == 0.7 and flipped.change_1d == -0.01 and token == "tok5n"


def test_polymarket_adapter_get_only_public_endpoints():
    urls = []

    def getter(url, **kw):
        urls.append(url)
        if "public-search" in url and "q=Fed" in url:
            ev = {"slug": "fed", "title": "Fed decision", "closed": False, "markets": [
                _market("1", "Fed cuts in December?", 0.62, 9000), _market("2", "Fed hikes?", 0.02, 100, 500),
                _market("3", "Fed holds?", 0.36, 7000), _market("9", "old", 0.5, closed=True)]}
            return 200, {}, json.dumps({"events": [ev]}).encode()
        if "public-search" in url and "q=Nvidia" in url:
            ev = {"slug": "nv", "title": "Largest company", "closed": False, "markets": [
                _market("7", "Will Nvidia be the largest company?", 0.4, 800),
                _market("8", "Will Apple be the largest company?", 0.5, 900)]}
            return 200, {}, json.dumps({"events": [ev]}).encode()
        if "public-search" in url:
            raise base.AdapterError("HTTP 500")
        if "prices-history" in url:
            return 200, {}, json.dumps({"history": [{"t": 1, "p": 0.5}, {"t": 2, "p": 0.62}]}).encode()
        if "/markets?slug=" in url:
            return 200, {}, json.dumps([_market("20", "Pinned?", 0.11, 1)]).encode()
        raise AssertionError(url)

    ad = PolymarketAdapter([{"q": "Fed", "label": "美联储"}, "broken"], slugs=["pinned-slug"], getter=getter,
                           per_keyword=2, min_volume=10000)
    out = ad.fetch({"NVDA": ["Nvidia"], "T": []})
    by = {o.id: o for o in out}
    assert set(by) == {"1", "3", "7", "20"}        # low-volume, closed and off-topic markets dropped
    assert by["1"].label == "美联储" and by["1"].history == [0.5, 0.62]
    assert by["7"].tickers == ["NVDA"]
    assert len(ad.errors) == 1                      # one keyword failed, the rest still worked
    assert all(u.startswith(("https://gamma-api.polymarket.com/", "https://clob.polymarket.com/prices-history")) for u in urls)

    def down(url, **kw):
        raise base.AdapterUnavailable("offline")
    with pytest.raises(base.AdapterError):
        PolymarketAdapter([{"q": "Fed"}], getter=down).fetch({})


def test_polymarket_follows_watchlist():
    from stockdesk.adapters.polymarket import for_watchlist
    queries = []

    def getter(url, **kw):
        if "public-search" in url:
            q = url.split("q=")[1].split("&")[0]
            queries.append(q)
            mk = {"Nvidia": [_market("n1", "Will Nvidia be the largest company?", 0.4, 900),
                             _market("n2", "Will Nvidia be the #1 searched term?", 0.2, 990)],
                  "HOOD": [_market("h1", "Will HOOD close above $100?", 0.3, 500),
                           _market("h2", "Hoodie sales record?", 0.3, 800)],
                  "Fed": [_market("f1", "Fed cuts?", 0.6, 700)]}.get(q, [])
            return 200, {}, json.dumps({"events": [{"slug": q, "title": "", "closed": False, "markets": mk}]}).encode()
        if "prices-history" in url:
            return 200, {}, json.dumps({"history": []}).encode()
        raise AssertionError(url)

    kws = [{"q": "Fed"}, {"q": "Tesla", "tickers": ["TSLA"]}]
    ad = PolymarketAdapter(kws, getter=getter, exclude=["searched"])
    out = {o.id: o for o in ad.fetch({"NVDA": ["Nvidia"], "HOOD": []})}
    assert "Tesla" not in queries                   # pinned to a stock that is not on the watchlist
    assert "HOOD" in queries                        # no company name -> ticker search
    assert set(out) == {"n1", "h1", "f1"}           # excluded and non-matching markets dropped
    assert PolymarketAdapter(kws, getter=getter, macro=False).fetch({}) == []

    # removing a stock drops its markets immediately; stock-linked markets come before macro ones
    shown = for_watchlist(list(out.values()), ["HOOD"])
    assert [o.id for o in shown] == ["h1", "f1"]
    assert [o.id for o in for_watchlist(list(out.values()), ["HOOD"], macro=False)] == ["h1"]


def _xlsx(path, header, rows):
    """Minimal workbook: strings via sharedStrings, numbers / Excel serial dates inline."""
    import zipfile
    strings, cells_xml = [], []

    def cell(col, r, v):
        ref = f"{chr(65 + col)}{r}"
        if isinstance(v, str):
            if v not in strings:
                strings.append(v)
            return f'<c r="{ref}" t="s"><v>{strings.index(v)}</v></c>'
        return f'<c r="{ref}"><v>{v}</v></c>'
    for r, row in enumerate([header] + rows, start=1):
        cells_xml.append(f'<row r="{r}">' + "".join(cell(c, r, v) for c, v in enumerate(row) if v != "") + "</row>")
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{''.join(cells_xml)}</sheetData></worksheet>")


def test_robinhood_xlsx_matches_csv_and_flags_incomplete_history(tmp_path):
    from stockdesk.adapters.robinhood_csv import coverage_notes, parse_activity_file
    header = ["Activity Date", "Process Date", "Settle Date", "Instrument", "Description", "Trans Code",
              "Quantity", "Price", "Amount"]
    # 46027 = 2026-01-05, 46034 = 2026-01-12 as Excel serial dates
    _xlsx(tmp_path / "a.xlsx", header, [
        [46034, 46034, 46035, "NVDA", "NVIDIA", "Sell", 3, 160.5, 481.5],
        [46027, 46027, 46028, "NVDA", "NVIDIA", "Buy", 1, 150.25, 150.25],
        [46027, 46027, 46027, "QNT", "Stock Lending", "SLIP", "", "", 0.01]])
    from stockdesk.adapters.robinhood_csv import _money
    assert _money("3.484E-4") == pytest.approx(0.0003484) and _money("($1,234.50)") == -1234.5
    (tmp_path / "a.csv").write_text(
        ",".join(header) + "\n"
        '1/12/2026,1/12/2026,1/13/2026,NVDA,"NVIDIA\nCUSIP: 1",Sell,3,$160.50 ,$481.50 ,\n'
        "1/5/2026,1/5/2026,1/6/2026,NVDA,NVIDIA,Buy,1,$150.25 ,($150.25),\n", encoding="utf-8")
    x, _ = parse_activity_file(tmp_path / "a.xlsx")
    c, _ = parse_activity_file(tmp_path / "a.csv")
    assert [(t.side, t.qty, t.price) for t in x] == [("BUY", 1.0, 150.25), ("SELL", 3.0, 160.5)]
    assert [t.id for t in x] == [t.id for t in c]                # importing both formats never double counts
    ad = RobinhoodCsvAdapter(tmp_path)
    data = ad.fetch()
    assert len(ad.files()) == 2 and len(data.trades) == 2 and data.positions == []
    assert any("不完整" in n for n in ad.notes) and "2026-01-05 ~ 2026-01-12" in coverage_notes(x)[0]
