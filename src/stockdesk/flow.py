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
Trades at a price of 95 %+ (or 5 %-) are counted separately: buying a near-certain outcome is
usually yield collection and carries little information.

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

DATA = "https://data-api.polymarket.com/v2"
GAMMA = "https://gamma-api.polymarket.com"

DEFAULTS: dict = {
    "polymarket": {
        "enabled": True, "min_usd": 10000.0, "big_usd": 50000.0, "recent_minutes": 60, "window_hours": 24,
        "share_pct": 25.0, "wallet_usd": 100000.0, "flow_usd": 60000.0, "jump_pts": 5.0, "extreme_price": 0.95,
        "exclude_tags": ["Sports", "Esports"], "exclude_titles": ["Up or Down"],
        "macro_tags": ["Fed", "fomc", "Economy", "Finance", "Business", "Big Tech", "AI", "Tech", "Geopolitics",
                       "Crypto", "Bitcoin", "Commodities", "Oil", "Tariffs", "Trade War", "Inflation", "Stocks",
                       "IPO", "IPOs", "Earnings", "Macro", "China", "Politics", "Trump"],
        "show_other": True, "max_items": 30,
    },
    "options": {"enabled": True, "interval_minutes": 30, "expiries": 4, "min_volume": 500, "vol_oi": 3.0,
                "min_premium": 200000.0, "min_dte": 2, "min_oi": 100, "per_symbol": 2,
                "extra_symbols": ["SPY", "QQQ"], "max_items": 20},
    "futures": {"enabled": True, "z": 2.0, "volume_mult": 1.8,
                "symbols": {"ES=F": "标普 500 期货", "NQ=F": "纳指 100 期货", "^VIX": "VIX 波动率指数",
                            "ZN=F": "10 年期美债期货", "CL=F": "原油期货", "GC=F": "黄金期货"}},
    "alert_tiers": ["watch", "macro"], "max_alerts": 5, "email_min_severity": "high", "max_emails_per_day": 6,
}

TIER_LABEL = {"watch": "自选相关", "macro": "宏观 / 财经", "other": "其它"}


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
    return f"{name}（{short}）" if name else short


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

        def low_info(t):        # buying a near-certain outcome (or dumping a near-worthless one)
            px, buy = _f(t.get("price")), str(t.get("side")).upper() == "BUY"
            return (buy and px >= ext) or (not buy and px <= 1 - ext)

        info = [t for t in recent if not low_info(t)]
        dull = [t for t in recent if low_info(t)]
        low_info_n += len(dull)
        low_info_usd += sum(abs(signed(t)) for t in dull)

        facts: list[str] = []
        score = 0.0
        gross = sum(abs(signed(t)) for t in info)
        net = sum(signed(t) for t in info)
        top = max(info, key=lambda t: abs(signed(t)), default=None)
        if top is not None and abs(signed(top)) >= float(p["big_usd"]):
            amt = abs(signed(top))
            score += 2 + (1 if amt >= 4 * float(p["big_usd"]) else 0)
            who = "买入" if str(top.get("side")).upper() == "BUY" else "卖出"
            facts.append(f"大额单：{_wallet(top)} 一笔{who}「{top.get('outcome')}」{usd(amt)}，成交价 {_f(top.get('price')) * 100:.1f}%"
                         f"（{datetime.fromtimestamp(_f(top.get('timestamp'))):%H:%M}）")
        if vol24 > 0 and gross >= 2 * float(p["min_usd"]) and gross / vol24 * 100 >= float(p["share_pct"]):
            score += 2
            facts.append(f"相对成交异常：近 {int(p['recent_minutes'])} 分钟的大单合计 {usd(gross)}，"
                         f"相当于该市场 24 小时成交（{usd(vol24)}）的 {gross / vol24 * 100:.0f}%")
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
            facts.append(f"同一钱包集中下注：{_wallet(one)} {int(p['window_hours'])} 小时内 {n} 笔，"
                         f"净押「{outcomes[0 if whale[1] > 0 else 1]}」{usd(whale[1])}")
        if len(info) >= 3 and gross >= float(p["flow_usd"]) and abs(net) / gross >= 0.8:
            score += 1.5
            facts.append(f"单边资金：{len(info)} 笔大单合计 {usd(gross)}，其中 {abs(net) / gross * 100:.0f}% 在同一方向")
        if hour_pts is not None and abs(hour_pts) >= float(p["jump_pts"]):
            score += 2
            facts.append(f"价格跳变：「{outcomes[0]}」一小时内 {hour_pts:+.1f} 个百分点")
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
        prob = f"，现在「{outcomes[0]}」{prices[0] * 100:.1f}%" if prices and 0 <= prices[0] <= 1 else ""
        headline = (f"近 {int(p['recent_minutes'])} 分钟 {len(info)} 笔大单，净流向「{side}」{usd(net)}{prob}" if info
                    else f"近 {int(p['recent_minutes'])} 分钟只有高确定性价位的大单{prob}")
        reading = []
        if tickers:
            reading.append(f"和你的自选 {'、'.join(tickers)} 有关：对照这只股票当天的走势和新闻，看两边是否互相印证；"
                           "只有一边在动时，先当作噪音。")
        if any(f.startswith("价格跳变") for f in facts):
            reading.append("先找有没有对应的新闻。有新闻的跳变是在消化消息；没有新闻、只靠一两笔单推出来的跳变常常会回吐。")
        if any(f.startswith(("大额单", "同一钱包")) for f in facts):
            reading.append("大单只说明有人愿意押这么多，不说明他知道什么：可能是对冲别处的仓位、做市，或单纯判断错了。"
                           "可以点开市场看这个钱包的历史盈亏再决定要不要当回事。")
        if vol24 and vol24 < 5 * float(p["big_usd"]):
            reading.append(f"这个市场 24 小时只成交 {usd(vol24)}，盘子小，一笔单就能把价格推走，概率的参考价值有限。")
        if dull:
            reading.append(f"另有 {len(dull)} 笔成交价在 {ext * 100:.0f}% 以上的大单（{usd(sum(abs(signed(t)) for t in dull))}），"
                           "多半是吃到期收益，没有计入上面的判断。")
        day = datetime.fromtimestamp(now).strftime("%Y%m%d")
        items.append(FlowItem(
            "polymarket", f"pm:{cid[:14]}:{'a' if net >= 0 else 'b'}:{day}", title,
            f"https://polymarket.com/event/{slug}", round(score, 2), _severity(score), tier, tickers, headline,
            facts, reading, gross, max(_f(t.get("timestamp")) for t in recent)))
    items.sort(key=lambda i: (-{"watch": 2, "macro": 1, "other": 0}[i.tier], -i.score, -i.amount))
    notes = []
    if low_info_n:
        notes.append(f"近 {int(p['recent_minutes'])} 分钟另有 {low_info_n} 笔（{usd(low_info_usd)}）成交价 ≥{ext * 100:.0f}% 的大单，"
                     "属于买入近乎确定的结果，信息量低，未单独列出。")
    if skipped_sport:
        notes.append(f"已按设置跳过 {skipped_sport} 个体育 / 短周期涨跌市场。")
    return items[: int(p["max_items"])], notes


# ---- options ------------------------------------------------------------------------------------

def scan_options(chains: dict[str, dict[str, tuple]], cfg: dict | None = None, watch: set[str] | None = None,
                 now: float | None = None) -> tuple[list[FlowItem], list[str]]:
    """``chains``: symbol -> expiry -> (calls, puts) DataFrames in Yahoo's layout."""
    p = settings({"options": cfg or {}})["options"]
    today = datetime.fromtimestamp(now or time.time()).date()
    day = today.strftime("%Y%m%d")
    items: list[FlowItem] = []
    notes: list[str] = []
    for sym, exps in chains.items():
        tot = {"C": 0.0, "P": 0.0}
        hits = []
        for expiry, (calls, puts) in exps.items():
            for kind, df in (("C", calls), ("P", puts)):
                if df is None or not len(df):
                    continue
                for r in df.to_dict("records"):
                    vol, oi, last = _f(r.get("volume")), _f(r.get("openInterest")), _f(r.get("lastPrice"))
                    tot[kind] += vol
                    prem = vol * last * 100
                    try:
                        dte = (datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date() - today).days
                    except ValueError:
                        dte = 99
                    if dte < int(p["min_dte"]) or oi < float(p["min_oi"]):
                        continue
                    if vol >= float(p["min_volume"]) and vol >= float(p["vol_oi"]) * max(oi, 1.0) \
                            and prem >= float(p["min_premium"]):
                        hits.append((prem, kind, expiry, r, vol, oi, last))
        if tot["C"] + tot["P"] > 0:
            pc = tot["P"] / tot["C"] if tot["C"] else float("inf")
            notes.append(f"{sym}：看涨 {tot['C']:,.0f} 张 / 看跌 {tot['P']:,.0f} 张，看跌看涨比 {pc:.2f}"
                         f"（近 {len(exps)} 个到期日）")
        for prem, kind, expiry, r, vol, oi, last in sorted(hits, key=lambda h: -h[0])[: int(p["per_symbol"])]:
            score = 2.0 + (1 if prem >= 5 * float(p["min_premium"]) else 0) + (1 if vol >= 10 * max(oi, 1.0) else 0)
            name = "看涨" if kind == "C" else "看跌"
            iv = _f(r.get("impliedVolatility")) * 100
            contract = r.get("contractSymbol") or f"{sym}{expiry}{kind}{_f(r.get('strike')):g}"
            items.append(FlowItem(
                "options", f"opt:{contract}:{day}",
                f"{sym} {expiry} {_f(r.get('strike')):g} {name}", f"https://finance.yahoo.com/quote/{sym}/options",
                round(score, 2), _severity(score), "watch" if watch and sym in watch else "macro",
                [sym] if watch and sym in watch else [],
                f"今日成交 {vol:,.0f} 张，是未平仓量（{oi:,.0f}）的 {vol / max(oi, 1.0):.1f} 倍，权利金约 {usd(prem)}",
                [f"成交量 / 未平仓量 = {vol / max(oi, 1.0):.1f}（门槛 {float(p['vol_oi']):g}）",
                 f"最新价 {last:,.2f}，隐含波动率 {iv:.0f}%，{'价内' if r.get('inTheMoney') else '价外'}"],
                ["成交量远大于未平仓量，说明今天大多是新开的仓。但这份数据看不出是主动买还是主动卖："
                 f"{name}期权放量既可能是押方向，也可能是卖出收权利金、对冲正股，或价差组合的一条腿。",
                 "可核对：下一个交易日未平仓量是否跟着增加（增加 = 仓位留下了）；同到期日相邻行权价是否同时放量（多半是价差）；"
                 "是否临近财报。"], prem, now or time.time()))
    items.sort(key=lambda i: (-i.score, -i.amount))
    return items[: int(p["max_items"])], notes


# ---- futures ------------------------------------------------------------------------------------

def scan_futures(bars: dict, cfg: dict | None = None, now: float | None = None) -> tuple[list[FlowItem], list[str]]:
    """``bars``: symbol -> daily OHLCV DataFrame."""
    p = settings({"futures": cfg or {}})["futures"]
    names = dict(p["symbols"] or {})
    items: list[FlowItem] = []
    notes: list[str] = []
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
        notes.append(f"{name}（{sym}）{float(c.iloc[-1]):,.2f}，{last:+.2f}%（{z:+.1f}σ）"
                     + (f"，上一交易日成交量为 20 日均量的 {vr:.1f} 倍" if vr else ""))
        facts, score = [], 0.0
        if abs(z) >= float(p["z"]):
            score += 2 + (1 if abs(z) >= 3 else 0)
            facts.append(f"单日 {last:+.2f}%，相当于近 60 个交易日波动的 {z:+.1f} 倍标准差（门槛 {float(p['z']):g}）")
        if vr and vr >= float(p["volume_mult"]):
            score += 1.5
            facts.append(f"上一交易日成交量是 20 日均量的 {vr:.1f} 倍（门槛 {float(p['volume_mult']):g}）")
        if not facts:
            continue
        reading = ["期货是全市场的风险温度计：股指期货大跌、VIX 大涨、美债期货上涨同时出现时，通常是避险情绪，高波动的科技股往往跌得更多。"
                   if sym != "CL=F" and sym != "GC=F" else
                   "商品期货的大幅波动多与地缘和通胀预期有关，对个股的影响是间接的。",
                   "可核对：是不是有数据公布（CPI、非农、议息）或突发新闻；盘中数字是未走完的一天，收盘后再确认。"]
        day = f"{df.index[-1]:%Y%m%d}" if hasattr(df.index[-1], "strftime") else str(df.index[-1])
        items.append(FlowItem("futures", f"fut:{sym}:{day}", f"{name}（{sym}）", f"https://finance.yahoo.com/quote/{quote(sym)}",
                              round(score, 2), _severity(score), "macro", [], f"{float(c.iloc[-1]):,.2f}，{last:+.2f}%（{z:+.1f}σ）",
                              facts, reading, 0.0, now or time.time()))
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


def alert_text(i: FlowItem) -> tuple[str, str]:
    src = {"polymarket": "Polymarket 异动", "options": "期权异动", "futures": "期货异动"}[i.source]
    lines = [i.headline] + [f"· {f}" for f in i.facts] + ["怎么看："] + [f"· {r}" for r in i.reading]
    if i.url:
        lines.append(i.url)
    lines.append("这是按固定规则筛出的异常成交，不是预测，也不是买卖建议。")
    return f"{src}：{i.title}", "\n".join(lines)


__all__ = ["FlowItem", "FlowReport", "AdapterError", "settings", "fetch_trades", "fetch_events", "scan_polymarket",
           "scan_options", "scan_futures", "alert_items", "alert_text", "match_tickers", "usd", "TIER_LABEL"]
