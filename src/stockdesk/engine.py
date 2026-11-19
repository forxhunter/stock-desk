"""Orchestration: sync (read-only) -> cache -> analyse -> one immutable Snapshot for the UI."""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

import pandas as pd

from . import briefing, credentials, flow, notify, paths, plans
from .adapters.base import AdapterError, AdapterUnavailable, BrokerAdapter, RateLimited
from .adapters.ibkr_cp import IbkrClientPortalAdapter
from .adapters.ibkr_flex import IbkrFlexAdapter
from .adapters.ibkr_tws import IbkrTwsAdapter
from .adapters.polymarket import PolymarketAdapter, for_watchlist
from .adapters.robinhood_csv import RobinhoodCsvAdapter
from .adapters.robinhood_unofficial import RobinhoodUnofficialAdapter
from .adapters.semianalysis import SemiAnalysisAdapter
from .adapters.yahoo import YahooAdapter
from .behavior import detectors
from .behavior.demo import make_demo_trades
from .config import Config
from .i18n import L
from .models import Alert, BrokerData, MarketOdds, NewsItem, Position, Trade
from .quant import optimizer
from .quant.analysis import Analysis, analyze
from .quant.bottom import LABELS as bottom_labels
from .quant.bottom import BottomState, bottom_state
from .quant.reference import RefState, compare
from .quant.risk import Concentration, concentration
from .store import Store
from .watchlist import Watchlist

log = logging.getLogger(__name__)


@dataclass
class TickerView:
    symbol: str
    price: float = float("nan")
    prev_close: float = float("nan")
    day_chg: float = 0.0
    day_pct: float = 0.0
    qty: float = 0.0
    avg_cost: float = 0.0
    value: float = 0.0
    upl: float = 0.0
    upl_pct: float = 0.0
    day_pnl: float = 0.0
    weight: float = 0.0
    spark: list[float] = field(default_factory=list)
    target: float | None = None
    stop: float | None = None
    levels_src: str = ""          # "user" | "auto" | ""
    progress: float | None = None  # 0 = at stop, 1 = at target
    low52: float = float("nan")
    high52: float = float("nan")
    source: str = ""
    currency: str = "USD"
    fx: float = 1.0               # local currency -> USD; value / upl / day_pnl are in USD
    note: str = ""
    auto: bool = False
    analysis: Analysis | None = None
    bars: pd.DataFrame | None = None
    news: list[NewsItem] = field(default_factory=list)
    odds: list[MarketOdds] = field(default_factory=list)
    plan: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    ts: float = 0.0
    tickers: list[TickerView] = field(default_factory=list)
    total_value: float = 0.0
    total_cost: float = 0.0
    total_upl: float = 0.0
    day_pnl: float = 0.0
    day_pct: float = 0.0
    cash: float | None = None
    equity: float = 0.0
    equity_known: bool = False     # False = equity is just the sum of positions (cash unknown)
    mode: str = L("演示", "Demo")
    sources: dict[str, tuple[str, str]] = field(default_factory=dict)   # name -> (state, message)
    conc: Concentration | None = None
    opt: optimizer.OptResult | None = None
    alerts: list[Alert] = field(default_factory=list)
    new_alerts: list[Alert] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    odds: list[MarketOdds] = field(default_factory=list)
    market_ok: bool = True
    market_msg: str = ""
    demo_trades: bool = False
    sample_holdings: bool = False  # manual holdings are still the demo numbers from watchlist.sample.yaml
    n_trades: int = 0
    trades: list[Trade] = field(default_factory=list)
    briefing: briefing.Briefing | None = None
    refs: dict[str, RefState] = field(default_factory=dict)       # listing -> home-market comparison
    plans: list[plans.PlanStatus] = field(default_factory=list)   # the user's own written plans
    bottoms: dict[str, BottomState] = field(default_factory=dict)  # symbol -> bottoming check
    flow: flow.FlowReport | None = None                           # unusual-activity scan

    def get(self, symbol: str) -> TickerView | None:
        return next((t for t in self.tickers if t.symbol == symbol), None)


class Engine:
    def __init__(self, cfg: Config | None = None, store: Store | None = None,
                 watchlist: Watchlist | None = None, market=None,
                 code_prompt: Callable[[str], str] | None = None):
        paths.ensure_dirs()
        self.cfg = cfg or Config.load()
        self.store = store or Store(paths.DB_PATH)
        self.watchlist = watchlist or Watchlist()
        self.market = market or YahooAdapter()
        self.code_prompt = code_prompt
        self._daily: dict[str, pd.DataFrame] = {}
        self._intraday: dict[str, pd.DataFrame] = {}
        self._hist_at = 0.0
        self._broker_at = 0.0
        self._broker: dict[str, BrokerData] = {}
        self._status: dict[str, tuple[str, str]] = {}
        self._rh_unofficial: RobinhoodUnofficialAdapter | None = None
        self._backoff_until = 0.0
        self._market_msg = ""
        self._news: list[NewsItem] = self.store.load_news()
        self._odds: list[MarketOdds] = [MarketOdds(**d) for d in self.store.get_meta("polymarket:odds", []) or []]
        self._flow = flow.FlowReport.from_dict(self.store.get_meta("flow:report", None))
        self._flow_parts: dict[str, tuple[list, list]] = {}
        self.snapshot = Snapshot()

    # ------------------------------------------------------------------ brokers
    def _adapters(self) -> list[BrokerAdapter]:
        c, out = self.cfg, []
        if c.get("sources.ibkr_tws.enabled"):
            out.append(IbkrTwsAdapter(c.get("sources.ibkr_tws.host", "127.0.0.1"), c.get("sources.ibkr_tws.port", 7497),
                                      c.get("sources.ibkr_tws.client_id", 47), c.get("sources.ibkr_tws.timeout", 4)))
        if c.get("sources.ibkr_cp.enabled"):
            out.append(IbkrClientPortalAdapter(c.get("sources.ibkr_cp.base_url", "https://localhost:5000/v1/api")))
        if c.get("sources.ibkr_flex.enabled"):
            out.append(IbkrFlexAdapter(credentials.get_secret(credentials.KEY_FLEX_TOKEN),
                                       c.get("sources.ibkr_flex.query_id", "")))
        if c.get("sources.robinhood_unofficial.enabled"):
            user = c.get("sources.robinhood_unofficial.username", "")
            if self._rh_unofficial is None or self._rh_unofficial.username != user:
                self._rh_unofficial = RobinhoodUnofficialAdapter(
                    user, lambda: credentials.get_secret(credentials.KEY_RH_PASSWORD), self.code_prompt,
                    bool(c.get("sources.robinhood_unofficial.store_session", False)))
            out.append(self._rh_unofficial)
        if c.get("sources.robinhood_csv.enabled") and RobinhoodCsvAdapter(paths.RH_IMPORT_DIR).files():
            out.append(RobinhoodCsvAdapter(paths.RH_IMPORT_DIR))
        return out

    def _min_interval(self, name: str) -> float:
        if name == "ibkr_flex":   # Flex is rate limited by IBKR; do not hammer it
            return float(self.cfg.get("sources.ibkr_flex.min_interval_minutes", 30)) * 60
        return 0.0

    def sync_brokers(self, force: bool = False) -> None:
        adapters = self._adapters()
        active = {a.name for a in adapters}
        for name in list(self._status):
            if name not in active and name not in ("yahoo", "semianalysis", "polymarket", "email"):
                self._status.pop(name, None)
                self._broker.pop(name, None)
        if not force and time.time() - self._broker_at < float(self.cfg.get("sources.broker_refresh_seconds", 300)):
            return
        self._broker_at = time.time()
        for a in adapters:
            age = self.store.meta_age(f"broker:{a.name}")
            if not force and a.name in self._broker and age is not None and age < self._min_interval(a.name):
                continue
            try:
                data = a.fetch()
                self._broker[a.name] = data
                self.store.set_meta(f"broker:{a.name}", {
                    "positions": [asdict(p) for p in data.positions], "cash": data.cash,
                    "net_liquidation": data.net_liquidation})
                notes = L("；", "; ").join(getattr(a, "notes", []) or [])
                self._status[a.name] = ("ok", L(f"{a.label}：已同步 {len(data.positions)} 个持仓"
                                                f"{'、' + str(len(data.trades)) + ' 笔成交' if data.trades else ''}"
                                                f"{'。' + notes if notes else ''}",
                                                f"{a.label}: synced {len(data.positions)} position(s)"
                                                f"{', ' + str(len(data.trades)) + ' trade(s)' if data.trades else ''}"
                                                f"{'. ' + notes if notes else ''}"))
            except AdapterError as e:
                cached = self.store.get_meta(f"broker:{a.name}")
                state = "off" if isinstance(e, AdapterUnavailable) else "error"
                msg = L(f"{a.label}：{e}", f"{a.label}: {e}")
                if cached and a.name not in self._broker:
                    self._broker[a.name] = BrokerData(
                        positions=[Position(**p) for p in cached.get("positions", [])],
                        cash=cached.get("cash"), net_liquidation=cached.get("net_liquidation"))
                if a.name in self._broker:
                    msg += L("（显示的是上次同步的持仓）", " (showing the positions from the last sync)")
                self._status[a.name] = (state, msg)
                log.warning("broker %s unavailable: %s", a.name, e)
            except Exception as e:  # never let a broker problem kill the refresh loop
                self._status[a.name] = ("error", L(f"{a.label}：内部错误 {type(e).__name__}", f"{a.label}: internal error {type(e).__name__}"))
                log.exception("broker %s failed", a.name)

        # persist executions: one trade source per broker to avoid double counting
        ibkr_src = next((n for n in ("ibkr_flex", "ibkr_tws", "ibkr_cp") if self._broker.get(n) and self._broker[n].trades), None)
        rh_src = next((n for n in ("robinhood_unofficial", "robinhood_csv") if self._broker.get(n) and self._broker[n].trades), None)
        real: list[Trade] = []
        for src in (ibkr_src, rh_src):
            if src:
                real.extend(self._broker[src].trades)
        if rh_src == "robinhood_csv":      # rebuilt from files each time: drop rows the files no longer contain
            self.store.prune_trades("rhcsv:", [t.id for t in self._broker[rh_src].trades])
        if real:
            self.store.delete_demo_trades()
            self.store.upsert_trades(real)

    def _broker_positions(self) -> list[Position]:
        """One position source per broker (TWS > Client Portal > Flex; unofficial RH > CSV)."""
        out: list[Position] = []
        for group in (("ibkr_tws", "ibkr_cp", "ibkr_flex"), ("robinhood_unofficial", "robinhood_csv")):
            src = next((n for n in group if n in self._broker), None)
            if src:
                out.extend(self._broker[src].positions)
        return out

    # ------------------------------------------------------------------ currencies / foreign listings
    @staticmethod
    def _fx_symbol(currency: str) -> str:
        return f"{currency}USD=X"

    def _currencies(self, positions: dict[str, Position]) -> dict[str, str]:
        """symbol -> quote currency: config (market.currencies) first, then what the broker reported."""
        out = {s: (p.currency or "USD").upper() for s, p in positions.items()}
        out.update({s: str(c).upper() for s, c in (self.cfg.get("market.currencies", {}) or {}).items()})
        return out

    def _references(self) -> dict[str, dict]:
        """listing -> {symbol, currency, ratio, name} from market.reference (only for watched symbols)."""
        out = {}
        known = set(self.watchlist.symbols())
        for sym, r in (self.cfg.get("market.reference", {}) or {}).items():
            if sym in known and isinstance(r, dict) and r.get("symbol"):
                out[sym] = {"symbol": str(r["symbol"]), "currency": str(r.get("currency", "USD")).upper(),
                            "ratio": float(r.get("ratio", 1.0) or 1.0), "name": str(r.get("name", r["symbol"]))}
        return out

    def _fx_series(self, currency: str) -> pd.Series | None:
        df = self._daily.get(self._fx_symbol(currency)) if currency != "USD" else None
        return df["close"] if df is not None and len(df) else None

    def _fx_rate(self, currency: str) -> float | None:
        if currency == "USD":
            return 1.0
        df = self._daily.get(self._fx_symbol(currency))
        return float(df["close"].iloc[-1]) if df is not None and len(df) else None

    def _fetch(self, kind: str, symbols: list[str], *args) -> dict[str, pd.DataFrame]:
        """Call the market adapter with the data source's own symbols (market.symbol_map)."""
        smap = self.cfg.get("market.symbol_map", {}) or {}
        back = {smap.get(s, s): s for s in symbols}
        got = getattr(self.market, kind)(list(back), *args)
        return {back[k]: df for k, df in got.items() if k in back}

    # ------------------------------------------------------------------ market data
    def _load_market(self, symbols: list[str], force: bool = False) -> None:
        now = time.time()
        ttl = float(self.cfg.get("app.history_ttl_minutes", 30)) * 60
        missing = [s for s in symbols if s not in self._daily]
        for s in missing:  # warm start from the local cache
            df = self.store.load_bars(s, "1d")
            if not df.empty:
                self._daily[s] = df
            intr = self.store.load_bars(s, "15m")
            if not intr.empty:
                self._intraday[s] = intr
        if now < self._backoff_until and not force:
            self._market_msg = L("行情源限流中，暂用本地缓存", "Quote source is rate-limiting; using the local cache for now")
            return
        need_hist = force or now - self._hist_at > ttl or any(s not in self._daily for s in symbols)
        hist_age = self.store.meta_age("yahoo:history_at")
        if need_hist and not force and self._hist_at == 0 and hist_age is not None and hist_age < ttl \
                and all(s in self._daily for s in symbols):
            need_hist, self._hist_at = False, now - hist_age
        try:
            if need_hist:
                hist = self._fetch("history", symbols, self.cfg.get("app.history_period", "5y"))
                for s, df in hist.items():
                    self._daily[s] = df
                    self.store.save_bars(s, "1d", df)
                if hist:
                    self._hist_at = now
                    self.store.set_meta("yahoo:history_at", now)
            intr = self._fetch("intraday", symbols)
            for s, df in intr.items():
                self._intraday[s] = df
                self.store.save_bars(s, "15m", df)
            if not intr:
                raise AdapterUnavailable(L("行情源没有返回数据", "The quote source returned no data"))
            self._market_msg = ""
            self._status["yahoo"] = ("ok", L("Yahoo 行情：正常", "Yahoo quotes: OK"))
        except RateLimited as e:
            self._backoff_until = now + 600
            self._market_msg = L(f"{e}，10 分钟后重试，暂用本地缓存", f"{e}; retrying in 10 minutes, using the local cache for now")
            self._status["yahoo"] = ("error", self._market_msg)
        except AdapterError as e:
            self._market_msg = L(f"{e}，暂用本地缓存", f"{e}; using the local cache for now")
            self._status["yahoo"] = ("off", self._market_msg)
            log.warning("market data: %s", e)

    @staticmethod
    def _session_stats(intr: pd.DataFrame | None, daily: pd.DataFrame | None):
        """(last price, previous close, sparkline points) from intraday bars, falling back to daily."""
        price = prev = float("nan")
        spark: list[float] = []
        if intr is not None and len(intr):
            idx = intr.index.tz_localize("UTC").tz_convert("America/New_York")
            days = pd.Series(idx.date, index=intr.index)
            last_day = days.iloc[-1]
            today = intr[days == last_day]
            before = intr[days < last_day]
            price = float(today["close"].iloc[-1])
            spark = [float(x) for x in today["close"]]
            if len(before):
                prev = float(before["close"].iloc[-1])
                if len(spark) < 6:
                    spark = [float(x) for x in before["close"].iloc[-20:]] + spark
            if daily is not None and len(daily) >= 2:
                d_last = daily.index[-1].date()
                dprev = float(daily["close"].iloc[-2] if d_last >= last_day else daily["close"].iloc[-1])
                if math.isnan(prev):
                    prev = dprev
        elif daily is not None and len(daily) >= 2:
            price, prev = float(daily["close"].iloc[-1]), float(daily["close"].iloc[-2])
            spark = [float(x) for x in daily["close"].iloc[-30:]]
        return price, prev, spark

    # ------------------------------------------------------------------ research
    def aliases(self) -> dict[str, list[str]]:
        table = self.cfg.get("aliases", {}) or {}
        return {s: list(table.get(s, [])) for s in self.watchlist.symbols()}

    def research_due(self) -> bool:
        sa = self.cfg.get("research.semianalysis.enabled") and (
            (self.store.meta_age("semianalysis:at") or 1e12) > float(self.cfg.get("research.semianalysis.min_interval_minutes", 60)) * 60)
        pm = self.cfg.get("research.polymarket.enabled") and (
            (self.store.meta_age("polymarket:at") or 1e12) > float(self.cfg.get("research.polymarket.min_interval_minutes", 15)) * 60)
        key = ",".join(sorted(self.watchlist.symbols()))
        changed = self.store.get_meta("research:symbols") != key
        return bool(sa or pm or changed)

    def refresh_research(self, force: bool = False) -> None:
        c = self.cfg
        watch = self.aliases()
        key = ",".join(sorted(watch))
        changed = self.store.get_meta("research:symbols") != key
        if c.get("research.semianalysis.enabled"):
            age = self.store.meta_age("semianalysis:at")
            if force or changed or age is None or age > float(c.get("research.semianalysis.min_interval_minutes", 60)) * 60:
                validators = {} if (changed or force) else (self.store.get_meta("semianalysis:validators", {}) or {})
                ad = SemiAnalysisAdapter(c.get("research.semianalysis.feeds", []), watch,
                                         max_items=int(c.get("research.semianalysis.max_items", 40)),
                                         validators=validators)
                try:
                    items = ad.fetch()
                    if items:
                        self.store.upsert_news(items)
                    self.store.set_meta("semianalysis:validators", ad.validators)
                    self._news = self.store.load_news()
                    self._status["semianalysis"] = ("ok", L(f"SemiAnalysis RSS：正常（缓存 {len(self._news)} 篇）", f"SemiAnalysis RSS: OK ({len(self._news)} articles cached)"))
                except AdapterError as e:
                    self._status["semianalysis"] = ("off", L(f"SemiAnalysis RSS：{e}（显示缓存内容）", f"SemiAnalysis RSS: {e} (showing cached content)"))
                except Exception as e:
                    self._status["semianalysis"] = ("error", L(f"SemiAnalysis RSS：内部错误 {type(e).__name__}", f"SemiAnalysis RSS: internal error {type(e).__name__}"))
                    log.exception("semianalysis failed")
                self.store.set_meta("semianalysis:at", time.time())
        if c.get("research.polymarket.enabled"):
            age = self.store.meta_age("polymarket:at")
            if force or changed or age is None or age > float(c.get("research.polymarket.min_interval_minutes", 15)) * 60:
                ad = PolymarketAdapter(c.get("research.polymarket.keywords", []), c.get("research.polymarket.slugs", []),
                                       per_keyword=int(c.get("research.polymarket.per_keyword", 3)),
                                       min_volume=float(c.get("research.polymarket.min_volume", 10000)),
                                       auto_ticker_search=bool(c.get("research.polymarket.auto_ticker_search", True)),
                                       exclude=c.get("research.polymarket.exclude", []) or [],
                                       macro=bool(c.get("research.polymarket.show_macro", True)))
                try:
                    self._odds = ad.fetch(watch)
                    self.store.set_meta("polymarket:odds", [asdict(o) for o in self._odds])
                    extra = L(f"，{len(ad.errors)} 个关键词失败", f", {len(ad.errors)} keyword(s) failed") if ad.errors else ""
                    self._status["polymarket"] = ("ok", L(f"Polymarket：正常（{len(self._odds)} 个市场{extra}）", f"Polymarket: OK ({len(self._odds)} markets{extra})"))
                except AdapterError as e:
                    self._status["polymarket"] = ("off", L(f"Polymarket：{e}（显示缓存内容）", f"Polymarket: {e} (showing cached content)"))
                except Exception as e:
                    self._status["polymarket"] = ("error", L(f"Polymarket：内部错误 {type(e).__name__}", f"Polymarket: internal error {type(e).__name__}"))
                    log.exception("polymarket failed")
                self.store.set_meta("polymarket:at", time.time())
        self.store.set_meta("research:symbols", key)

    # ------------------------------------------------------------------ unusual-activity scan
    def flow_due(self) -> bool:
        if not self.cfg.get("flow.enabled", True):
            return False
        age = self.store.meta_age("flow:at")
        return age is None or age > float(self.cfg.get("flow.scan_minutes", 10)) * 60

    def _option_chains(self, symbols: list[str], expiries: int) -> dict:
        import yfinance as yf
        out = {}
        for sym in symbols:
            try:
                tk = yf.Ticker(sym)
                chain = {}
                for exp in list(tk.options or [])[:expiries]:
                    oc = tk.option_chain(exp)
                    chain[exp] = (oc.calls, oc.puts)
                if chain:
                    out[sym] = chain
            except Exception as e:                       # no listed options, or Yahoo said no
                log.info("option chain unavailable for %s: %s", sym, type(e).__name__)
        return out

    def scan_flow(self, force: bool = False) -> flow.FlowReport:
        """Large Polymarket bets, unusual option volume and futures moves (read-only, public data)."""
        s = flow.settings(self.cfg.get("flow", {}) or {})
        watch = self.aliases()
        now = time.time()
        rep = flow.FlowReport(ts=now)
        parts = self._flow_parts
        if s["polymarket"].get("enabled", True):
            try:
                trades = flow.fetch_trades(min_usd=float(s["polymarket"]["min_usd"]))
                cut = now - float(s["polymarket"]["recent_minutes"]) * 60
                slugs = list(dict.fromkeys(str(t.get("event_slug")) for t in trades
                                           if float(t.get("timestamp") or 0) >= cut and t.get("event_slug")))
                events = flow.fetch_events(slugs) if slugs else {}
                parts["polymarket"] = flow.scan_polymarket(trades, events, watch, s["polymarket"], now)
                self._status["flow"] = ("ok", L(f"异动扫描：正常（每 {int(self.cfg.get('flow.scan_minutes', 10))} 分钟，"
                                                f"本次看了 {len(trades)} 笔 Polymarket 大单）",
                                                f"Unusual-activity scan: OK (every {int(self.cfg.get('flow.scan_minutes', 10))} minutes, "
                                                f"looked at {len(trades)} large Polymarket orders this time)"))
            except AdapterError as e:
                rep.errors.append(L(f"Polymarket：{e}", f"Polymarket: {e}"))
                self._status["flow"] = ("off", L(f"异动扫描：Polymarket {e}（显示上次结果）", f"Unusual-activity scan: Polymarket {e} (showing the last result)"))
            except Exception as e:
                rep.errors.append(L(f"Polymarket：内部错误 {type(e).__name__}", f"Polymarket: internal error {type(e).__name__}"))
                log.exception("polymarket flow scan failed")
        else:
            parts.pop("polymarket", None)
        if s["futures"].get("enabled", True):
            try:
                bars = self.market.history(list(s["futures"]["symbols"]), "6mo")
                parts["futures"] = flow.scan_futures(bars, s["futures"], now)
            except Exception as e:
                rep.errors.append(L(f"期货：{type(e).__name__}", f"Futures: {type(e).__name__}"))
                log.exception("futures scan failed")
        else:
            parts.pop("futures", None)
        if s["options"].get("enabled", True):
            age = self.store.meta_age("flow:options_at")
            if "options" not in parts and self._flow.ts and age is not None:      # new process: reuse the last result
                parts["options"] = (self._flow.by("options"),
                                    [n.partition("|")[2] for n in self._flow.notes if n.startswith("options|")])
            if force or "options" not in parts or age is None or age > float(s["options"]["interval_minutes"]) * 60:
                try:
                    syms = [x for x in self.watchlist.symbols() if x.isalpha()]
                    syms += [x for x in s["options"]["extra_symbols"] if x not in syms]
                    chains = self._option_chains(syms, int(s["options"]["expiries"]))
                    parts["options"] = flow.scan_options(chains, s["options"], set(self.watchlist.symbols()), now)
                    self.store.set_meta("flow:options_at", now)
                except Exception as e:
                    rep.errors.append(L(f"期权：{type(e).__name__}", f"Options: {type(e).__name__}"))
                    log.exception("options scan failed")
        else:
            parts.pop("options", None)
        for name in ("polymarket", "options", "futures"):
            items, notes = parts.get(name, ([], []))
            rep.items += items
            rep.notes += [f"{name}|{n}" for n in notes]
        self._flow = rep
        self.store.set_meta("flow:report", rep.to_dict())
        self.store.set_meta("flow:at", now)
        return rep

    def _flow_alerts(self) -> list[Alert]:
        if not self.cfg.get("flow.enabled", True) or not self._flow.ts:
            return []
        out = []
        for i in flow.alert_items(self._flow, self.cfg.get("flow", {}) or {}):
            title, body = flow.alert_text(i)
            out.append(Alert(f"flow:{i.key}", "flow", i.tickers[0] if i.tickers else "", i.severity, title, body,
                             {"source": i.source, "score": i.score, "amount": i.amount}))
        return out

    # ------------------------------------------------------------------ main refresh
    def refresh(self, force: bool = False) -> Snapshot:
        self.watchlist.load()
        self.sync_brokers(force)
        positions = self.watchlist.merge_positions(self._broker_positions())
        symbols = self.watchlist.symbols()
        bench = self.cfg.get("app.benchmark", "SPY")
        fx = {self._fx_symbol(c) for c in self._currencies(positions).values() if c != "USD"}
        for ref in self._references().values():
            fx.add(ref["symbol"])
            if ref["currency"] != "USD":
                fx.add(self._fx_symbol(ref["currency"]))
        try:                               # a plan may be written in the prices of another stock
            fx |= {str(p["follow"]).upper() for p in plans.load().values() if p.get("follow")}
        except Exception:
            log.exception("reading plans failed")
        self._load_market(sorted(set(symbols) | {bench} | fx), force)
        snap = self.build_snapshot(positions)
        return snap

    def build_snapshot(self, positions: dict[str, Position] | None = None) -> Snapshot:
        if positions is None:
            positions = self.watchlist.merge_positions(self._broker_positions())
        cfg = self.cfg
        bench_sym = cfg.get("app.benchmark", "SPY")
        bench = self._daily.get(bench_sym)
        bench_close = bench["close"] if bench is not None and len(bench) else None
        q = dict(cfg.get("quant", {}) or {})
        snap = Snapshot(ts=time.time(), sources=dict(self._status), market_msg=self._market_msg,
                        market_ok=not self._market_msg)

        views: list[TickerView] = []
        prices: dict[str, float] = {}
        currencies = self._currencies(positions)
        no_fx: list[str] = []
        for item in self.watchlist.items:
            sym = item.symbol
            daily = self._daily.get(sym)
            price, prev, spark = self._session_stats(self._intraday.get(sym), daily)
            pos = positions.get(sym)
            if math.isnan(price) and pos and pos.market_price:
                price = pos.market_price
            v = TickerView(symbol=sym, price=price, prev_close=prev, spark=spark, note=item.note, auto=item.auto,
                           bars=daily, plan={"stop": item.stop, "target": item.target, "max_pct": item.max_pct})
            v.currency = currencies.get(sym, "USD")
            rate = self._fx_rate(v.currency)
            if rate is None:
                no_fx.append(sym)
            v.fx = rate or 1.0
            if not math.isnan(price):
                prices[sym] = price
                if not math.isnan(prev) and prev:
                    v.day_chg, v.day_pct = price - prev, (price / prev - 1) * 100
            if pos and pos.qty:
                v.qty, v.avg_cost, v.source = pos.qty, pos.avg_cost, pos.source
                if not math.isnan(price):
                    v.value = pos.qty * price * v.fx
                    v.upl = (price - pos.avg_cost) * pos.qty * v.fx
                    v.upl_pct = (price / pos.avg_cost - 1) * 100 if pos.avg_cost else 0.0
                    v.day_pnl = v.day_chg * pos.qty * v.fx
            if daily is not None and len(daily):
                tail = daily["close"].tail(252)
                v.low52, v.high52 = float(tail.min()), float(tail.max())
            views.append(v)

        snap.total_value = sum(v.value for v in views)
        snap.total_cost = sum(v.qty * v.avg_cost * v.fx for v in views)
        snap.total_upl = sum(v.upl for v in views)
        snap.day_pnl = sum(v.day_pnl for v in views)
        base = snap.total_value - snap.day_pnl
        snap.day_pct = snap.day_pnl / base * 100 if base > 0 else 0.0
        netliq = [b.net_liquidation for b in self._broker.values() if b.net_liquidation]
        cash = [b.cash for b in self._broker.values() if b.cash is not None]
        snap.cash = sum(cash) if cash else None
        manual_equity = float(cfg.get("app.account_equity", 0) or 0)
        snap.equity = max(sum(netliq), snap.total_value) if netliq else (manual_equity or snap.total_value)
        snap.equity_known = bool(netliq or manual_equity)

        for v in views:
            v.weight = v.value / snap.equity if snap.equity else 0.0
            if v.bars is not None and len(v.bars) >= 60:
                try:
                    v.analysis = analyze(v.symbol, v.bars, bench_close, qty=v.qty, avg_cost=v.avg_cost,
                                         equity=snap.equity / v.fx, position_value=v.value / v.fx, cfg=q,
                                         benchmark=bench_sym, price=None if math.isnan(v.price) else v.price)
                except Exception:
                    log.exception("analysis failed for %s", v.symbol)
            lv = v.analysis.levels if v.analysis else None
            user_stop, user_target = v.plan.get("stop"), v.plan.get("target")
            v.stop = user_stop or (lv.stop if lv else None)
            v.target = user_target or (lv.target if lv else None)
            v.levels_src = "user" if (user_stop or user_target) else ("auto" if lv else "")
            if v.qty and v.stop and v.target and v.target > v.stop and not math.isnan(v.price):
                v.progress = (v.price - v.stop) / (v.target - v.stop)
            v.news = [n for n in self._news if v.symbol in n.tickers][:6]
            v.odds = [o for o in self._odds if v.symbol in o.tickers]
        snap.tickers = views

        enabled_brokers = [n for n in self._status if n.startswith(("ibkr", "robinhood"))]
        live = [n for n in enabled_brokers if self._status[n][0] == "ok"]
        snap.mode = ("IBKR" if any(n.startswith("ibkr") for n in live) else "") + \
                    ("+RH" if any(n.startswith("robinhood") for n in live) else "")
        snap.mode = snap.mode.strip("+") or (L("券商离线", "Broker offline") if enabled_brokers else L("演示", "Demo"))
        snap.sample_holdings = self.watchlist.has_sample_holdings()

        snap.conc = concentration({v.symbol: v.value for v in views if v.value > 0},
                                  q.get("max_position_pct", 25.0), q.get("top3_warn_pct", 60.0))
        try:
            closes = pd.DataFrame({v.symbol: v.bars["close"] for v in views
                                   if v.bars is not None and len(v.bars) >= 120})
            snap.opt = optimizer.optimize(closes, float(q.get("optimizer_max_weight", 0.35)),
                                          int(q.get("optimizer_lookback_days", 504))) if closes.shape[1] >= 2 else None
        except Exception:
            log.exception("optimizer failed")

        snap.news = list(self._news)
        snap.odds = for_watchlist(self._odds, self.watchlist.symbols(),
                                  bool(cfg.get("research.polymarket.show_macro", True)))
        self._extra_alerts = self._bottoming(snap) + self._plan_alerts(snap) + self._flow_alerts()
        snap.flow = self._flow if self.cfg.get("flow.enabled", True) else None
        self._behaviour(snap, positions, prices)
        self._email(snap)
        for v in views:
            ref = self._references().get(v.symbol)
            rb = self._daily.get(ref["symbol"]) if ref else None
            if ref is None or rb is None or v.bars is None:
                continue
            try:
                ra = analyze(ref["symbol"], rb, None, cfg=q) if len(rb) >= 60 else None
                st = compare(v.symbol, v.bars, ref["symbol"], rb, self._fx_series(v.currency),
                             self._fx_series(ref["currency"]), ratio=ref["ratio"],
                             price=None if math.isnan(v.price) else v.price, name=ref["name"],
                             currency=v.currency, ref_currency=ref["currency"], analysis=ra)
                if st is not None:
                    snap.refs[v.symbol] = st
            except Exception:
                log.exception("reference comparison failed for %s", v.symbol)
        try:
            snap.briefing = briefing.build(snap, self.store, dict(cfg.get("briefing", {}) or {}),
                                           float(q.get("max_position_pct", 25.0)),
                                           float(q.get("rebalance_min_trade_pct", 1.0)))
            if no_fx:
                snap.briefing.notes.append(
                    L(f"{'、'.join(no_fx)} 没有取到汇率，市值暂按 1:1 计入美元，占比不准。",
                      f"No exchange rate for {', '.join(no_fx)}; market value is counted 1:1 in USD for now, "
                      "so the weights are not exact."))
        except Exception:
            log.exception("briefing failed")
        self.snapshot = snap
        return snap

    def _bottoming(self, snap: Snapshot) -> list[Alert]:
        """Rule-based bottoming check for the watched symbols; an alert once per base."""
        c = dict(self.cfg.get("alerts.bottoming", {}) or {})
        if not c.get("enabled", True):
            return []
        refs = self._references()
        names = {r["symbol"]: r["name"] for r in refs.values()}
        symbols = [str(s) for s in (c.get("symbols") or [])] or \
            [s for pair in ((k, r["symbol"]) for k, r in refs.items()) for s in pair]
        out: list[Alert] = []
        for sym in dict.fromkeys(symbols):          # first all states, so an alert can mention the peers
            bars = self._daily.get(sym)
            if bars is None or not len(bars):
                continue
            try:
                st = bottom_state(bars, c)
            except Exception:
                log.exception("bottoming check failed for %s", sym)
                continue
            if st.ok:
                snap.bottoms[sym] = st
        for sym, st in snap.bottoms.items():
            if st.triggered:
                label = L(f"{sym}（{names[sym]}）", f"{sym} ({names[sym]})") if sym in names else sym
                lines = [L(f"{label} 出现有成交量确认的筑底信号：价格条件 {st.score}/6 成立（门槛 {st.min_score}），"
                           f"成交量条件 {st.vol_score}/3 成立（门槛 {st.min_vol_score}）。",
                           f"{label}: bottoming signal with volume confirmation: price conditions {st.score}/6 met "
                           f"(threshold {st.min_score}), volume conditions {st.vol_score}/3 met (threshold {st.min_vol_score})."),
                         L(f"收盘 {st.close:,.2f}（{st.asof:%Y-%m-%d}），距 52 周高点 {st.drawdown_pct:.1f}%，"
                           f"这一轮的低点 {st.base_low:,.2f}（{st.base_low_date:%m-%d}）。",
                           f"Close {st.close:,.2f} ({st.asof:%Y-%m-%d}), {st.drawdown_pct:.1f}% below the 52-week high, "
                           f"low of this leg {st.base_low:,.2f} ({st.base_low_date:%m-%d}).")]
                lines += [L(f"{'✓' if ok else '✗'} {bottom_labels[k]}：{d}", f"{'✓' if ok else '✗'} {bottom_labels[k]}: {d}")
                          for k, ok, d in st.checks]
                peers = [L(f"{o}：{snap.bottoms[o].summary}", f"{o}: {snap.bottoms[o].summary}")
                         + (L("（也已达到门槛）", " (threshold also met)") if snap.bottoms[o].triggered else "")
                         for o in dict.fromkeys(symbols) if o != sym and o in snap.bottoms]
                if peers:
                    lines.append(L("同时盯着的其它股票 — " + "；".join(peers) + "。",
                                   "Other stocks being watched — " + "; ".join(peers) + "."))
                lines.append(L("这是按固定规则统计的形态信号，不是预测：历史上这类信号出现后既有继续上涨的，也有再创新低的。"
                               "如果收盘跌破上面这个低点，这次信号就作废。",
                               "This is a pattern signal counted by fixed rules, not a prediction: historically this kind of signal "
                               "has been followed both by further gains and by new lows. If the close falls below the low "
                               "above, this signal is void."))
                out.append(Alert(f"bottom:{sym}:{st.base_low_date:%Y%m%d}", "bottoming", sym, "warn",
                                 L(f"筑底信号：{label}", f"Bottoming signal: {label}"), "\n".join(lines),
                                 {"score": st.score, "vol_score": st.vol_score, "base_low": st.base_low}))
        return out

    def _follow_info(self, snap: Snapshot, sym: str, v: TickerView, plan: dict) -> dict:
        """Price, name and currency of the stock a plan's levels are written in (plan field ``follow``)."""
        follow = str(plan.get("follow") or "").upper()
        if not follow:
            return {}
        fv = snap.get(follow)
        px = fv.price if fv is not None and not math.isnan(fv.price) else None
        if px is None:
            fb = self._daily.get(follow)
            px = float(fb["close"].iloc[-1]) if fb is not None and len(fb) else None
        ref = self._references().get(sym)
        paired = ref is not None and ref["symbol"].upper() == follow
        fccy = ref["currency"] if paired else self._currencies({}).get(follow, fv.currency if fv is not None else "USD")
        r_f, r_l = self._fx_rate(fccy), self._fx_rate(v.currency)
        to_local = (ref["ratio"] if paired else 1.0) * r_f / r_l if r_f and r_l else None
        return {"follow_price": px, "follow_name": ref["name"] if paired else follow, "follow_ccy": fccy,
                "to_local": to_local}

    def _plan_alerts(self, snap: Snapshot) -> list[Alert]:
        """Status of the user's own plans; an alert when the price reaches a level they wrote down."""
        out: list[Alert] = []
        try:
            trades = self.store.load_trades(include_demo=False)
            for sym, plan in plans.load().items():
                v = snap.get(sym)
                if v is None:
                    continue
                st = plans.status(sym, plan, price=v.price, weight_pct=v.weight * 100, qty=v.qty, trades=trades,
                                  ccy=v.currency, **self._follow_info(snap, sym, v, plan))
                snap.plans.append(st)
                for key, level, title, text in st.events:
                    out.append(Alert(f"plan:{sym}:{key}:{level:.10g}", "plan_level", sym,
                                     "high" if key == "exit" else "warn", title,
                                     text + L("\n价位是你自己在计划里写的；程序只负责提醒，不会下单。",
                                              "\nThe levels are ones you wrote in your plan; the program only reminds you and never places orders."),
                                     {"key": key, "level": level}))
        except Exception:
            log.exception("plans failed")
        return out

    def mail_role_ok(self) -> bool:
        """Only one machine sends mail: the desktop ("local") or the always-on runner ("remote")."""
        want = str(self.cfg.get("notify.email.send_from", "local") or "local")
        return want == os.environ.get("STOCKDESK_ROLE", "local")

    def _email(self, snap: Snapshot) -> None:
        """Mail new alerts of the kinds the user opted into (each alert at most once)."""
        s = notify.settings(self.cfg.get("notify.email", {}))
        if not s["enabled"]:
            self._status.pop("email", None)
            return
        if not self.mail_role_ok():
            if os.environ.get("STOCKDESK_ROLE", "local") == "remote":
                msg = L("邮件提醒：目前由桌面电脑发送（在桌面电脑上运行 scripts/push_secrets.py 后改由本机发）",
                        "E-mail alerts: currently sent by the desktop computer (run scripts/push_secrets.py on the desktop to send from this machine instead)")
            else:
                other = self.cfg.get('remote.host', '') or L('另一台机器', 'another machine')
                msg = L(f"邮件提醒：由 {other} 发送（这台电脑不发，避免重复）",
                        f"E-mail alerts: sent by {other} (not by this computer, to avoid duplicates)")
            self._status["email"] = ("ok", msg)
            snap.sources = dict(self._status)
            return
        sent = set(self.store.get_meta("email:sent", []) or [])
        todo = [a for a in snap.alerts if a.kind in s["kinds"] and a.id not in sent and not a.demo]
        fs = flow.settings(self.cfg.get("flow", {}) or {})          # scan hits: only the strongest, a few per day
        day = time.strftime("%Y%m%d")
        quota = self.store.get_meta("email:flow_quota", {}) or {}
        left = max(0, int(fs["max_emails_per_day"]) - int(quota.get(day, 0)))
        ranks = {"info": 0, "warn": 1, "high": 2}
        flows = [a for a in todo if a.kind == "flow" and ranks.get(a.severity, 0) >= ranks.get(fs["email_min_severity"], 2)][:left]
        todo = [a for a in todo if a.kind != "flow"] + flows
        if not todo:
            self._status.setdefault("email", ("ok", L(f"邮件提醒：已开启（发往 {s['to']}）", f"E-mail alerts: on (sending to {s['to']})")))
            return
        try:
            notify.send(s, *notify.alert_mail(todo))
            self.store.set_meta("email:sent", sorted(sent | {a.id for a in todo})[-500:])
            if flows:
                self.store.set_meta("email:flow_quota", {day: int(quota.get(day, 0)) + len(flows)})
            self._status["email"] = ("ok", L(f"邮件提醒：已发送 {len(todo)} 条到 {s['to']}", f"E-mail alerts: sent {len(todo)} to {s['to']}"))
        except notify.NotifyError as e:
            self._status["email"] = ("error", L(f"邮件提醒：{e}", f"E-mail alerts: {e}"))
            log.warning("email notification failed: %s", e)
        snap.sources = dict(self._status)

    def _behaviour(self, snap: Snapshot, positions: dict[str, Position], prices: dict[str, float]) -> None:
        b = dict(self.cfg.get("behavior", {}) or {})
        if not b.get("enabled", True):
            return
        trades = self.store.load_trades()
        real = [t for t in trades if not t.demo]
        if not real and b.get("demo_sample_trades", True) and snap.mode == L("演示", "Demo"):
            if not trades and self._daily:
                demo = make_demo_trades(self._daily, self.watchlist.symbols(), int(b.get("runup_days", 5)))
                self.store.upsert_trades(demo)
                trades = self.store.load_trades()
        elif real and len(real) != len(trades):
            self.store.delete_demo_trades()
            trades = real
        snap.n_trades, snap.demo_trades = len(trades), bool(trades) and all(t.demo for t in trades)
        snap.trades = trades
        th = {**b, "max_position_pct": self.cfg.get("quant.max_position_pct", 25.0),
              "risk_per_trade_pct": self.cfg.get("quant.risk_per_trade_pct", 1.0),
              "atr_stop_mult": self.cfg.get("quant.atr_stop_mult", 2.0)}
        plans = {v.symbol: v.plan for v in snap.tickers}
        try:
            found = detectors.detect(trades, self._daily, positions, prices, plans, self.store.load_wl_events(),
                                     th, snap.equity, fx={v.symbol: v.fx for v in snap.tickers})
        except Exception:
            log.exception("behaviour detection failed")
            found = []
        found = list(found) + list(getattr(self, "_extra_alerts", []))
        self.store.add_alerts(found)
        # conditions that no longer hold (e.g. oversize fixed) disappear from the panel
        live_ids = {a.id for a in found}
        visible = [a for a in self.store.load_alerts()
                   if a.id in live_ids or a.kind in ("chase", "panic", "revenge", "avg_down", "fomo_watch", "bottoming")]
        snap.alerts = visible
        snap.new_alerts = [a for a in visible if not a.notified]
        self.store.mark_notified([a.id for a in snap.new_alerts])

    # ------------------------------------------------------------------ user actions
    def add_symbol(self, raw: str) -> str | None:
        item = self.watchlist.add(raw)
        if not item:
            return None
        self.store.add_wl_event(item.symbol, "add")
        return item.symbol

    def remove_symbol(self, symbol: str) -> None:
        if self.watchlist.remove(symbol):
            self.store.add_wl_event(symbol, "remove")

    def set_plan(self, symbol: str, stop: float | None, target: float | None, max_pct: float | None) -> None:
        item = self.watchlist.get(symbol)
        if item and item.auto:           # make an auto-added broker holding a real watchlist entry
            item.auto = False
        self.watchlist.update(symbol, stop=stop, target=target, max_pct=max_pct)

    def pretrade(self, symbol: str, side: str, qty: float, price: float | None) -> tuple[list[detectors.Check], float]:
        bars = self._daily.get(symbol)
        if bars is None:
            try:
                bars = self.market.history([symbol], self.cfg.get("app.history_period", "5y")).get(symbol)
                if bars is not None:
                    self._daily[symbol] = bars
            except AdapterError:
                bars = None
        snap = self.snapshot
        prices = {v.symbol: v.price for v in snap.tickers if not math.isnan(v.price)}
        px = price or prices.get(symbol) or (float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0)
        positions = {v.symbol: Position(v.symbol, v.qty, v.avg_cost, v.source) for v in snap.tickers if v.qty}
        item = self.watchlist.get(symbol)
        plan = {"stop": item.stop, "target": item.target, "max_pct": item.max_pct} if item else {}
        b = dict(self.cfg.get("behavior", {}) or {})
        th = {**b, "max_position_pct": self.cfg.get("quant.max_position_pct", 25.0),
              "risk_per_trade_pct": self.cfg.get("quant.risk_per_trade_pct", 1.0),
              "atr_stop_mult": self.cfg.get("quant.atr_stop_mult", 2.0)}
        checks = detectors.pretrade_check(symbol, side, qty, px, bars, self.store.load_trades(), positions,
                                          prices, plan, th, snap.equity if snap.equity_known else 0.0)
        return checks, px
