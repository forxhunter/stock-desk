"""Local SQLite cache: price bars, key/value JSON, trades, news, alerts, watchlist events.

A new connection is opened per operation so the store can be used from any thread.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .models import Alert, NewsItem, Trade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(
  symbol TEXT, interval TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
  PRIMARY KEY(symbol, interval, ts));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS trades(
  id TEXT PRIMARY KEY, source TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
  ts REAL, fees REAL, demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS news(
  id TEXT PRIMARY KEY, source TEXT, title TEXT, summary TEXT, link TEXT,
  published REAL, tickers TEXT);
CREATE TABLE IF NOT EXISTS alerts(
  id TEXT PRIMARY KEY, kind TEXT, symbol TEXT, severity TEXT, title TEXT, body TEXT,
  data TEXT, created REAL, snoozed_until REAL DEFAULT 0, dismissed INTEGER DEFAULT 0,
  notified INTEGER DEFAULT 0, demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS wl_events(symbol TEXT, action TEXT, ts REAL, price REAL);
CREATE TABLE IF NOT EXISTS congress_docs(
  id TEXT PRIMARY KEY, chamber TEXT, member TEXT, state TEXT, filed TEXT, url TEXT, status TEXT, n INTEGER, seen REAL);
CREATE TABLE IF NOT EXISTS congress_trades(id TEXT PRIMARY KEY, doc TEXT, filed TEXT, traded TEXT, ticker TEXT, data TEXT);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        if self._mem is not None:
            yield self._mem
            self._mem.commit()
            return
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- key/value -------------------------------------------------------
    def set_meta(self, key: str, value: Any, updated: float | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (key, json.dumps(value), time.time() if updated is None else updated))

    def get_meta(self, key: str, default: Any = None, max_age: float | None = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value, updated FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        if max_age is not None and time.time() - row[1] > max_age:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def meta_age(self, key: str) -> float | None:
        with self._conn() as c:
            row = c.execute("SELECT updated FROM meta WHERE key=?", (key,)).fetchone()
        return None if not row else time.time() - row[0]

    # ---- bars ------------------------------------------------------------
    def save_bars(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        ts = idx.as_unit("s").asi8
        rows = list(zip([symbol] * len(df), [interval] * len(df), [int(t) for t in ts],
                        df["open"].astype(float), df["high"].astype(float), df["low"].astype(float),
                        df["close"].astype(float), df["volume"].astype(float)))
        with self._conn() as c:
            # adjusted history can be restated (splits/dividends): replace the whole series
            c.execute("DELETE FROM bars WHERE symbol=? AND interval=?", (symbol, interval))
            c.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?)", rows)

    def load_bars(self, symbol: str, interval: str) -> pd.DataFrame:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts,o,h,l,c,v FROM bars WHERE symbol=? AND interval=? ORDER BY ts", (symbol, interval)
            ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df.pop("ts"), unit="s")
        df.index.name = None
        return df

    # ---- trades ----------------------------------------------------------
    def upsert_trades(self, trades: Iterable[Trade]) -> int:
        n = 0
        with self._conn() as c:
            for t in trades:
                cur = c.execute(
                    "INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
                    (t.id, t.source, t.symbol, t.side, t.qty, t.price, t.ts, t.fees, int(t.demo)))
                n += cur.rowcount
        return n

    def prune_trades(self, id_prefix: str, keep_ids: Iterable[str]) -> int:
        """Drop trades of a file-based source that are no longer in its files (ids start with ``id_prefix``)."""
        keep = set(keep_ids)
        with self._conn() as c:
            old = [r[0] for r in c.execute("SELECT id FROM trades WHERE id LIKE ?", (id_prefix + "%",))]
            gone = [i for i in old if i not in keep]
            c.executemany("DELETE FROM trades WHERE id=?", [(i,) for i in gone])
        return len(gone)

    def load_trades(self, include_demo: bool = True) -> list[Trade]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,symbol,side,qty,price,ts,source,fees,demo FROM trades ORDER BY ts").fetchall()
        out = [Trade(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], bool(r[8])) for r in rows]
        return out if include_demo else [t for t in out if not t.demo]

    def delete_demo_trades(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM trades WHERE demo=1")
            c.execute("DELETE FROM alerts WHERE demo=1")

    # ---- news ------------------------------------------------------------
    def upsert_news(self, items: Iterable[NewsItem]) -> None:
        with self._conn() as c:
            for n in items:
                c.execute("INSERT OR REPLACE INTO news VALUES(?,?,?,?,?,?,?)",
                          (n.id, n.source, n.title, n.summary, n.link, n.published, json.dumps(n.tickers)))

    def load_news(self, limit: int = 60) -> list[NewsItem]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,source,title,summary,link,published,tickers FROM news "
                "ORDER BY published DESC LIMIT ?", (limit,)).fetchall()
        return [NewsItem(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6] or "[]")) for r in rows]

    # ---- alerts ----------------------------------------------------------
    def add_alerts(self, alerts: Iterable[Alert]) -> list[Alert]:
        """Insert alerts that are not known yet; returns the newly inserted ones."""
        new: list[Alert] = []
        now = time.time()
        with self._conn() as c:
            for a in alerts:
                if c.execute("SELECT 1 FROM alerts WHERE id=?", (a.id,)).fetchone():
                    # keep the text fresh for still-open alerts
                    c.execute("UPDATE alerts SET title=?, body=?, data=?, severity=? WHERE id=?",
                              (a.title, a.body, json.dumps(a.data, default=str), a.severity, a.id))
                    continue
                snooze = self._kind_snooze(c, a.kind, a.symbol)
                a.created = a.created or now
                a.snoozed_until = max(a.snoozed_until, snooze)
                c.execute("INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (a.id, a.kind, a.symbol, a.severity, a.title, a.body,
                           json.dumps(a.data, default=str), a.created, a.snoozed_until, 0, 0, int(a.demo)))
                new.append(a)
        return new

    @staticmethod
    def _kind_snooze(c, kind: str, symbol: str) -> float:
        row = c.execute("SELECT value FROM meta WHERE key=?", (f"snooze:{kind}:{symbol}",)).fetchone()
        try:
            return float(json.loads(row[0])) if row else 0.0
        except (ValueError, TypeError):
            return 0.0

    def load_alerts(self, include_hidden: bool = False) -> list[Alert]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,kind,symbol,severity,title,body,data,created,snoozed_until,dismissed,notified,demo "
                "FROM alerts ORDER BY created DESC").fetchall()
        out = [Alert(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6] or "{}"), r[7], r[8],
                     bool(r[9]), bool(r[10]), bool(r[11])) for r in rows]
        if include_hidden:
            return out
        now = time.time()
        return [a for a in out if not a.dismissed and a.snoozed_until <= now]

    def snooze_alert(self, alert_id: str, days: float) -> None:
        until = time.time() + days * 86400
        with self._conn() as c:
            row = c.execute("SELECT kind, symbol FROM alerts WHERE id=?", (alert_id,)).fetchone()
            c.execute("UPDATE alerts SET snoozed_until=? WHERE id=?", (until, alert_id))
            if row:  # also mute new alerts of the same kind for the same symbol
                c.execute("INSERT OR REPLACE INTO meta VALUES(?,?,?)",
                          (f"snooze:{row[0]}:{row[1]}", json.dumps(until), time.time()))

    def dismiss_alert(self, alert_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE alerts SET dismissed=1 WHERE id=?", (alert_id,))

    def mark_notified(self, ids: Iterable[str]) -> None:
        with self._conn() as c:
            c.executemany("UPDATE alerts SET notified=1 WHERE id=?", [(i,) for i in ids])

    # ---- disclosures by members of Congress ------------------------------
    def congress_known(self) -> dict[str, str]:
        """document key ("house:20036844") -> status ("ok" | "scan" | "error")."""
        with self._conn() as c:
            return {r[0]: r[1] for r in c.execute("SELECT id, status FROM congress_docs")}

    def save_congress_doc(self, filing, status: str, trades: list) -> None:
        key = f"{filing.chamber}:{filing.doc_id}"
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO congress_docs VALUES(?,?,?,?,?,?,?,?,?)",
                      (key, filing.chamber, filing.member, filing.state, filing.filed, filing.url, status, len(trades), time.time()))
            c.execute("DELETE FROM congress_trades WHERE doc=?", (key,))
            c.executemany("INSERT OR REPLACE INTO congress_trades VALUES(?,?,?,?,?,?)",
                          [(t.id, key, t.filed, t.traded, t.ticker, json.dumps(t.to_dict())) for t in trades])

    def clear_congress(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM congress_docs")
            c.execute("DELETE FROM congress_trades")
            c.execute("DELETE FROM meta WHERE key LIKE 'congress:todo:%' OR key LIKE 'congress:index:%'")

    def load_congress_trades(self, since: str = "") -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT data FROM congress_trades WHERE filed >= ? ORDER BY filed DESC, id", (since,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def congress_docs(self, since: str = "") -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, chamber, member, state, filed, url, status, n, seen FROM congress_docs "
                             "WHERE filed >= ? ORDER BY filed DESC", (since,)).fetchall()
        return [dict(zip(("id", "chamber", "member", "state", "filed", "url", "status", "n", "seen"), r)) for r in rows]

    # ---- watchlist events -----------------------------------------------
    def add_wl_event(self, symbol: str, action: str, price: float | None = None, ts: float | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO wl_events VALUES(?,?,?,?)", (symbol, action, ts or time.time(), price))

    def load_wl_events(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT symbol, action, ts, price FROM wl_events ORDER BY ts").fetchall()
        return [dict(symbol=r[0], action=r[1], ts=r[2], price=r[3]) for r in rows]


def trade_to_dict(t: Trade) -> dict:
    return asdict(t)
