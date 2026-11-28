"""Hedge funds and large holders: CFTC positioning, FINRA short data, SEC 13F / Form 4 / 13D, with their delays."""
import json
from datetime import date, timedelta

from stockdesk import funds

from test_engine_misc import env  # noqa: F401  (fixture)

TODAY = date(2026, 11, 27)


def cftc_rows():
    rows = []
    for i in range(160):                       # weekly, oldest first; the S&P net short shrinks to its lightest at the end
        d = (date(2026, 11, 17) - timedelta(weeks=159 - i)).isoformat() + "T00:00:00.000"
        short = 500000 - i * 1000 if i < 159 else 250000
        for code in funds.CONTRACTS:
            rows.append({"cftc_contract_market_code": code, "report_date_as_yyyy_mm_dd": d, "open_interest_all": "2000000",
                         "lev_money_positions_long": "200000", "lev_money_positions_short": str(short if code == "13874A" else 600000 + (i % 7) * 1000),
                         "asset_mgr_positions_long": "900000", "asset_mgr_positions_short": "100000"})
    return rows


def shvol_file(day: date) -> str:
    ratio = 0.62 if day == date(2026, 11, 25) else 0.40 + (day.day % 3) * 0.01
    return "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n" + \
           f"{day:%Y%m%d}|AAA|{ratio * 1e6:.0f}|0|1000000|B,Q,N\n{day:%Y%m%d}|ZZZ|1|0|2|Q\n"


INFO = """<?xml version="1.0"?><informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>AAA CORP</nameOfIssuer><cusip>{c}</cusip><value>{v}</value><shrsOrPrnAmt><sshPrnamt>{s}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>AAA CORP</nameOfIssuer><cusip>{c}</cusip><value>5</value><shrsOrPrnAmt><sshPrnamt>300000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
<infoTable><nameOfIssuer>OTHER</nameOfIssuer><cusip>999999999</cusip><value>1</value><shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""

FORM4 = """<?xml version="1.0"?><ownershipDocument><issuer><issuerTradingSymbol>AAA</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
<aff10b5One>0</aff10b5One><nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle>
<transactionDate><value>2026-11-20</value></transactionDate><transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>20000</value></transactionShares><transactionPricePerShare><value>21.5</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""

SC13D = """<?xml version="1.0"?><edgarSubmission><formData><coverPageHeader><dateOfEvent>11/14/2026</dateOfEvent></coverPageHeader>
<reportingPersons><reportingPersonInfo><reportingPersonName>Activist Partners LP</reportingPersonName><percentOfClass>6.2</percentOfClass></reportingPersonInfo></reportingPersons></formData></edgarSubmission>"""


def subs(rows):
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    return json.dumps({"filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}).encode()


def getter(url, headers=None, **kw):
    if "publicreporting.cftc.gov" in url:
        return 200, {}, json.dumps(cftc_rows()).encode()
    if "regsho/daily" in url:
        d = date(int(url[-12:-8]), int(url[-8:-6]), int(url[-6:-4]))
        return (200, {}, shvol_file(d).encode()) if d <= date(2026, 11, 25) and d != date(2026, 11, 11) else (403, {}, b"")
    assert headers and headers["User-Agent"] == "StockDesk me@example.com"     # SEC: the user's declared contact
    if url.endswith("company_tickers.json"):
        return 200, {}, json.dumps({"0": {"cik_str": 1, "ticker": "AAA", "title": "AAA Corp"}}).encode()
    if url.endswith("CIK0000000001.json"):                                    # the issuer
        return 200, {}, subs([("4", "2026-11-23", "2026-11-20", "0001-26-000001", "xslF345X06/form4.xml"),
                              ("SCHEDULE 13D", "2026-11-18", "", "0002-26-000002", "primary_doc.xml"),
                              ("10-Q", "2026-11-01", "2026-09-30", "0001-26-000000", "q.htm")])
    if url.endswith(("CIK0000000007.json", "CIK0000000008.json")):           # two funds
        f = url[-6]
        return 200, {}, subs([("13F-HR", "2026-11-14", "2026-09-30", f"00{f}-26-000002", "primary_doc.xml"),
                              ("13F-HR", "2026-08-14", "2026-06-30", f"00{f}-26-000001", "primary_doc.xml")])
    if url.endswith("index.json"):
        return 200, {}, json.dumps({"directory": {"item": [{"name": "primary_doc.xml", "size": "10"}, {"name": "infotable.xml", "size": "900"}]}}).encode()
    if url.endswith("infotable.xml"):
        fund, q = url.split("/")[-2][2], url.split("/")[-2][-1]
        shares = {("7", "2"): 2_000_000, ("7", "1"): 1_000_000, ("8", "2"): 0, ("8", "1"): 500_000}[(fund, q)]
        body = INFO.format(c="000000AA1", v=shares * 20, s=shares)
        if shares == 0:
            body = body.replace("<cusip>000000AA1</cusip><value>0</value>", "<cusip>000000XXX</cusip><value>0</value>")
        return 200, {}, body.encode()
    if url.endswith("form4.xml"):
        return 200, {}, FORM4.encode()
    if url.endswith("primary_doc.xml"):
        return 200, {}, SC13D.encode()
    raise AssertionError(url)


def post(url, body):
    return [{"settlementDate": "2026-10-31", "currentShortPositionQuantity": 10e6, "previousShortPositionQuantity": 9e6, "changePercent": 11.1, "daysToCoverQuantity": 2.0},
            {"settlementDate": "2026-11-13", "currentShortPositionQuantity": 13e6, "previousShortPositionQuantity": 10e6, "changePercent": 30.0, "daysToCoverQuantity": 3.1}]


def test_cftc_percentile_and_delay():
    ps = {p["code"]: p for p in funds.cftc_positions(cftc_rows(), TODAY)}
    es = ps["13874A"]
    assert es["date"] == "2026-11-17" and es["published"] == "2026-11-20" and es["rank"] == 100.0 and es["change"] == 92000
    line = funds.cftc_line(es, TODAY)
    assert "净空" in line and "100% 分位" in line and "数据截至 11-17，约 11-20 公布，距今 10 天" in line
    assert "基差交易" in funds.cftc_reading(ps["043602"])


def test_short_volume_and_short_interest():
    days = {}
    d = date(2026, 11, 25)
    while len(days) < 21:
        days[d.isoformat()] = funds.parse_shvol(shvol_file(d), ["AAA"])
        d -= timedelta(days=1)
    st = funds.shvol_stats(days, "AAA")
    assert st["date"] == "2026-11-25" and round(st["ratio"], 2) == 0.62 and st["z"] > 10 and st["days"] == 20
    si = funds.short_interest(post(None, None))
    assert si["date"] == "2026-11-13" and si["published"] == "2026-11-24" and si["change_pct"] == 30.0
    assert "距今 14 天" in funds.si_line("AAA", si, TODAY)


def test_engine_reads_everything_and_raises_alerts(env, monkeypatch):
    from stockdesk import headless
    from stockdesk.ui import render, theme as T
    eng, market, store, _ = env
    monkeypatch.setattr(funds, "CIK", {"AAA": 1})
    monkeypatch.setattr(funds, "FUNDS", {7: "Fund Seven", 8: "Fund Eight"})
    monkeypatch.setattr(funds.Sec, "__init__", lambda self, contact, getter=None, pause=0: (
        setattr(self, "ua", f"StockDesk {contact}".strip()), setattr(self, "getter", getter), setattr(self, "pause", 0), setattr(self, "_t", 0.0)) and None)
    eng.cfg.set("funds.enabled", True)
    eng.cfg.set("funds.cusips", {"AAA": "000000AA1"})
    eng.cfg.set("funds.sec_contact", "me@example.com")
    monkeypatch.setattr("stockdesk.engine.datetime", type("D", (), {"now": staticmethod(lambda: __import__("datetime").datetime(2026, 11, 27, 9))}))
    rep = eng.scan_funds(getter=getter, post=post, today=TODAY)
    assert not rep["errors"], rep["errors"]
    assert rep["shvol"]["AAA"]["z"] > 10 and rep["si"]["AAA"]["change_pct"] == 30.0
    f = rep["insiders"][0]
    assert f["owner"] == "Doe Jane" and f["role"] == "CEO" and f["tx"][0]["code"] == "P" and f["tx"][0]["shares"] == 20000
    h = rep["holders"][0]
    assert h["filer"] == "Activist Partners LP" and h["percent"] == 6.2 and h["event"] == "2026-11-14"
    s = rep["f13"]["AAA"]
    assert s["period"] == "2026-09-30" and s["added"] == ["Fund Seven"] and s["exited"] == ["Fund Eight"] and s["net_shares"] == 1_000_000 - 500_000
    assert s["rows"][0]["options"] == {"Put": 300000.0}

    snap = eng.refresh(force=True)
    al = {a.id.split(":")[1]: a for a in snap.alerts if a.kind == "funds"}
    assert {"cftc", "shvol", "si", "f4", "13dg", "13f"} <= set(al)
    assert al["f4"].severity == "warn" and "内部人公开市场买入约 $0.43M" in al["f4"].title and "交易日 11-20，11-23 报送" in al["f4"].body
    assert al["13dg"].severity == "warn" and "13D" in al["13dg"].title and "Activist Partners LP" in al["13dg"].body
    assert al["cftc"].severity == "warn" and "最偏多" in al["cftc"].title
    assert al["shvol"].severity == "info" and al["13f"].severity == "info"
    html = render.funds_html(snap.funds, T.Palette(), {"AAA"})
    assert "期货仓位" in html and "Fund Seven" in html and "各数据的延迟" in html and "sec_contact" not in html
    text = "\n".join(headless.digest(snap))
    assert "【对冲基金与大户】" in text and "对冲基金仓位：" in text and "Doe Jane" in text

    # a second scan within the schedule reads nothing again
    calls = []
    rep2 = eng.scan_funds(getter=lambda *a, **k: calls.append(a) or getter(*a, **k), post=post, today=TODAY)
    assert calls == [] and rep2["f13"]["AAA"]["added"] == ["Fund Seven"]


def test_without_sec_contact_the_sec_parts_stay_off(env, monkeypatch):
    from stockdesk.ui import render, theme as T
    eng, *_ = env
    eng.cfg.set("funds.enabled", True)
    monkeypatch.setattr(funds, "CIK", {})
    seen = []
    rep = eng.scan_funds(getter=lambda url, headers=None, **k: seen.append(url) or getter(url, headers={"User-Agent": "StockDesk me@example.com"}, **k),
                         post=post, today=TODAY)
    assert not rep["sec_ready"] and "f13" not in rep and not any("sec.gov" in u for u in seen)
    assert "funds.sec_contact" in eng._status["funds"][1]
    assert "sec_contact" in render.funds_html(rep, T.Palette(), set())
