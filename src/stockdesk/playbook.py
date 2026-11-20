"""A drawdown playbook (大盘预案): the rungs below the market, what followed each of them in the past, how the
holdings would move, and what the USER decided to do at each rung.

The program fills in the facts. The actions are the user's: they are written by hand into
``data/playbook.yaml`` (git-ignored), never suggested or pre-filled, and quoted back word for word
when a rung is reached. Nothing here places an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from . import paths
from .i18n import L
from .quant import scenario

HEADER = L("""# 大盘预案（本文件不进 git）。每一档“到了以后做什么”由你自己写；程序只在条件到达时把你写的话原样提醒你，
# 不会替你决定，也不会下单。数字是“较 52 周收盘高点回落的百分比”，以收盘价为准。
# 写的时候可以回答这几个问题（只是提纲，答案由你定）：
#   到这一档时，总仓位 / 现金比例的上限是多少？
#   哪些持仓先处理，依据是什么（跌破自己的底线价？占比超过上限？）
#   想加仓的话，需要什么条件（例如程序里的“止跌形态”成立）？每次最多多少？
#   哪些事这一档明确不做（例如不加杠杆、不摊平已经跌破底线的仓位）？
""", """# Market playbook (this file is not tracked by git). What to do at each rung is yours to write; the program only
# quotes your words back when the condition is met. It decides nothing and places no orders. The numbers are the
# fall from the 52-week closing high in per cent, judged on closing prices.
# Questions that a rung's entry can answer (an outline only; the answers are yours):
#   At this rung, what is the cap on total exposure / the cash share?
#   Which holdings are dealt with first, and on what grounds (under their own bottom line? above their cap?)
#   If adding, what must be true first (for example the program's stop-falling pattern)? How much at most each time?
#   What is explicitly not done at this rung (no leverage, no averaging down below a bottom line ...)?
""")


def path() -> Path:
    return paths.DATA_DIR / "playbook.yaml"


def load(p: Path | None = None) -> dict:
    """{"rules": str, "<symbol>": {rung: text}}; empty when there is no file or it cannot be read."""
    p = p or path()
    if not p.exists():
        return {}
    try:
        raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("playbook") or {}
    except (yaml.YAMLError, AttributeError, OSError):
        return {}
    out: dict = {"rules": str(raw.get("rules") or "").strip()}
    for sym, rungs in raw.items():
        if sym == "rules" or not isinstance(rungs, dict):
            continue
        row = {}
        for k, v in rungs.items():
            try:
                row[int(float(k))] = str(v or "").strip()
            except (TypeError, ValueError):
                continue
        out[str(sym)] = row
    return out


def ensure_template(symbols: list[str], p: Path | None = None, rungs=(10, 15, 20, 25, 30)) -> bool:
    """Create the file with empty entries when it does not exist. Never overwrites and never fills an action in."""
    p = p or path()
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"playbook": {"rules": "", **{s: {int(r): "" for r in rungs} for s in symbols}}}
    p.write_text(HEADER + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding="utf-8")
    return True


def action(book: dict, symbol: str, rung: float) -> str:
    return str((book.get(symbol) or {}).get(int(rung), "") or "").strip()


@dataclass
class Rung:
    rung: int
    level: float
    gap_pct: float                   # the level against the price, in % (negative = below)
    reached: pd.Timestamp | None = None      # close at or under it in the open episode
    touched: bool = False            # the price is under it right now (possibly intraday only)
    stat: scenario.RungStat | None = None
    action: str = ""


@dataclass
class IndexBook:
    symbol: str
    name: str
    price: float
    peak: float
    peak_date: pd.Timestamp | None
    depth_pct: float
    live: bool = False
    episodes: int = 0
    since: pd.Timestamp | None = None
    rungs: list[Rung] = field(default_factory=list)


@dataclass
class Playbook:
    indices: list[IndexBook] = field(default_factory=list)
    exposures: list[scenario.Exposure] = field(default_factory=list)
    stress: list[tuple] = field(default_factory=list)      # [(rung, index move, holdings' estimated move, [(symbol, move, price)])]
    stress_index: str = ""
    rules: str = ""
    lines: dict = field(default_factory=dict)              # symbol -> {name: level}: the user's own plan lines
    path: str = ""
    exists: bool = False


def build(states: dict, studies: dict, book: dict, views=None, index_bars: pd.DataFrame | None = None,
          plan_lines: dict | None = None) -> Playbook:
    """``states``: symbol -> keylevels.IndexState; ``studies``: symbol -> scenario.Study."""
    pb = Playbook(rules=book.get("rules", ""), path=str(path()), exists=path().exists(), lines=plan_lines or {})
    for sym, st in states.items():
        peak = float(st.ind.get("high252", float("nan")))
        if not (peak == peak and peak > 0):
            continue
        sd = studies.get(sym) or scenario.Study()
        ib = IndexBook(sym, st.name, st.price, peak, st.ind.get("high252_date"), (st.price / peak - 1) * 100, st.live, sd.n, sd.since)
        for r in scenario.ladder(ib.depth_pct):
            level = peak * (1 - r / 100.0)
            when = sd.dates.get(r)
            if st.live and when is not None and when == st.asof:
                when = None                          # today's bar is not a close yet: touched, not reached
            ib.rungs.append(Rung(r, level, (level / st.price - 1) * 100, when, st.price <= level, sd.rungs.get(r), action(book, sym, r)))
        pb.indices.append(ib)
    if pb.indices and views is not None:
        first = pb.indices[0]
        pb.stress_index = first.name
        pb.exposures = scenario.exposures(views, index_bars)
        for rg in first.rungs:
            if rg.level >= first.price:
                continue
            move = rg.level / first.price - 1
            total, rows = scenario.stress(pb.exposures, move)
            pb.stress.append((rg.rung, move, total, rows))
    return pb


def rung_lines(ib: IndexBook, rung: float, book_exists: bool = True) -> list[str]:
    """For an alert: what the user wrote for this rung, and what the record says about it."""
    rg = next((x for x in ib.rungs if x.rung == int(rung)), None)
    out = []
    text = rg.action if rg is not None else ""
    if text:
        out.append(L(f"· 回落 {int(rung)}% 这一档，你在预案里写的是：{text}", f"· For the {int(rung)}% rung your playbook says: {text}"))
    else:
        out.append(L(f"· 回落 {int(rung)}% 这一档，你还没有写预案（分析中心 → 预案 页，或直接编辑 data/playbook.yaml）。",
                     f"· You have not written anything for the {int(rung)}% rung yet (Analysis hub → Playbook, or edit data/playbook.yaml)."))
    st = rg.stat if rg is not None else None
    if st is not None and st.n:
        nxt = (L(f"其中 {st.deeper * 100:.0f}% 继续跌到了 −{st.next_rung}%", f"{st.deeper * 100:.0f}% of them went on to −{st.next_rung}%")
               if st.deeper is not None else "")
        more = (L(f"；从到达这一档那天算，到最低点又跌的中位数是 {st.more[0]:+.1f}%（最差的一成超过 {st.more[1]:+.1f}%），中位数用了 {st.to_low:.0f} 个交易日",
                  f"; from the day the rung was reached, the further fall to the low was {st.more[0]:+.1f}% at the median (worst tenth beyond {st.more[1]:+.1f}%), "
                  f"taking a median of {st.to_low:.0f} sessions") if st.more else "")
        out.append(L(f"· 历史上（{ib.since:%Y} 年以来）{ib.name}到过这一档 {st.n} 次，{nxt}{more}",
                     f"· In the past (since {ib.since:%Y}) the {ib.name} reached this rung {st.n} time(s); {nxt}{more}"))
        if 252 in st.fwd and 63 in st.fwd:
            a, b = st.fwd[63], st.fwd[252]
            out.append(L(f"· 到达这一档之后 3 个月 / 12 个月的涨跌中位数 {a[1] * 100:+.1f}% / {b[1] * 100:+.1f}%，上涨的比例 {a[2] * 100:.0f}% / {b[2] * 100:.0f}%，"
                         f"最差一成低于 {a[3] * 100:+.1f}% / {b[3] * 100:+.1f}%。样本只有 {st.n} 次，彼此差别很大。",
                         f"· After the rung was reached: median change over 3 / 12 months {a[1] * 100:+.1f}% / {b[1] * 100:+.1f}%, share of rises "
                         f"{a[2] * 100:.0f}% / {b[2] * 100:.0f}%, worst tenth below {a[3] * 100:+.1f}% / {b[3] * 100:+.1f}%. Only {st.n} cases, and they differ widely."))
    return out
