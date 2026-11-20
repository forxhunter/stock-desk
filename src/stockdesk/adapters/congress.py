"""Stock trades that members of Congress disclose under the STOCK Act (official public records, read-only).

House of Representatives: the Clerk publishes one index per year (``<year>FD.zip`` with every
filing) and each Periodic Transaction Report as a PDF. Electronically filed reports are text and
are parsed here; scanned paper reports cannot be read and are only counted and linked.

Senate: the Secretary of the Senate's eFD site (efdsearch.senate.gov). It only answers a search after
its agreement form ("I understand the prohibitions on obtaining and use of financial disclosure
reports") has been submitted, so it is OFF until the user has read that page and set
``congress.senate.agree_terms: true`` themselves. Reports filed electronically are HTML tables; paper
filings are images and are only counted and linked.

What a report gives: who filed, the asset (with ticker and an asset-type code such as ``ST`` for
stock), purchase / sale / exchange, the transaction date, the date the member was notified, an
amount RANGE (never the exact size) and the owner (self, spouse, joint, dependent child). The law
allows up to 45 days between a trade and its disclosure.

Use of these reports is restricted by 5 U.S.C. app. 105(c): no commercial use (other than by news
media), no use for credit ratings or for soliciting money.
"""
from __future__ import annotations

import html
import http.cookiejar
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime

from ..i18n import L
from .base import USER_AGENT, AdapterError, AdapterUnavailable, HttpGet, RateLimited, http_get

HOUSE = "https://disclosures-clerk.house.gov/public_disc"
SENATE = "https://efdsearch.senate.gov"
PARSER = 1                           # raise when parse_house_ptr changes what it returns: stored reports are then re-read

_CORE = re.compile(r"(?<![A-Za-z])(?P<type>P|S \(partial\)|S|E)\s+(?P<traded>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})"
                   r"\s*(?P<amount>.*)$")
_TICKER = re.compile(r"\(([A-Z][A-Z0-9./\-]{0,7})\)\s*\[([A-Z]{2})\]")
_KIND = re.compile(r"\[([A-Z]{2})\]")
_META = re.compile(r"^[A-Z](?:\s{2,}[A-Z])*\s*:")           # "F      S     : New", "S          O : ...", "D          : ..."
_MONEY = re.compile(r"\$([\d,]+)")
_OWNERS = {"SP": "spouse", "JT": "joint", "DC": "child"}
_CTRL = re.compile("[" + chr(0) + "-" + chr(8) + chr(11) + "-" + chr(31) + chr(160) + "]")


@dataclass
class Filing:
    chamber: str                     # "house" | "senate"
    doc_id: str
    member: str
    state: str                       # "NJ07" (House: state + district) or "NJ"
    filed: str                       # ISO date the report was filed
    year: int
    url: str = ""


@dataclass
class CongressTrade:
    id: str                          # stable: chamber, document, row number
    chamber: str
    doc_id: str
    member: str
    state: str
    filed: str                       # ISO
    traded: str                      # ISO
    notified: str                    # ISO, "" when not given
    ticker: str                      # "" when the asset has none
    asset: str
    asset_type: str                  # "ST" stock, "OP" option, "GS" government security, ...
    side: str                        # "buy" | "sell" | "exchange" (options: bought calls = buy, bought puts = sell,
    #                                  anything else = exchange, i.e. listed but not judged)
    partial: bool = False
    owner: str = "self"              # "self" | "spouse" | "joint" | "child"
    lo: float = 0.0                  # amount range in USD (hi = 0: open-ended)
    hi: float = 0.0
    url: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(mdy: str) -> str:
    try:
        return datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def house_index(year: int, getter: HttpGet = http_get) -> list[Filing]:
    """Every Periodic Transaction Report filed in ``year`` (newest first)."""
    status, _, body = getter(f"{HOUSE}/financial-pdfs/{int(year)}FD.zip", timeout=40)
    if status != 200:
        raise AdapterError(f"HTTP {status}")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            root = ET.fromstring(z.read(name))
    except (zipfile.BadZipFile, StopIteration, ET.ParseError):
        raise AdapterError(L("众议院披露索引的格式无法识别", "The House disclosure index is not in the expected format")) from None
    out = []
    for m in root:
        f = {c.tag: (c.text or "").strip() for c in m}
        if f.get("FilingType") != "P" or not f.get("DocID"):
            continue
        name = " ".join(x for x in (f.get("First"), f.get("Last"), f.get("Suffix")) if x)
        filed = _iso(f.get("FilingDate", ""))
        if not filed:
            continue
        out.append(Filing("house", f["DocID"], name, f.get("StateDst", ""), filed, int(year),
                          f"{HOUSE}/ptr-pdfs/{int(year)}/{f['DocID']}.pdf"))
    out.sort(key=lambda x: (x.filed, x.doc_id), reverse=True)
    return out


def pdf_text(data: bytes) -> str:
    try:
        import pypdf
    except ImportError as e:  # pragma: no cover
        raise AdapterUnavailable(L("未安装 pypdf（读取披露 PDF 需要）", "pypdf is not installed (needed to read the disclosure PDFs)")) from e
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:                       # a damaged or unusual file: treat as unreadable
        raise AdapterError(L(f"PDF 无法读取：{type(e).__name__}", f"The PDF cannot be read: {type(e).__name__}")) from None


def _amount(text: str) -> tuple[float, float]:
    nums = [float(x.replace(",", "")) for x in _MONEY.findall(text)]
    if not nums:
        return 0.0, 0.0
    if len(nums) == 1:
        return (nums[0], 0.0) if "over" in text.lower() else (nums[0], nums[0])
    return nums[0], nums[1]


def parse_house_ptr(text: str, filing: Filing) -> list[CongressTrade]:
    """The transactions in the text of one electronically filed House report."""
    lines = [_CTRL.sub(" ", ln).strip() for ln in text.splitlines()]
    out: list[CongressTrade] = []
    for i, line in enumerate(lines):
        m = _CORE.search(line)
        if m is None:
            continue
        # the asset: what precedes the type on this line plus the lines above, up to the previous block
        asset = [line[:m.start()].strip()]
        j = i - 1
        while j >= 0 and len(asset) < 6:
            prev = lines[j]
            if not prev or _META.match(prev) or prev.startswith(("Filing ID", "$200?", "Gains >", "Amount Cap", "* For the")) \
                    or _CORE.search(prev) or re.match(r"^\$[\d,]+$", prev):
                break
            asset.insert(0, prev)
            j -= 1
        name = re.sub(r"^\d{6,}\s+", "", " ".join(x for x in asset if x).strip())
        owner = "self"
        marks = list(re.finditer(r"(?:^|\s)(SP|JT|DC)\s+(?=[A-Z0-9])", name))
        if marks:                                # also drops a wrapped description line of the row above
            owner, name = _OWNERS[marks[-1].group(1)], name[marks[-1].end():].strip()
        amount = m.group("amount").strip()
        if (amount.endswith("-") or not amount) and i + 1 < len(lines) and lines[i + 1].startswith("$"):
            amount = f"{amount} {lines[i + 1]}"
        lo, hi = _amount(amount)
        tk = (list(_TICKER.finditer(name)) or [None])[-1]
        kind = tk.group(2) if tk else (_KIND.findall(name) or [""])[-1]
        note = ""
        for k in range(i + 1, min(i + 6, len(lines))):
            if _CORE.search(lines[k]):
                break
            d = re.match(r"^D\s{2,}:\s*(.+)$", lines[k])
            if d:
                note = d.group(1).strip()[:200]
                break
        typ = m.group("type")
        traded = _iso(m.group("traded"))
        if not traded:
            continue
        side = "buy" if typ == "P" else "exchange" if typ == "E" else "sell"
        if kind == "OP":                         # an option: the direction is that of the bet, when it can be told
            what = f"{name} {note}".lower()
            side = ("buy" if "call" in what else "sell" if "put" in what else "exchange") if typ == "P" else "exchange"
        out.append(CongressTrade(
            f"house:{filing.doc_id}:{len(out)}", "house", filing.doc_id, filing.member, filing.state, filing.filed, traded,
            _iso(m.group("notified")), tk.group(1).upper() if tk else "", re.sub(r"\s*\[[A-Z]{2}\]\s*$", "", name)[:120], kind,
            side, typ == "S (partial)", owner, lo, hi, filing.url, note))
    return out


def house_trades(filing: Filing, getter: HttpGet = http_get) -> list[CongressTrade] | None:
    """Transactions of one House report; ``None`` when it is a scanned paper filing that cannot be read."""
    status, _, body = getter(filing.url, timeout=40)
    if status != 200:
        raise AdapterError(f"HTTP {status}")
    text = pdf_text(body)
    if len(text.strip()) < 40:                   # a scan: pages are images
        return None
    return parse_house_ptr(text, filing)


def years(back: int, today: date | None = None) -> list[int]:
    t = today or date.today()
    return [t.year - k for k in range(max(1, int(back)))]


# ---- Senate (eFD) -------------------------------------------------------------------------------------

_ROW = re.compile(r"(?s)<tr[^>]*>(.*?)</tr>")
_CELL = re.compile(r"(?s)<td[^>]*>(.*?)</td>")
_KINDS = {"stock": "ST", "stock option": "OP", "corporate bond": "CS", "municipal security": "GS"}
_SIDES = {"purchase": ("buy", False), "sale (full)": ("sell", False), "sale (partial)": ("sell", True), "sale": ("sell", False)}


def _cell(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))).strip()


def parse_senate_ptr(page: str, filing: Filing) -> list[CongressTrade] | None:
    """The transactions in one electronically filed Senate report; ``None`` when the page has no table (a paper filing)."""
    start = page.find("<tbody")
    if start < 0:
        return None
    out: list[CongressTrade] = []
    for row in _ROW.findall(page[start:]):
        c = [_cell(x) for x in _CELL.findall(row)]
        if len(c) < 8:
            continue
        num, traded, owner, ticker, asset, kind, typ, amount = c[:8]
        note = c[8] if len(c) > 8 and c[8] != "--" else ""
        when = _iso(traded)
        if not when:
            continue
        code = _KINDS.get(kind.lower(), "OT")
        side, partial = _SIDES.get(typ.lower(), ("exchange", False))
        if code == "OP":
            what = f"{asset} {note}".lower()
            side = ("buy" if "call" in what else "sell" if "put" in what else "exchange") if typ.lower() == "purchase" else "exchange"
        lo, hi = _amount(amount)
        tick = "" if ticker in ("--", "") else ticker.split()[0].upper()
        out.append(CongressTrade(f"senate:{filing.doc_id}:{num or len(out)}", "senate", filing.doc_id, filing.member, filing.state,
                                 filing.filed, when, "", tick, asset[:120], code, side, partial,
                                 {"self": "self", "spouse": "spouse", "joint": "joint", "child": "child"}.get(owner.lower(), "self"),
                                 lo, hi, filing.url, note[:200]))
    return out


class SenateEfd:
    """One visit to the Senate's eFD site: submit its agreement form, list the transaction reports, read them.

    The site answers searches only after the agreement form has been submitted, and a search is a form submission
    too, so this class is the one place where the program sends a request body. It goes to this host only and carries
    nothing but the agreement tick, the site's own anti-forgery token and the search filters (report type, start date).
    """

    def __init__(self, send=None):
        self._send = send or self._http
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._token = ""
        self.agreed = False

    def _http(self, path: str, form: dict | None = None, referer: str = "", ajax: bool = False) -> tuple[int, str, str]:
        url = SENATE + path
        if urllib.parse.urlparse(url).hostname != "efdsearch.senate.gov":
            raise AdapterError(L("只允许访问参议院披露站点", "Only the Senate disclosure site may be contacted here"))
        headers = {"User-Agent": USER_AGENT}
        if referer:
            headers["Referer"] = SENATE + referer
        if ajax:
            headers.update({"X-Requested-With": "XMLHttpRequest",
                            "X-CSRFToken": next((c.value for c in self._jar if c.name == "csrftoken"), "")})
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode() if form else None, headers=headers)
        try:
            with self._opener.open(req, timeout=40) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(L("HTTP 429：请求过于频繁", "HTTP 429: too many requests")) from None
            raise AdapterError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise AdapterUnavailable(L(f"网络不可用：{type(reason).__name__}", f"Network unavailable: {type(reason).__name__}")) from None

    def agree(self) -> None:
        """Submit the agreement form (only ever called when the user has set ``congress.senate.agree_terms``)."""
        _, _, home = self._send("/search/home/")
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', home)
        if m is None:
            raise AdapterError(L("参议院披露站点的页面格式变了", "The Senate disclosure site's page has changed"))
        _, url, page = self._send("/search/home/", {"prohibition_agreement": "1", "csrfmiddlewaretoken": m.group(1)}, "/search/home/")
        if "/search/home" in url and 'id="agreement_form"' in page:
            raise AdapterError(L("参议院披露站点没有接受协议确认", "The Senate disclosure site did not accept the agreement"))
        m2 = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
        self._token = m2.group(1) if m2 else m.group(1)
        self.agreed = True

    def filings(self, since: date, page_size: int = 100, max_pages: int = 30) -> list[Filing]:
        """Periodic Transaction Reports of senators filed since ``since`` (newest first)."""
        if not self.agreed:
            self.agree()
        out: list[Filing] = []
        for k in range(max_pages):
            form = {"start": str(k * page_size), "length": str(page_size), "report_types": "[11]", "filer_types": "[1]",
                    "submitted_start_date": f"{since:%m/%d/%Y} 00:00:00", "submitted_end_date": "", "candidate_state": "",
                    "senator_state": "", "office_id": "", "first_name": "", "last_name": "", "csrfmiddlewaretoken": self._token,
                    "draw": str(k + 1), "order[0][column]": "4", "order[0][dir]": "desc"}
            _, _, body = self._send("/search/report/data/", form, "/search/", True)
            try:
                data = json.loads(body)
                rows = data["data"]
            except (ValueError, KeyError, TypeError):
                raise AdapterError(L("参议院披露站点返回的不是预期的列表", "The Senate disclosure site did not return the expected list")) from None
            for r in rows:
                link = re.search(r'href="(/search/view/(ptr|paper)/([0-9a-f\-]+)/)"', str(r[3]) if len(r) > 3 else "")
                filed = _iso(str(r[4])) if len(r) > 4 else ""
                if link is None or not filed:
                    continue
                name = " ".join(x for x in (_cell(str(r[0])), _cell(str(r[1]))) if x)
                out.append(Filing("senate", link.group(3), name, "", filed, int(filed[:4]), SENATE + link.group(1)))
            if len(rows) < page_size or len(out) >= int(data.get("recordsTotal") or 0):
                break
        out.sort(key=lambda x: (x.filed, x.doc_id), reverse=True)
        return out

    def trades(self, filing: Filing) -> list[CongressTrade] | None:
        """Transactions of one Senate report; ``None`` for a paper filing (scanned images)."""
        if "/paper/" in filing.url:
            return None
        if not self.agreed:
            self.agree()
        _, _, page = self._send(filing.url[len(SENATE):], None, "/search/")
        return parse_senate_ptr(page, filing)
