"""Treasury yields and auctions: parsing the official data, the tail estimate, the verdict, the alert."""
import json
import time

from stockdesk import treasury as tr

from test_engine_misc import env  # noqa: F401  (fixture)

CURVE = "Date,\"3 Mo\",\"2 Yr\",\"10 Yr\",\"30 Yr\"\n" + "\n".join(
    f"11/{d:02d}/2026,4.30,4.60,{4.80 + (0.12 if d == 25 else 0):.2f},5.20" for d in range(2, 26) if d not in (8, 9, 15, 16, 22, 23))


def auction(day, high, btc, dealer, term="10-Year", cusip=None):
    comp = 40e9
    return {"originalSecurityTerm": term, "securityType": "Note", "auctionDate": f"2026-11-{day:02d}T00:00:00", "cusip": cusip or f"C{day}",
            "highYield": str(high), "medianYield": str(high - 0.03), "bidToCoverRatio": str(btc), "offeringAmount": "42000000000",
            "primaryDealersCompetitiveParAwarded": str(comp * dealer), "directBiddersCompetitiveParAwarded": str(comp * 0.2),
            "indirectBiddersCompetitiveParAwarded": str(comp * (0.8 - dealer)), "reopening": "No"}


def rows():
    out = [auction(d, 4.80, 2.6, 0.10) for d in (3, 4, 5, 6, 7, 10)]                # six ordinary auctions, gap 0
    out.append(auction(24, 4.85, 2.35, 0.18))                                     # weak: low cover, dealers stuck, +5 bp
    out.append(auction(25, 4.88, 2.80, 0.05, cusip="S25"))                         # the curve says 4.92: stopped through
    out.append({**auction(25, 2.0, 3.0, 0.1), "inflationIndexSecurity": "Yes"})    # TIPS: left out
    return out


def getter(url, **kw):
    if "daily-treasury-rates" in url:
        return 200, {}, CURVE.encode()
    return 200, {}, json.dumps(rows() if "type=Note" in url else []).encode()


def test_auctions_are_judged_against_their_own_history():
    curve = tr.fetch_curve(2026, getter)
    au = tr.assess(tr.parse(tr.fetch_auctions(getter)), curve)
    assert len(au) == 8 and abs(au[0].indirect - 0.7) < 1e-9 and au[0].dealer == 0.1 and au[0].spread == 3.0
    weak, strong = au[-2], au[-1]
    assert weak.verdict == "weak" and weak.tail == 5.0 and len(weak.reasons) == 3
    assert strong.verdict == "strong" and strong.tail < -1 and strong.curve == 4.92
    text = "\n".join(tr.auction_lines(weak))
    assert "倍数 2.35" in text and "间接投标人" in text and "直接投标人 20.0%" in text and "一级交易商 18.0%（平均 10.0%）" in text and "疲弱" in text
    s = tr.curve_summary(curve)
    assert s["10 Yr chg"] == 12.0 and s["10-2"] == 32.0 and "10 年 4.92%（+12bp）" in tr.curve_line(s)


def test_engine_raises_rates_alerts_and_shows_them(env, monkeypatch):
    from stockdesk import headless
    from stockdesk.ui import render, theme as T
    eng, market, store, _ = env
    eng.cfg.set("rates.enabled", True)
    monkeypatch.setattr(time, "strftime", lambda f, t=None: "2026-11-22" if "%Y-%m-%d" in f and t is not None else time.asctime())
    rep = eng.scan_rates(getter)
    assert rep["summary"]["10 Yr"] == 4.92 and len(rep["auctions"]) == 8 and "美债数据：正常" in eng._status["rates"][1]
    snap = eng.refresh(force=True)
    al = {a.id.split(":")[1]: a for a in snap.alerts if a.kind == "rates"}
    assert set(al) == {"auction", "move"} or len([a for a in snap.alerts if a.kind == "rates"]) == 3
    titles = " ".join(a.title for a in snap.alerts if a.kind == "rates")
    assert "美债拍卖疲弱" in titles and "美债拍卖强劲" in titles and "单日 +12bp" in titles
    assert "美债收益率与拍卖" in render.flow_html(snap.flow, T.Palette(), snap.rates)
    assert "美债收益率" in "\n".join(headless.digest(snap))
