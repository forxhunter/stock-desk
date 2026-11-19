"""Render every page, alert and mail in English and report any Chinese that is left.

Run as a script with STOCKDESK_LANG=en (tests/test_english.py does that); prints "OK" or the leftovers.
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["STOCKDESK_LANG"] = "en"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "src")]

import yaml  # noqa: E402

from stockdesk import flow, headless, i18n, notify, paths, plans  # noqa: E402
from stockdesk.config import Config  # noqa: E402
from stockdesk.engine import Engine  # noqa: E402
from stockdesk.models import Position, Trade  # noqa: E402
from stockdesk.store import Store  # noqa: E402
from stockdesk.ui import render, theme as T  # noqa: E402
from stockdesk.watchlist import Watchlist  # noqa: E402
from test_engine_misc import FakeMarket, _Broker  # noqa: E402
from test_range_trading import _fall_then_base  # noqa: E402

CJK = re.compile(r"[一-鿿、-〿＀-￯]+")   # U+3000 is only used as spacing


def main() -> int:
    assert i18n.LANG == "en"
    tmp = Path(tempfile.mkdtemp())
    paths.DATA_DIR = tmp / "data"
    paths.RH_IMPORT_DIR = tmp / "data" / "imports" / "robinhood"
    paths.ROOT = tmp
    wl = tmp / "watchlist.yaml"
    wl.write_text(yaml.safe_dump({"tickers": [
        {"symbol": "AAA", "shares": 10, "cost": 50.0, "stop": 1.0, "target": 100000.0},
        {"symbol": "BBB", "shares": 500, "cost": 80.0, "stop": 100000.0}, {"symbol": "CCC"}]}), encoding="utf-8")
    cfg = Config.load(HERE.parent / "config.default.yaml", tmp / "data" / "config.yaml")
    cfg.set("research.semianalysis.enabled", False)
    cfg.set("research.polymarket.enabled", False)
    market = FakeMarket(["AAA", "BBB", "CCC", "SPY"])
    base = _fall_then_base(True)
    base.index = market.daily["SPY"].index[-len(base):]
    market.daily["CCC"] = base
    home = market.daily["AAA"].copy()
    home[["open", "high", "low", "close"]] *= 1000.0
    market.daily["HOME.KS"] = home
    market.daily["EUR1.F"] = market.daily["AAA"].copy()
    for sym, rate in (("EURUSD=X", 1.25), ("KRWUSD=X", 0.00125)):
        fx = market.daily["SPY"].copy()
        fx[["open", "high", "low", "close"]] = rate
        market.daily[sym] = fx
    cfg.set("market.symbol_map", {"EUR1": "EUR1.F"})
    cfg.set("market.reference", {"EUR1": {"symbol": "HOME.KS", "currency": "KRW", "ratio": 1, "name": "Home"}})
    cfg.set("alerts.bottoming", {"enabled": True, "symbols": ["CCC", "AAA"], "min_score": 5})
    cfg.set("notify.email", {"enabled": True, "to": "me@example.com", "smtp_host": "h", "smtp_port": 587,
                             "username": "bot@example.com"})
    cfg.set("remote.host", "me@box")
    store = Store(tmp / "data" / "t.sqlite")
    eng = Engine(cfg, store, Watchlist(wl, tmp / "none.yaml"), market)
    eng._adapters = lambda: [_Broker([Position("EUR1", 10, 100.0, "IBKR", "EUR")])]
    px = float(market.daily["AAA"]["close"].iloc[-1])
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "plans.yaml").write_text(yaml.safe_dump({"plans": {
        "AAA": {"start": "2026-01-01", "end": "2027-01-01", "kind": "build", "buy_below": [px * 1.1, px * 0.5],
                "target_price": px * 0.9, "max_weight_pct": 15.0, "exit_below": px * 1.2, "review_days": 1},
        "EUR1": {"start": "2026-01-01", "end": "2027-01-01", "follow": "HOME.KS", "exit_below": px * 2000,
                 "reduce_above": [px * 500], "target_weight_pct": 5.0, "no_add": True},
        "BBB": {}}}), encoding="utf-8")
    days = market.daily["BBB"].index
    store.upsert_trades([Trade(f"t{i}", "BBB", "BUY" if i % 2 == 0 else "SELL", 5, float(market.daily["BBB"]["close"].iloc[-40 + i * 3]),
                               days[-40 + i * 3].timestamp(), "ibkr") for i in range(12)])
    mails = []
    notify.send = lambda s, subject, body, **k: mails.append((subject, body + notify.FOOTER))
    it = flow.scan_polymarket(
        [{"proxy_wallet": "0x" + "a" * 40, "side": "BUY", "condition_id": "0xc1", "size": 400000.0, "price": 0.5,
          "timestamp": time.time() - 300 * (k + 1), "title": "Will AAA beat?", "event_slug": "ev", "outcome": "Yes",
          "outcome_index": 0, "pseudonym": "Calm-Otter"} for k in range(4)] +
        [{"proxy_wallet": "0x" + "b" * 40, "side": "BUY", "condition_id": "0xc1", "size": 50000.0, "price": 0.98,
          "timestamp": time.time() - 60, "title": "Will AAA beat?", "event_slug": "ev", "outcome": "Yes", "outcome_index": 0}],
        {"ev": {"slug": "ev", "title": "E", "tags": [{"label": "Fed"}], "markets": [
            {"conditionId": "0xc1", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.55", "0.45"]', "volume24hr": 90000.0,
             "oneHourPriceChange": 0.09}]}}, {"AAA": ["AAA"]}, None)
    import pandas as pd
    far = f"{pd.Timestamp.now() + pd.Timedelta(days=30):%Y-%m-%d}"
    calls = pd.DataFrame([{"contractSymbol": "AAA1C", "strike": 400.0, "lastPrice": 5.0, "volume": 4000, "openInterest": 500,
                           "impliedVolatility": 0.5, "inTheMoney": False}])
    op = flow.scan_options({"AAA": {far: (calls, calls)}}, None, {"AAA"})
    idx = pd.bdate_range("2026-06-01", periods=80)
    calm = [100 + 0.2 * (i % 2) for i in range(79)]
    fu = flow.scan_futures({s: pd.DataFrame({"close": calm + [95.0], "volume": [1000.0] * 78 + [2500.0, 400.0]}, index=idx)
                            for s in flow.DEFAULTS["futures"]["symbols"]})
    eng._flow = flow.FlowReport(ts=time.time(), items=it[0] + op[0] + fu[0],
                                notes=[f"polymarket|{n}" for n in it[1]] + [f"options|{n}" for n in op[1]] +
                                      [f"futures|{n}" for n in fu[1]], errors=["Polymarket: x"])
    os.environ["STOCKDESK_ROLE"] = "local"
    eng.refresh(force=True)
    snap = eng.refresh(force=True)
    pal = T.Palette("red")
    texts = {"sources": render.sources_html(snap), "alerts": render.alerts_html(snap.alerts, snap),
             "briefing": render.briefing_html(snap.briefing, pal, snap.refs, snap.plans, snap.bottoms),
             "research": render.research_html(snap.news, snap.odds), "flow": render.flow_html(snap.flow, pal),
             "flow_off": render.flow_html(None, pal), "private": render.private_html("x"),
             "disclaimer": render.disclaimer_html(), "digest": "\n".join(headless.digest(snap)),
             "mails": "\n".join(s + "\n" + b for s, b in mails),
             "alert_text": "\n".join(a.title + "\n" + a.body for a in snap.alerts),
             "status": "\n".join(m for _, m in snap.sources.values()),
             "plan_levels": str(plans.reference_levels(snap.get("AAA"), 25.0, None, False))}
    for method in ("risk_parity", "min_vol", "max_sharpe", "mean_variance"):
        try:
            texts[f"portfolio_{method}"] = render.portfolio_html(snap, method, pal)
        except Exception as e:                     # an unknown method name is not what this script checks
            texts[f"portfolio_{method}"] = "" if isinstance(e, KeyError) else (_ for _ in ()).throw(e)
    for v in snap.tickers:
        texts[f"analysis_{v.symbol}"] = render.analysis_html(v, pal, snap.refs.get(v.symbol))
        if v.analysis is not None and v.analysis.range is not None:
            texts[f"range_{v.symbol}"] = render.range_html(v.analysis.range, pal)
    checks, used = eng.pretrade("BBB", "BUY", 100000, None)
    texts["pretrade"] = render.pretrade_html(checks, "BBB", "BUY", 100000, used)
    checks, used = eng.pretrade("AAA", "SELL", 5, None)
    texts["pretrade_sell"] = render.pretrade_html(checks, "AAA", "SELL", 5, used)

    kinds = {a.kind for a in snap.alerts}
    need = {"bottoming", "plan_level", "flow"}
    problems = [f"missing alert kinds: {sorted(need - kinds)}"] if need - kinds else []
    if not mails:
        problems.append("no mail was produced")
    if "[Position]" not in "".join(v for k, v in texts.items() if k.startswith("analysis_")):
        problems.append("the [Position] line is missing from the analysis page")
    for name, text in texts.items():
        left = sorted(set(CJK.findall(text)))
        if left:
            problems.append(f"{name}: " + " | ".join(left[:12]))
    print("\n".join(problems) if problems else "OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
