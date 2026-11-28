"""What hedge funds and other large holders are doing, from free public records (read-only).

None of this is real time: funds do not have to publish their trades as they happen. Every figure
below therefore carries the date it describes and the date it was published, and the report says
how many days old it is.

  cftc      CFTC Commitments of Traders, Traders in Financial Futures (futures only), from the CFTC's
            public API (publicreporting.cftc.gov). "Leveraged funds" = hedge funds and CTAs. Positions
            as of Tuesday's close, published Friday 3:30 pm ET (later after a federal holiday).
            The net position is compared with the last three years of the same contract. In Treasury
            futures much of the leveraged-fund short is the cash-futures basis trade (long bonds, short
            futures), not a bet on rates; in S&P futures part of the short hedges long stock books.
  shortvol  FINRA daily short sale volume (cdn.finra.org, Reg SHO files): shares sold short that day
            through FINRA's off-exchange facilities, published the same evening. Includes market makers'
            short sales made to fill buy orders, so 40-55 % is ordinary; only a change against the
            stock's own recent average says anything. It is not short interest.
  shortint  FINRA short interest (api.finra.org): open short positions on the settlement date, twice
            a month, published about seven trading days later.
  13f       SEC Form 13F: long US holdings of large managers at each quarter end, filed up to 45 days
            later. Shorts are not reported; options are reported as underlying shares. A list of
            well-known hedge funds is compared quarter on quarter for the watchlist.
  insiders  SEC Form 4: officers', directors' and 10 % holders' trades, filed within two business days.
  holders   SEC Schedule 13D / 13G: anyone crossing 5 %. 13D = may seek to influence the company
            (activists), filed within 5 business days; 13G = passive, up to 45 days after quarter end.

SEC asks automated clients to name themselves with a contact address in the User-Agent and refuses
requests that do not. That contact is the user's to give: the SEC parts stay off until
``funds.sec_contact`` is filled in data/config.yaml (by the user, not by this program).
"""
from __future__ import annotations

import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

from .adapters.base import USER_AGENT, AdapterError, HttpGet, http_get
from .i18n import L

CFTC = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
SHVOL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
FINRA_SI = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
SEC_SUB = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_ARCH = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

CONTRACTS = {                                  # CFTC contract market code -> (short name, kind)
    "13874A": (L("标普 500 E-mini", "S&P 500 E-mini"), "equity"),
    "209742": (L("纳指 100 E-mini", "Nasdaq-100 E-mini"), "equity"),
    "042601": (L("2 年美债期货", "2-year T-note futures"), "rates"),
    "044601": (L("5 年美债期货", "5-year T-note futures"), "rates"),
    "043602": (L("10 年美债期货", "10-year T-note futures"), "rates"),
    "020601": (L("30 年美债期货", "30-year T-bond futures"), "rates"),
}

FUNDS = {                                      # SEC CIK -> name (hedge funds that file 13F)
    1350694: "Bridgewater", 1037389: "Renaissance", 1273087: "Millennium", 1423053: "Citadel Advisors",
    1179392: "Two Sigma", 1009207: "D. E. Shaw", 1603466: "Point72", 1103804: "Viking Global",
    1061165: "Lone Pine", 1135730: "Coatue", 1167483: "Tiger Global", 1040273: "Third Point",
    1656456: "Appaloosa", 1536411: "Duquesne Family Office", 1791786: "Elliott", 1167557: "AQR",
    1218710: "Balyasny", 1318757: "Marshall Wace", 1029160: "Soros Fund", 1061768: "Baupost",
    2045724: "Situational Awareness", 1336528: "Pershing Square",
}

CUSIP = {                                      # ticker -> CUSIP (13F reports holdings by CUSIP)
    "TSM": "874039100", "INTC": "458140100", "GOOGL": "02079K305", "GOOG": "02079K107", "MSFT": "594918104",
    "NVDA": "67066G104", "AAPL": "037833100", "AMZN": "023135106", "META": "30303M102", "AVGO": "11135F101",
    "AMD": "007903107", "MU": "595112103", "TSLA": "88160R101", "SPY": "78462F103", "QQQ": "46090E103",
}

CIK = {                                        # ticker -> issuer CIK (Form 4 / 13D / 13G are filed under it)
    "TSM": 1046179, "INTC": 50863, "GOOGL": 1652044, "GOOG": 1652044, "MSFT": 789019, "NVDA": 1045810,
    "AAPL": 320193, "AMZN": 1018724, "META": 1326801, "AVGO": 1730168, "AMD": 2488, "MU": 723125, "TSLA": 1318605,
}

HOLDER_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A")
CODES = {"P": L("公开市场买入", "open-market buy"), "S": L("公开市场卖出", "open-market sale")}


def us_symbols(symbols) -> list[str]:
    """Symbols listed in the US (no exchange suffix, not an index or future)."""
    return [s for s in dict.fromkeys(symbols) if s and "." not in s and not s.startswith("^") and "=" not in s]


def _d(x) -> date | None:
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def lag(asof, published=None, today: date | None = None, approx: bool = False) -> str:
    """"数据截至 11-17，约 11-20 公布，距今 10 天"."""
    a, p, t = _d(asof), _d(published), today or date.today()
    if a is None:
        return ""
    s = L(f"数据截至 {a:%m-%d}", f"as of {a:%m-%d}")
    if p is not None:
        s += L(f"，{'约 ' if approx else ''}{p:%m-%d} 公布", f", {'about ' if approx else ''}published {p:%m-%d}")
    return s + L(f"，距今 {(t - a).days} 天", f", {(t - a).days} days old")


def busday(d: date, n: int) -> date:
    out = d
    while n > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out


# ---- CFTC ---------------------------------------------------------------------------------------

def fetch_cftc(getter: HttpGet = http_get, weeks: int = 160) -> list[dict]:
    codes = ",".join(f"'{c}'" for c in CONTRACTS)
    q = urllib.parse.urlencode({"$where": f"cftc_contract_market_code in({codes})",
                                "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": weeks * len(CONTRACTS)})
    status, _, body = getter(f"{CFTC}?{q}", timeout=40)
    if status != 200:
        raise AdapterError(f"HTTP {status}")
    try:
        rows = json.loads(body.decode("utf-8"))
    except ValueError:
        raise AdapterError(L("CFTC 数据不是预期的格式", "The CFTC data is not in the expected format")) from None
    return [r for r in rows if isinstance(r, dict)]


def _num(r: dict, k: str) -> float:
    try:
        return float(r.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def cftc_positions(rows: list[dict], today: date | None = None, years: int = 3) -> list[dict]:
    """One entry per contract: leveraged funds' net position now, its weekly change and where it sits in ``years``."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r.get("cftc_contract_market_code", ""), []).append(r)
    out = []
    for code, (name, kind) in CONTRACTS.items():
        rs = sorted(by.get(code, []), key=lambda r: r.get("report_date_as_yyyy_mm_dd", ""))
        if not rs:
            continue
        hist = []
        for r in rs:
            oi = _num(r, "open_interest_all")
            net = _num(r, "lev_money_positions_long") - _num(r, "lev_money_positions_short")
            hist.append((r["report_date_as_yyyy_mm_dd"][:10], net, net / oi * 100 if oi else 0.0,
                         _num(r, "asset_mgr_positions_long") - _num(r, "asset_mgr_positions_short")))
        last = _d(hist[-1][0])
        hist = [h for h in hist if _d(h[0]) >= last - timedelta(days=365 * years)]
        d, net, pct, am = hist[-1]
        prev = hist[-2] if len(hist) > 1 else None
        pcts = [h[2] for h in hist]
        rank = sum(1 for x in pcts if x < pct) / max(1, len(pcts) - 1) * 100 if len(pcts) > 1 else 50.0
        chg = [hist[i][1] - hist[i - 1][1] for i in range(1, len(hist))]
        sd = (sum(x * x for x in chg) / len(chg) - (sum(chg) / len(chg)) ** 2) ** 0.5 if len(chg) > 4 else 0.0
        out.append({"code": code, "name": name, "kind": kind, "date": d, "published": (last + timedelta(days=3)).isoformat(),
                    "net": net, "pct_oi": pct, "rank": rank, "weeks": len(hist),
                    "change": net - prev[1] if prev else None, "change_sd": sd, "low": min(pcts), "high": max(pcts),
                    "asset_mgr": am, "trend": [round(h[2], 2) for h in hist[-26:]]})
    return out


def cftc_line(p: dict, today: date | None = None) -> str:
    side = L("净多", "net long") if p["net"] >= 0 else L("净空", "net short")
    chg = ""
    if p.get("change") is not None:
        chg = L(f"，一周{'加多' if p['change'] >= 0 else '加空'} {abs(p['change']):,.0f} 张", f", {'+' if p['change'] >= 0 else '-'}{abs(p['change']):,.0f} on the week")
    return L(f"{p['name']}：杠杆基金{side} {abs(p['net']):,.0f} 张（占持仓 {p['pct_oi']:+.1f}%），在近 3 年里处于 {p['rank']:.0f}% 分位（0% = 最偏空，100% = 最偏多）{chg}（{lag(p['date'], p['published'], today, True)}）",
             f"{p['name']}: leveraged funds {side} {abs(p['net']):,.0f} contracts ({p['pct_oi']:+.1f}% of open interest), "
             f"{p['rank']:.0f}th percentile of 3 years (0 = most short, 100 = most long){chg} ({lag(p['date'], p['published'], today, True)})")


def cftc_reading(p: dict) -> str:
    if p["kind"] == "rates":
        return L("美债期货里杠杆基金的空头大部分是期现基差交易（买现券、卖期货），不等于看空美债；空头急减有时是基差交易平仓，会让美债市场短时波动。",
                 "Most of the leveraged-fund short in Treasury futures is the basis trade (long cash bonds, short futures), not a bearish bet; "
                 "a fast drop in that short can be the trade unwinding, which can jolt the bond market.")
    if p["rank"] <= 10:
        return L("对冲基金和 CTA 在指数期货上的仓位接近 3 年最偏空：空头拥挤时，一有好消息就容易被迫回补，出现急涨；但这部分空头也可能是在对冲股票多头，不全是看跌。",
                 "Hedge funds and CTAs are close to their most short in 3 years: a crowded short is prone to forced covering and sharp rallies on good news, "
                 "though part of it may hedge long stock books rather than bet on a fall.")
    if p["rank"] >= 90:
        return L("对冲基金和 CTA 在指数期货上的仓位接近 3 年最偏多（或空头最轻）：继续加多的余地变小，遇到坏消息时被动减仓的压力更大。",
                 "Hedge funds and CTAs are close to their most long (or least short) in 3 years: less room to add, and more forced selling if bad news comes.")
    return L("仓位在近 3 年的中间区域，本身不构成信号。", "Positioning is in the middle of its 3-year range and is not a signal by itself.")


# ---- FINRA daily short volume --------------------------------------------------------------------

def parse_shvol(text: str, symbols) -> dict:
    want, out = set(symbols), {}
    for line in text.splitlines()[1:]:
        p = line.split("|")
        if len(p) >= 5 and p[1] in want:
            try:
                out[p[1]] = [float(p[2]), float(p[4])]
            except ValueError:
                pass
    return out


def fetch_shvol(day: date, symbols, getter: HttpGet = http_get) -> dict | None:
    """{symbol: [short volume, total volume]} for ``day``; None when FINRA has no file (holiday, not out yet)."""
    try:
        status, _, body = getter(SHVOL.format(d=f"{day:%Y%m%d}"), timeout=40)
    except AdapterError:
        return None
    if status != 200:
        return None
    return parse_shvol(body.decode("utf-8", "replace"), symbols)


def shvol_stats(days: dict[str, dict], symbol: str, window: int = 20) -> dict | None:
    """Short share of volume on the last day against the ``window`` days before it."""
    seq = [(d, v[symbol]) for d, v in sorted(days.items()) if v and symbol in v and v[symbol][1] > 0]
    if len(seq) < 6:
        return None
    d, (s, t) = seq[-1]
    base = [x[0] / x[1] for _, x in seq[-window - 1:-1]]
    mean = sum(base) / len(base)
    sd = (sum((x - mean) ** 2 for x in base) / len(base)) ** 0.5
    ratio = s / t
    return {"date": d, "ratio": ratio, "mean": mean, "z": (ratio - mean) / sd if sd > 0 else 0.0, "short": s, "total": t,
            "days": len(base)}


def shvol_line(sym: str, st: dict, today: date | None = None) -> str:
    return L(f"{sym} 场外成交中卖空占 {st['ratio'] * 100:.0f}%（近 {st['days']} 天平均 {st['mean'] * 100:.0f}%，偏离 {st['z']:+.1f} 个标准差；{lag(st['date'], st['date'], today)}）",
             f"{sym} short sales were {st['ratio'] * 100:.0f}% of off-exchange volume ({st['days']}-day average {st['mean'] * 100:.0f}%, "
             f"{st['z']:+.1f} standard deviations; {lag(st['date'], st['date'], today)})")


# ---- FINRA short interest ------------------------------------------------------------------------

def _post_json(url: str, body: dict, timeout: float = 30) -> list:
    if urllib.parse.urlparse(url).hostname != "api.finra.org":
        raise AdapterError("host")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),            # a read-only query; FINRA takes it as a JSON body
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw.decode("utf-8")) if r.status != 204 and raw.strip() else []      # 204 = no data for that symbol
    except urllib.error.HTTPError as e:
        raise AdapterError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise AdapterError(type(e).__name__) from None


def fetch_short_interest(symbol: str, today: date | None = None, post=_post_json) -> list[dict]:
    t = today or date.today()
    rows = post(FINRA_SI, {"limit": 20, "compareFilters": [{"compareType": "equal", "fieldName": "symbolCode", "fieldValue": symbol}],
                           "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": (t - timedelta(days=150)).isoformat(),
                                                 "endDate": t.isoformat()}]})
    return sorted((r for r in rows or [] if isinstance(r, dict) and r.get("settlementDate")), key=lambda r: r["settlementDate"])


def short_interest(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    r = rows[-1]
    d = _d(r["settlementDate"])
    return {"date": r["settlementDate"], "published": busday(d, 7).isoformat(), "shares": float(r.get("currentShortPositionQuantity") or 0),
            "prev": float(r.get("previousShortPositionQuantity") or 0), "change_pct": float(r.get("changePercent") or 0),
            "days_to_cover": float(r.get("daysToCoverQuantity") or 0),
            "history": [[x["settlementDate"], float(x.get("currentShortPositionQuantity") or 0)] for x in rows[-8:]]}


def si_line(sym: str, si: dict, today: date | None = None) -> str:
    return L(f"{sym} 空头仓位 {si['shares'] / 1e6:,.1f}M 股，比上期 {si['change_pct']:+.1f}%，回补天数 {si['days_to_cover']:.1f} 天（{lag(si['date'], si['published'], today, True)}）",
             f"{sym} short interest {si['shares'] / 1e6:,.1f}M shares, {si['change_pct']:+.1f}% vs the previous report, "
             f"{si['days_to_cover']:.1f} days to cover ({lag(si['date'], si['published'], today, True)})")


# ---- SEC ----------------------------------------------------------------------------------------

class Sec:
    """Reads EDGAR with the user's declared User-Agent, at most about 6 requests a second."""

    def __init__(self, contact: str, getter: HttpGet = http_get, pause: float = 0.16):
        self.ua = f"StockDesk {contact}".strip()
        self.getter, self.pause, self._t = getter, pause, 0.0

    def get(self, url: str, timeout: float = 60) -> bytes:
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in ("www.sec.gov", "data.sec.gov"):
            raise AdapterError("host")
        wait = self._t + self.pause - time.time()
        if wait > 0:
            time.sleep(wait)
        self._t = time.time()
        status, _, body = self.getter(url, headers={"User-Agent": self.ua, "Accept-Encoding": "identity"}, timeout=timeout)
        if status != 200:
            raise AdapterError(f"HTTP {status}")
        return body

    def json(self, url: str):
        return json.loads(self.get(url).decode("utf-8"))

    def submissions(self, cik: int) -> list[dict]:
        """Recent filings, newest first: [{form, filed, period, acc, doc}]."""
        f = self.json(SEC_SUB.format(cik=int(cik)))["filings"]["recent"]
        return [{"form": f["form"][i], "filed": f["filingDate"][i], "period": f["reportDate"][i],
                 "acc": f["accessionNumber"][i], "doc": f["primaryDocument"][i]} for i in range(len(f["form"]))]

    def files(self, cik: int, acc: str) -> list[dict]:
        idx = self.json(SEC_ARCH.format(cik=int(cik), acc=acc.replace("-", "")) + "index.json")
        return list((idx.get("directory") or {}).get("item") or [])

    def doc(self, cik: int, acc: str, name: str) -> bytes:
        return self.get(SEC_ARCH.format(cik=int(cik), acc=acc.replace("-", "")) + name)

    def tickers(self) -> dict[str, int]:
        return {v["ticker"].upper(): int(v["cik_str"]) for v in self.json(SEC_TICKERS).values()}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_13f(xml: bytes, cusips) -> dict:
    """{"CUSIP|" or "CUSIP|Put"/"CUSIP|Call": [shares, value $]} for the wanted CUSIPs (streamed: tables can be large)."""
    want, out, row = {c.upper() for c in cusips}, {}, {}
    for ev, el in ET.iterparse(io.BytesIO(xml), events=("end",)):
        t = _local(el.tag)
        if t in ("cusip", "value", "sshPrnamt", "putCall", "sshPrnamtType"):
            row[t] = (el.text or "").strip()
        elif t == "infoTable":
            c = row.get("cusip", "").upper()
            if c in want and row.get("sshPrnamtType", "SH") == "SH":
                k = f"{c}|{row.get('putCall', '')}"
                cur = out.setdefault(k, [0.0, 0.0])
                cur[0] += float(row.get("sshPrnamt") or 0)
                cur[1] += float(row.get("value") or 0)
            row = {}
            el.clear()
    return out


def infotable_name(items: list[dict]) -> str | None:
    xml = [i for i in items if str(i.get("name", "")).lower().endswith(".xml") and "primary_doc" not in str(i.get("name", "")).lower()]
    if not xml:
        return None
    return max(xml, key=lambda i: int(i.get("size") or 0) if str(i.get("size") or "").isdigit() else 0)["name"]


def compare_13f(filings: dict[int, list[dict]], symbols: dict[str, str]) -> dict:
    """Per symbol: what each fund held at the last two quarter ends.

    ``filings`` = {cik: [newer, older]} with each {"period", "filed", "rows"}; ``symbols`` = {symbol: cusip}.
    """
    out = {}
    for sym, cu in symbols.items():
        rows, latest = [], ""
        for cik, fl in filings.items():
            if not fl:
                continue
            new = fl[0]
            old = fl[1] if len(fl) > 1 else None
            latest = max(latest, new["period"])
            sh = new["rows"].get(f"{cu}|", [0, 0])
            was = old["rows"].get(f"{cu}|", [0, 0]) if old else None
            opt = {k.split("|")[1]: v[0] for k, v in new["rows"].items() if k.startswith(cu + "|") and k.split("|")[1]}
            if sh[0] == 0 and (was is None or was[0] == 0) and not opt:
                continue
            rows.append({"fund": FUNDS.get(cik, str(cik)), "period": new["period"], "filed": new["filed"], "shares": sh[0], "value": sh[1],
                         "prev": was[0] if was else None, "prev_period": old["period"] if old else None, "options": opt})
        cur = [r for r in rows if r["period"] == latest]
        add = [r for r in cur if r["prev"] is not None and r["shares"] > r["prev"] * 1.05]
        cut = [r for r in cur if r["prev"] is not None and r["shares"] < r["prev"] * 0.95]
        out[sym] = {"period": latest, "rows": sorted(rows, key=lambda r: -r["value"]),
                    "new": [r["fund"] for r in cur if not r["prev"] and r["shares"] > 0],
                    "exited": [r["fund"] for r in cur if r["prev"] and r["shares"] == 0],
                    "added": [r["fund"] for r in add if r["prev"]], "trimmed": [r["fund"] for r in cut if r["shares"] > 0],
                    "net_shares": sum(r["shares"] - (r["prev"] or 0) for r in cur if r["prev"] is not None),
                    "filed_last": max((r["filed"] for r in cur), default="")}
    return out


def f13_line(sym: str, s: dict, today: date | None = None) -> str:
    n = lambda xs: L(f"{len(xs)} 家", f"{len(xs)}") + (f"（{'、'.join(xs[:4])}{'…' if len(xs) > 4 else ''}）" if xs else "")
    return L(f"{sym} 季度持仓（{len(FUNDS)} 家对冲基金）：加仓 {n(s['added'])}，减仓 {n(s['trimmed'])}，新建仓 {n(s['new'])}，清仓 {n(s['exited'])}，"
             f"合计{'增持' if s['net_shares'] >= 0 else '减持'} {abs(s['net_shares']) / 1e6:,.1f}M 股（持仓截至 {s['period'][5:]}，最晚 {s['filed_last'][5:]} 报送，"
             f"距今 {((today or date.today()) - _d(s['period'])).days} 天）",
             f"{sym} quarterly holdings ({len(FUNDS)} hedge funds): added {len(s['added'])}, trimmed {len(s['trimmed'])}, new {len(s['new'])}, exited {len(s['exited'])}, "
             f"net {'+' if s['net_shares'] >= 0 else '-'}{abs(s['net_shares']) / 1e6:,.1f}M shares (holdings as of {s['period'][5:]}, filed by {s['filed_last'][5:]}, "
             f"{((today or date.today()) - _d(s['period'])).days} days old)")


def _v(el, name: str) -> str:
    """Text of the first descendant called ``name`` (or of its <value> child)."""
    if el is None:
        return ""
    x = next((x for x in el.iter() if _local(x.tag) == name), None)
    if x is None:
        return ""
    v = next((y for y in x if _local(y.tag) == "value"), None)
    return ((v.text if v is not None else x.text) or "").strip()


def parse_form4(xml: bytes) -> dict:
    root = ET.fromstring(xml)
    rel = next((x for x in root.iter() if _local(x.tag) == "reportingOwnerRelationship"), None)
    yes = lambda name: _v(rel, name).lower() in ("1", "true")
    role = _v(rel, "officerTitle") or (L("董事", "director") if yes("isDirector") else L("10% 股东", "10% owner") if yes("isTenPercentOwner") else "")
    plan = _v(root, "aff10b5One").lower() in ("1", "true") or "10b5-1" in ET.tostring(root, encoding="unicode")
    tx = []
    for t in root.iter():
        if _local(t.tag) != "nonDerivativeTransaction":
            continue
        try:
            tx.append({"date": _v(t, "transactionDate")[:10], "code": _v(t, "transactionCode"), "shares": float(_v(t, "transactionShares") or 0),
                       "price": float(_v(t, "transactionPricePerShare") or 0), "ad": _v(t, "transactionAcquiredDisposedCode")})
        except ValueError:
            continue
    return {"owner": _v(root, "rptOwnerName"), "role": role, "plan": plan, "tx": tx}


def parse_holder(doc: bytes) -> dict:
    """Best effort on a Schedule 13D / 13G: filer name, percent of class, event date."""
    s = doc.decode("utf-8", "replace")
    out = {}
    m = re.search(r"<(?:\w+:)?(?:reportingPersonName|nameOfReportingPerson)>\s*([^<]+)<", s)
    if m:
        out["filer"] = m.group(1).strip()
    m = re.search(r"<(?:\w+:)?(?:percentOfClass|classPercent|percentOfClassRepresented)>\s*([\d.]+)", s)
    if not m:
        plain = re.sub(r"<[^>]+>", " ", s)
        m = re.search(r"(?i)percent of class represented[^%]{0,200}?([\d]{1,2}\.?\d*)\s*%", plain)
        if not out.get("filer"):
            n = re.search(r"(?i)names? of reporting persons?\.?\s*(?:I\.R\.S\.[^\n]{0,80})?\s+([A-Z][A-Za-z0-9&.,' \-]{3,80})", plain)
            if n:
                out["filer"] = n.group(1).strip()
    if m:
        try:
            out["percent"] = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"<(?:\w+:)?(?:dateOfEvent|eventDateRequiresFilingThisStatement)>\s*([\d/\-]+)", s)
    if m:
        v = m.group(1)
        try:
            out["event"] = (datetime.strptime(v, "%m/%d/%Y").date() if "/" in v else date.fromisoformat(v)).isoformat()
        except ValueError:
            pass
    return out


def insider_line(sym: str, f: dict, today: date | None = None) -> str:
    t = [x for x in f["tx"] if x["code"] in CODES]
    if not t:
        return ""
    sh = sum(x["shares"] for x in t)
    usd = sum(x["shares"] * x["price"] for x in t)
    code = t[0]["code"]
    day = max(x["date"] for x in t)
    plan = L("（预先设定的 10b5-1 计划）", " (pre-set 10b5-1 plan)") if f.get("plan") else ""
    return L(f"{sym} {f['owner']}（{f['role'] or '内部人'}）{CODES[code]} {sh:,.0f} 股，约 ${usd / 1e6:,.2f}M{plan}；交易日 {day[5:]}，{f['filed'][5:]} 报送，距今 {((today or date.today()) - _d(day)).days} 天",
             f"{sym} {f['owner']} ({f['role'] or 'insider'}) {CODES[code]} of {sh:,.0f} shares, about ${usd / 1e6:,.2f}M{plan}; traded {day[5:]}, "
             f"filed {f['filed'][5:]}, {((today or date.today()) - _d(day)).days} days old")


def holder_line(sym: str, h: dict, today: date | None = None) -> str:
    kind = L("13D（可能谋求影响公司）", "13D (may seek influence)") if "13D" in h["form"] else L("13G（被动持股）", "13G (passive)")
    who = h.get("filer") or L("（名称见原文）", "(name in the filing)")
    pct = L(f"，持股 {h['percent']:.1f}%", f", {h['percent']:.1f}%") if h.get("percent") is not None else ""
    ev = L(f"，触发日 {h['event'][5:]}", f", event {h['event'][5:]}") if h.get("event") else ""
    amend = L("（修订）", " (amendment)") if h["form"].endswith("/A") else ""
    return L(f"{sym} {who} 报送 {kind}{amend}{pct}{ev}；{h['filed'][5:]} 报送，距今 {((today or date.today()) - _d(h['filed'])).days} 天",
             f"{sym} {who} filed {kind}{amend}{pct}{ev}; filed {h['filed'][5:]}, {((today or date.today()) - _d(h['filed'])).days} days old")


def delays_text() -> list[str]:
    return [L("CFTC 期货持仓：周二收盘的仓位，周五下午公布（遇联邦假日顺延），拿到时已是 3 天前的数据，下次公布前最多 10 天。",
              "CFTC futures positions: Tuesday's close, published Friday afternoon (later after federal holidays): 3 days old on arrival, up to 10 days before the next."),
            L("FINRA 每日卖空成交：当天收盘后的晚上公布，晚 0–1 天；只含场外成交，且包括做市商为接单做的卖空。",
              "FINRA daily short volume: published the same evening, 0-1 days late; off-exchange volume only, including market makers' short sales made to fill orders."),
            L("FINRA 空头仓位：每月两次（月中、月底结算日），约 7 个交易日后公布，拿到时已晚 1–2 周。",
              "FINRA short interest: twice a month (mid-month and month-end settlement), published about 7 trading days later: 1-2 weeks old on arrival."),
            L("13F 季度持仓：季末持仓，季末后最多 45 天报送，拿到时晚 6–19 周；只有多头，不报空头。",
              "13F holdings: quarter-end holdings filed up to 45 days later: 6-19 weeks old on arrival; long positions only, shorts are not reported."),
            L("Form 4 内部人交易：交易后 2 个工作日内报送。", "Form 4 insider trades: filed within 2 business days of the trade."),
            L("13D / 13G 举牌：13D 越过 5% 后 5 个工作日内；13G 最晚季末后 45 天。",
              "13D / 13G: 13D within 5 business days of crossing 5 %; 13G up to 45 days after the quarter end.")]
