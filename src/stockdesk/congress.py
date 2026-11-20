"""Trades disclosed by members of Congress: what followed each one, who has been right, and the write-up
of a new disclosure.

Every disclosed purchase or sale of a stock is a "call". A call is judged by the stock's return over a
fixed number of sessions after the TRANSACTION date, minus the S&P 500 ETF's over the same sessions: a
purchase was right when the stock beat the market, a sale when it lagged it. (Bought call options count
as a purchase, bought put options as a sale; other option trades and exchanges are listed but not
judged.) Several filings of the same member, stock, direction and day count once.

A member's record is the share of right calls with its 95 % Wilson interval and the average signed
excess return, over a ROLLING window: only calls traded in the last ``window_days`` count, so old
calls drop out and the record moves with every scan. Tiers: "star" needs at least ``min_calls``
judged calls, an interval entirely above 50 % and a positive average; "good" needs ``watch_calls``
calls, 60 % right and a positive average; "poor" is the mirror of "star".

Records change, so the recent form is checked as well: the calls of the last ``recent_days`` days,
judged at the short horizon so that they count sooner. An accurate member whose recent form has
turned (at least ``recent_min`` calls, under half right, negative average) is moved down one tier;
a member whose recent calls alone would earn "star" is moved up to "good". Every change of tier is
recorded with its date.

Only members rated "good" or better are referred to: they alone raise alerts, and only their trades
carry weight (3 for "star", 2 for "good", 0 for everyone else) when the buyers and sellers of one
stock are added up. ``alert_scope: accurate_or_watchlist`` also alerts on any trade in a watched stock.

What this cannot tell: reports come up to 45 days after the trade, give amount ranges only, and many
are made by a spouse or an adviser. Among several hundred members some will look skilful by chance,
so a good record here is a reason to look, never a forecast. The same calls are therefore also judged
from the day after the DISCLOSURE, which is all a reader of the reports could have acted on.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .i18n import L
from .quant.backtest import wilson_interval

DEFAULTS: dict = {"enabled": True, "scan_minutes": 60, "years_back": 2, "docs_per_scan": 40, "pause_seconds": 0.3,
                  "asset_types": ["ST", "OP"], "horizon_days": 63, "short_days": 21, "min_calls": 10, "watch_calls": 5,
                  "alert_days": 7, "consensus_days": 90, "price_batch": 60, "price_batches": 4, "price_period": "3y",
                  "price_ttl_hours": 20, "email_min_severity": "warn", "max_emails_per_day": 6, "max_recent": 120,
                  "window_days": 730, "recent_days": 180, "recent_min": 5, "alert_scope": "accurate"}

TIER = {"star": L("★ 准", "★ accurate"), "good": L("偏准", "fairly accurate"), "plain": L("一般", "ordinary"),
        "thin": L("样本少", "few calls"), "poor": L("偏差", "mostly wrong")}
WEIGHT = {"star": 3.0, "good": 2.0, "plain": 0.0, "thin": 0.0, "poor": 0.0}      # only accurate members are referred to
TREND = {"down": L("近期转差", "recent form worse"), "up": L("近期转好", "recent form better"), "": ""}
OWNER = {"self": L("本人", "self"), "spouse": L("配偶", "spouse"), "joint": L("共同", "joint"), "child": L("子女", "dependent child")}
CHAMBER = {"house": L("众议院", "House"), "senate": L("参议院", "Senate")}


def settings(cfg: dict | None) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    out.update({k: v for k, v in (cfg or {}).items() if v is not None and not isinstance(v, dict)})
    return out


def ysym(ticker: str) -> str:
    """The ticker as Yahoo writes it (BRK.B -> BRK-B)."""
    return ticker.replace(".", "-").replace("/", "-").upper()


def money(lo: float, hi: float) -> str:
    def f(x):
        return f"${x / 1e6:g}M" if x >= 1e6 else f"${x:,.0f}"
    if not lo and not hi:
        return "—"
    if not hi:
        return L(f"{f(lo)} 以上", f"over {f(lo)}")
    return f(lo) if lo == hi else f"{f(lo)}–{f(hi)}"


@dataclass
class Call:
    member: str
    state: str
    chamber: str
    ticker: str
    side: str                        # "buy" | "sell"
    traded: str
    filed: str
    lo: float = 0.0
    hi: float = 0.0
    owner: str = "self"
    partial: bool = False
    asset_type: str = "ST"
    asset: str = ""
    doc: str = ""
    url: str = ""
    px: float | None = None          # close on the transaction day (or the next session)
    px_now: float | None = None
    ret_now: float | None = None     # the stock since the transaction
    ex_now: float | None = None      # signed excess over the benchmark since the transaction (+ = the call was right)
    ex: dict = field(default_factory=dict)       # horizon -> signed excess from the transaction date
    exf: dict = field(default_factory=dict)      # horizon -> signed excess from the session after the disclosure
    exf_now: float | None = None

    @property
    def who(self) -> str:
        return f"{self.chamber}:{self.member}"

    @property
    def delay(self) -> int:
        try:
            return (date.fromisoformat(self.filed) - date.fromisoformat(self.traded)).days
        except ValueError:
            return 0


@dataclass
class MemberScore:
    key: str
    member: str
    state: str
    chamber: str
    trades: int = 0                  # every disclosed stock call
    n: int = 0                       # judged at the main horizon
    hits: int = 0
    hit_rate: float | None = None
    ci: tuple | None = None
    mean: float | None = None        # average signed excess at the main horizon
    buys: tuple = (0, None)          # (n, average signed excess)
    sells: tuple = (0, None)
    short: tuple = (0, None, None)   # (n, share right, average) at the short horizon
    follow: tuple = (0, None, None)  # the same calls judged from the session after the disclosure
    recent: tuple = (0, None, None)  # (n, share right, average): calls of the last ``recent_days``, short horizon
    trend: str = ""                  # "down" | "up" | "": what the recent form says against the whole window
    base: str = "thin"               # the tier by the whole window, before the recent form is applied
    tier: str = "thin"
    weight: float = 0.0
    best: Call | None = None
    worst: Call | None = None
    last_filed: str = ""


@dataclass
class FilingNote:
    """One recent report and what is notable about it."""
    doc: str
    member: str
    state: str
    chamber: str
    filed: str
    url: str
    calls: list[Call] = field(default_factory=list)
    others: int = 0                  # rows that are not stock calls (bonds, funds, exchanges ...)
    watch: list[str] = field(default_factory=list)   # tickers of the report that are on the watchlist


@dataclass
class CongressReport:
    ts: float = 0.0
    horizon: int = 63
    short: int = 21
    since: str = ""
    docs: int = 0
    scans: int = 0                   # scanned paper reports that could not be read
    backlog: int = 0
    priced: int = 0                  # tickers with price history
    tickers: int = 0
    members: list[MemberScore] = field(default_factory=list)       # best record first
    recent: list[Call] = field(default_factory=list)               # newest disclosures first
    filings: list[FilingNote] = field(default_factory=list)        # reports of the last ``alert_days`` days
    calls: list[Call] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    senate: bool = False             # the Senate's reports are read too (the user accepted that site's terms)
    scope: str = "accurate"          # who raises alerts: "accurate" | "accurate_or_watchlist"
    window_days: int = 730
    recent_days: int = 180
    changes: list = field(default_factory=list)   # [{member, chamber, old, new, at}], newest first

    def score(self, key: str) -> MemberScore | None:
        return next((m for m in self.members if m.key == key), None)

    def consensus(self, ticker: str, days: int = 90, today: date | None = None) -> dict:
        """Who bought and who sold ``ticker`` in the last ``days`` days. Everyone is listed; only members rated
        accurate carry weight, so ``net`` is the accurate buyers' weights minus the accurate sellers'."""
        cut = ((today or date.today()) - timedelta(days=days)).isoformat()
        buy: dict[str, float] = {}
        sell: dict[str, float] = {}
        for c in self.calls:
            if c.ticker == ticker and c.traded >= cut:
                m = self.score(c.who)
                (buy if c.side == "buy" else sell)[c.member] = m.weight if m else 0.0
        return {"buyers": buy, "sellers": sell, "net": sum(buy.values()) - sum(sell.values()),
                "acc_buy": [n for n, w in buy.items() if w > 0], "acc_sell": [n for n, w in sell.items() if w > 0]}


def build_calls(trades: list[dict], bars: dict[str, pd.DataFrame], bench: pd.DataFrame | None,
                horizons: tuple[int, ...] = (21, 63), kinds: tuple[str, ...] = ("ST", "OP")) -> tuple[list[Call], int]:
    """(judged-able calls, number of other rows). ``bars`` is keyed by the Yahoo spelling of the ticker."""
    bclose = bench["close"].astype(float) if bench is not None and len(bench) else None
    seen: dict[tuple, Call] = {}
    other = 0
    for t in trades:
        if t.get("side") not in ("buy", "sell") or not t.get("ticker") or t.get("asset_type") not in kinds:
            other += 1
            continue
        key = (t["chamber"], t["member"], t["ticker"], t["side"], t["traded"])
        if key in seen:
            c = seen[key]
            c.hi, c.lo = max(c.hi, float(t.get("hi") or 0)), max(c.lo, float(t.get("lo") or 0))
            if t["filed"] < c.filed:
                c.filed, c.doc, c.url = t["filed"], f"{t['chamber']}:{t['doc_id']}", t.get("url", "")
            continue
        seen[key] = Call(t["member"], t.get("state", ""), t["chamber"], t["ticker"], t["side"], t["traded"], t["filed"],
                         float(t.get("lo") or 0), float(t.get("hi") or 0), t.get("owner", "self"), bool(t.get("partial")),
                         t.get("asset_type", ""), t.get("asset", ""), f"{t['chamber']}:{t['doc_id']}", t.get("url", ""))
    out = list(seen.values())
    for c in out:
        df = bars.get(ysym(c.ticker))
        if df is None or len(df) < 5 or bclose is None:
            continue
        close = df["close"].astype(float)
        sign = 1.0 if c.side == "buy" else -1.0

        def excess(i0: int, i1: int) -> float | None:
            b0, b1 = bclose.asof(close.index[i0]), bclose.asof(close.index[i1])
            if not (b0 == b0 and b1 == b1 and b0 and close.iloc[i0]):
                return None
            return sign * float(close.iloc[i1] / close.iloc[i0] - b1 / b0)

        i0 = int(close.index.searchsorted(pd.Timestamp(c.traded)))
        if i0 >= len(close) or (close.index[i0] - pd.Timestamp(c.traded)).days > 7:
            continue                                   # no price near the transaction date (not listed yet, or stale)
        last = len(close) - 1
        c.px, c.px_now = float(close.iloc[i0]), float(close.iloc[-1])
        c.ret_now = c.px_now / c.px - 1 if c.px else None
        c.ex_now = excess(i0, last) if last > i0 else None
        i1 = int(close.index.searchsorted(pd.Timestamp(c.filed), side="right"))     # the session after the disclosure
        for h in horizons:
            if i0 + h <= last:
                e = excess(i0, i0 + h)
                if e is not None:
                    c.ex[h] = e
            if i1 + h <= last:
                e = excess(i1, i1 + h)
                if e is not None:
                    c.exf[h] = e
        if i1 < last:
            c.exf_now = excess(i1, last)
    return out, other


def score_members(calls: list[Call], cfg: dict | None = None, today: date | None = None) -> list[MemberScore]:
    p = settings(cfg)
    h, hs = int(p["horizon_days"]), int(p["short_days"])
    day = today or date.today()
    cut = (day - timedelta(days=int(p["window_days"]))).isoformat()         # the rolling window of the record
    fresh = (day - timedelta(days=int(p["recent_days"]))).isoformat()       # the recent form
    by: dict[str, list[Call]] = {}
    for c in calls:
        if c.traded >= cut:
            by.setdefault(c.who, []).append(c)
    out = []
    for key, rows in by.items():
        m = MemberScore(key, rows[0].member, rows[0].state, rows[0].chamber, trades=len(rows),
                        last_filed=max(c.filed for c in rows))
        done = [c for c in rows if h in c.ex]
        if done:
            ex = np.array([c.ex[h] for c in done])
            m.n, m.hits = len(done), int((ex > 0).sum())
            m.hit_rate, m.ci, m.mean = m.hits / m.n, wilson_interval(m.hits, m.n), float(ex.mean())
            for side in ("buy", "sell"):
                part = [c.ex[h] for c in done if c.side == side]
                setattr(m, "buys" if side == "buy" else "sells", (len(part), float(np.mean(part)) if part else None))
            m.best, m.worst = max(done, key=lambda c: c.ex[h]), min(done, key=lambda c: c.ex[h])
        sh = [c.ex[hs] for c in rows if hs in c.ex]
        if sh:
            m.short = (len(sh), float(np.mean([x > 0 for x in sh])), float(np.mean(sh)))
        fo = [c.exf[h] for c in rows if h in c.exf]
        if fo:
            m.follow = (len(fo), float(np.mean([x > 0 for x in fo])), float(np.mean(fo)))
        if m.n < int(p["watch_calls"]):
            m.base = "thin"
        elif m.n >= int(p["min_calls"]) and m.ci and m.ci[0] > 0.5 and m.mean > 0:
            m.base = "star"
        elif m.hit_rate >= 0.6 and m.mean > 0:
            m.base = "good"
        elif m.n >= int(p["min_calls"]) and m.ci and m.ci[1] < 0.5 and m.mean < 0:
            m.base = "poor"
        else:
            m.base = "plain"
        m.tier = m.base
        rec = [c.ex[hs] for c in rows if c.traded >= fresh and hs in c.ex]
        if rec:
            hits = sum(1 for x in rec if x > 0)
            m.recent = (len(rec), hits / len(rec), float(np.mean(rec)))
            if len(rec) >= int(p["recent_min"]):
                ci = wilson_interval(hits, len(rec))
                if m.recent[1] < 0.5 and m.recent[2] < 0:
                    m.trend = "down"
                    m.tier = {"star": "good", "good": "plain"}.get(m.base, m.base)
                elif m.recent[1] >= 0.65 and m.recent[2] > 0:
                    m.trend = "up"
                    if m.base in ("plain", "thin") and len(rec) >= int(p["min_calls"]) and ci and ci[0] > 0.5:
                        m.tier = "good"
        m.weight = WEIGHT[m.tier]
        out.append(m)
    rank = {"star": 0, "good": 1, "plain": 2, "poor": 3, "thin": 4}
    out.sort(key=lambda m: (rank[m.tier], -(m.ci[0] if m.ci else 0), -(m.mean or 0)))
    return out


def build_report(trades: list[dict], bars: dict[str, pd.DataFrame], bench: pd.DataFrame | None, cfg: dict | None = None,
                 watch: set[str] | None = None, today: date | None = None, now: float = 0.0) -> CongressReport:
    p = settings(cfg)
    h, hs = int(p["horizon_days"]), int(p["short_days"])
    calls, _ = build_calls(trades, bars, bench, (hs, h), tuple(p["asset_types"]))
    rep = CongressReport(ts=now, horizon=h, short=hs, calls=calls, members=score_members(calls, p, today),
                         scope=str(p["alert_scope"]), window_days=int(p["window_days"]), recent_days=int(p["recent_days"]))
    rep.tickers = len({c.ticker for c in calls})
    rep.priced = len({c.ticker for c in calls if c.px is not None})
    rep.recent = sorted(calls, key=lambda c: (c.filed, c.traded), reverse=True)[: int(p["max_recent"])]
    cut = ((today or date.today()) - timedelta(days=int(p["alert_days"]))).isoformat()
    notes: dict[str, FilingNote] = {}
    for c in calls:
        if c.filed >= cut:
            n = notes.setdefault(c.doc, FilingNote(c.doc, c.member, c.state, c.chamber, c.filed, c.url))
            n.calls.append(c)
            if watch and c.ticker in watch and c.ticker not in n.watch:
                n.watch.append(c.ticker)
    rep.filings = sorted(notes.values(), key=lambda n: (n.filed, n.doc), reverse=True)
    return rep


# ---- the write-up of one new report -----------------------------------------------------------------

def pct(x: float | None) -> str:
    return "—" if x is None or x != x else f"{x * 100:+.1f}%"


def side_text(c: Call) -> str:
    if c.asset_type == "OP":
        return L("买入看涨期权", "bought calls") if c.side == "buy" else L("买入看跌期权", "bought puts")
    if c.side == "buy":
        return L("买入", "bought")
    return L("部分卖出", "sold part of") if c.partial else L("卖出", "sold")


def asset_name(c: Call) -> str:
    """The asset without the ticker in brackets that the report appends."""
    return re.sub(r"\s*\([A-Z0-9./\-]+\)\s*$", "", c.asset).strip()[:44]


def call_line(c: Call, horizon: int) -> str:
    """One disclosed trade with what the stock has done since."""
    d = f"{c.traded[5:]}"
    since = ""
    if c.px is not None and c.ret_now is not None:
        right = "" if c.ex_now is None else (L("，到目前这笔方向是对的", "; so far the call is right") if c.ex_now > 0
                                             else L("，到目前这笔方向是错的", "; so far the call is wrong"))
        rel = "" if c.ex_now is None else L(f"，相对标普 {pct(c.ex_now if c.side == 'buy' else -c.ex_now)}",
                                            f", {pct(c.ex_now if c.side == 'buy' else -c.ex_now)} versus the S&P")
        since = L(f"；交易日收盘 {c.px:,.2f} → 现在 {c.px_now:,.2f}（{pct(c.ret_now)}{rel}）{right}",
                  f"; close on the trade day {c.px:,.2f} → now {c.px_now:,.2f} ({pct(c.ret_now)}{rel}){right}")
    return L(f"· {side_text(c)} {c.ticker}（{asset_name(c)}）：交易日 {d}，{c.delay} 天后披露，金额 {money(c.lo, c.hi)}，"
             f"持有人：{OWNER.get(c.owner, c.owner)}{since}",
             f"· {side_text(c)} {c.ticker} ({asset_name(c)}): traded {d}, disclosed {c.delay} days later, amount {money(c.lo, c.hi)}, "
             f"owner: {OWNER.get(c.owner, c.owner)}{since}")


def record_lines(m: MemberScore | None, rep: CongressReport, ticker_calls: list[Call] | None = None) -> list[str]:
    h = rep.horizon
    if m is None or not m.trades:
        return [L("· 这位议员在已读取的申报里没有可评估的股票交易。", "· No judgeable stock trades by this member in the reports read so far.")]
    out = []
    if not m.n:
        out.append(L(f"· 评级：{TIER[m.tier]}。已披露 {m.trades} 笔股票交易，但还没有一笔满 {h} 个交易日，暂时无法评估。",
                     f"· Rating: {TIER[m.tier]}. {m.trades} stock trade(s) disclosed, but none is {h} sessions old yet, so nothing can be judged."))
    else:
        ci = f"{m.ci[0] * 100:.0f}%–{m.ci[1] * 100:.0f}%" if m.ci else "—"
        out.append(L(f"· 评级：{TIER[m.tier]}（加权系数 {m.weight:g}）。近 {rep.window_days} 天披露 {m.trades} 笔股票交易，其中 {m.n} 笔满 {h} 个交易日可以评估："
                     f"方向正确 {m.hits} 笔，占 {m.hit_rate * 100:.0f}%（95% 区间 {ci}），平均超额 {pct(m.mean)}",
                     f"· Rating: {TIER[m.tier]} (weight {m.weight:g}). {m.trades} stock trade(s) disclosed in the last {rep.window_days} days, {m.n} of them "
                     f"{h} sessions old and judgeable: {m.hits} right, {m.hit_rate * 100:.0f}% (95% interval {ci}), average excess {pct(m.mean)}"))
        if m.recent[0]:
            moved = ""
            if m.tier != m.base:
                moved = L(f" → {TREND[m.trend]}，评级由“{TIER[m.base]}”调为“{TIER[m.tier]}”", f" → {TREND[m.trend]}; rating moved from '{TIER[m.base]}' to '{TIER[m.tier]}'")
            elif m.trend:
                moved = L(f" → {TREND[m.trend]}", f" → {TREND[m.trend]}")
            out.append(L(f"· 近期表现（近 {rep.recent_days} 天的交易，按 {rep.short} 个交易日评估）：{m.recent[0]} 笔，方向正确 {m.recent[1] * 100:.0f}%，"
                         f"平均超额 {pct(m.recent[2])}{moved}",
                         f"· Recent form (trades of the last {rep.recent_days} days, judged at {rep.short} sessions): {m.recent[0]} call(s), "
                         f"{m.recent[1] * 100:.0f}% right, average excess {pct(m.recent[2])}{moved}"))
        parts = []
        if m.buys[0]:
            parts.append(L(f"买入 {m.buys[0]} 笔，平均超额 {pct(m.buys[1])}", f"{m.buys[0]} purchase(s), average excess {pct(m.buys[1])}"))
        if m.sells[0]:
            parts.append(L(f"卖出 {m.sells[0]} 笔，平均超额 {pct(m.sells[1])}", f"{m.sells[0]} sale(s), average excess {pct(m.sells[1])}"))
        if parts:
            out.append(L("· 分开看：", "· Split: ") + L("；", "; ").join(parts)
                       + L("（超额都带方向：正数 = 判断对了，即买入后跑赢标普、或卖出后股票跑输标普）",
                           " (every excess is signed: positive = the call was right, i.e. the stock beat the S&P after a purchase or lagged it after a sale)"))
        if m.follow[0]:
            out.append(L(f"· 如果等到披露之后的下一个交易日才跟：{m.follow[0]} 笔里方向正确的占 {m.follow[1] * 100:.0f}%，平均超额 {pct(m.follow[2])}"
                         "（这才是读申报的人能拿到的）",
                         f"· Had one followed on the session after the disclosure: {m.follow[1] * 100:.0f}% of {m.follow[0]} right, average excess "
                         f"{pct(m.follow[2])} (this is what a reader of the reports could have had)"))
        if m.best is not None and m.worst is not None and m.n >= 2:
            out.append(L(f"· 最好的一笔：{m.best.traded} {side_text(m.best)} {m.best.ticker}，{h} 日超额 {pct(m.best.ex[h])}；"
                         f"最差的一笔：{m.worst.traded} {side_text(m.worst)} {m.worst.ticker}，{pct(m.worst.ex[h])}",
                         f"· Best call: {m.best.traded} {side_text(m.best)} {m.best.ticker}, {h}-session excess {pct(m.best.ex[h])}; "
                         f"worst: {m.worst.traded} {side_text(m.worst)} {m.worst.ticker}, {pct(m.worst.ex[h])}"))
    for c in (ticker_calls or [])[:4]:
        res = L(f"{h} 日超额 {pct(c.ex[h])}", f"{h}-session excess {pct(c.ex[h])}") if h in c.ex else \
            L(f"至今 {pct(c.ex_now)}（未满 {h} 日）", f"{pct(c.ex_now)} so far (not {h} sessions yet)")
        out.append(L(f"· 此前在同一只股票上：{c.traded} {side_text(c)} {c.ticker}，{res}",
                     f"· Earlier in the same stock: {c.traded} {side_text(c)} {c.ticker}, {res}"))
    return out


def severity(note: FilingNote, m: MemberScore | None, scope: str = "accurate") -> str | None:
    """Whether a report deserves an alert. By default only reports of members rated accurate do; with
    ``accurate_or_watchlist`` any trade in a watched stock does too."""
    strong = m is not None and m.weight >= 2
    if strong:
        return "high" if note.watch else "warn"
    return "warn" if note.watch and scope == "accurate_or_watchlist" else None


def alert_text(note: FilingNote, rep: CongressReport, extra: list[str] | None = None, days: int = 90) -> tuple[str, str]:
    from .insight import head, tidy
    m = rep.score(f"{note.chamber}:{note.member}")
    buys = [c for c in note.calls if c.side == "buy"]
    sells = [c for c in note.calls if c.side == "sell"]
    star = f"{TIER[m.tier]} " if m is not None and m.weight >= 2 else ""
    what = L("、", ", ").join(([L(f"买入 {'/'.join(dict.fromkeys(c.ticker for c in buys))}", f"bought {'/'.join(dict.fromkeys(c.ticker for c in buys))}")] if buys else [])
                             + ([L(f"卖出 {'/'.join(dict.fromkeys(c.ticker for c in sells))}", f"sold {'/'.join(dict.fromkeys(c.ticker for c in sells))}")] if sells else []))
    if len(what) > 70:
        what = L(f"买入 {len(buys)} 笔、卖出 {len(sells)} 笔", f"{len(buys)} purchase(s), {len(sells)} sale(s)")
    title = L(f"议员交易：{star}{note.member} {what}", f"Congress trade: {star}{note.member} {what}")
    lines = [L(f"{note.member}（{'，'.join(x for x in (CHAMBER.get(note.chamber, note.chamber), note.state) if x)}）{note.filed} 披露了 {len(note.calls)} 笔股票交易："
               f"买入 {len(buys)} 笔，卖出 {len(sells)} 笔" + (f"；其中 {'、'.join(note.watch)} 在你的自选里" if note.watch else "") + "。",
               f"{note.member} ({', '.join(x for x in (CHAMBER.get(note.chamber, note.chamber), note.state) if x)}) disclosed {len(note.calls)} stock trade(s) on {note.filed}: "
               f"{len(buys)} purchase(s), {len(sells)} sale(s)" + (f"; {', '.join(note.watch)} on your watchlist" if note.watch else "") + ".")]
    lines.append(head("披露了什么", "What was disclosed"))
    first = sorted(note.calls, key=lambda c: (c.ticker not in note.watch, -c.hi, c.ticker))
    lines += [call_line(c, rep.horizon) for c in first[:10]]
    if len(first) > 10:
        lines.append(L(f"· 另有 {len(first) - 10} 笔，见原始申报。", f"· {len(first) - 10} more; see the original report."))
    lines.append(head("这位议员的记录", "This member's record"))
    tick = {c.ticker for c in note.calls}
    earlier = [c for c in rep.calls if c.who == f"{note.chamber}:{note.member}" and c.ticker in tick and c.doc != note.doc]
    lines += record_lines(m, rep, sorted(earlier, key=lambda c: c.traded, reverse=True))
    cons = []
    for t in list(dict.fromkeys(note.watch + [c.ticker for c in first]))[:5]:
        k = rep.consensus(t, days)
        others_b = [n for n in k["buyers"] if n != note.member]
        others_s = [n for n in k["sellers"] if n != note.member]
        if others_b or others_s:
            ab = [n for n in k["acc_buy"] if n != note.member]
            asell = [n for n in k["acc_sell"] if n != note.member]
            net = sum(k["buyers"][n] for n in ab) - sum(k["sellers"][n] for n in asell)
            acc = (L(f"其中评级偏准以上的：买入 {len(ab)} 人" + (f"（{'、'.join(ab[:3])}）" if ab else "") + f"，卖出 {len(asell)} 人"
                     + (f"（{'、'.join(asell[:3])}）" if asell else "") + f"，加权净值 {net:+.1f}（正 = 准的人里买方占优）",
                     f"of those rated fairly accurate or better: {len(ab)} bought" + (f" ({', '.join(ab[:3])})" if ab else "") + f", {len(asell)} sold"
                     + (f" ({', '.join(asell[:3])})" if asell else "") + f", weighted net {net:+.1f} (positive = more buying among the accurate)")
                   if ab or asell else
                   L("其中没有评级偏准以上的议员，所以不作参考", "none of them is rated fairly accurate or better, so they are not referred to"))
            cons.append(L(f"· {t}：近 {days} 天另有 {len(others_b)} 人买入、{len(others_s)} 人卖出；{acc}",
                          f"· {t}: in the last {days} days {len(others_b)} other member(s) bought and {len(others_s)} sold; {acc}"))
    if cons:
        lines.append(head("其他议员在同样的股票上", "Other members in the same stocks"))
        lines += cons
    lines += list(extra or [])
    lines.append(head("怎么看", "How to read it"))
    slow = max((c.delay for c in note.calls), default=0)
    lines.append(L(f"· 这是 {slow} 天前的交易（法律允许最晚 45 天披露），披露时价格已经走了一段：上面每一笔都给了“交易日收盘 → 现在”。",
                   f"· These trades are up to {slow} days old (the law allows 45 days to disclose) and the price has moved since: "
                   "each line above gives 'close on the trade day → now'."))
    lines.append(L("· 金额只有区间，不知道确切规模，也不知道占他资产的比例；持有人是配偶或子女、或由投资顾问代为操作的情况很常见，申报里往往会注明。",
                   "· Amounts are ranges only: neither the exact size nor its share of the member's assets is known; trades by a spouse or child, "
                   "or made by an adviser, are common and often noted in the report."))
    lines.append(L("· 卖出的信息量通常比买入低（缴税、用钱、再平衡都会卖）；“卖对了”只说明卖出后股票跑输大盘。",
                   "· Sales usually say less than purchases (taxes, cash needs and rebalancing all lead to selling); a 'right' sale only means "
                   "the stock lagged the market afterwards."))
    lines.append(L("· 几百名议员里总有一些人的记录因为运气而好看。评级是“值得看一眼”的理由，不是这一笔会对的依据；更该看的是“披露之后才跟”的那一行。",
                   "· Among several hundred members some records look good by luck. A rating is a reason to look, not grounds to expect this "
                   "trade to be right; the line on following after the disclosure is the one to weigh."))
    if note.url:
        lines.append(note.url)
    lines.append(L("这是公开申报的整理和按固定规则做的统计，不是预测，也不是买卖建议。",
                   "This is a digest of public filings with statistics by fixed rules; it is not a prediction and not a recommendation to buy or sell."))
    return tidy(title), lines[0] + "\n" + tidy("\n".join(lines[1:]))


def digest_lines(rep: CongressReport | None, days: int = 2, today: date | None = None) -> list[str]:
    """For the daily digest: reports of the last days from accurate members or about watched stocks."""
    if rep is None or not rep.ts:
        return []
    cut = ((today or date.today()) - timedelta(days=days)).isoformat()
    out = []
    for n in rep.filings:
        m = rep.score(f"{n.chamber}:{n.member}")
        if n.filed < cut or severity(n, m, rep.scope) is None:
            continue
        tag = f"{TIER[m.tier]} " if m is not None and m.weight >= 2 else ""
        rows = L("；", "; ").join(L(f"{side_text(c)} {c.ticker}（{c.traded[5:]}，{money(c.lo, c.hi)}）",
                                    f"{side_text(c)} {c.ticker} ({c.traded[5:]}, {money(c.lo, c.hi)})") for c in n.calls[:6])
        out.append(L(f"  {n.filed} {tag}{n.member}：{rows}", f"  {n.filed} {tag}{n.member}: {rows}"))
    moved = [c for c in rep.changes if c["at"] >= cut and (WEIGHT.get(c["old"], 0) > 0) != (WEIGHT.get(c["new"], 0) > 0)]
    if moved:
        out.append(L("  评级变动：", "  Rating changes: ") + L("；", "; ").join(change_text(c) for c in moved[:6]))
    return out


def tier_changes(prev: dict, members: list[MemberScore], today: date | None = None) -> list[dict]:
    """Members whose tier differs from the one remembered in ``prev`` (key -> tier)."""
    day = (today or date.today()).isoformat()
    return [{"member": m.member, "chamber": m.chamber, "key": m.key, "old": prev[m.key], "new": m.tier, "at": day}
            for m in members if m.key in prev and prev[m.key] != m.tier]


def change_text(ch: dict) -> str:
    into = WEIGHT.get(ch["new"], 0) > 0 and WEIGHT.get(ch["old"], 0) == 0
    out = WEIGHT.get(ch["new"], 0) == 0 and WEIGHT.get(ch["old"], 0) > 0
    note = (L("，开始纳入参考", "; now referred to") if into else L("，不再作参考", "; no longer referred to") if out else "")
    return L(f"{ch['at']} {ch['member']}：{TIER.get(ch['old'], ch['old'])} → {TIER.get(ch['new'], ch['new'])}{note}",
             f"{ch['at']} {ch['member']}: {TIER.get(ch['old'], ch['old'])} → {TIER.get(ch['new'], ch['new'])}{note}")


def as_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "—"
