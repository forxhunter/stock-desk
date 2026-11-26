"""Unusual-activity scan (异动扫描): large Polymarket bets, unusual option volume, futures moves.

Read-only and rule-based. Every item says which fixed rule it tripped and with which numbers, and
adds a short "how to read this / what to check" note. Nothing here predicts anything and nothing
can place a bet or an order: only public, unauthenticated GET endpoints and Yahoo quotes are used.

Polymarket (public Data API v2 ``/v2/trades`` + Gamma ``/events``), per market, looking at trades of
at least ``min_usd`` during the last ``recent_minutes``:
  big       one trade of at least ``big_usd``
  share     the recent large trades add up to ``share_pct`` % of the market's 24 h volume
  wallet    one wallet has put ``wallet_usd`` net on one side within ``window_hours``
  onesided  three or more large trades, at least 80 % of the money on the same side
  jump      the price moved ``jump_pts`` points within the hour
Trades at a price of 95 %+ (and sales at 5 %-) are counted separately: buying a near-certain outcome
is usually yield collection, selling one is cashing in, and neither carries much information.

Options (Yahoo chains, delayed): contracts whose volume today is at least ``vol_oi`` x the open
interest, with at least ``min_volume`` contracts and ``min_premium`` dollars of premium traded.
Expiries within ``min_dte`` days are skipped (same-day options always trade far more than their open
interest) and so are strikes with almost no open interest (``min_oi``).
Futures (Yahoo daily bars): a daily move beyond ``z`` standard deviations of the last 60 sessions,
or the last finished session's volume at ``volume_mult`` x its 20-day average.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import quote

from .adapters.base import AdapterError, HttpGet, get_json, http_get
from .i18n import L

DATA = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"

DEFAULTS: dict = {
    "polymarket": {
        "enabled": True, "min_usd": 10000.0, "big_usd": 50000.0, "recent_minutes": 60, "window_hours": 24,
        "share_pct": 25.0, "wallet_usd": 100000.0, "flow_usd": 60000.0, "jump_pts": 5.0, "extreme_price": 0.95,
        "exclude_tags": ["Sports", "Esports"], "exclude_titles": ["Up or Down"],
        "macro_tags": ["Fed", "fomc", "Fed Rates", "Interest Rates", "Economy", "Economic Policy", "Recession", "Inflation",
                       "Finance", "Business", "Big Tech", "Tech", "AI", "Commodities", "Oil", "Tariffs", "Trade War",
                       "Stocks", "IPO", "IPOs", "Earnings", "Macro", "Treasuries", "Jobs"],
        "show_other": False, "max_items": 15,
    },
    "options": {"enabled": True, "interval_minutes": 30, "expiries": 4, "min_volume": 500, "vol_oi": 3.0,
                "min_premium": 200000.0, "min_dte": 2, "min_oi": 100, "per_symbol": 2,
                "extra_symbols": ["SPY", "QQQ"], "max_items": 20},
    "futures": {"enabled": True, "z": 2.0, "volume_mult": 1.8,
                "symbols": {"^GSPC": L("标普 500 指数", "S&P 500 index"), "ES=F": L("标普 500 期货", "S&P 500 futures"), "NQ=F": L("纳指 100 期货", "Nasdaq 100 futures"),
                            "^VIX": L("VIX 波动率指数", "VIX volatility index"),
                            "ZN=F": L("10 年期美债期货", "10-year Treasury futures"),
                            "CL=F": L("原油期货", "Crude oil futures"), "GC=F": L("黄金期货", "Gold futures")}},
    "alert_tiers": ["watch", "macro"], "max_alerts": 5, "email_min_severity": "high", "max_emails_per_day": 6,
}

TIER_LABEL = {"watch": L("自选相关", "Watchlist-related"), "macro": L("宏观 / 财经", "Macro / finance"),
              "other": L("其它", "Other")}


def settings(cfg: dict | None) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update({a: b for a, b in v.items() if b is not None})
        elif v is not None:
            out[k] = v
    return out


@dataclass
class FlowItem:
    source: str                      # "polymarket" | "options" | "futures"
    key: str                         # stable id (one per market / contract / future, side and day)
    title: str
    url: str = ""
    score: float = 0.0
    severity: str = "info"           # "info" | "warn" | "high"
    tier: str = "other"              # "watch" | "macro" | "other"
    tickers: list[str] = field(default_factory=list)
    headline: str = ""               # one line: what happened
    facts: list[str] = field(default_factory=list)      # the rules that tripped, with numbers
    reading: list[str] = field(default_factory=list)    # how to read it / what to check
    amount: float = 0.0
    ts: float = 0.0
    detail: list[str] = field(default_factory=list)     # the concrete picture: trades, prices, pay-offs, levels


@dataclass
class FlowReport:
    ts: float = 0.0
    items: list[FlowItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by(self, source: str) -> list[FlowItem]:
        return [i for i in self.items if i.source == source]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "FlowReport":
        d = dict(d or {})
        try:
            return cls(ts=float(d.get("ts") or 0), items=[FlowItem(**i) for i in d.get("items") or []],
                       notes=list(d.get("notes") or []), errors=list(d.get("errors") or []))
        except TypeError:
            return cls()


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


def _jlist(v) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def usd(x: float) -> str:
    x = abs(x)
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.0f}"


def _severity(score: float) -> str:
    return "high" if score >= 4 else "warn" if score >= 2 else "info"


def _mentions(name: str, text: str) -> bool:
    if name.isupper() and len(name) <= 5:
        return re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text) is not None
    return re.search(rf"(?<![A-Za-z]){re.escape(name.lower())}(?![A-Za-z])", text.lower()) is not None


def match_tickers(text: str, watch: dict[str, list[str]]) -> list[str]:
    out = []
    for sym, aliases in watch.items():
        names = [a for a in aliases if len(a) >= 3]
        if sym.isalpha() and len(sym) >= 3:
            names.append(sym.upper())
        if any(_mentions(n, text) for n in names):
            out.append(sym)
    return out


# ---- Polymarket ---------------------------------------------------------------------------------

def fetch_trades(getter: HttpGet = http_get, min_usd: float = 10000.0, limit: int = 500) -> list[dict]:
    data = get_json(getter, f"{DATA}/trades?limit={int(limit)}&takerOnly=true&filterType=CASH&filterAmount={int(min_usd)}")
    rows = data.get("data") if isinstance(data, dict) else data
    return [r for r in rows or [] if isinstance(r, dict)]


def fetch_events(slugs: list[str], getter: HttpGet = http_get, batch: int = 20) -> dict[str, dict]:
    """Gamma events (tags and their markets) for the given slugs; missing ones are simply absent."""
    out: dict[str, dict] = {}
    for i in range(0, len(slugs), batch):
        q = "&".join("slug=" + quote(str(s)) for s in slugs[i:i + batch])
        for ev in get_json(getter, f"{GAMMA}/events?{q}") or []:
            if isinstance(ev, dict) and ev.get("slug"):
                out[ev["slug"]] = ev
    return out


def _wallet(t: dict) -> str:
    w = str(t.get("proxy_wallet") or "")
    name = str(t.get("pseudonym") or t.get("name") or "").strip()
    short = f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w
    return L(f"{name}（{short}）", f"{name} ({short})") if name else short


def scan_polymarket(trades: list[dict], events: dict[str, dict], watch: dict[str, list[str]],
                    cfg: dict | None = None, now: float | None = None) -> tuple[list[FlowItem], list[str]]:
    p = settings({"polymarket": cfg or {}})["polymarket"]
    now = now or time.time()
    recent_from = now - float(p["recent_minutes"]) * 60
    window_from = now - float(p["window_hours"]) * 3600
    ext = float(p["extreme_price"])
    excl_tags = {str(x).lower() for x in p["exclude_tags"]}
    excl_titles = [str(x).lower() for x in p["exclude_titles"]]
    macro_tags = {str(x).lower() for x in p["macro_tags"]}

    by_market: dict[str, list[dict]] = {}
    for t in trades:
        ts = _f(t.get("timestamp"))
        if ts >= window_from and t.get("condition_id"):
            by_market.setdefault(str(t["condition_id"]), []).append(t)

    items: list[FlowItem] = []
    skipped_sport = low_info_n = 0
    low_info_usd = 0.0
    for cid, rows in by_market.items():
        recent = [t for t in rows if _f(t.get("timestamp")) >= recent_from]
        if not recent:
            continue
        slug = str(recent[0].get("event_slug") or "")
        ev = events.get(slug)
        if ev is None:
            continue                                   # resolved or unknown event
        tags = [str(t.get("label") or "") for t in ev.get("tags") or []]
        title = str(recent[0].get("title") or "")
        if {t.lower() for t in tags} & excl_tags or any(x in title.lower() for x in excl_titles):
            skipped_sport += 1
            continue
        mk = next((m for m in ev.get("markets") or [] if str(m.get("conditionId")) == cid), {})
        if mk.get("closed"):
            continue
        outcomes = [str(o) for o in _jlist(mk.get("outcomes"))] or ["Yes", "No"]
        prices = [_f(x, -1) for x in _jlist(mk.get("outcomePrices"))]
        vol24 = _f(mk.get("volume24hr"))
        hour = mk.get("oneHourPriceChange")
        hour_pts = _f(hour) * 100 if hour is not None else None

        def signed(t):          # + = money towards the first outcome, - = towards the second
            first = int(_f(t.get("outcome_index"))) == 0
            buy = str(t.get("side")).upper() == "BUY"
            return (1 if first == buy else -1) * _f(t.get("size")) * _f(t.get("price"))

        def low_info(t):        # buying or cashing in a near-certain outcome, or dumping a near-worthless one
            px, buy = _f(t.get("price")), str(t.get("side")).upper() == "BUY"
            return px >= ext or (not buy and px <= 1 - ext)

        info = [t for t in recent if not low_info(t)]
        dull = [t for t in recent if low_info(t)]
        low_info_n += len(dull)
        low_info_usd += sum(abs(signed(t)) for t in dull)

        facts: list[str] = []
        rules: set[str] = set()           # which rules fired (identifiers; ``facts`` is display text)
        score = 0.0
        gross = sum(abs(signed(t)) for t in info)
        net = sum(signed(t) for t in info)
        top = max(info, key=lambda t: abs(signed(t)), default=None)
        if top is not None and abs(signed(top)) >= float(p["big_usd"]):
            amt = abs(signed(top))
            score += 2 + (1 if amt >= 4 * float(p["big_usd"]) else 0)
            buying = str(top.get("side")).upper() == "BUY"
            who = L("买入", "bought") if buying else L("卖出", "sold")
            rules.add("big")
            facts.append(L(f"大额单：{_wallet(top)} 一笔{who}「{top.get('outcome')}」{usd(amt)}，成交价 {_f(top.get('price')) * 100:.1f}%"
                           f"（{datetime.fromtimestamp(_f(top.get('timestamp'))):%H:%M}）",
                           f"Large order: {_wallet(top)} {who} \"{top.get('outcome')}\" for {usd(amt)} in one trade, "
                           f"at a price of {_f(top.get('price')) * 100:.1f}% "
                           f"({datetime.fromtimestamp(_f(top.get('timestamp'))):%H:%M})"))
        if vol24 > 0 and gross >= 2 * float(p["min_usd"]) and gross / vol24 * 100 >= float(p["share_pct"]):
            score += 2
            facts.append(L(f"相对成交异常：近 {int(p['recent_minutes'])} 分钟的大单合计 {usd(gross)}，"
                           f"相当于该市场 24 小时成交（{usd(vol24)}）的 {gross / vol24 * 100:.0f}%",
                           f"Unusual relative to volume: large orders in the last {int(p['recent_minutes'])} minutes add up to "
                           f"{usd(gross)}, which is {gross / vol24 * 100:.0f}% of this market's 24-hour volume ({usd(vol24)})"))
        wallets: dict[str, float] = {}
        for t in rows:
            if not low_info(t):
                wallets[str(t.get("proxy_wallet"))] = wallets.get(str(t.get("proxy_wallet")), 0.0) + signed(t)
        active = {str(t.get("proxy_wallet")) for t in info}
        whale = max(((w, v) for w, v in wallets.items() if w in active), key=lambda x: abs(x[1]), default=None)
        if whale and abs(whale[1]) >= float(p["wallet_usd"]):
            score += 2
            n = sum(1 for t in rows if str(t.get("proxy_wallet")) == whale[0] and not low_info(t))
            one = next(t for t in info if str(t.get("proxy_wallet")) == whale[0])
            rules.add("wallet")
            facts.append(L(f"同一钱包集中下注：{_wallet(one)} {int(p['window_hours'])} 小时内 {n} 笔，"
                           f"净押「{outcomes[0 if whale[1] > 0 else 1]}」{usd(whale[1])}",
                           f"One wallet concentrating bets: {_wallet(one)} placed {n} trades within {int(p['window_hours'])} hours, "
                           f"net \"{outcomes[0 if whale[1] > 0 else 1]}\" {usd(whale[1])}"))
        if len(info) >= 3 and gross >= float(p["flow_usd"]) and abs(net) / gross >= 0.8:
            score += 1.5
            facts.append(L(f"单边资金：{len(info)} 笔大单合计 {usd(gross)}，其中 {abs(net) / gross * 100:.0f}% 在同一方向",
                           f"One-sided money: {len(info)} large orders, {usd(gross)} in total, "
                           f"{abs(net) / gross * 100:.0f}% of it in the same direction"))
        if hour_pts is not None and abs(hour_pts) >= float(p["jump_pts"]):
            score += 2
            rules.add("jump")
            facts.append(L(f"价格跳变：「{outcomes[0]}」一小时内 {hour_pts:+.1f} 个百分点",
                           f"Price jump: \"{outcomes[0]}\" moved {hour_pts:+.1f} percentage points within an hour"))
        if not facts:
            continue

        text = f"{title} {ev.get('title', '')} {' '.join(tags)}"
        tickers = match_tickers(text, watch)
        tier = "watch" if tickers else "macro" if {t.lower() for t in tags} & macro_tags else "other"
        if tier == "other" and not p.get("show_other", True):
            continue
        if tier == "watch":
            score *= 1.5
        side = outcomes[0 if net >= 0 else 1] if info else ""
        prob = (L(f"，现在「{outcomes[0]}」{prices[0] * 100:.1f}%", f"; now \"{outcomes[0]}\" at {prices[0] * 100:.1f}%")
                if prices and 0 <= prices[0] <= 1 else "")
        headline = (L(f"近 {int(p['recent_minutes'])} 分钟 {len(info)} 笔大单，净流向「{side}」{usd(net)}{prob}",
                      f"Large orders in the last {int(p['recent_minutes'])} minutes: {len(info)}, net flow to \"{side}\" {usd(net)}{prob}")
                    if info else
                    L(f"近 {int(p['recent_minutes'])} 分钟只有高确定性价位的大单{prob}",
                      f"Only large orders at near-certain prices in the last {int(p['recent_minutes'])} minutes{prob}"))
        reading = []
        if tickers:
            reading.append(L(f"和你的自选 {'、'.join(tickers)} 有关：对照这只股票当天的走势和新闻，看两边是否互相印证；"
                             "只有一边在动时，先当作噪音。",
                             f"Related to your watchlist ({', '.join(tickers)}): compare with the stock's move and news that day "
                             "to see whether the two back each other up; when only one side is moving, treat it as noise first."))
        if "jump" in rules:
            reading.append(L("先找有没有对应的新闻。有新闻的跳变是在消化消息；没有新闻、只靠一两笔单推出来的跳变常常会回吐。",
                             "First look for matching news. A jump with news is the market digesting it; a jump with no news, "
                             "pushed by just one or two orders, often reverses."))
        if rules & {"big", "wallet"}:
            reading.append(L("大单只说明有人愿意押这么多，不说明他知道什么：可能是对冲别处的仓位、做市，或单纯判断错了。"
                             "可以点开市场看这个钱包的历史盈亏再决定要不要当回事。",
                             "A large order only shows that someone was willing to bet that much, not that they know anything: it may "
                             "hedge a position elsewhere, be market making, or simply be wrong. You can open the market and check "
                             "this wallet's past profit and loss before deciding how much weight to give it."))
        if vol24 and vol24 < 5 * float(p["big_usd"]):
            reading.append(L(f"这个市场 24 小时只成交 {usd(vol24)}，盘子小，一笔单就能把价格推走，概率的参考价值有限。",
                             f"This market traded only {usd(vol24)} in 24 hours; it is thin, one order can move the price, "
                             "so the probability is of limited reference value."))
        if dull:
            reading.append(L(f"另有 {len(dull)} 笔成交价在 {ext * 100:.0f}% 以上的大单（{usd(sum(abs(signed(t)) for t in dull))}），"
                             "多半是吃到期收益，没有计入上面的判断。",
                             f"Also at prices of {ext * 100:.0f}% or more: {len(dull)} more large trade(s), "
                             f"{usd(sum(abs(signed(t)) for t in dull))}; mostly yield collection into expiry, "
                             "not counted in the assessment above."))
        detail: list[str] = []
        book = []
        odds = " / ".join(L(f"「{o}」{x * 100:.1f}%", f"\"{o}\" {x * 100:.1f}%") for o, x in zip(outcomes, prices) if 0 <= x <= 1)
        if odds:
            book.append(L(f"现在的价格 {odds}", f"prices now {odds}"))
        moves = [L(f"{zh} {_f(mk.get(k)) * 100:+.1f}", f"{en} {_f(mk.get(k)) * 100:+.1f}")
                 for k, zh, en in (("oneHourPriceChange", "1 小时", "1h"), ("oneDayPriceChange", "1 天", "1d"),
                                   ("oneWeekPriceChange", "1 周", "1w")) if mk.get(k) is not None]
        if moves:
            book.append(L(f"「{outcomes[0]}」的变动（百分点）：{'，'.join(moves)}", f"change in \"{outcomes[0]}\" (points): {', '.join(moves)}"))
        total, depth = _f(mk.get("volumeNum", mk.get("volume"))), _f(mk.get("liquidityNum", mk.get("liquidity")))
        money = [L(f"24 小时成交 {usd(vol24)}", f"24h volume {usd(vol24)}")] if vol24 else []
        if total:
            money.append(L(f"累计成交 {usd(total)}", f"total volume {usd(total)}"))
        if depth:
            money.append(L(f"挂单深度 {usd(depth)}", f"order-book depth {usd(depth)}"))
        if money:
            book.append(L("，", ", ").join(money))
        end = str(mk.get("endDateIso") or (mk.get("endDate") or "")[:10] or (ev.get("endDate") or "")[:10])
        try:
            left = (datetime.strptime(end, "%Y-%m-%d").date() - datetime.fromtimestamp(now).date()).days
            book.append(L(f"{end} 到期（还有 {left} 天）", f"resolves {end} ({left} days left)"))
        except ValueError:
            pass
        if book:
            detail.append(L("市场：", "The market: ") + L("；", "; ").join(book))
        if info:
            rows_txt = []
            for t in sorted(info, key=lambda t: -_f(t.get("timestamp")))[:5]:
                buy = str(t.get("side")).upper() == "BUY"
                rows_txt.append(L(f"{datetime.fromtimestamp(_f(t.get('timestamp'))):%H:%M} {_wallet(t)} {'买入' if buy else '卖出'}"
                                  f"「{t.get('outcome')}」{usd(abs(signed(t)))} @ {_f(t.get('price')) * 100:.1f}%",
                                  f"{datetime.fromtimestamp(_f(t.get('timestamp'))):%H:%M} {_wallet(t)} {'bought' if buy else 'sold'} "
                                  f"\"{t.get('outcome')}\" {usd(abs(signed(t)))} @ {_f(t.get('price')) * 100:.1f}%"))
            detail.append(L(f"近 {int(p['recent_minutes'])} 分钟的大单（新的在前）：", f"Large trades in the last {int(p['recent_minutes'])} minutes (newest first): ")
                          + L("；", "; ").join(rows_txt))
            pos, neg = sum(signed(t) for t in info if signed(t) > 0), -sum(signed(t) for t in info if signed(t) < 0)
            if len(outcomes) > 1:
                detail.append(L(f"资金方向：押「{outcomes[0]}」{usd(pos)}，押「{outcomes[1]}」{usd(neg)}（买入一方或卖出另一方都算押这一方）",
                                f"Direction of the money: towards \"{outcomes[0]}\" {usd(pos)}, towards \"{outcomes[1]}\" {usd(neg)} "
                                "(buying one side or selling the other both count towards that side)"))
        if top is not None:
            amt, px0 = abs(signed(top)), _f(top.get("price"))
            if str(top.get("side")).upper() == "BUY" and 0 < px0 < 1:
                pay = _f(top.get("size"))
                detail.append(L(f"最大一笔的赔率：{usd(amt)} 买在 {px0 * 100:.1f}%，「{top.get('outcome')}」兑现可收回 {usd(pay)}"
                                f"（净赚 {usd(pay - amt)}，{(pay - amt) / amt:.1f} 倍），不兑现则 {usd(amt)} 归零。"
                                f"愿意在这个价位买，等于认为实际概率高于 {px0 * 100:.0f}%。",
                                f"Pay-off of the largest trade: {usd(amt)} bought at {px0 * 100:.1f}%; if \"{top.get('outcome')}\" comes true it "
                                f"returns {usd(pay)} (a gain of {usd(pay - amt)}, {(pay - amt) / amt:.1f}x), otherwise the {usd(amt)} is lost. "
                                f"Buying at this price means judging the real probability to be above {px0 * 100:.0f}%."))
            else:
                detail.append(L(f"最大一笔是卖出（{usd(amt)}，{px0 * 100:.1f}%）：可能是获利了结、止损，或在押相反的结果，单看成交记录分不清。",
                                f"The largest trade is a sale ({usd(amt)} at {px0 * 100:.1f}%): it may be profit-taking, a stop, or a bet on "
                                "the opposite outcome; the trade record alone cannot tell."))
            k = int(_f(top.get("outcome_index")))
            if k < len(prices) and 0 <= prices[k] <= 1 and px0:
                d = (prices[k] - px0) * 100
                after = (L("成交之后价格基本没动", "the price has hardly moved since") if abs(d) < 1
                         else L("成交之后价格继续同方向走", "the price kept moving the same way afterwards")
                         if (d > 0) == (str(top.get("side")).upper() == "BUY")
                         else L("成交之后价格反向走了，这笔目前是浮亏", "the price went the other way afterwards; the trade is under water for now"))
                detail.append(L(f"成交后的价格：成交价 {px0 * 100:.1f}% → 现在 {prices[k] * 100:.1f}%（{d:+.1f} 个百分点），{after}。",
                                f"Price since the trade: {px0 * 100:.1f}% → now {prices[k] * 100:.1f}% ({d:+.1f} points); {after}."))
            if vol24:
                detail.append(L(f"这一笔相当于该市场 24 小时成交的 {amt / vol24 * 100:.0f}%。",
                                f"This one trade equals {amt / vol24 * 100:.0f}% of the market's 24-hour volume."))
        big_wallets = sorted(wallets.items(), key=lambda x: -abs(x[1]))[:3]
        if len(wallets) > 1 and big_wallets and len(outcomes) > 1:
            names_by = {str(t.get("proxy_wallet")): _wallet(t) for t in rows}
            detail.append(L(f"{int(p['window_hours'])} 小时内净下注最多的钱包：", f"Wallets with the largest net bets in {int(p['window_hours'])} hours: ")
                          + L("；", "; ").join(L(f"{names_by.get(w, w)} 净押「{outcomes[0 if v > 0 else 1]}」{usd(v)}",
                                                  f"{names_by.get(w, w)} net \"{outcomes[0 if v > 0 else 1]}\" {usd(v)}") for w, v in big_wallets))
        day = datetime.fromtimestamp(now).strftime("%Y%m%d")
        items.append(FlowItem(
            "polymarket", f"pm:{cid[:14]}:{'a' if net >= 0 else 'b'}:{day}", title,
            f"https://polymarket.com/event/{slug}", round(score, 2), _severity(score), tier, tickers, headline,
            facts, reading, gross, max(_f(t.get("timestamp")) for t in recent), detail))
    items.sort(key=lambda i: (-{"watch": 2, "macro": 1, "other": 0}[i.tier], -i.score, -i.amount))
    notes = []
    if low_info_n:
        notes.append(L(f"近 {int(p['recent_minutes'])} 分钟另有 {low_info_n} 笔（{usd(low_info_usd)}）成交价 ≥{ext * 100:.0f}% 的大单，"
                       "属于买入近乎确定的结果，信息量低，未单独列出。",
                       f"Also in the last {int(p['recent_minutes'])} minutes: {low_info_n} more large trade(s), {usd(low_info_usd)}, "
                       f"at prices ≥{ext * 100:.0f}%; they buy a near-certain outcome, carry little information and are "
                       "not listed separately."))
    if skipped_sport:
        notes.append(L(f"已按设置跳过 {skipped_sport} 个体育 / 短周期涨跌市场。",
                       f"Skipped {skipped_sport} sports / short-term up-or-down markets, as set."))
    return items[: int(p["max_items"])], notes


# ---- options ------------------------------------------------------------------------------------

def _option_detail(sym: str, kind: str, expiry: str, r: dict, vol: float, oi: float, last: float, prem: float, dte: int,
                   book: dict, tot: dict, sp: dict, p: dict) -> list[str]:
    """The concrete picture of one contract: where the strike is, what it takes to pay off, what else traded."""
    call = kind == "C"
    name = L("看涨", "call") if call else L("看跌", "put")
    other = L("看跌", "put") if call else L("看涨", "call")
    strike, iv = _f(r.get("strike")), _f(r.get("impliedVolatility"))
    spot = _f(sp.get("price"))
    out = []
    if spot > 0 and strike > 0:
        d = (strike / spot - 1) * 100
        itm = (strike < spot) if call else (strike > spot)
        out.append(L(f"位置：正股 {spot:,.2f}，行权价 {strike:g} {'高于' if d >= 0 else '低于'}现价 {abs(d):.1f}%（{'价内' if itm else '价外'}）；"
                     f"{expiry} 到期，还有 {dte} 天",
                     f"Where it sits: stock {spot:,.2f}, strike {strike:g} is {abs(d):.1f}% {'above' if d >= 0 else 'below'} the price "
                     f"({'in' if itm else 'out of'} the money); expires {expiry}, {dte} days left"))
        be = strike + last if call else strike - last
        need = (be / spot - 1) * 100
        ref = ""
        hi, lo = _f(sp.get("high252")), _f(sp.get("low252"))
        if call and hi and be > hi:
            ref = L(f"，这个价位在 52 周收盘高点 {hi:,.2f} 之上", f", which is above the 52-week closing high {hi:,.2f}")
        elif not call and lo and be < lo:
            ref = L(f"，这个价位在 52 周收盘低点 {lo:,.2f} 之下", f", which is below the 52-week closing low {lo:,.2f}")
        out.append(L(f"盈亏平衡：按最新价 {last:,.2f} 买入，到期要正股{'涨到' if call else '跌到'} {be:,.2f}（距现价 {need:+.1f}%）才不亏{ref}；"
                     f"如果这些是卖出的，就反过来：到期时股价没有{'涨过' if call else '跌破'} {be:,.2f}，卖方留下权利金",
                     f"Break-even: bought at the last price {last:,.2f}, the stock must {'rise' if call else 'fall'} to {be:,.2f} "
                     f"({need:+.1f}% from here) by expiry not to lose{ref}; if these were sold it is the reverse: the seller keeps the "
                     f"premium as long as the stock has not {'risen above' if call else 'fallen below'} {be:,.2f} at expiry"))
        if iv > 0 and dte > 0:
            m = iv * math.sqrt(dte / 365.0)
            real = _f(sp.get("vol20"))
            cmp_ = ""
            if real > 0:
                cmp_ = L(f"；近 20 日实际年化波动 {real * 100:.0f}%，隐含波动{'明显高于' if iv > real * 1.2 else '明显低于' if iv < real * 0.8 else '接近'}实际"
                         f"{'（期权偏贵，市场在为大波动付钱）' if iv > real * 1.2 else ''}",
                         f"; realised 20-day annualised volatility {real * 100:.0f}%, so implied is "
                         f"{'clearly above' if iv > real * 1.2 else 'clearly below' if iv < real * 0.8 else 'close to'} realised"
                         f"{' (options are dear: the market is paying for a big move)' if iv > real * 1.2 else ''}")
            out.append(L(f"定价隐含的波动：隐含波动率 {iv * 100:.0f}% → 到期前一个标准差约 ±{m * 100:.1f}%（{spot * (1 - m):,.2f}–{spot * (1 + m):,.2f}）；"
                         f"行权价在这个范围{'之内' if abs(strike / spot - 1) <= m else '之外'}{cmp_}",
                         f"What the pricing implies: implied volatility {iv * 100:.0f}% → about ±{m * 100:.1f}% for one standard deviation "
                         f"by expiry ({spot * (1 - m):,.2f}–{spot * (1 + m):,.2f}); the strike is "
                         f"{'inside' if abs(strike / spot - 1) <= m else 'outside'} that range{cmp_}"))
        out.append(L(f"规模：{vol:,.0f} 张对应 {vol * 100:,.0f} 股，名义市值约 {usd(vol * 100 * spot)}；权利金 {usd(prem)}",
                     f"Size: {vol:,.0f} contracts cover {vol * 100:,.0f} shares, about {usd(vol * 100 * spot)} of stock; premium {usd(prem)}"))
    else:
        out.append(L(f"{expiry} 到期，还有 {dte} 天；{vol:,.0f} 张对应 {vol * 100:,.0f} 股，权利金 {usd(prem)}",
                     f"Expires {expiry}, {dte} days left; {vol:,.0f} contracts cover {vol * 100:,.0f} shares, premium {usd(prem)}"))
    same = sorted(((k, v) for k, v in book.get((expiry, kind), {}).items() if k != strike and v >= max(0.5 * vol, float(p["min_volume"]))),
                  key=lambda x: -x[1])[:2]
    opp = book.get((expiry, "P" if call else "C"), {}).get(strike, 0.0)
    legs = [L(f"同到期日的 {k:g} {name}也成交 {v:,.0f} 张", f"the {k:g} {name} of the same expiry also traded {v:,.0f}") for k, v in same]
    if opp >= max(0.5 * vol, float(p["min_volume"])):
        legs.append(L(f"同行权价的{other}成交 {opp:,.0f} 张", f"the {other} at the same strike traded {opp:,.0f}"))
    out.append(L("可能的组合：" + "；".join(legs) + " → 更像价差 / 跨式这类组合的一条腿，而不是单纯押方向",
                 "Possible combination: " + "; ".join(legs) + " → more likely one leg of a spread / straddle than a plain directional bet")
               if legs else
               L("可能的组合：同到期日没有规模相当的其它行权价成交 → 更像单腿交易", "Possible combination: no other strike of the same expiry traded in comparable size → more likely a single-leg trade"))
    if tot["C"] + tot["P"] > 0:
        pc = tot["P"] / tot["C"] if tot["C"] else float("inf")
        out.append(L(f"{sym} 整体：这几个到期日看涨 {tot['C']:,.0f} 张 / 看跌 {tot['P']:,.0f} 张，看跌看涨比 {pc:.2f}"
                     f"（{'看跌成交明显更多' if pc > 1.2 else '看涨成交明显更多' if pc < 0.7 else '两边大体均衡'}）",
                     f"{sym} overall: calls {tot['C']:,.0f} / puts {tot['P']:,.0f} contracts over these expiries, put/call ratio {pc:.2f} "
                     f"({'clearly more put volume' if pc > 1.2 else 'clearly more call volume' if pc < 0.7 else 'roughly balanced'})"))
    return out


def scan_options(chains: dict[str, dict[str, tuple]], cfg: dict | None = None, watch: set[str] | None = None,
                 now: float | None = None, spot: dict[str, dict] | None = None) -> tuple[list[FlowItem], list[str]]:
    """``chains``: symbol -> expiry -> (calls, puts) DataFrames in Yahoo's layout.
    ``spot``: symbol -> {price, vol20, high252, low252} of the stock, when known."""
    p = settings({"options": cfg or {}})["options"]
    today = datetime.fromtimestamp(now or time.time()).date()
    day = today.strftime("%Y%m%d")
    items: list[FlowItem] = []
    notes: list[str] = []
    for sym, exps in chains.items():
        tot = {"C": 0.0, "P": 0.0}
        hits = []
        book: dict[tuple, dict[float, float]] = {}        # (expiry, kind) -> strike -> volume
        for expiry, (calls, puts) in exps.items():
            for kind, df in (("C", calls), ("P", puts)):
                if df is None or not len(df):
                    continue
                for r in df.to_dict("records"):
                    vol, oi, last = _f(r.get("volume")), _f(r.get("openInterest")), _f(r.get("lastPrice"))
                    tot[kind] += vol
                    book.setdefault((expiry, kind), {})[_f(r.get("strike"))] = vol
                    prem = vol * last * 100
                    try:
                        dte = (datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date() - today).days
                    except ValueError:
                        dte = 99
                    if dte < int(p["min_dte"]) or oi < float(p["min_oi"]):
                        continue
                    if vol >= float(p["min_volume"]) and vol >= float(p["vol_oi"]) * max(oi, 1.0) \
                            and prem >= float(p["min_premium"]):
                        hits.append((prem, kind, expiry, r, vol, oi, last, dte))
        if tot["C"] + tot["P"] > 0:
            pc = tot["P"] / tot["C"] if tot["C"] else float("inf")
            notes.append(L(f"{sym}：看涨 {tot['C']:,.0f} 张 / 看跌 {tot['P']:,.0f} 张，看跌看涨比 {pc:.2f}"
                           f"（近 {len(exps)} 个到期日）",
                           f"{sym}: calls {tot['C']:,.0f} / puts {tot['P']:,.0f} contracts, put/call ratio {pc:.2f} "
                           f"(nearest {len(exps)} expiries)"))
        for prem, kind, expiry, r, vol, oi, last, dte in sorted(hits, key=lambda h: -h[0])[: int(p["per_symbol"])]:
            score = 2.0 + (1 if prem >= 5 * float(p["min_premium"]) else 0) + (1 if vol >= 10 * max(oi, 1.0) else 0)
            name = L("看涨", "call") if kind == "C" else L("看跌", "put")
            iv = _f(r.get("impliedVolatility")) * 100
            contract = r.get("contractSymbol") or f"{sym}{expiry}{kind}{_f(r.get('strike')):g}"
            items.append(FlowItem(
                "options", f"opt:{contract}:{day}",
                f"{sym} {expiry} {_f(r.get('strike')):g} {name}", f"https://finance.yahoo.com/quote/{sym}/options",
                round(score, 2), _severity(score), "watch" if watch and sym in watch else "macro",
                [sym] if watch and sym in watch else [],
                L(f"今日成交 {vol:,.0f} 张，是未平仓量（{oi:,.0f}）的 {vol / max(oi, 1.0):.1f} 倍，权利金约 {usd(prem)}",
                  f"Traded {vol:,.0f} contracts today, {vol / max(oi, 1.0):.1f}x the open interest ({oi:,.0f}), "
                  f"premium about {usd(prem)}"),
                [L(f"成交量 / 未平仓量 = {vol / max(oi, 1.0):.1f}（门槛 {float(p['vol_oi']):g}）",
                   f"Volume / open interest = {vol / max(oi, 1.0):.1f} (threshold {float(p['vol_oi']):g})"),
                 L(f"最新价 {last:,.2f}，隐含波动率 {iv:.0f}%，{'价内' if r.get('inTheMoney') else '价外'}",
                   f"Last price {last:,.2f}, implied volatility {iv:.0f}%, "
                   f"{'in the money' if r.get('inTheMoney') else 'out of the money'}")],
                [L("成交量远大于未平仓量，说明今天大多是新开的仓。但这份数据看不出是主动买还是主动卖："
                   f"{name}期权放量既可能是押方向，也可能是卖出收权利金、对冲正股，或价差组合的一条腿。",
                   "Volume far above open interest means most of today's trades opened new positions. But this data cannot "
                   "tell whether they were buys or sells: heavy "
                   f"{name} volume may be a directional bet, selling to collect premium, hedging the stock, or one leg of a spread."),
                 L("可核对：下一个交易日未平仓量是否跟着增加（增加 = 仓位留下了）；同到期日相邻行权价是否同时放量（多半是价差）；"
                   "是否临近财报。",
                   "To check: whether open interest rises on the next trading day (a rise = the positions stayed); whether "
                   "neighbouring strikes of the same expiry also saw heavy volume (usually a spread); whether earnings are near.")],
                prem, now or time.time(),
                _option_detail(sym, kind, str(expiry), r, vol, oi, last, prem, dte, book, tot, (spot or {}).get(sym) or {}, p)))
    items.sort(key=lambda i: (-i.score, -i.amount))
    return items[: int(p["max_items"])], notes


# ---- futures ------------------------------------------------------------------------------------

def _futures_detail(sym: str, c, last: float, z: float, thr: float, names: dict, board: dict) -> list[str]:
    px = float(c.iloc[-1])
    out = []
    lv = []
    for n in (20, 50):
        if len(c) >= n:
            m = float(c.iloc[-n:].mean())
            d = (px / m - 1) * 100
            lv.append(L(f"{n} 日均线 {m:,.2f}（现价在{'上方' if d >= 0 else '下方'} {abs(d):.1f}%）",
                        f"{n}-day average {m:,.2f} (price {abs(d):.1f}% {'above' if d >= 0 else 'below'})"))
    w = c.iloc[-60:]
    lo, hi = float(w.min()), float(w.max())
    where = (px - lo) / (hi - lo) * 100 if hi > lo else 50.0
    lv.append(L(f"近 {len(w)} 个交易日区间 {lo:,.2f}–{hi:,.2f}，现在在区间的 {where:.0f}% 位置（0% = 最低，100% = 最高）",
                f"range of the last {len(w)} sessions {lo:,.2f}–{hi:,.2f}, now at {where:.0f}% of it (0% = low, 100% = high)"))
    chg = [L(f"近 {n} 日 {(px / float(c.iloc[-1 - n]) - 1) * 100:+.1f}%", f"{n} sessions {(px / float(c.iloc[-1 - n]) - 1) * 100:+.1f}%")
           for n in (5, 20) if len(c) > n]
    out.append(L("位置：", "Where it stands: ") + L("；", "; ").join(lv + chg))
    rest = [L(f"{names[s]} {a:+.2f}%（{b:+.1f}σ）", f"{names[s]} {a:+.2f}% ({b:+.1f}σ)") for s, (a, b) in board.items() if s != sym]
    if rest:
        out.append(L("同一时间的其它品种：", "The other contracts at the same time: ") + L("；", "; ").join(rest))
    eq = [board[s][1] for s in ("ES=F", "NQ=F") if s in board]
    vix, bond = board.get("^VIX", (0, 0))[1], board.get("ZN=F", (0, 0))[1]
    if eq:
        e = sum(eq) / len(eq)
        if e <= -1 and vix >= 1 and bond >= 0.5:
            pat = L("股指跌、波动率升、美债涨：典型的避险组合（资金从股票转向债券）", "equities down, volatility up, Treasuries up: the classic risk-off mix (money moving from stocks to bonds)")
        elif e <= -1 and bond <= -1:
            pat = L("股债同跌：多见于利率 / 通胀预期上升，而不是单纯的避险", "stocks and bonds down together: more typical of rising rate / inflation expectations than of plain risk aversion")
        elif e <= -1 and vix >= 1:
            pat = L("股指跌、波动率升，但美债没有明显上涨：是股市自己的下跌，避险买盘不明显", "equities down and volatility up, but Treasuries not clearly higher: a fall in stocks themselves, without obvious flight to safety")
        elif e >= 1 and vix <= -1:
            pat = L("股指涨、波动率降：风险偏好回升", "equities up, volatility down: risk appetite returning")
        elif abs(e) < 1 and abs(vix) < 1 and abs(bond) < 1:
            pat = L("其它品种都在正常波动范围内：更像是这一个品种自己的事", "the other contracts are all within their normal range: more likely a matter of this one contract")
        else:
            pat = L("各品种没有形成一致的方向", "the contracts do not line up in one direction")
        out.append(L(f"跨品种的样子：{pat}。", f"The cross-asset picture: {pat}."))
    ret = c.pct_change()
    zs = (ret / ret.rolling(60, min_periods=30).std().shift(1)).iloc[:-1]
    same = zs[(zs <= -thr) if z < 0 else (zs >= thr)] if abs(z) >= thr else zs.iloc[0:0]
    if len(same):
        pos = [c.index.get_loc(d) for d in same.index]
        nxt = [(float(c.iloc[k + 1]) / float(c.iloc[k]) - 1) * 100 for k in pos if k + 1 < len(c) - 1]
        f5 = sorted((float(c.iloc[k + 5]) / float(c.iloc[k]) - 1) * 100 for k in pos if k + 5 < len(c))
        cont = sum(1 for x in nxt if (x < 0) == (z < 0))
        txt = L(f"过去 {len(c)} 个交易日里，同方向超过 {thr:g}σ 的单日变动出现过 {len(same)} 次",
                f"In the last {len(c)} sessions a daily move beyond {thr:g}σ in the same direction happened {len(same)} time(s)")
        if nxt:
            txt += L(f"；次日继续同向的 {cont}/{len(nxt)} 次", f"; continued the same way the next day in {cont}/{len(nxt)}")
        if f5:
            txt += L(f"，之后 5 日涨跌的中位数 {f5[len(f5) // 2]:+.1f}%（最差 {f5[0]:+.1f}%，最好 {f5[-1]:+.1f}%）",
                     f", median change over the next 5 sessions {f5[len(f5) // 2]:+.1f}% (worst {f5[0]:+.1f}%, best {f5[-1]:+.1f}%)")
        out.append(L(f"这段时间里的同类情况：{txt}。样本很少，只说明大致的样子。", f"Like cases in this period: {txt}. A small sample; it only shows the rough shape."))
    return out


def scan_futures(bars: dict, cfg: dict | None = None, now: float | None = None,
                 levels: dict | None = None) -> tuple[list[FlowItem], list[str]]:
    """``bars``: symbol -> daily OHLCV DataFrame. ``levels``: symbol -> [(name, price, source)] key levels to watch:
    a level crossed between the previous close and the latest price is a finding of its own."""
    p = settings({"futures": cfg or {}})["futures"]
    names = dict(p["symbols"] or {})
    items: list[FlowItem] = []
    notes: list[str] = []
    board: dict[str, tuple[float, float]] = {}            # symbol -> (last change %, z)
    for sym in names:
        df = bars.get(sym)
        if df is None or len(df) < 30:
            continue
        r0 = df["close"].astype(float).pct_change().dropna()
        sd0 = float(r0.iloc[-61:-1].std()) * 100
        board[sym] = (float(r0.iloc[-1]) * 100, float(r0.iloc[-1]) * 100 / sd0 if sd0 else 0.0)
    for sym, name in names.items():
        df = bars.get(sym)
        if df is None or len(df) < 30:
            continue
        c = df["close"].astype(float)
        ret = c.pct_change().dropna()
        last = float(ret.iloc[-1]) * 100
        sd = float(ret.iloc[-61:-1].std()) * 100
        z = last / sd if sd else 0.0
        v = df["volume"].astype(float) if "volume" in df else None
        vr = None
        if v is not None and len(v) >= 23 and float(v.iloc[-22:-2].mean()) > 0:
            vr = float(v.iloc[-2]) / float(v.iloc[-22:-2].mean())       # last finished session
        notes.append(L(f"{name}（{sym}）{float(c.iloc[-1]):,.2f}，{last:+.2f}%（{z:+.1f}σ）"
                       + (f"，上一交易日成交量为 20 日均量的 {vr:.1f} 倍" if vr else ""),
                       f"{name} ({sym}) {float(c.iloc[-1]):,.2f}, {last:+.2f}% ({z:+.1f}σ)"
                       + (f", last session's volume was {vr:.1f}x its 20-day average" if vr else "")))
        facts, score = [], 0.0
        prev, now_px = float(c.iloc[-2]), float(c.iloc[-1])
        marks = sorted((levels or {}).get(sym) or [], key=lambda x: -x[1])
        for lname, lvl, _src in marks:
            if prev >= lvl > now_px or prev <= lvl < now_px:
                down = now_px < lvl
                score += 2
                facts.append(L(f"{'跌破' if down else '站上'}关键位 {lname}：上一收盘 {prev:,.2f} → 现在 {now_px:,.2f}",
                               f"{'Fell through' if down else 'Rose through'} the key level {lname}: previous close {prev:,.2f} → now {now_px:,.2f}"))
        if abs(z) >= float(p["z"]):
            score += 2 + (1 if abs(z) >= 3 else 0)
            facts.append(L(f"单日 {last:+.2f}%，相当于近 60 个交易日波动的 {z:+.1f} 倍标准差（门槛 {float(p['z']):g}）",
                           f"Daily move {last:+.2f}%, which is {z:+.1f} standard deviations of the last 60 sessions' "
                           f"moves (threshold {float(p['z']):g})"))
        if vr and vr >= float(p["volume_mult"]):
            score += 1.5
            facts.append(L(f"上一交易日成交量是 20 日均量的 {vr:.1f} 倍（门槛 {float(p['volume_mult']):g}）",
                           f"Last session's volume was {vr:.1f}x its 20-day average (threshold {float(p['volume_mult']):g})"))
        if not facts:
            continue
        reading = [L("期货是全市场的风险温度计：股指期货大跌、VIX 大涨、美债期货上涨同时出现时，通常是避险情绪，高波动的科技股往往跌得更多。",
                     "Futures are a risk thermometer for the whole market: when equity index futures fall sharply, the VIX jumps "
                     "and Treasury futures rise together, it is usually risk aversion, and high-volatility tech stocks tend to fall more.")
                   if sym != "CL=F" and sym != "GC=F" else
                   L("商品期货的大幅波动多与地缘和通胀预期有关，对个股的影响是间接的。",
                     "Large commodity futures moves are mostly tied to geopolitics and inflation expectations; "
                     "the effect on individual stocks is indirect."),
                   L("可核对：是不是有数据公布（CPI、非农、议息）或突发新闻；盘中数字是未走完的一天，收盘后再确认。",
                     "To check: whether data was released (CPI, payrolls, a rate decision) or there was breaking news; an "
                     "intraday figure is an unfinished day, so confirm after the close.")]
        day = f"{df.index[-1]:%Y%m%d}" if hasattr(df.index[-1], "strftime") else str(df.index[-1])
        detail = _futures_detail(sym, c, last, z, float(p["z"]), names, board)
        if marks:
            detail.insert(0, L("关键位：", "Key levels: ") + L("；", "; ").join(
                L(f"{n_} {lv_:,.0f}（距现价 {(lv_ / now_px - 1) * 100:+.1f}%）", f"{n_} {lv_:,.0f} ({(lv_ / now_px - 1) * 100:+.1f}% from the price)")
                for n_, lv_, _ in marks[:8]))
            srcs = [f"{n_}: {s_}" for n_, _, s_ in marks if s_]
            if srcs:
                detail.append(L("这些价位的出处：", "Where these levels come from: ") + L("；", "; ").join(srcs))
        items.append(FlowItem("futures", f"fut:{sym}:{day}", L(f"{name}（{sym}）", f"{name} ({sym})"), f"https://finance.yahoo.com/quote/{quote(sym)}",
                              round(score, 2), _severity(score), "macro", [], L(f"{float(c.iloc[-1]):,.2f}，{last:+.2f}%（{z:+.1f}σ）", f"{float(c.iloc[-1]):,.2f}, {last:+.2f}% ({z:+.1f}σ)"),
                              facts, reading, 0.0, now or time.time(), detail))
    items.sort(key=lambda i: -i.score)
    return items, notes


# ---- alerts -------------------------------------------------------------------------------------

def alert_items(report: FlowReport, cfg: dict | None = None) -> list[FlowItem]:
    s = settings(cfg)
    keep = [i for i in report.items if i.severity in ("warn", "high") and i.tier in set(s["alert_tiers"])]
    keep.sort(key=lambda i: (-{"watch": 1}.get(i.tier, 0), -i.score, -i.amount))
    out: list[FlowItem] = []
    for src, cap in (("polymarket", int(s["max_alerts"])), ("options", 2), ("futures", 2)):   # bets first
        out += [i for i in keep if i.source == src][:cap]
    return out[: int(s["max_alerts"])]


def alert_text(i: FlowItem, extra: list[str] | None = None) -> tuple[str, str]:
    src = {"polymarket": L("Polymarket 异动", "Polymarket unusual activity"),
           "options": L("期权异动", "Unusual options activity"),
           "futures": L("期货异动", "Unusual futures activity")}[i.source]
    lines = [i.headline, L("【触发的规则】", "[Rules that fired]")] + [f"· {f}" for f in i.facts]
    if i.detail:
        lines += [L("【具体情况】", "[The details]")] + [f"· {d}" for d in i.detail]
    lines += [L("【怎么看】", "[How to read it]")] + [f"· {r}" for r in i.reading] + list(extra or [])
    if i.url:
        lines.append(i.url)
    lines.append(L("这是按固定规则筛出的异常成交，不是预测，也不是买卖建议。",
                   "These are unusual trades picked out by fixed rules; they are not a prediction and not a recommendation to buy or sell."))
    return L(f"{src}：{i.title}", f"{src}: {i.title}"), "\n".join(lines)


__all__ = ["FlowItem", "FlowReport", "AdapterError", "settings", "fetch_trades", "fetch_events", "scan_polymarket",
           "scan_options", "scan_futures", "alert_items", "alert_text", "match_tickers", "usd", "TIER_LABEL"]
