"""Robinhood via the OFFICIAL account-activity export, CSV or XLSX (no credentials, no API).

In Robinhood: Account -> Reports and statements -> Reports -> generate an activity report
(CSV) and drop the file into ``data/imports/robinhood/``. The report has the columns
  Activity Date, Process Date, Settle Date, Instrument, Description, Trans Code,
  Quantity, Price, Amount
Positions are rebuilt from the trades with the average-cost method, so they are only
correct if the report covers the account's whole history.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

from ..i18n import L
from ..models import BrokerData, Position, Trade
from .base import AdapterUnavailable, BrokerAdapter

_NY = ZoneInfo("America/New_York")
_BUY, _SELL, _SPLIT = {"BUY"}, {"SELL"}, {"SPL", "SPR"}


def _money(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    s = (s or "").strip()
    if not s:
        return 0.0
    if re.fullmatch(r"-?\d+(\.\d+)?[eE][-+]?\d+", s):       # xlsx stores tiny fractions as 3.484E-4
        return float(s)
    neg = s.startswith("(") or s.startswith("-")
    s = re.sub(r"[^0-9.]", "", s)
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


def _date(s: str) -> float:
    s = str(s or "").strip()
    if re.fullmatch(r"\d{5}(\.\d+)?", s):                  # Excel serial date (xlsx export)
        day = datetime(1899, 12, 30) + timedelta(days=int(float(s)))
        return day.replace(hour=12, tzinfo=_NY).timestamp()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(hour=12, tzinfo=_NY).timestamp()
        except ValueError:
            continue
    return 0.0


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    """First worksheet of an .xlsx as header-keyed rows (standard library only, read-only)."""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
        sheet = next((n for n in sorted(z.namelist()) if n.startswith("xl/worksheets/sheet")), None)
        if sheet is None:
            return []
        table: list[dict[int, str]] = []
        for row in ET.fromstring(z.read(sheet)).iter(f"{_NS}row"):
            cells: dict[int, str] = {}
            for c in row.iter(f"{_NS}c"):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r", "A")).group():
                    col = col * 26 + ord(ch) - 64
                v = c.find(f"{_NS}v")
                if c.get("t") == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
                elif v is None or v.text is None:
                    continue
                elif c.get("t") == "s":
                    val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                else:
                    val = v.text
                cells[col] = val
            table.append(cells)
    if not table:
        return []
    header = {i: str(h).strip() for i, h in table[0].items()}
    return [{h: r.get(i, "") for i, h in header.items()} for r in table[1:]]


def parse_activity_csv(text: str) -> tuple[list[Trade], list[tuple[float, str, float]]]:
    """Returns (trades, splits) where splits are (ts, symbol, share_delta)."""
    return parse_activity_rows(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


def parse_activity_file(path: Path) -> tuple[list[Trade], list[tuple[float, str, float]]]:
    if path.suffix.lower() == ".xlsx":
        return parse_activity_rows(read_xlsx_rows(path))
    return parse_activity_csv(path.read_text(encoding="utf-8-sig", errors="replace"))


def parse_activity_rows(reader) -> tuple[list[Trade], list[tuple[float, str, float]]]:
    trades: list[Trade] = []
    splits: list[tuple[float, str, float]] = []
    seen: Counter = Counter()
    for row in reader:
        row = {(k or "").strip(): str(v if v is not None else "").strip() for k, v in row.items()}
        code = row.get("Trans Code", "").upper()
        sym = row.get("Instrument", "").upper()
        ts = _date(row.get("Activity Date", ""))
        if not sym or not ts or not re.fullmatch(r"[A-Z0-9.\-]{1,10}", sym):
            continue
        qty = abs(_money(row.get("Quantity", "")))
        if code in _SPLIT:
            splits.append((ts, sym, _money(row.get("Quantity", ""))))
            continue
        if code not in _BUY | _SELL or qty <= 0:
            continue
        price = abs(_money(row.get("Price", "")))
        if price <= 0:
            amount = abs(_money(row.get("Amount", "")))
            price = amount / qty if amount else 0.0
        if price <= 0:
            continue
        # normalised so the same fill gets the same id from a CSV and from an XLSX export
        day = datetime.fromtimestamp(ts, _NY).strftime("%Y-%m-%d")
        key = f"{day}|{sym}|{code}|{round(qty, 6):g}|{round(price, 4):g}"
        seen[key] += 1                       # identical fills on one day stay distinct
        tid = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:16]
        trades.append(Trade(f"rhcsv:{tid}", sym, "BUY" if code in _BUY else "SELL", qty, price, ts,
                            "Robinhood"))
    trades.sort(key=lambda t: t.ts)
    return trades, splits


def positions_from_trades(trades: list[Trade], splits: list[tuple[float, str, float]] = (),
                          source: str = "Robinhood") -> list[Position]:
    """Average-cost positions. Sells that exceed the known quantity are clipped at zero."""
    events = [(t.ts, 0, t) for t in trades] + [(s[0], 1, s) for s in splits]
    events.sort(key=lambda e: (e[0], e[1]))
    qty: dict[str, float] = {}
    cost: dict[str, float] = {}
    for _, kind, ev in events:
        if kind == 1:
            _, sym, delta = ev
            if qty.get(sym, 0) > 0:
                qty[sym] = max(0.0, qty[sym] + delta)
            continue
        t: Trade = ev
        q, c = qty.get(t.symbol, 0.0), cost.get(t.symbol, 0.0)
        if t.side == "BUY":
            qty[t.symbol], cost[t.symbol] = q + t.qty, c + t.qty * t.price
        else:
            sold = min(q, t.qty)
            if q > 0:
                cost[t.symbol] = c * (1 - sold / q)
            qty[t.symbol] = q - sold
    return [Position(s, round(q, 6), cost[s] / q, source) for s, q in sorted(qty.items()) if q > 1e-6]


def oversold_symbols(trades: list[Trade]) -> list[str]:
    """Symbols sold in larger quantity than the report shows bought: their earlier buys are missing."""
    q: dict[str, float] = {}
    bad: set[str] = set()
    for t in sorted(trades, key=lambda t: t.ts):
        q[t.symbol] = q.get(t.symbol, 0.0) + (t.qty if t.side == "BUY" else -t.qty)
        if q[t.symbol] < -1e-6:
            bad.add(t.symbol)
    return sorted(bad)


def coverage_notes(trades: list[Trade]) -> list[str]:
    if not trades:
        return []
    first = datetime.fromtimestamp(trades[0].ts, _NY).strftime("%Y-%m-%d")
    last = datetime.fromtimestamp(trades[-1].ts, _NY).strftime("%Y-%m-%d")
    out = [L(f"成交记录覆盖 {first} ~ {last}", f"Trades cover {first} ~ {last}")]
    bad = oversold_symbols(trades)
    if bad:
        out.append(L(f"报告不完整：{len(bad)} 只股票卖出的数量多于报告里的买入（更早的买入不在报告内），"
                     "重建出的 Robinhood 持仓和成本不可靠。请导出从开户日开始的报告再导入",
                     f"Incomplete report: for {len(bad)} stock(s) more shares were sold than bought in the report "
                     "(earlier purchases are not in it), so the rebuilt Robinhood positions and cost are unreliable. "
                     "Export a report starting from the account opening date and import again"))
    return out


class RobinhoodCsvAdapter(BrokerAdapter):
    name = "robinhood_csv"
    label = L("Robinhood (官方报告导出)", "Robinhood (official report export)")
    notes: list[str] = []
    official = True

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def files(self) -> list[Path]:
        if not self.folder.exists():
            return []
        return sorted(f for f in self.folder.iterdir() if f.suffix.lower() in (".csv", ".xlsx"))

    def fetch(self) -> BrokerData:
        files = self.files()
        if not files:
            raise AdapterUnavailable(L("没有找到 Robinhood 活动报告（CSV / XLSX）", "No Robinhood activity report (CSV / XLSX) found"))
        by_id: dict[str, Trade] = {}
        splits: set[tuple[float, str, float]] = set()
        for f in files:
            try:
                trades, sp = parse_activity_file(f)
            except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError, ValueError):
                continue
            by_id.update({t.id: t for t in trades})
            splits.update(sp)
        trades = sorted(by_id.values(), key=lambda t: t.ts)
        self.notes = coverage_notes(trades)
        return BrokerData(positions=positions_from_trades(trades, sorted(splits)), trades=trades)
