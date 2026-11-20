"""Trades disclosed by members of Congress: reading the official reports, judging the calls, the alert."""
import io
import zipfile
from datetime import date

import numpy as np
import pandas as pd

from stockdesk import congress as cg
from stockdesk.adapters import congress as src

from conftest import make_bars
from test_engine_misc import env  # noqa: F401  (fixture)

PTR = """P        T           R
Name: Hon. Pat Example
ID Owner Asset Transaction
Type
Date Notification
Date
Amount Cap.
Gains >
$200?
Johnson & Johnson Common Stock
(JNJ) [ST]
S (partial) 10/27/2026 11/12/2026 $1,001 - $15,000
F      S     : New
S          O : Some Bank & Trust Co.
Microsoft Corporation - Common
Stock (MSFT) [ST]
P 10/27/2026 11/12/2026 $15,001 -
$50,000
F      S     : New
D          : Bought on the advice of the manager, a long
description that wraps.
SP Alphabet Inc. - Class A Common Stock (GOOGL) [OP] P 12/30/2025 12/30/2025 $250,001 - $500,000
F      S     : New
D          : Purchased 50 call options with a strike price of $150
U.S. Treasury Note [GS] P 10/20/2026 11/05/2026 $50,001 -
$100,000
F      S     : New
2000166537 JT Organon & Co. Common Stock (OGN) [ST] S 08/05/2026 08/05/2026 Over $50,000,000
Berkshire Hathaway Inc. New Common
Stock (BRK.B) [ST]
E 08/05/2026 08/05/2026 $1,001 - $15,000
Filing ID #20036844
"""


def filing(doc="20036844", member="Pat Example", filed="2026-11-19"):
    return src.Filing("house", doc, member, "NJ07", filed, 2026, f"https://example.org/{doc}.pdf")


def test_house_report_text_is_parsed_row_by_row():
    rows = src.parse_house_ptr(PTR, filing())
    assert [(r.ticker, r.side, r.asset_type, r.owner) for r in rows] == [
        ("JNJ", "sell", "ST", "self"), ("MSFT", "buy", "ST", "self"), ("GOOGL", "buy", "OP", "spouse"),
        ("", "buy", "GS", "self"), ("OGN", "sell", "ST", "joint"), ("BRK.B", "exchange", "ST", "self")]
    jnj, msft, googl, note, ogn, brk = rows
    assert jnj.partial and jnj.traded == "2026-10-27" and jnj.notified == "2026-11-12" and (jnj.lo, jnj.hi) == (1001, 15000)
    assert (msft.lo, msft.hi) == (15001, 50000) and msft.asset.startswith("Microsoft Corporation") and "advice" in msft.note
    assert googl.asset.startswith("Alphabet") and "call options" in googl.note          # a bought call counts as a purchase
    assert (note.lo, note.hi) == (50001, 100000) and (ogn.lo, ogn.hi) == (50000000, 0) and ogn.asset.startswith("Organon")
    assert jnj.id == "house:20036844:0" and jnj.member == "Pat Example" and jnj.url.endswith("20036844.pdf")
    assert src.parse_house_ptr("", filing()) == [] and cg.ysym("BRK.B") == "BRK-B"


def test_house_index_lists_only_transaction_reports():
    xml = ("<FinancialDisclosure>"
           "<Member><Prefix/><Last>Example</Last><First>Pat</First><Suffix>Jr</Suffix><FilingType>P</FilingType>"
           "<StateDst>NJ07</StateDst><Year>2026</Year><FilingDate>11/19/2026</FilingDate><DocID>20036844</DocID></Member>"
           "<Member><Last>Other</Last><First>Sam</First><FilingType>C</FilingType><StateDst>TX01</StateDst>"
           "<FilingDate>5/13/2026</FilingDate><DocID>10078016</DocID></Member>"
           "<Member><Last>Old</Last><First>Lee</First><FilingType>P</FilingType><StateDst>OH06</StateDst>"
           "<FilingDate>1/5/2026</FilingDate><DocID>9116403</DocID></Member></FinancialDisclosure>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", xml)
    urls = []

    def getter(url, **kw):
        urls.append(url)
        return 200, {}, buf.getvalue()

    fil = src.house_index(2026, getter)
    assert [(f.member, f.doc_id, f.filed) for f in fil] == [("Pat Example Jr", "20036844", "2026-11-19"), ("Lee Old", "9116403", "2026-01-05")]
    assert urls == ["https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip"]
    assert fil[0].url == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20036844.pdf"
    assert src.years(2, date(2026, 3, 1)) == [2026, 2025]


def _market(n=400):
    idx = pd.bdate_range(end="2026-11-20", periods=n)
    flat = make_bars([100.0] * n, spread=0.001)
    up = make_bars(np.linspace(100, 180, n), spread=0.001)
    down = make_bars(np.linspace(100, 60, n), spread=0.001)
    for df in (flat, up, down):
        df.index = idx
    return idx, {"SPY": flat, "UPP": up, "DWN": down, "BRK-B": up}


def trade(member, ticker, side, traded, filed=None, doc="d1", **kw):
    return {"id": f"house:{doc}:{member}:{ticker}:{side}:{traded}", "chamber": "house", "doc_id": doc, "member": member, "state": "NJ07",
            "filed": filed or traded, "traded": traded, "notified": "", "ticker": ticker, "asset": f"{ticker} Inc.", "asset_type": "ST",
            "side": side, "partial": False, "owner": "self", "lo": 1001.0, "hi": 15000.0, "url": "https://example.org/x.pdf", **kw}


def test_calls_are_judged_against_the_benchmark_in_both_directions():
    idx, bars = _market()
    d = lambda k: idx[k].date().isoformat()
    trades = [trade("A", "UPP", "buy", d(100), d(130)), trade("A", "UPP", "buy", d(100), d(125), doc="d2", hi=50000.0),   # same call twice
              trade("A", "DWN", "sell", d(100)), trade("A", "DWN", "buy", d(100)), trade("A", "UPP", "sell", d(390)),
              trade("A", "NOPX", "buy", d(100)), trade("A", "", "buy", d(100)), trade("A", "UPP", "exchange", d(100)),
              trade("A", "BRK.B", "buy", d(100), asset_type="OP")]
    calls, other = cg.build_calls(trades, bars, bars["SPY"], (21, 63))
    assert other == 2 and len(calls) == 6                              # no ticker / exchange are not calls; the double counts once
    by = {(c.ticker, c.side, c.traded): c for c in calls}
    up = by[("UPP", "buy", d(100))]
    assert up.hi == 50000.0 and up.filed == d(125) and up.doc == "house:d2"          # the earlier report, the larger range
    assert up.ex[63] > 0 and up.ex[21] > 0 and up.ex_now > 0 and up.exf[63] > 0 and up.delay == 35
    assert by[("DWN", "sell", d(100))].ex[63] > 0                      # sold before a fall: right
    assert by[("DWN", "buy", d(100))].ex[63] < 0                       # bought before a fall: wrong
    late = by[("UPP", "sell", d(390))]
    assert late.ex == {} and late.ex_now < 0 and late.px is not None   # too recent to judge at 63 sessions, wrong so far
    assert by[("NOPX", "buy", d(100))].px is None                      # no price history: listed, never judged
    assert by[("BRK.B", "buy", d(100))].ex[63] > 0                     # looked up under Yahoo's spelling


def test_members_are_rated_with_a_minimum_sample_and_weighted():
    idx, bars = _market()
    d = lambda k: idx[k].date().isoformat()
    trades = [trade("Sharp", "UPP", "buy", d(20 + 5 * k)) for k in range(8)] + [trade("Sharp", "DWN", "sell", d(20 + 5 * k)) for k in range(6)]
    trades += [trade("Wrong", "DWN", "buy", d(20 + 5 * k)) for k in range(12)]
    trades += [trade("Few", "UPP", "buy", d(20 + 5 * k)) for k in range(3)]
    trades += [trade("Mixed", "UPP" if k % 2 else "DWN", "buy", d(20 + 5 * k)) for k in range(10)]
    trades += [trade("Decent", "UPP", "buy", d(20 + 5 * k)) for k in range(6)]
    calls, _ = cg.build_calls(trades, bars, bars["SPY"], (21, 63))
    today = idx[-1].date()
    ms = {m.member: m for m in cg.score_members(calls, today=today)}
    assert ms["Sharp"].tier == "star" and ms["Sharp"].weight == 3 and ms["Sharp"].n == 14 and ms["Sharp"].hit_rate == 1.0
    assert ms["Sharp"].buys[0] == 8 and ms["Sharp"].sells[0] == 6 and ms["Sharp"].ci[0] > 0.5 and ms["Sharp"].follow[0] == 14
    assert ms["Wrong"].tier == "poor" and ms["Wrong"].weight == 0 and ms["Wrong"].mean < 0       # not accurate: carries no weight
    assert ms["Few"].tier == "thin" and ms["Mixed"].tier == "plain" and ms["Decent"].tier == "good" and ms["Decent"].weight == 2
    assert ms["Few"].weight == 0 and ms["Mixed"].weight == 0
    assert [m.member for m in cg.score_members(calls, today=today)][0] == "Sharp"
    assert ms["Sharp"].best.ticker in ("UPP", "DWN") and ms["Sharp"].worst is not None


def test_ratings_roll_and_follow_the_recent_form():
    idx, bars = _market()
    n = len(idx)
    d = lambda k: idx[k].date().isoformat()
    today = idx[-1].date()
    good_old = [trade("Faded", "UPP", "buy", d(20 + 3 * k)) for k in range(30)]                  # right for a long time ...
    bad_new = [trade("Faded", "DWN", "buy", d(n - 110 + 8 * k)) for k in range(8)]              # ... wrong lately
    riser = [trade("Riser", "DWN", "buy", d(20 + 10 * k)) for k in range(10)] + \
            [trade("Riser", "UPP", "buy", d(n - 115 + 7 * k)) for k in range(12)]                # a poor start, right lately
    steady = [trade("Steady", "UPP", "buy", d(20 + 20 * k)) for k in range(16)]
    calls, _ = cg.build_calls(good_old + bad_new + riser + steady, bars, bars["SPY"], (21, 63))
    ms = {m.member: m for m in cg.score_members(calls, today=today)}
    f = ms["Faded"]
    assert f.base == "star" and f.trend == "down" and f.tier == "good" and f.weight == 2 and f.recent[0] >= 5 and f.recent[1] < 0.5
    r = ms["Riser"]
    assert r.base in ("plain", "thin") and r.trend == "up" and r.tier == "good" and r.recent[0] >= 10
    assert ms["Steady"].tier == "star" and ms["Steady"].trend in ("", "up")
    # the window rolls: seen from far enough in the future the old calls no longer count
    from datetime import timedelta
    later = {m.member: m for m in cg.score_members(calls, {"window_days": 200}, today=today)}
    assert later["Faded"].trades < f.trades and later["Faded"].tier != "star"
    assert cg.score_members(calls, today=today + timedelta(days=5000)) == []
    # a change of tier is recorded once, with what it means
    ch = cg.tier_changes({"house:Faded": "star", "house:Steady": "star", "house:Riser": "plain", "house:Gone": "good"},
                         list(ms.values()), today)
    assert {(c["member"], c["old"], c["new"]) for c in ch} == {("Faded", "star", "good"), ("Riser", "plain", "good")}
    text = cg.change_text(next(c for c in ch if c["member"] == "Riser"))
    assert "一般 → 偏准" in text and "开始纳入参考" in text
    assert "不再作参考" in cg.change_text({"member": "X", "old": "good", "new": "plain", "at": "2026-11-20"})


def test_report_alert_and_page(env):
    from stockdesk import headless, notify
    from stockdesk.ui import render, theme as T
    eng, market, store, _ = env
    idx = market.daily["SPY"].index
    n = len(idx)
    for sym, arr in (("UPP", np.linspace(100, 180, n)), ("DWN", np.linspace(100, 60, n)), ("SPY", [100.0] * n)):
        df = make_bars(arr, spread=0.001)
        df.index = idx
        market.daily[sym] = df
    d = lambda k: idx[k].date().isoformat()
    today = idx[-1].date().isoformat()
    rows = [src.CongressTrade(**{k: v for k, v in trade("Sharp Trader", "UPP", "buy", d(300 + 10 * k), doc=f"old{k}").items()}) for k in range(12)]
    new = [src.CongressTrade(**trade("Sharp Trader", "DWN", "sell", d(n - 12), today, doc="new1")),
           src.CongressTrade(**trade("Nobody Known", "AAA", "buy", d(n - 20), today, doc="new2")),
           src.CongressTrade(**trade("Nobody Known", "DWN", "buy", d(n - 20), today, doc="new3"))]
    for t in rows + new:
        store.save_congress_doc(src.Filing("house", t.doc_id, t.member, t.state, t.filed, 2026, t.url), "ok", [t])
    store.save_congress_doc(src.Filing("house", "9116403", "Paper Filer", "TN03", today, 2026, "u"), "scan", [])
    store.set_meta("congress:at", 1e12)               # "just scanned": build from the store, no network
    store.set_meta("congress:parser", src.PARSER)
    eng.cfg.set("congress.enabled", True)
    eng.cfg.set("congress.years_back", 3)
    sent = []
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(notify, "send", lambda s, subject, body, **k: sent.append((subject, body)))
    try:
        eng.cfg.set("notify.email", {"enabled": True, "to": "me@example.com", "smtp_host": "h", "smtp_port": 587, "username": "bot@example.com"})
        eng.refresh(force=True)
        assert eng.congress_due()                         # a new process builds its report first
        rep = eng.scan_congress()
        assert rep.docs == 15 and rep.scans == 1 and rep.backlog == 0 and rep.score("house:Sharp Trader").tier == "star"
        assert not eng.congress_due()
        snap = eng.build_snapshot()
        al = {a.id: a for a in snap.alerts if a.kind == "congress"}
        assert set(al) == {"cg:house:new1"}                               # only the accurate member is referred to
        eng.cfg.set("congress.alert_scope", "accurate_or_watchlist")      # ... unless trades in watched stocks are wanted too
        eng.scan_congress()
        snap = eng.build_snapshot()
        al = {a.id: a for a in snap.alerts if a.kind == "congress"}
        assert set(al) == {"cg:house:new1", "cg:house:new2"}
        star, watch = al["cg:house:new1"], al["cg:house:new2"]
        assert star.severity == "warn" and "★ 准" in star.title and "卖出 DWN" in star.title
        assert "近 730 天披露" in star.body
        for section in ("【披露了什么】", "【这位议员的记录】", "【怎么看】"):
            assert section in star.body
        assert "方向正确 12 笔，占 100%" in star.body and "45 天" in star.body and "不是买卖建议" in star.body
        assert "披露之后的下一个交易日才跟" in star.body and "交易日收盘" in star.body
        assert watch.symbol == "AAA" and "在你的自选里" in watch.body and "【AAA 现在的位置】" in watch.body and "样本少" in watch.body
        eng.build_snapshot()
        assert len(sent) == 2 and "议员交易" in sent[0][1] and "市值" not in sent[0][1] and "盈亏" not in sent[0][1]
        html = render.congress_html(snap.congress, T.Palette(), {"AAA", "BBB"})
        assert "谁买卖得准" in html and "Sharp Trader" in html and "最近披露" in html and "<b>AAA</b>" in html and "1 份是扫描的纸质申报" in html
        assert "【议员交易】" in "\\n".join(headless.digest(snap)) and "Sharp Trader" in "\\n".join(headless.digest(snap))
        k = rep.consensus("DWN", 90, idx[-1].date())
        assert k["buyers"] == {"Nobody Known": 0.0} and k["sellers"] == {"Sharp Trader": 3.0} and k["net"] == -3.0
        assert k["acc_buy"] == [] and k["acc_sell"] == ["Sharp Trader"]                       # the unrated buyer carries no weight
        assert "没有评级偏准以上的议员，所以不作参考" in star.body and "只参考评级偏准以上的议员" in html and "近期表现" in html
        # a change of tier between two scans is remembered and shown
        assert store.get_meta("congress:tiers")["house:Sharp Trader"] == "star" and snap.congress.changes == []
        store.set_meta("congress:tiers", {**store.get_meta("congress:tiers"), "house:Sharp Trader": "plain"})
        rep2 = eng.scan_congress()
        assert [(c["member"], c["old"], c["new"]) for c in rep2.changes] == [("Sharp Trader", "plain", "star")]
        assert eng.scan_congress().changes == rep2.changes                                    # recorded once
        snap2 = eng.build_snapshot()
        assert "评级变动" in render.congress_html(snap2.congress, T.Palette(), {"AAA"}) and "评级变动" in "\n".join(headless.digest(snap2))
        store.set_meta("congress:backlog", 7)             # history still being read: no alerts yet
        eng.scan_congress()
        assert not [a for a in eng.build_snapshot().alerts if a.kind == "congress"]
    finally:
        mp.undo()


def test_fetch_reads_new_reports_once_and_survives_bad_ones(env, monkeypatch):
    eng, market, store, _ = env
    xml = "<FinancialDisclosure>" + "".join(
        f"<Member><Last>M{k}</Last><First>A</First><FilingType>P</FilingType><StateDst>NJ0{k}</StateDst>"
        f"<FilingDate>11/1{k}/2026</FilingDate><DocID>2000{k}</DocID></Member>" for k in range(4)) + "</FinancialDisclosure>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.xml", xml)
    calls = []

    def getter(url, **kw):
        calls.append(url)
        return 200, {}, buf.getvalue() if url.endswith(".zip") else b"%PDF-" + url.encode()

    def fake_text(data):
        if data.endswith(b"20001.pdf"):
            return ""                                        # a scan
        if data.endswith(b"20002.pdf"):
            raise src.AdapterError("bad pdf")
        return PTR

    monkeypatch.setattr(src, "pdf_text", fake_text)
    eng.cfg.set("congress.enabled", True)
    eng.cfg.set("congress.years_back", 1)
    eng.cfg.set("congress.docs_per_scan", 3)
    eng.refresh(force=True)
    rep = eng.scan_congress(force=True, getter=getter)
    assert rep.backlog == 1 and store.congress_known() == {"house:20003": "ok", "house:20002": "error", "house:20001": "scan"}
    rep = eng.scan_congress(force=True, getter=getter)
    assert rep.backlog == 0 and rep.docs == 2 and rep.scans == 1 and len(store.load_congress_trades()) == 12
    n = len(calls)
    eng.scan_congress(force=True, getter=getter)
    assert len(calls) == n + 1                               # only the index again: nothing is downloaded twice
    assert "议员交易：正常" in eng._status["congress"][1]
    monkeypatch.setattr(src, "PARSER", src.PARSER + 1)       # a new parser re-reads everything
    eng.scan_congress(force=True, getter=getter)
    assert len(calls) > n + 2


# ---- Senate (eFD) --------------------------------------------------------------------------------------

SENATE_PTR = """<html><body><h1>Periodic Transaction Report for 11/17/2026</h1>
<table class="table table-striped"><thead><tr class="header"><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
<th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr></thead><tbody>
<tr><td>1</td><td>
 10/21/2026 </td><td>Joint</td><td> <a href="https://finance.yahoo.com/quote/UPP" target="_blank">UPP</a> </td>
<td> Upp Holdings &amp; Co </td><td>Stock</td><td>Sale (Partial)</td><td>$1,001 - $15,000</td><td>--</td></tr>
<tr><td>2</td><td>10/22/2026</td><td>Spouse</td><td>DWN</td><td>Down Corp <div>Option Type: Call</div></td><td>Stock Option</td>
<td>Purchase</td><td>$100,001 - $250,000</td><td>Strike $50</td></tr>
<tr><td>3</td><td>10/23/2026</td><td>Self</td><td>--</td><td>City of Example 5% 2035</td><td>Municipal Security</td>
<td>Purchase</td><td>Over $50,000,000</td><td>--</td></tr>
<tr><td>4</td><td>10/24/2026</td><td>Child</td><td>UPP</td><td>Upp Holdings</td><td>Stock</td><td>Exchange</td><td>$1,001 - $15,000</td><td>--</td></tr>
</tbody></table></body></html>"""


class FakeSenate:
    """Stands in for the Senate site: the agreement form, the search and the report pages."""

    def __init__(self, reports=3, refuse=False):
        self.log, self.reports, self.refuse = [], reports, refuse

    def __call__(self, path, form=None, referer="", ajax=False):
        self.log.append((path, dict(form or {}), ajax))
        if path == "/search/home/" and form is None:
            return 200, "https://efdsearch.senate.gov/search/home/", '<form id="agreement_form"><input name="csrfmiddlewaretoken" value="tok1"></form>'
        if path == "/search/home/":
            if self.refuse:
                return 200, "https://efdsearch.senate.gov/search/home/", '<form id="agreement_form"></form>'
            return 200, "https://efdsearch.senate.gov/search/", '<input name="csrfmiddlewaretoken" value="tok2">'
        if path == "/search/report/data/":
            start, size = int(form["start"]), int(form["length"])
            rows = [["Sam Q", "Senator", "Senator, Sam Q (Senator)",
                     f'<a href="/search/view/{"paper" if k == 1 else "ptr"}/00000000-0000-0000-0000-00000000000{k}/" target="_blank">PTR</a>',
                     f"11/1{k}/2026"] for k in range(self.reports)]
            return 200, "", __import__("json").dumps({"recordsTotal": len(rows), "data": rows[start:start + size], "result": "ok"})
        return 200, "", SENATE_PTR


def test_senate_report_table_is_parsed():
    f = src.Filing("senate", "abc", "Sam Q Senator", "", "2026-11-17", 2026, src.SENATE + "/search/view/ptr/abc/")
    rows = src.parse_senate_ptr(SENATE_PTR, f)
    assert [(r.ticker, r.side, r.partial, r.asset_type, r.owner) for r in rows] == [
        ("UPP", "sell", True, "ST", "joint"), ("DWN", "buy", False, "OP", "spouse"), ("", "buy", False, "GS", "self"),
        ("UPP", "exchange", False, "ST", "child")]
    assert rows[0].asset == "Upp Holdings & Co" and rows[0].traded == "2026-10-21" and (rows[0].lo, rows[0].hi) == (1001, 15000)
    assert rows[1].note == "Strike $50" and (rows[2].lo, rows[2].hi) == (50000000, 0) and rows[0].id == "senate:abc:1"
    assert rows[0].chamber == "senate" and rows[0].url.endswith("/ptr/abc/")
    assert src.parse_senate_ptr("<html>scanned pages</html>", f) is None


def test_senate_site_is_only_searched_after_its_agreement():
    import pytest
    site = FakeSenate()
    efd = src.SenateEfd(send=site)
    fil = efd.filings(date(2026, 1, 1), page_size=2)
    assert [x[0] for x in site.log[:2]] == ["/search/home/", "/search/home/"]                 # the form first, then the tick
    assert site.log[1][1] == {"prohibition_agreement": "1", "csrfmiddlewaretoken": "tok1"}
    searches = [x for x in site.log if x[0] == "/search/report/data/"]
    assert len(searches) == 2 and all(x[2] for x in searches)                                 # two pages of two
    assert searches[0][1]["report_types"] == "[11]" and searches[0][1]["submitted_start_date"] == "01/01/2026 00:00:00"
    assert searches[0][1]["csrfmiddlewaretoken"] == "tok2"
    assert [(f.member, f.filed, f.chamber) for f in fil] == [("Sam Q Senator", "2026-11-12", "senate"),
                                                              ("Sam Q Senator", "2026-11-11", "senate"), ("Sam Q Senator", "2026-11-10", "senate")]
    assert len(efd.trades(fil[0])) == 4 and efd.trades(fil[1]) is None                        # a paper filing is not fetched
    with pytest.raises(src.AdapterError):
        src.SenateEfd(send=FakeSenate(refuse=True)).filings(date(2026, 1, 1))
    with pytest.raises(src.AdapterError):                                                     # never another host
        src.SenateEfd()._http("@evil.example/x")


def test_engine_reads_the_senate_only_with_the_users_confirmation(env):
    eng, market, store, _ = env
    site = FakeSenate()
    eng._senate_send = site
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("x.xml", "<FinancialDisclosure></FinancialDisclosure>")
    getter = lambda url, **kw: (200, {}, empty.getvalue())
    eng.cfg.set("congress.enabled", True)
    eng.cfg.set("congress.years_back", 1)
    eng.refresh(force=True)
    eng.cfg.set("congress.senate", {"enabled": True, "agree_terms": False})
    rep = eng.scan_congress(force=True, getter=getter)
    assert site.log == [] and rep.docs == 0 and not rep.senate                                # switched on but not confirmed: nothing is sent
    eng.cfg.set("congress.senate", {"enabled": True, "agree_terms": True})
    rep = eng.scan_congress(force=True, getter=getter)
    assert rep.senate and rep.docs == 2 and rep.scans == 1 and rep.backlog == 0
    assert {t["chamber"] for t in store.load_congress_trades()} == {"senate"} and len(store.load_congress_trades()) == 8
    assert "参议院 2" in eng._status["congress"][1]
    n = len(site.log)
    eng.scan_congress(force=True, getter=getter)
    assert len(site.log) == n                                                                 # the list is cached, nothing is read twice

    def down(path, form=None, referer="", ajax=False):
        raise src.AdapterUnavailable("down")
    store.set_meta("congress:index:senate", 0)
    with store._conn() as c:
        c.execute("UPDATE meta SET updated=0 WHERE key='congress:index:senate'")
    eng._senate_send = down
    rep = eng.scan_congress(force=True, getter=getter)
    assert rep.errors and "参议院站点" in rep.errors[0] and rep.docs == 2                       # the House part still ran, the cache stays
