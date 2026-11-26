"""US Treasury yields and auction results, from the Treasury's own public data (read-only).

Yields: the daily par yield curve published by the Treasury (home.treasury.gov, end of day).
Auctions: TreasuryDirect's auction results (www.treasurydirect.gov/TA_WS), for nominal notes and
bonds (2, 3, 5, 7, 10, 20, 30 years; TIPS and floating-rate notes left out).

For each auction:
  bid-to-cover (倍数)   total bids / amount awarded, as published
  indirect / direct / dealer share   of the competitive amount awarded (indirect bidders are mostly
                         foreign and domestic investors bidding through dealers; a large dealer share
                         means dealers had to take what others did not)
  high - median          the spread inside the auction, in basis points, from the published yields
  tail (尾差, estimate)  the gap between the high yield and the same-maturity yield of the Treasury's
                         par curve on the auction day, minus the average of that gap over the last six
                         auctions of the term (the par curve is an end-of-day, bid-side fit, so the raw
                         gap carries a steady offset that this removes). The usual tail is measured
                         against the when-issued yield at the 1 pm close of bidding, which is dealer
                         data and not public, so this is an approximation.
Each figure is compared with the average of the last six auctions of the same original term. An
auction is called weak when at least two of: bid-to-cover 0.1 or more below that average, dealer
share 3 points or more above it, tail estimate of +1 bp or more; strong for the mirror image.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from .adapters.base import AdapterError, HttpGet, http_get
from .i18n import L

TA = "https://www.treasurydirect.gov/TA_WS/securities/auctioned"
CURVE = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all"
         "?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
TERMS = ("2-Year", "3-Year", "5-Year", "7-Year", "10-Year", "20-Year", "30-Year")
COL = {t: t.split("-")[0] + " Yr" for t in TERMS}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_curve(year: int, getter: HttpGet = http_get) -> list[dict]:
    """[{date, "2 Yr": 4.61, ...}] for ``year``, oldest first."""
    status, _, body = getter(CURVE.format(y=int(year)), timeout=40)
    if status != 200:
        raise AdapterError(f"HTTP {status}")
    rows = []
    for r in csv.DictReader(io.StringIO(body.decode("utf-8-sig", "replace"))):
        try:
            d = datetime.strptime(r.pop("Date"), "%m/%d/%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        rows.append({"date": d, **{k.strip(): _f(v) for k, v in r.items()}})
    rows.sort(key=lambda x: x["date"])
    return rows


def fetch_auctions(getter: HttpGet = http_get, size: int = 60) -> list[dict]:
    out = []
    for kind in ("Note", "Bond"):
        status, _, body = getter(f"{TA}?format=json&type={kind}&pagesize={int(size)}", timeout=40)
        if status != 200:
            raise AdapterError(f"HTTP {status}")
        try:
            out += [r for r in json.loads(body.decode("utf-8")) if isinstance(r, dict)]
        except ValueError:
            raise AdapterError(L("国债拍卖数据不是预期的格式", "The Treasury auction data is not in the expected format")) from None
    return out


@dataclass
class Auction:
    cusip: str
    term: str                        # original term, "10-Year"
    date: str                        # auction date (ISO)
    reopening: bool
    high: float
    median: float | None
    btc: float | None
    indirect: float | None           # shares of the competitive amount awarded, 0..1
    direct: float | None
    dealer: float | None
    offering: float
    curve: float | None = None       # par-curve yield of the same term on the auction day
    gap: float | None = None         # high - curve, bp
    tail: float | None = None        # bp, estimate
    spread: float | None = None      # high - median, bp
    avg: dict = field(default_factory=dict)      # averages of the last six auctions of the term
    verdict: str = "normal"          # "weak" | "strong" | "normal"
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def parse(rows: list[dict]) -> list[Auction]:
    out = []
    for r in rows:
        term = str(r.get("originalSecurityTerm") or r.get("securityTerm") or "")
        if term not in TERMS or r.get("inflationIndexSecurity") == "Yes" or r.get("floatingRate") == "Yes":
            continue
        high = _f(r.get("highYield"))
        if high is None:
            continue
        comp = sum(_f(r.get(k)) or 0.0 for k in ("indirectBiddersCompetitiveParAwarded", "directBiddersCompetitiveParAwarded",
                                                  "primaryDealersCompetitiveParAwarded"))
        share = lambda k: (_f(r.get(k)) or 0.0) / comp if comp else None
        out.append(Auction(str(r.get("cusip")), term, str(r.get("auctionDate") or r.get("competitiveAuctionCloseDate") or "")[:10],
                           r.get("reopening") == "Yes", high, _f(r.get("medianYield")), _f(r.get("bidToCoverRatio")),
                           share("indirectBiddersCompetitiveParAwarded"), share("directBiddersCompetitiveParAwarded"),
                           share("primaryDealersCompetitiveParAwarded"), _f(r.get("offeringAmount")) or 0.0))
    out.sort(key=lambda a: (a.date, a.term))
    return out


def assess(auctions: list[Auction], curve: list[dict]) -> list[Auction]:
    by_day = {r["date"]: r for r in curve}
    seen: dict[str, list[Auction]] = {}
    for a in auctions:
        row = by_day.get(a.date)
        if row is not None and row.get(COL[a.term]) is not None:
            a.curve = row[COL[a.term]]
            a.gap = round((a.high - a.curve) * 100, 1)
        if a.median is not None:
            a.spread = round((a.high - a.median) * 100, 1)
        prev = seen.get(a.term, [])[-6:]
        if prev:
            mean = lambda k: (sum(getattr(p, k) for p in prev if getattr(p, k) is not None) / max(1, sum(1 for p in prev if getattr(p, k) is not None))
                              if any(getattr(p, k) is not None for p in prev) else None)
            a.avg = {k: mean(k) for k in ("btc", "indirect", "direct", "dealer", "gap")}
            if a.gap is not None and a.avg["gap"] is not None:
                a.tail = round(a.gap - a.avg["gap"], 1)
            weak = strong = 0
            if a.btc is not None and a.avg["btc"] is not None:
                d = a.btc - a.avg["btc"]
                if d <= -0.1:
                    weak += 1
                    a.reasons.append(L(f"倍数 {a.btc:.2f} 低于近 6 次平均 {a.avg['btc']:.2f}", f"bid-to-cover {a.btc:.2f} below the 6-auction average {a.avg['btc']:.2f}"))
                elif d >= 0.1:
                    strong += 1
                    a.reasons.append(L(f"倍数 {a.btc:.2f} 高于近 6 次平均 {a.avg['btc']:.2f}", f"bid-to-cover {a.btc:.2f} above the 6-auction average {a.avg['btc']:.2f}"))
            if a.dealer is not None and a.avg["dealer"] is not None:
                d = (a.dealer - a.avg["dealer"]) * 100
                if d >= 3:
                    weak += 1
                    a.reasons.append(L(f"交易商承接 {a.dealer * 100:.1f}%，高于平均 {a.avg['dealer'] * 100:.1f}%", f"dealers took {a.dealer * 100:.1f}%, above the average {a.avg['dealer'] * 100:.1f}%"))
                elif d <= -3:
                    strong += 1
                    a.reasons.append(L(f"交易商承接 {a.dealer * 100:.1f}%，低于平均 {a.avg['dealer'] * 100:.1f}%", f"dealers took {a.dealer * 100:.1f}%, below the average {a.avg['dealer'] * 100:.1f}%"))
            if a.tail is not None:
                if a.tail >= 1:
                    weak += 1
                    a.reasons.append(L(f"尾差估算 +{a.tail:.1f}bp", f"tail estimate +{a.tail:.1f}bp"))
                elif a.tail <= -1:
                    strong += 1
                    a.reasons.append(L(f"尾差估算 {a.tail:.1f}bp（拍出的收益率低于市场）", f"tail estimate {a.tail:.1f}bp (stopped through the market)"))
            a.verdict = "weak" if weak >= 2 else "strong" if strong >= 2 else "normal"
        seen.setdefault(a.term, []).append(a)
    return auctions


def curve_summary(curve: list[dict]) -> dict | None:
    """Latest levels of the 2 / 10 / 30-year yields, their daily change and the 10-2 spread (all in % or bp)."""
    if len(curve) < 2:
        return None
    a, b = curve[-1], curve[-2]
    get = lambda r, k: r.get(k)
    out = {"date": a["date"]}
    for k in ("3 Mo", "2 Yr", "10 Yr", "30 Yr"):
        if get(a, k) is not None:
            out[k] = get(a, k)
            if get(b, k) is not None:
                out[k + " chg"] = round((get(a, k) - get(b, k)) * 100, 1)
    if out.get("10 Yr") is not None and out.get("2 Yr") is not None:
        out["10-2"] = round((out["10 Yr"] - out["2 Yr"]) * 100, 1)
    return out


def curve_line(s: dict | None) -> str:
    if not s:
        return ""
    c = lambda k: f"（{s[k + ' chg']:+.0f}bp）" if s.get(k + " chg") is not None else ""
    ce = lambda k: f" ({s[k + ' chg']:+.0f}bp)" if s.get(k + " chg") is not None else ""
    inv = (L("，倒挂", ", inverted") if s.get("10-2", 0) < 0 else "")
    return L(f"美债收益率（{s['date'][5:]}）：2 年 {s.get('2 Yr', 0):.2f}%{c('2 Yr')}，10 年 {s.get('10 Yr', 0):.2f}%{c('10 Yr')}，30 年 {s.get('30 Yr', 0):.2f}%{c('30 Yr')}；"
             f"10 年 − 2 年 {s.get('10-2', 0):+.0f}bp{inv}",
             f"Treasury yields ({s['date'][5:]}): 2y {s.get('2 Yr', 0):.2f}%{ce('2 Yr')}, 10y {s.get('10 Yr', 0):.2f}%{ce('10 Yr')}, 30y {s.get('30 Yr', 0):.2f}%{ce('30 Yr')}; "
             f"10y − 2y {s.get('10-2', 0):+.0f}bp{inv}")


VERDICT = {"weak": L("疲弱", "weak"), "strong": L("强劲", "strong"), "normal": L("一般", "ordinary")}


def auction_lines(a: Auction) -> list[str]:
    """The brief of one auction: the figures, the comparison, what it means."""
    out = [L(f"{a.term}{'（续发）' if a.reopening else ''} {a.date[5:]}：中标收益率 {a.high:.3f}%，倍数 {a.btc:.2f}" if a.btc else f"{a.term} {a.date[5:]}：中标收益率 {a.high:.3f}%",
             f"{a.term}{' (reopening)' if a.reopening else ''} {a.date[5:]}: high yield {a.high:.3f}%, bid-to-cover {a.btc:.2f}" if a.btc else f"{a.term} {a.date[5:]}: high yield {a.high:.3f}%")]
    def vs(k):
        x, m = getattr(a, k), (a.avg or {}).get(k)
        return "" if x is None or m is None else L(f"（平均 {m * 100:.1f}%）", f" (avg {m * 100:.1f}%)")
    parts = []
    if a.indirect is not None:
        parts.append(L(f"间接投标人 {a.indirect * 100:.1f}%{vs('indirect')}", f"indirect bidders {a.indirect * 100:.1f}%{vs('indirect')}"))
    if a.direct is not None:
        parts.append(L(f"直接投标人 {a.direct * 100:.1f}%{vs('direct')}", f"direct bidders {a.direct * 100:.1f}%{vs('direct')}"))
    if a.dealer is not None:
        parts.append(L(f"一级交易商 {a.dealer * 100:.1f}%{vs('dealer')}", f"primary dealers {a.dealer * 100:.1f}%{vs('dealer')}"))
    if parts:
        out.append(L("谁买的：", "Who bought: ") + L("，", ", ").join(parts))
    extra = []
    if a.avg.get("btc") is not None and a.btc is not None:
        extra.append(L(f"倍数近 6 次平均 {a.avg['btc']:.2f}", f"6-auction average bid-to-cover {a.avg['btc']:.2f}"))
    if a.tail is not None:
        extra.append(L(f"尾差估算 {a.tail:+.1f}bp", f"tail estimate {a.tail:+.1f}bp"))
    if a.spread is not None:
        extra.append(L(f"最高与中位收益率差 {a.spread:.1f}bp", f"high minus median {a.spread:.1f}bp"))
    if extra:
        out.append(L("；", "; ").join(extra))
    mean = {"weak": L("需求偏弱：要更高的收益率才卖得出去，对长端利率和高估值股票是压力",
                      "Weak demand: it took a higher yield to sell, a pressure on long rates and highly valued stocks"),
            "strong": L("需求强：收益率低于市场水平就卖完，长端利率压力缓和", "Strong demand: it sold below the market yield, easing pressure on long rates"),
            "normal": L("需求和平时差不多", "Demand close to usual")}[a.verdict]
    out.append(L(f"结论：{VERDICT[a.verdict]}。{mean}", f"Reading: {VERDICT[a.verdict]}. {mean}"))
    return out
