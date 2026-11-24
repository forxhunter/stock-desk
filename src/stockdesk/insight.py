"""Alert write-ups: a concrete, sectioned analysis instead of a one-line notice.

Every alert that leaves the program (panel, toast, e-mail) is built from the same blocks: what
happened (with the numbers), where the price stands now, the next key levels, what followed the
same event in the past, what it means for the watched stocks, and what to check next. All of it is
computed from prices by fixed rules: it describes and counts, it does not predict and it is not a
recommendation. Nothing here mentions holdings, amounts or P&L, so the text can go into an e-mail.
"""
from __future__ import annotations

import re

import pandas as pd

from .i18n import L
from .quant import keylevels as kl
from .quant.analysis import compute_indicators
from .quant.levels import support_zones

FOOT = L("这是按固定规则判断的价位事件和它在历史上的统计，不是预测，也不是买卖建议。",
         "This is a price-level event judged by fixed rules, with its historical record; it is not a prediction "
         "and not a recommendation to buy or sell.")


_SP1 = re.compile(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9^])")
_SP2 = re.compile(r"(?<=[A-Za-z0-9%])(?=[\u4e00-\u9fff])")


def tidy(text: str) -> str:
    """Chinese typography: a space where a Chinese character meets a number or a Latin word."""
    return _SP2.sub(" ", _SP1.sub(" ", text))


def num(x: float | None) -> str:
    if x is None or x != x:
        return "—"
    return f"{x:,.0f}" if abs(x) >= 2000 else f"{x:,.2f}"


def gap(level: float, px: float) -> str:
    """Where ``level`` is relative to the price, e.g. "-3.2%" = the level is 3.2 % below the price."""
    return f"{(level / px - 1) * 100:+.1f}%" if px else "—"


def head(zh: str, en: str) -> str:
    return L(f"【{zh}】", f"[{en}]")


def is_head(line: str) -> bool:
    s = line.strip()
    return (s.startswith("【") and "】" in s) or (s.startswith("[") and s.endswith("]") and len(s) > 2)


def _join(items) -> str:
    return L("、", ", ").join(items)


def _side(px: float, level: float) -> str:
    """"above by 2.1%" / "below by 3.0%": where the price is relative to a level."""
    d = (px / level - 1) * 100 if level else 0.0
    return L(f"上方 {d:.1f}%", f"{d:.1f}% above") if d >= 0 else L(f"下方 {-d:.1f}%", f"{-d:.1f}% below")


def vix_text(vix: dict | None) -> str:
    if not vix or vix.get("price") != vix.get("price"):
        return ""
    v = float(vix["price"])
    mood = (L("平静", "calm") if v < 15 else L("正常范围", "normal range") if v < 20
            else L("偏紧张", "tense") if v < 30 else L("恐慌水平", "panic level"))
    return L(f"VIX {v:.1f}（今日 {vix.get('chg', 0):+.1f}，{vix.get('pct', 0):+.1f}%）：{mood}"
             "（一般 20 以上偏紧张，30 以上是恐慌水平）",
             f"VIX {v:.1f} (today {vix.get('chg', 0):+.1f}, {vix.get('pct', 0):+.1f}%): {mood} "
             "(roughly, above 20 is tense and above 30 is panic level)")


# ---- an index and its key levels ------------------------------------------------------------------

def _the(lv: kl.Level) -> str:
    return lv.name if lv.kind == "user" else L(lv.name, f"the {lv.name}")


def label(lv: kl.Level) -> str:
    """A level with its value (zones and user levels already carry the numbers in their name)."""
    if lv.kind == "user":
        return lv.name if num(lv.value) in lv.name or f"{lv.value:g}" in lv.name else f"{lv.name} {num(lv.value)}"
    return lv.name if lv.kind == "zone" else f"{lv.name} {num(lv.value)}"


def _the_label(lv: kl.Level) -> str:
    return label(lv) if lv.kind == "user" else L(label(lv), f"the {label(lv)}")


def _rule(lv: kl.Level, direction: str, hold: int) -> str:
    if lv.kind == "ma":
        return (L(f"收盘跌破 {lv.n:g} 日均线、且此前至少 {hold} 个交易日收在上方",
                  f"a close below the {lv.n:g}-day average after at least {hold} closes above it") if direction == "down"
                else L(f"收盘站上 {lv.n:g} 日均线、且此前至少 {hold} 个交易日收在下方",
                       f"a close above the {lv.n:g}-day average after at least {hold} closes below it"))
    if lv.kind == "mom":
        return (L(f"收盘跌破 {lv.n:g} 个交易日前的收盘价（{lv.n:g} 日涨跌转负）、且此前至少 {hold} 个交易日为正",
                  f"a close under the close of {lv.n:g} sessions earlier (the {lv.n:g}-session return turning negative) after at least {hold} positive sessions")
                if direction == "down" else
                L(f"收盘站上 {lv.n:g} 个交易日前的收盘价（{lv.n:g} 日涨跌转正）、且此前至少 {hold} 个交易日为负",
                  f"a close above the close of {lv.n:g} sessions earlier (the {lv.n:g}-session return turning positive) after at least {hold} negative sessions"))
    if lv.kind == "low":
        return L(f"收盘创 {lv.n:g} 日新低、且此前至少 {hold} 个交易日没有创过",
                 f"a new {lv.n:g}-day closing low, the first in at least {hold} sessions")
    if lv.kind == "high":
        return L(f"收盘创 52 周新高、且此前至少 {hold} 个交易日没有创过",
                 f"a new 52-week closing high, the first in at least {hold} sessions")
    return L(f"收盘较 52 周高点回落达到 {lv.n:g}%、且此前至少 {hold} 个交易日没有达到",
             f"a close {lv.n:g}% or more below the 52-week high, the first in at least {hold} sessions")


def _event_line(lv: kl.Level, st: kl.IndexState, direction: str) -> str:
    px = st.price
    what = L("现价", "the price") if st.live else L("收盘", "the close")
    side = _side(px, lv.value)
    base = L(f"· {label(lv)}：{what}在其{side}", f"· {label(lv)}: {what} is {side} it")
    if lv.kind == "ma" and lv.held:
        return base + (L(f"；此前连续 {lv.held} 个交易日收在上方", f"; before this it had closed above it for {lv.held} sessions in a row")
                       if direction == "down" else
                       L(f"；此前连续 {lv.held} 个交易日收在下方", f"; before this it had closed below it for {lv.held} sessions in a row"))
    if lv.kind in ("low", "high") and lv.held:
        return base + L(f"；此前 {lv.held} 个交易日没有出现过这样的收盘", f"; no such close in the previous {lv.held} sessions")
    if lv.kind == "dd":
        return base + L(f"（52 周收盘高点 {num(st.ind['high252'])} 的 {100 - lv.n:g}%；习惯上把从高点回落 10% 叫“回调”，20% 叫“熊市”）",
                        f" ({100 - lv.n:g}% of the 52-week closing high {num(st.ind['high252'])}; by convention a 10% fall "
                        "from the high is called a correction and 20% a bear market)")
    if lv.kind == "mom":
        return base + L(f"（{lv.n:g} 个交易日前的收盘价；趋势跟踪类模型常用的动量翻转点之一，银行的 CTA 触发价是各自的模型估算，不公开，这里是按公开收盘价计算的近似）",
                        f" (the close of {lv.n:g} sessions earlier, one of the momentum flips trend-following models use; banks' CTA triggers come from their own "
                        "unpublished models, this is an approximation from public closes)")
    if lv.kind == "user" and lv.source:
        return base + L(f"（来源：{lv.source}）", f" (source: {lv.source})")
    if lv.kind == "zone" and lv.zone is not None:
        return base + L(f"（整个区间都已失守；它由过去一年的 {lv.zone.touches} 个波段高低点聚成，最近一个在 {lv.zone.last:%m-%d}）",
                        f" (the whole zone is lost; it is a cluster of {lv.zone.touches} swing high(s) / low(s) of the past year, "
                        f"the most recent on {lv.zone.last:%m-%d})")
    return base


def _record_lines(lv: kl.Level, rec: kl.Record, st: kl.IndexState, direction: str, hold: int) -> list[str]:
    hs = [h for h in sorted(rec.fwd)]
    out = [L(f"{st.name}自 {rec.since:%Y} 年以来，“{_rule(lv, direction, hold)}”共出现 {rec.n} 次（不含这次）"
             + ("；样本少，统计意义有限" if rec.n < 20 else "") + "：",
             f"On the {st.name} since {rec.since:%Y}, '{_rule(lv, direction, hold)}' has happened {rec.n} time(s) "
             "(not counting this one)" + ("; a small sample, so the statistics mean little" if rec.n < 20 else "") + ":")]
    if hs:
        days = " / ".join(str(h) for h in hs)
        med = " / ".join(f"{rec.fwd[h][1] * 100:+.1f}%" for h in hs)
        up = " / ".join(f"{rec.fwd[h][2] * 100:.0f}%" for h in hs)
        bmed = " / ".join(f"{rec.base[h][0] * 100:+.1f}%" for h in hs if h in rec.base)
        bup = " / ".join(f"{rec.base[h][1] * 100:.0f}%" for h in hs if h in rec.base)
        out.append(L(f"· 之后 {days} 个交易日：涨跌中位数 {med}，上涨的比例 {up}",
                     f"· After {days} sessions: median change {med}, share of rises {up}"))
        out.append(L(f"· 对照（同期任意一天之后同样天数）：中位数 {bmed}，上涨比例 {bup}",
                     f"· For comparison (any day in the same period, same horizons): median {bmed}, share of rises {bup}"))
        tail = [h for h in hs if h >= 21] or hs[-1:]
        out.append(L("· 较差的情形（最差的 10%）：" + "，".join(f"{h} 日后低于 {rec.fwd[h][3] * 100:+.1f}%" for h in tail),
                     "· The poor outcomes (worst 10%): " + ", ".join(f"below {rec.fwd[h][3] * 100:+.1f}% after {h} sessions" for h in tail)))
    bits = []
    if rec.back5 is not None:
        bits.append(L(f"5 个交易日内收回该位置的占 {rec.back5 * 100:.0f}%（常说的“假破位”）" if direction == "down"
                      else f"5 个交易日内又跌回该位置下方的占 {rec.back5 * 100:.0f}%",
                      f"back above the level within 5 sessions in {rec.back5 * 100:.0f}% of cases (a so-called false break)"
                      if direction == "down" else f"back below the level within 5 sessions in {rec.back5 * 100:.0f}% of cases"))
    if rec.dip21 is not None:
        bits.append(L(f"之后 21 个交易日内的最低收盘，较事件当日的中位数是 {rec.dip21[0] * 100:+.1f}%，最差的 10% 超过 {rec.dip21[1] * 100:+.1f}%",
                      f"lowest close within the next 21 sessions: median {rec.dip21[0] * 100:+.1f}% from the event close, "
                      f"worst 10% beyond {rec.dip21[1] * 100:+.1f}%"))
    if bits:
        out.append("· " + L("；", "; ").join(bits))
    if rec.last:
        out.append(L("· 最近几次：", "· The most recent ones: ") + _join(
            L(f"{d:%Y-%m-%d}（之后 21 日 {'未满' if r is None else f'{r * 100:+.1f}%'}）",
              f"{d:%Y-%m-%d} ({'21 sessions not over yet' if r is None else f'{r * 100:+.1f}% after 21 sessions'})")
            for d, r in rec.last))
    h = 21 if 21 in rec.fwd and 21 in rec.base else (hs[-1] if hs else None)
    if h is not None and h in rec.base:
        ev, base = rec.fwd[h][1] * 100, rec.base[h][0] * 100
        tail_ev, tail_base = rec.fwd[h][3] * 100, rec.base[h][2] * 100
        if abs(ev - base) < 0.5:
            verdict = L(f"中位数和平时差别不大（{h} 日 {ev:+.1f}% 对 {base:+.1f}%），单看这个信号区分度有限",
                        f"the median is close to an ordinary day's ({h} sessions: {ev:+.1f}% vs {base:+.1f}%), so on its own this signal separates little")
        elif ev < base:
            verdict = L(f"之后的中位数比平时弱（{h} 日 {ev:+.1f}% 对 {base:+.1f}%）",
                        f"the median afterwards was weaker than usual ({h} sessions: {ev:+.1f}% vs {base:+.1f}%)")
        else:
            verdict = L(f"之后的中位数反而略好于平时（{h} 日 {ev:+.1f}% 对 {base:+.1f}%）",
                        f"the median afterwards was in fact a little better than usual ({h} sessions: {ev:+.1f}% vs {base:+.1f}%)")
        if tail_ev < tail_base - 0.5:
            verdict += L(f"；差别在尾部：最差的 10% 是 {tail_ev:+.1f}%，平时是 {tail_base:+.1f}%，也就是演变成大跌的那一小部分情形更深",
                         f"; the difference is in the tail: the worst 10% is {tail_ev:+.1f}% against {tail_base:+.1f}% normally, "
                         "so the small share of cases that turn into a big fall go deeper")
        else:
            verdict += L(f"；最差 10% 的情形（{tail_ev:+.1f}%）也不比平时（{tail_base:+.1f}%）更差",
                         f"; the worst 10% ({tail_ev:+.1f}%) is no worse than normal ({tail_base:+.1f}%) either")
        out.append(L(f"· 读法：{verdict}。这是过去的统计，不代表这一次；中位数掩盖了个别很差的年份（见“最近几次”）。",
                     f"· Reading: {verdict}. This is a record of the past, not a statement about this time; a median hides the "
                     "occasional very bad year (see the most recent ones)."))
    return out


def _standing(st: kl.IndexState, skip: set[str]) -> list[str]:
    """Earlier breaks the price is still on the far side of."""
    out = []
    force = st.inforce
    if force is not None and force[0].key not in skip:
        lv, day, n, _ = force
        out.append(L(f"· 仍然有效的上一次规则破位：{day:%m-%d} 收盘跌破{lv.name}（当时 {num(lv.value)}），到今天 {n} 个交易日，现价仍在它下方",
                     f"· The last rule-based break still in force: the close of {day:%m-%d} below {_the(lv)} (then {num(lv.value)}); "
                     f"{n} session(s) later the price is still below it"))
    rest = [(d, x) for d, x in st.recent if not (force is not None and force[0].key == x.key) and x.key not in skip]
    if rest:
        out.append(L("· 近几天发生、至今没有收复的其它破位：", "· Other breaks of the last few sessions that still stand: ")
                   + L("；", "; ").join(f"{d:%m-%d} {label(x)}" for d, x in rest))
    return out


def index_position(st: kl.IndexState) -> list[str]:
    i = st.ind
    run = (L(f"；已连续 {i['down_days']} 天收跌", f"; down {i['down_days']} sessions in a row") if i["down_days"] >= 2
           else L(f"；已连续 {i['up_days']} 天收涨", f"; up {i['up_days']} sessions in a row") if i["up_days"] >= 2 else "")
    out = [L(f"· 距 52 周收盘高点 {num(i['high252'])}（{i['high252_date']:%m-%d}）{i['dd']:+.1f}%；"
             f"近 5 日 {i['chg5']:+.1f}%，近 21 日 {i['chg21']:+.1f}%{run}",
             f"· {i['dd']:+.1f}% from the 52-week closing high {num(i['high252'])} ({i['high252_date']:%m-%d}); "
             f"5 sessions {i['chg5']:+.1f}%, 21 sessions {i['chg21']:+.1f}%{run}")]
    mas = [L(f"{n} 日 {num(v)}（现价在{_side(st.price, v)}，已 {d} 天）", f"{n}-day {num(v)} (price {_side(st.price, v)}, for {d} sessions)")
           for n, (v, d) in i["mas"].items()]
    if mas:
        out.append(L("· 均线：", "· Averages: ") + _join(mas))
    rsi = i["rsi"]
    note = (L("（低于 30 的超卖区：说明跌得急，不等于跌完）", " (below 30, oversold: it fell fast, which is not the same as being done falling)") if rsi < 30
            else L("（高于 70 的超买区）", " (above 70, overbought)") if rsi > 70 else "")
    move = abs(st.price - st.prev_close) / i["atr"] if i["atr"] else 0.0
    out.append(L(f"· RSI(14) {rsi:.0f}{note}；日均波幅 ATR {num(i['atr'])} 点（{i['atr_pct']:.1f}%），今天的变动相当于 {move:.1f} 个 ATR",
                 f"· RSI(14) {rsi:.0f}{note}; average daily range ATR {num(i['atr'])} points ({i['atr_pct']:.1f}%), "
                 f"today's move equals {move:.1f} ATR"))
    return out


def index_brief(st: kl.IndexState) -> str:
    """One line on an index: price, distance from the high, the two main averages, today's breaks."""
    i = st.ind
    mas = _join(L(f"{n} 日线{_side(st.price, i['mas'][n][0])}", f"{_side(st.price, i['mas'][n][0])} its {n}-day average")
                for n in (50, 200) if n in i["mas"])
    ev = ""
    if st.broken:
        ev = L(f"；{'盘中' if st.live else '收盘'}跌破{_join(x.name for x in st.broken[:3])}",
               f"; {'intraday break' if st.live else 'close'} below {_join(_the(x) for x in st.broken[:3])}")
    elif st.reclaimed:
        ev = L(f"；{'盘中' if st.live else '收盘'}站上{_join(x.name for x in st.reclaimed[:3])}",
               f"; {'intraday move' if st.live else 'close'} above {_join(_the(x) for x in st.reclaimed[:3])}")
    return tidy(L(f"{st.name} {num(st.price)}（{st.day_pct:+.2f}%），距 52 周高点 {i['dd']:+.1f}%，{mas}{ev}",
                  f"{st.name} {num(st.price)} ({st.day_pct:+.2f}%), {i['dd']:+.1f}% from the 52-week high, {mas}{ev}"))


def market_lines(indices: dict, vix: dict | None = None, skip: str = "") -> list[str]:
    out = ["· " + index_brief(st) for sym, st in (indices or {}).items() if sym != skip]
    if vix_text(vix):
        out.append("· " + vix_text(vix))
    return out


def next_levels(st: kl.IndexState, direction: str = "down", n: int = 5) -> list[str]:
    rows = (st.below() if direction == "down" else st.above())[:n]
    out = []
    for x in rows:
        if x.zone is not None and x.zone.low <= st.price <= x.zone.high:
            out.append(L(f"· {label(x)}：现价已经在这个区间里，区间下沿距现价 {gap(x.value, st.price)}",
                         f"· {label(x)}: the price is already inside this zone; its bottom is {gap(x.value, st.price)} from the price"))
        elif x.zone is not None and x.zone.high > x.zone.low and (direction == "down") == (x.zone.high < st.price):
            out.append(L(f"· {label(x)}：上沿距现价 {gap(x.zone.high, st.price)}，下沿 {gap(x.zone.low, st.price)}",
                         f"· {label(x)}: its top is {gap(x.zone.high, st.price)} from the price, its bottom {gap(x.zone.low, st.price)}"))
        else:
            out.append(L(f"· {label(x)}：距现价 {gap(x.value, st.price)}", f"· {label(x)}: {gap(x.value, st.price)} from the price"))
    return out


def watch_rows(views, index_bars: pd.DataFrame | None, index_name: str) -> tuple[list[str], list[tuple[str, float]]]:
    """One line per watched stock (quotes and levels only) and its beta to the index."""
    lines, betas = [], []
    for v in views or []:
        a = getattr(v, "analysis", None)
        if a is None or not a.ok or v.bars is None or v.price != v.price:
            continue
        i = a.ind
        parts = [L(f"今日 {v.day_pct:+.2f}%", f"today {v.day_pct:+.2f}%")]
        if v.currency == "USD" and index_bars is not None:
            b = kl.beta(v.bars["close"], index_bars["close"])
            if b is not None:
                betas.append((v.symbol, b))
                parts.append(L(f"近 120 日对{index_name}的 β {b:.1f}（指数动 1% 时它平均动约 {b:.1f}%）",
                               f"120-session beta to the {index_name} {b:.1f} (it moves about {b:.1f}% for each 1% in the index)"))
        else:
            parts.append(L("不同交易时段，不算 β", "different trading hours, no beta computed"))
        mas = [L(f"{n} 日线{_side(v.price, i[k])}", f"{_side(v.price, i[k])} its {n}-day average")
               for n, k in ((50, "sma50"), (200, "sma200")) if i.get(k) == i.get(k)]
        if mas:
            parts.append(L("在自己的 ", "") + _join(mas))
        parts.append(f"RSI {i['rsi']:.0f}")
        parts.append(L(f"综合信号 {a.stance}（{a.composite:+.2f}）", f"composite signal {a.stance} ({a.composite:+.2f})"))
        try:
            z = support_zones(v.bars, v.price)
        except Exception:
            z = []
        if z:
            parts.append(L(f"下方最近支撑 {num(z[0].level)}（{gap(z[0].level, v.price)}）",
                           f"nearest support below {num(z[0].level)} ({gap(z[0].level, v.price)})"))
        lines.append(f"· {v.symbol}" + L("：", ": ") + L("；", "; ").join(parts))
    return lines, betas


def index_alert(st: kl.IndexState, direction: str, hits: list, *, others: dict | None = None, vix: dict | None = None,
                views=None, index_bars: pd.DataFrame | None = None, hold: int = 10, playbook: list[str] | None = None) -> tuple[str, str]:
    """(title, body) for the levels ``hits`` that the index broke on its latest bar."""
    top = hits[0]
    down = direction == "down"
    more = L(f"（同时失守 {len(hits) - 1} 个其它关键位）" if direction == "down" else f"（同时站上 {len(hits) - 1} 个其它关键位）",
             f" (plus {len(hits) - 1} other level(s))") if len(hits) > 1 else ""
    when = L("盘中", "intraday") if st.live else L("收盘", "at the close")
    if down:
        title = L(f"{st.name}{when}跌破{top.name}{more}",
                  f"{st.name} {'breaks' if st.live else 'closes'} below {_the(top)}{' intraday' if st.live else ''}{more}")
    elif top.kind == "high":
        title = L(f"{st.name}{when}创 52 周新高{more}", f"{st.name} at a new 52-week high {when}{more}")
    else:
        title = L(f"{st.name}{when}站上{top.name}{more}",
                  f"{st.name} {'moves' if st.live else 'closes'} above {_the(top)}{' intraday' if st.live else ''}{more}")
    verb = L("跌破", "below") if down else L("站上", "above")
    out = [L(f"{st.name}（{st.symbol}）{'盘中' if st.live else f'{st.asof:%m-%d} 收盘'} {num(st.price)}（当日 {st.day_pct:+.2f}%），"
             f"{verb}{_join(x.name for x in hits)}。",
             f"{st.name} ({st.symbol}) {'intraday' if st.live else f'close of {st.asof:%m-%d}'} {num(st.price)} "
             f"({st.day_pct:+.2f}% on the day), {verb} {_join(_the(x) for x in hits)}.")]

    out.append(head("发生了什么", "What happened"))
    out += [_event_line(x, st, direction) for x in hits]
    if st.live:
        out.append(L("· 状态：盘中，还没有收盘确认。要到收盘仍在这些位置的另一侧才算数，届时会再提醒一次；盘中刺破后收回的情况很常见。",
                     "· Status: intraday, not confirmed by a close yet. It only counts if the close is still on the far side, "
                     "and there will be another alert then; an intraday break that is taken back by the close is common."))
    else:
        vr = st.ind.get("vol_ratio")
        vol = "" if not vr else L(f"当日成交量是 20 日均量的 {vr:.1f} 倍（{'放量' if vr >= 1.2 else '缩量' if vr < 0.9 else '量能平常'}；放量的破位比缩量的更有分量）。",
                                   f" Volume on the day was {vr:.1f}x its 20-day average ({'heavy' if vr >= 1.2 else 'light' if vr < 0.9 else 'ordinary'}; "
                                   "a break on heavy volume carries more weight than one on light volume).")
        out.append(L(f"· 状态：收盘确认（{st.asof:%Y-%m-%d}）。{vol}", f"· Status: confirmed by the close ({st.asof:%Y-%m-%d}).{vol}"))

    if playbook:
        out.append(head("你的预案", "Your playbook"))
        out += playbook
    out.append(head("现在的位置", "Where it stands now"))
    out += index_position(st)
    if down:
        out += _standing(st, {x.key for x in hits})
    out += market_lines(others or {}, vix, skip=st.symbol)

    lv = next_levels(st, "down" if down else "up")
    if lv:
        out.append(head("下方的关键位", "Key levels below") if down else head("上方的关键位", "Key levels above"))
        out += lv
        back = (st.above() if down else st.below())[:2]
        if back:
            out.append(L(f"· {'上方要收复的' if down else '下方要守住的'}：", f"· {'To be regained above' if down else 'To be held below'}: ")
                       + _join(L(f"{label(x)}（{gap(x.value, st.price)}）", f"{label(x)} ({gap(x.value, st.price)})") for x in back))

    out.append(head("历史上同类情况", "The past record"))
    recs = [(x, st.records[f"{x.key}:{direction}"]) for x in hits if f"{x.key}:{direction}" in st.records]
    rec0 = recs[0][1] if recs else None
    if recs:
        out += _record_lines(recs[0][0], recs[0][1], st, direction, hold)
        for x, r in recs[1:3]:
            f21 = r.fwd.get(21)
            if f21:
                out.append(L(f"· 另：{x.name}的同类情况 {r.n} 次，之后 21 日中位数 {f21[1] * 100:+.1f}%、上涨比例 {f21[2] * 100:.0f}%"
                             + (f"，5 日内收回的占 {r.back5 * 100:.0f}%" if r.back5 is not None and down else ""),
                             f"· Also: {r.n} earlier case(s) for {_the(x)}, median {f21[1] * 100:+.1f}% after 21 sessions, "
                             f"share of rises {f21[2] * 100:.0f}%"
                             + (f", regained within 5 sessions in {r.back5 * 100:.0f}% of cases" if r.back5 is not None and down else "")))
    elif down and st.inforce is not None and st.inforce[3] is not None:
        lvf, day, n, recf = st.inforce
        out.append(L(f"· 这次失守的是支撑区 / 你设的价位，没有可以按固定规则回溯的同类样本。可以参照的是仍然有效的上一次规则破位："
                     f"{day:%m-%d} 收盘跌破{lvf.name}，今天是那之后的第 {n} 个交易日。",
                     "· What gave way is a support zone / a level you set; there is no rule-based sample of like cases to look back on. "
                     f"The reference is the last rule-based break still in force: the close of {day:%m-%d} below {_the(lvf)}; "
                     f"today is session {n} after it."))
        out += _record_lines(lvf, recf, st, "down", hold)
    else:
        out.append(L("· 这次涉及的是支撑区 / 你设的价位，没有可以按固定规则回溯的同类样本，所以只给位置，不给统计。",
                     "· This concerns a support zone / a level you set; there is no rule-based sample of like cases to look back on, "
                     "so only the levels are given, no statistics."))

    rows, betas = watch_rows(views, index_bars, st.name)
    if rows:
        out.append(head("对你的自选（只有行情和价位，不含持仓）", "For your watchlist (quotes and levels only, no holdings)"))
        out += rows
        day = [(v.symbol, v.day_pct) for v in views or [] if v.price == v.price and getattr(v, "currency", "USD") == "USD"]
        worse = [s for s, d in day if d < st.day_pct - 0.3]
        better = [s for s, d in day if d > st.day_pct + 0.3]
        if worse or better:
            out.append("· " + L("；", "; ").join(
                ([L(f"今天比指数弱的：{_join(worse)}", f"weaker than the index today: {_join(worse)}")] if worse else [])
                + ([L(f"比指数强的：{_join(better)}", f"stronger than the index: {_join(better)}")] if better else [])))

    out.append(head("接下来看什么", "What to watch next"))
    cur = st.level(top.key)
    ref = cur.value if cur is not None else top.value
    what = (L("这个支撑区的下沿", "the bottom of that zone") if top.kind == "zone" else L("你设的价位", "your level") if top.kind == "user"
            else top.name)
    if down:
        if st.live:
            out.append(L(f"· 今天的收盘：收在 {num(ref)}（{what}）上方，就只是盘中刺破；收在下方才是收盘破位。",
                         f"· Today's close: above {num(ref)} ({what}) and it was only an intraday break; below it and the break is confirmed."))
        out.append(L(f"· 能否收复：之后重新收在 {num(ref)} 上方，这次就归为假破位"
                     + (f"（历史上 5 个交易日内收复的占 {rec0.back5 * 100:.0f}%）" if rec0 and rec0.back5 is not None else "") + "。",
                     f"· Whether it is regained: a later close back above {num(ref)} makes this a false break"
                     + (f" (regained within 5 sessions in {rec0.back5 * 100:.0f}% of past cases)" if rec0 and rec0.back5 is not None else "") + "."))
        below = st.below()
        if below:
            nxt = L(f"· 继续走弱的话，下一个关键位是{label(below[0])}（距现价 {gap(below[0].value, st.price)}）",
                    f"· If it keeps weakening, the next key level is {_the_label(below[0])} ({gap(below[0].value, st.price)} from the price)")
            if len(below) > 1:
                nxt += L(f"，再往下是{label(below[1])}（{gap(below[1].value, st.price)}）",
                         f", then {_the_label(below[1])} ({gap(below[1].value, st.price)})")
            out.append(nxt + L("。", "."))
    else:
        out.append(L(f"· 能否站稳：接下来几天收盘是否守在 {num(ref)} 上方"
                     + (f"（历史上 5 个交易日内又跌回去的占 {rec0.back5 * 100:.0f}%）" if rec0 and rec0.back5 is not None else "") + "。",
                     f"· Whether it holds: whether the next few closes stay above {num(ref)}"
                     + (f" (back below within 5 sessions in {rec0.back5 * 100:.0f}% of past cases)" if rec0 and rec0.back5 is not None else "") + "."))
    cross = []
    for sym, o in (others or {}).items():
        if sym == st.symbol:
            continue
        same = [x.name for x in (o.broken if down else o.reclaimed)]
        same_key = o.level(top.key)
        if same:
            cross.append(L(f"{o.name}今天也{'跌破了' if down else '站上了'}{_join(same[:2])}",
                           f"the {o.name} also {'broke below' if down else 'moved above'} {_join(same[:2])} today"))
        elif same_key is not None and top.kind == "ma":
            cross.append(L(f"{o.name}现在在自己的{same_key.name}{_side(o.price, same_key.value)}",
                           f"the {o.name} is now {_side(o.price, same_key.value)} its own {same_key.name}"))
        else:
            near = (o.below() if down else o.above())[:1]
            if near:
                cross.append(L(f"{o.name}今天没有新的{'破位' if down else '突破'}，离它{'下方' if down else '上方'}最近的关键位{label(near[0])}还有 {gap(near[0].value, o.price)}",
                               f"the {o.name} has no new {'break' if down else 'breakout'} today; its nearest key level {'below' if down else 'above'}, "
                               f"{_the_label(near[0])}, is {gap(near[0].value, o.price)} away"))
    if cross or vix_text(vix):
        v = f"VIX {float(vix['price']):.1f}" if vix_text(vix) else ""
        out.append(L("· 交叉验证：" + "；".join(cross + ([v] if v else []))
                     + ("。几个指数同向破位、VIX 同时走高，比单个指数破位更有分量。" if down else "。几个指数同时走强、VIX 回落，比单个指数更有分量。"),
                     "· Cross-check: " + "; ".join(cross + ([v] if v else []))
                     + (". Several indices breaking the same way with the VIX rising carries more weight than one index alone."
                        if down else ". Several indices strengthening together with the VIX falling carries more weight than one index alone.")))
    if betas and down:
        s, b = max(betas, key=lambda x: x[1])
        out.append(L(f"· 自选里对指数最敏感的是 {s}（β {b:.1f}）：指数继续下行时它的波动通常最大。对照你自己写下的计划价位，而不是临时决定。",
                     f"· The most index-sensitive stock on your watchlist is {s} (beta {b:.1f}): it usually swings the most if the index keeps falling. "
                     "Check it against the plan levels you wrote down rather than deciding on the spot."))
    out.append(FOOT)
    return tidy(title), tidy("\n".join(out))


# ---- one stock ------------------------------------------------------------------------------------

def position_lines(symbol: str, bars: pd.DataFrame | None, price: float | None = None, *, day_pct: float | None = None,
                   analysis=None, index: kl.IndexState | None = None, index_bars: pd.DataFrame | None = None,
                   usd: bool = True) -> list[str]:
    """Where a stock stands: recent moves, averages, momentum, composite signal, supports, versus the index."""
    if bars is None or len(bars) < 60:
        return []
    i = analysis.ind if analysis is not None and getattr(analysis, "ok", False) else compute_indicators(bars)
    px = float(price) if price is not None and price == price else float(i["close"])
    out = []
    first = [L(f"今日 {day_pct:+.2f}%", f"today {day_pct:+.2f}%")] if day_pct is not None else []
    for k, zh, en in (("chg_5d", "近 5 日", "5 sessions"), ("chg_21d", "近 21 日", "21 sessions")):
        if i.get(k) == i.get(k) and i.get(k) is not None:
            first.append(L(f"{zh} {i[k] * 100:+.1f}%", f"{en} {i[k] * 100:+.1f}%"))
    first.append(L(f"距 52 周收盘高点 {num(i['high_252'])} {(px / i['high_252'] - 1) * 100:+.1f}%，距 52 周低点 {num(i['low_252'])} {(px / i['low_252'] - 1) * 100:+.1f}%",
                   f"{(px / i['high_252'] - 1) * 100:+.1f}% from the 52-week closing high {num(i['high_252'])}, "
                   f"{(px / i['low_252'] - 1) * 100:+.1f}% from the 52-week low {num(i['low_252'])}"))
    out.append("· " + L("；", "; ").join(first))
    mas = [L(f"{n} 日 {num(i[k])}（现价在{_side(px, i[k])}）", f"{n}-day {num(i[k])} (price {_side(px, i[k])})")
           for n, k in ((20, "sma20"), (50, "sma50"), (200, "sma200")) if i.get(k) == i.get(k)]
    if mas:
        out.append(L("· 均线：", "· Averages: ") + _join(mas))
    rsi = i["rsi"]
    note = (L("（超卖区）", " (oversold)") if rsi < 30 else L("（接近超卖）", " (near oversold)") if rsi < 35
            else L("（超买区）", " (overbought)") if rsi > 70 else "")
    hist, prev = i.get("macd_hist", float("nan")), i.get("macd_hist_prev", float("nan"))
    macd = "" if hist != hist else L(f"；MACD 柱 {hist:+.2f}，较前一日{'走强' if prev == prev and hist > prev else '走弱'}",
                                     f"; MACD histogram {hist:+.2f}, {'stronger' if prev == prev and hist > prev else 'weaker'} than the day before")
    out.append(L(f"· RSI(14) {rsi:.0f}{note}{macd}；日均波幅 ATR {num(i['atr'])}（{i['atr_pct'] * 100:.1f}%）",
                 f"· RSI(14) {rsi:.0f}{note}{macd}; average daily range ATR {num(i['atr'])} ({i['atr_pct'] * 100:.1f}%)"))
    if analysis is not None and getattr(analysis, "ok", False):
        out.append(L(f"· 综合信号 {analysis.stance}（{analysis.composite:+.2f}，范围 −1~+1）：", f"· Composite signal {analysis.stance} ({analysis.composite:+.2f}, range −1 to +1): ")
                   + L("；", "; ").join(L(f"{s.name} {s.label}", f"{s.name}: {s.label}") for s in analysis.signals))
        rg = analysis.range
        if rg is not None and rg.status not in ("none", "short"):
            out.append(L(f"· 震荡区间：{rg.label}，支撑 {num(rg.support)}（{gap(rg.support, px)}）/ 阻力 {num(rg.resistance)}（{gap(rg.resistance, px)}）",
                         f"· Trading range: {rg.label}, support {num(rg.support)} ({gap(rg.support, px)}) / resistance {num(rg.resistance)} ({gap(rg.resistance, px)})"))
    try:
        zones = support_zones(bars, px)
    except Exception:
        zones = []
    if zones:
        out.append(L("· 下方支撑区（一年内波段高低点聚类）：", "· Support zones below (clusters of the past year's swing highs / lows): ") + L("；", "; ").join(
            L(f"{num(z.level)}（{num(z.low)}–{num(z.high)}，{z.touches} 个转折点，距现价 {gap(z.level, px)}）",
              f"{num(z.level)} ({num(z.low)}–{num(z.high)}, {z.touches} turning point(s), {gap(z.level, px)} from the price)") for z in zones[:3]))
    else:
        out.append(L("· 下方一年内没有成形的支撑区（现价已在一年低点附近或之下）。",
                     "· No formed support zone below within the past year (the price is at or under the 1-year low)."))
    if index is not None and day_pct is not None and usd:
        b = kl.beta(bars["close"], index_bars["close"]) if index_bars is not None else None
        line = L(f"· 相对大盘：今日它 {day_pct:+.2f}%，{index.name} {index.day_pct:+.2f}%",
                 f"· Versus the market: it is {day_pct:+.2f}% today, the {index.name} {index.day_pct:+.2f}%")
        if b is not None:
            exp = b * index.day_pct
            diff = day_pct - exp
            verdict = (L("今天的变动大体是跟着大盘", "today's move is largely the market's") if abs(diff) < max(1.0, abs(exp) * 0.5)
                       else L(f"比大盘能解释的多跌 {abs(diff):.1f} 个点，可能有个股自己的原因", f"{abs(diff):.1f} points weaker than the market explains, so there may be a stock-specific reason") if diff < 0
                       else L(f"比大盘能解释的强 {diff:.1f} 个点", f"{diff:.1f} points stronger than the market explains"))
            line += L(f"；近 120 日 β {b:.1f} → 按 β 推算大盘能解释约 {exp:+.1f}%，{verdict}",
                      f"; 120-session beta {b:.1f} → the market explains about {exp:+.1f}% by beta, {verdict}")
        out.append(line)
    if analysis is not None and getattr(analysis, "evidence", None) is not None and analysis.evidence.n_bars:
        e = analysis.evidence
        hr = L("没有完整交易", "no completed trades") if e.hit_rate is None else L(f"胜率 {e.hit_rate * 100:.0f}%（{e.n_trades} 笔）", f"win rate {e.hit_rate * 100:.0f}% ({e.n_trades} trades)")
        out.append(L(f"· 历史检验：规则“{e.label}”在这只股票过去 {e.years:.1f} 年 {hr}，累计 {e.total_return * 100:+.0f}%（买入持有 {e.bh_return * 100:+.0f}%），"
                     f"最大回撤 {e.max_dd * 100:.0f}%（买入持有 {e.bh_max_dd * 100:.0f}%）；规则用的是通用默认参数",
                     f"· Historical test: rule '{e.label}' on this stock over the past {e.years:.1f} years, {hr}, cumulative {e.total_return * 100:+.0f}% "
                     f"(buy-and-hold {e.bh_return * 100:+.0f}%), max drawdown {e.max_dd * 100:.0f}% (buy-and-hold {e.bh_max_dd * 100:.0f}%); "
                     "the rule uses generic default parameters"))
    return [tidy(x) for x in out]


def plan_body(text: str, st, key: str, level: float, *, symbol: str, bars: pd.DataFrame | None, price: float,
              day_pct: float | None, analysis=None, indices: dict | None = None, vix: dict | None = None,
              index_bars: pd.DataFrame | None = None, bottom=None, flows: list | None = None, usd: bool = True,
              stop=None, stop_rec=None) -> str:
    """The write-up under a "your plan level was reached" alert. ``st`` is the PlanStatus, ``symbol`` the stock
    whose price the levels are written in."""
    px = st.check_price
    unit = f" {st.level_ccy}" if st.follow and st.level_ccy else ""
    what = {"buy": L("买入价位", "buy level"), "reduce": L("减仓价位", "trim level"), "target": L("目标价", "target price"),
            "step": L("分步减仓价", "staged-trim step"),
            "exit": L("底线价", "bottom-line price")}.get(key, key)
    out = [head("这次触发", "What triggered")]
    past = (px / level - 1) * 100 if level else 0.0
    out.append(L(f"· 触发的是你写的{what} {num(level)}{unit}；{symbol} 现价 {num(px)}{unit}，"
                 + (f"已{'低于' if past < 0 else '高于'}该价位 {abs(past):.1f}%" if abs(past) >= 0.05 else "正好在该价位"),
                 f"· The trigger is the {what} {num(level)}{unit} you wrote; {symbol} is at {num(px)}{unit}, "
                 + (f"{abs(past):.1f}% {'below' if past < 0 else 'above'} that level" if abs(past) >= 0.05 else "right at that level")))
    others = []
    if st.next_buy is not None:
        others.append(L(f"下一档买入价 {num(st.next_buy)}（距现价 {gap(st.next_buy, px)}）", f"next buy level {num(st.next_buy)} ({gap(st.next_buy, px)} from the price)"))
    if st.next_level is not None:
        others.append(L(f"下一档减仓价 {num(st.next_level)}（{gap(st.next_level, px)}）", f"next trim level {num(st.next_level)} ({gap(st.next_level, px)})"))
    if st.target_price and key != "target":
        others.append(L(f"目标价 {num(st.target_price)}（{gap(st.target_price, px)}）", f"target {num(st.target_price)} ({gap(st.target_price, px)})"))
    if st.exit_below and key != "exit":
        others.append(L(f"底线价 {num(st.exit_below)}（{gap(st.exit_below, px)}）", f"bottom line {num(st.exit_below)} ({gap(st.exit_below, px)})"))
    for lvl, w in getattr(st, "steps", []):
        if lvl != level:
            others.append(L(f"减仓价 {num(lvl)} → 占比 {w:g}%（{gap(lvl, px)}）", f"trim step {num(lvl)} → weight {w:g}% ({gap(lvl, px)})"))
    if st.days_left:
        others.append(L(f"计划还剩 {st.days_left} 天", f"{st.days_left} days left in the plan"))
    if others:
        out.append(L("· 计划里的其它价位：", "· The other levels in the plan: ") + L("；", "; ").join(others))
    if key == "buy" and stop is not None:
        out.append(head("止跌形态", "The stop-falling pattern"))
        out += stopfall_lines(stop, stop_rec)
    main = next(iter((indices or {}).values()), None)
    pos = position_lines(symbol, bars, price, day_pct=day_pct, analysis=analysis, index=main, index_bars=index_bars, usd=usd)
    if pos:
        out.append(head(f"{symbol} 现在的位置", f"Where {symbol} stands now"))
        out += pos
    if bottom is not None and getattr(bottom, "ok", False):
        mark = (L("已达到门槛", "threshold met") if bottom.triggered else L("未达到门槛：还看不到有成交量确认的企稳迹象", "threshold not met: no volume-confirmed sign of a base yet")
                if bottom.eligible else L("跌幅还不够深，不在筑底观察范围", "the decline is not deep enough for the bottoming watch"))
        out.append(L(f"· 筑底检查：{bottom.summary}，{mark}", f"· Bottoming check: {bottom.summary}, {mark}"))
    mk = market_lines(indices or {}, vix)
    if mk:
        out.append(head("同一时间的大盘", "The market at the same time"))
        out += mk
    if flows:
        out.append(head("相关异动", "Related unusual activity"))
        out += [L(f"· {f.title}：{f.headline}", f"· {f.title}: {f.headline}") for f in flows[:3]]
    out.append(head("可以核对的", "Worth checking"))
    versus = (L("是个股在跌还是大盘在跌：看上面“相对大盘”一行；大盘本身的位置看“同一时间的大盘”。",
                "Whether the stock or the market is falling: see the 'Versus the market' line above; for the market itself see the market section.")
              if usd and pos and indices else
              L("是个股自己在跌还是跟着大环境：它和美股不在同一时段交易，这里没有直接比较；可以对照它本土指数当天的表现。",
                "Whether the stock is falling on its own or with the wider market: it does not trade in US hours, so no direct comparison "
                "is made here; check how its home index did that day."))
    if key == "buy":
        out.append(L("· 这个买入价位是在什么前提下写的（业绩、行业、大盘环境）？这些前提现在是否还成立。",
                     "· Under what assumptions was this buy level written (earnings, sector, market backdrop)? Do they still hold."))
        out.append("· " + versus)
        if st.next_buy is not None or st.exit_below:
            out.append(L("· 分批的节奏：" + "，".join(([f"下一档在 {num(st.next_buy)}（还有 {gap(st.next_buy, px)}）"] if st.next_buy is not None else [])
                                                + ([f"底线在 {num(st.exit_below)}（{gap(st.exit_below, px)}）"] if st.exit_below else []))
                         + "；这一档和后面几档各占多少，是计划里应当事先定好的。",
                         "· The pacing: " + ", ".join(([f"the next level is {num(st.next_buy)} ({gap(st.next_buy, px)} away)"] if st.next_buy is not None else [])
                                                      + ([f"the bottom line is {num(st.exit_below)} ({gap(st.exit_below, px)})"] if st.exit_below else []))
                         + "; how much goes to this step and to the later ones is something the plan should fix in advance."))
    elif key in ("exit", "step"):
        out.append(L("· 底线价是你事先写下的“到这里就重新评估或退出”的位置。先确认是收盘价跌破还是盘中刺破，再对照计划里写的做法。",
                     "· The bottom line is where you wrote in advance 'reassess or exit here'. First confirm whether it is a closing break or an intraday dip, then check what the plan says to do."))
        out.append("· " + versus + L("大盘破位带下来的下跌和个股自己的坏消息，性质不同。",
                                     " A fall dragged by a market break and one caused by the stock's own bad news are different things."))
    else:
        out.append(L("· 这个价位是你事先定的。到了之后是否照做、做多少，对照计划里写的比例，而不是看当天的情绪。",
                     "· You set this level in advance. Whether to act on it and by how much is a matter of the proportions written in the plan, not of the day's mood."))
        out.append(L("· 上涨是否有量、是否已经短期过热：看上面的 RSI 和均线偏离。",
                     "· Whether the rise has volume behind it and whether it is overheated in the short term: see the RSI and the distance from the averages above."))
    return text + "\n" + tidy("\n".join(out))


def bottom_extra(symbol: str, st, bars: pd.DataFrame | None, *, day_pct: float | None = None, analysis=None,
                 indices: dict | None = None, vix: dict | None = None, index_bars: pd.DataFrame | None = None,
                 usd: bool = True) -> list[str]:
    """Extra sections for a bottoming alert: the position, the levels that matter, the market."""
    out = []
    main = next(iter((indices or {}).values()), None)
    pos = position_lines(symbol, bars, st.close, day_pct=day_pct, analysis=analysis, index=main, index_bars=index_bars, usd=usd)
    if pos:
        out.append(head(f"{symbol} 现在的位置", f"Where {symbol} stands now"))
        out += pos
    if bars is not None and len(bars) >= 60:
        i = analysis.ind if analysis is not None and getattr(analysis, "ok", False) else compute_indicators(bars)
        out.append(head("关键价位", "The levels that matter"))
        out.append(L(f"· 信号作废位：这一轮低点 {num(st.base_low)}（距收盘 {gap(st.base_low, st.close)}）；收盘跌破它，这次信号就不成立",
                     f"· Where the signal is void: the low of this leg {num(st.base_low)} ({gap(st.base_low, st.close)} from the close); "
                     "a close below it cancels this signal"))
        ups = [L(f"{n} 日均线 {num(i[k])}（{gap(i[k], st.close)}）", f"{n}-day average {num(i[k])} ({gap(i[k], st.close)})")
               for n, k in ((50, "sma50"), (200, "sma200")) if i.get(k) == i.get(k) and i[k] > st.close]
        ups.append(L(f"52 周收盘高点 {num(i['high_252'])}（{gap(i['high_252'], st.close)}）", f"52-week closing high {num(i['high_252'])} ({gap(i['high_252'], st.close)})"))
        out.append(L("· 上方的参考位（筑底之后通常要先面对的阻力）：", "· Reference levels above (the resistance a base usually meets first): ") + _join(ups))
    mk = market_lines(indices or {}, vix)
    if mk:
        out.append(head("同一时间的大盘", "The market at the same time"))
        out += mk
        out.append(L("· 个股的筑底信号出现在大盘仍在破位下行时，失败的比例通常高于大盘企稳时：两者要一起看。",
                     "· A bottoming signal in a stock while the market is still breaking down fails more often than one that appears "
                     "when the market has steadied: read the two together."))
    return [tidy(x) for x in out]


def flow_extra(tickers: list[str], views, indices: dict | None = None, vix: dict | None = None,
               index_bars: pd.DataFrame | None = None) -> list[str]:
    """Extra sections for an unusual-activity alert that concerns watched stocks."""
    out = []
    main = next(iter((indices or {}).values()), None)
    for sym in tickers[:2]:
        v = next((x for x in views or [] if x.symbol == sym), None)
        if v is None:
            continue
        pos = position_lines(sym, v.bars, v.price, day_pct=v.day_pct, analysis=v.analysis, index=main, index_bars=index_bars,
                             usd=v.currency == "USD")
        if pos:
            out.append(head(f"{sym} 现在的位置", f"Where {sym} stands now"))
            out += pos[:4] + [x for x in pos[4:] if x.startswith(L("· 相对大盘", "· Versus the market"))]
    mk = market_lines(indices or {}, vix)
    if mk:
        out.append(head("同一时间的大盘", "The market at the same time"))
        out += mk
    return [tidy(x) for x in out]


# ---- the stop-falling pattern and the edges of a trading range ----------------------------------------

def stopfall_lines(sf, rec=None, live: bool = False) -> list[str]:
    """The four conditions of the stop-falling pattern, each with its numbers, and the record of the signal."""
    if sf is None or not sf.ok:
        return [L("· 数据不足，无法判断止跌形态。", "· Not enough data to judge the stop-falling pattern.")]
    met = sum(1 for _, ok, _ in sf.checks if ok)
    out = [L(f"· 四项条件成立 {met}/4" + ("，形态成立" if sf.formed else "，形态还不成立") + (f"（{sf.formed_on:%m-%d} 起）" if sf.formed and sf.formed_on is not None else "")
             + ("；今天还没收盘，以收盘为准" if live else ""),
             f"· {met} of the 4 conditions hold" + ("; the pattern is formed" if sf.formed else "; the pattern is not formed yet")
             + (f" (since {sf.formed_on:%m-%d})" if sf.formed and sf.formed_on is not None else "")
             + ("; today has not closed yet, the close decides" if live else ""))]
    out += [f"· {'✓' if ok else '✗'} {d}" for _, ok, d in sf.checks]
    out.append(L(f"· 作废条件：收盘跌破这一轮的低点 {num(sf.low)}（{sf.low_date:%m-%d}，距现价 {gap(sf.low, sf.close)}）。跌破就重新计数。",
                 f"· Void if a close falls under the low of this leg, {num(sf.low)} ({sf.low_date:%m-%d}, {gap(sf.low, sf.close)} from the price); the count then starts again."))
    if rec is not None and rec.n:
        hs = sorted(rec.fwd)
        if hs:
            out.append(L(f"· 历史上同样的形态（{rec.since:%Y} 年以来 {rec.n} 次）：之后 {' / '.join(str(h) for h in hs)} 个交易日的涨跌中位数 "
                         f"{' / '.join(f'{rec.fwd[h][1] * 100:+.1f}%' for h in hs)}，上涨比例 {' / '.join(f'{rec.fwd[h][2] * 100:.0f}%' for h in hs)}；"
                         f"对照（任意一天）中位数 {' / '.join(f'{rec.base[h][0] * 100:+.1f}%' for h in hs if h in rec.base)}",
                         f"· The same pattern in the past ({rec.n} time(s) since {rec.since:%Y}): median change after {' / '.join(str(h) for h in hs)} sessions "
                         f"{' / '.join(f'{rec.fwd[h][1] * 100:+.1f}%' for h in hs)}, share of rises {' / '.join(f'{rec.fwd[h][2] * 100:.0f}%' for h in hs)}; "
                         f"for comparison (any day) median {' / '.join(f'{rec.base[h][0] * 100:+.1f}%' for h in hs if h in rec.base)}"))
        if rec.failed is not None:
            out.append(L(f"· 其中 {rec.failed * 100:.0f}% 在之后 21 个交易日内又收盘跌破了那一轮的低点（形态失败）。形态成立说明“跌势暂时停了”，不保证不再跌。",
                         f"· In {rec.failed * 100:.0f}% of them a close fell under that leg's low within the next 21 sessions (the pattern failed). "
                         "A formed pattern says the fall has paused, not that it cannot resume."))
    return out


def range_lines(rg, px: float, rec: dict | None = None, edge: str = "sell") -> list[str]:
    """The trading range, where the price is in it, and what followed earlier visits to this edge."""
    brk = float(rg.params.get("break_atr", 0.5))
    out = [L(f"· 区间：支撑 {num(rg.support)} / 阻力 {num(rg.resistance)}，宽 {rg.width_pct:.1f}%；横盘条件已连续成立 {rg.days} 天；下沿测试 {rg.touches[0]} 次、上沿 {rg.touches[1]} 次",
             f"· The range: support {num(rg.support)} / resistance {num(rg.resistance)}, {rg.width_pct:.1f}% wide; the sideways conditions have held for {rg.days} days; "
             f"lower edge tested {rg.touches[0]} time(s), upper edge {rg.touches[1]}"),
           L(f"· 现价 {num(px)} 在区间的 {rg.pos * 100:.0f}% 位置（0% = 支撑，100% = 阻力）；高抛区 {num(rg.sell_band[0])}–{num(rg.sell_band[1])}，低吸区 {num(rg.buy_band[0])}–{num(rg.buy_band[1])}",
             f"· The price {num(px)} is at {rg.pos * 100:.0f}% of the range (0% = support, 100% = resistance); sell zone {num(rg.sell_band[0])}–{num(rg.sell_band[1])}, "
             f"buy zone {num(rg.buy_band[0])}–{num(rg.buy_band[1])}")]
    if edge == "sell":
        out.append(L(f"· 区间作废位：收盘高于 {num(rg.breakout)}（阻力 + {brk:g}×ATR，距现价 {gap(rg.breakout, px)}）算向上突破，高抛低吸规则不再适用",
                     f"· Where the range is void: a close above {num(rg.breakout)} (resistance + {brk:g}×ATR, {gap(rg.breakout, px)} from the price) is an upward breakout "
                     "and the buy-low / sell-high rule no longer applies"))
    else:
        out.append(L(f"· 区间作废位：收盘低于 {num(rg.stop)}（支撑 − {brk:g}×ATR，距现价 {gap(rg.stop, px)}）算向下跌破，规则不再低吸",
                     f"· Where the range is void: a close below {num(rg.stop)} (support − {brk:g}×ATR, {gap(rg.stop, px)} from the price) is a breakdown and the rule stops buying low"))
    if rec:
        turn = L("回落到区间中部", "fell back to mid-range") if edge == "sell" else L("反弹到区间中部", "bounced to mid-range")
        broke = L("向上突破", "broke out upwards") if edge == "sell" else L("向下跌破", "broke down")
        out.append(L(f"· 这只股票过去每次进入{'高抛区' if edge == 'sell' else '低吸区'}（共 {rec['n']} 次）之后 10 个交易日内：先{turn}的 {rec['turned']} 次，"
                     f"先{broke}的 {rec['broke']} 次，两者都没有的 {rec['stayed']} 次；10 日涨跌中位数 {rec['median'] * 100:+.1f}%"
                     + ("（样本少）" if rec["n"] < 10 else ""),
                     f"· Each earlier entry of this stock into the {'sell' if edge == 'sell' else 'buy'} zone ({rec['n']} in all), within 10 sessions: {turn} first {rec['turned']} time(s), "
                     f"{broke} first {rec['broke']}, neither {rec['stayed']}; median 10-session change {rec['median'] * 100:+.1f}%"
                     + (" (a small sample)" if rec["n"] < 10 else "")))
    return out


def range_alert(symbol: str, rg, edge: str, *, bars, price: float, day_pct: float | None, analysis=None, rec: dict | None = None,
                sf=None, sf_rec=None, plan=None, indices: dict | None = None, vix: dict | None = None,
                index_bars: pd.DataFrame | None = None, usd: bool = True) -> tuple[str, str]:
    """(title, body): the top of the range reached (sell-zone reminder), or the bottom with a stop-falling pattern."""
    sell = edge == "sell"
    if sell:
        title = L(f"高抛提醒：{symbol} 到达震荡区间上沿", f"Sell-zone reminder: {symbol} at the top of its trading range")
        first = L(f"{symbol} 现价 {num(price)}，进入震荡区间上沿的高抛区（{num(rg.sell_band[0])}–{num(rg.sell_band[1])}，阻力 {num(rg.resistance)}）。",
                  f"{symbol} at {num(price)} has entered the sell zone at the top of its trading range ({num(rg.sell_band[0])}–{num(rg.sell_band[1])}, resistance {num(rg.resistance)}).")
    else:
        title = L(f"低吸区出现止跌形态：{symbol}", f"Stop-falling pattern in the buy zone: {symbol}")
        first = L(f"{symbol} 现价 {num(price)}，在震荡区间下沿的低吸区（{num(rg.buy_band[0])}–{num(rg.buy_band[1])}，支撑 {num(rg.support)}），并且止跌形态已成立。",
                  f"{symbol} at {num(price)} is in the buy zone at the bottom of its trading range ({num(rg.buy_band[0])}–{num(rg.buy_band[1])}, support {num(rg.support)}) "
                  "and the stop-falling pattern is formed.")
    out = [head("震荡区间", "The trading range")] + range_lines(rg, price, rec, edge)
    if not sell and sf is not None:
        out += [head("止跌形态", "The stop-falling pattern")] + stopfall_lines(sf, sf_rec)
    bt = next((b for b in getattr(analysis, "backtests", []) or [] if b.key == "range_trading" and b.n_bars), None)
    if bt is not None:
        hr = L("没有完整交易", "no completed trades") if bt.hit_rate is None else L(f"胜率 {bt.hit_rate * 100:.0f}%（{bt.n_trades} 笔）", f"win rate {bt.hit_rate * 100:.0f}% ({bt.n_trades} trades)")
        out += [head("这条规则在这只股票上的历史", "This rule's history on this stock"),
                L(f"· 区间高抛低吸规则过去 {bt.years:.1f} 年：{hr}，累计 {bt.total_return * 100:+.0f}%（买入持有 {bt.bh_return * 100:+.0f}%），"
                  f"最大回撤 {bt.max_dd * 100:.0f}%（买入持有 {bt.bh_max_dd * 100:.0f}%）。参数是通用默认值，没有针对这只股票调过。",
                  f"· The range rule over the past {bt.years:.1f} years: {hr}, cumulative {bt.total_return * 100:+.0f}% (buy-and-hold {bt.bh_return * 100:+.0f}%), "
                  f"max drawdown {bt.max_dd * 100:.0f}% (buy-and-hold {bt.bh_max_dd * 100:.0f}%). The parameters are generic defaults, not tuned to this stock.")]
    if plan is not None:
        mine = []
        if sell and plan.next_level is not None:
            mine.append(L(f"下一档减仓价 {num(plan.next_level)}（{gap(plan.next_level, price)}）", f"next trim level {num(plan.next_level)} ({gap(plan.next_level, price)})"))
        if sell and plan.target_price:
            mine.append(L(f"目标价 {num(plan.target_price)}（{gap(plan.target_price, price)}）", f"target {num(plan.target_price)} ({gap(plan.target_price, price)})"))
        if not sell and plan.buy_levels:
            mine.append(L("买入价位 " + " / ".join(num(x) for x in plan.buy_levels), "buy levels " + " / ".join(num(x) for x in plan.buy_levels)))
        if plan.exit_below:
            mine.append(L(f"底线价 {num(plan.exit_below)}（{gap(plan.exit_below, price)}）", f"bottom line {num(plan.exit_below)} ({gap(plan.exit_below, price)})"))
        if mine and not plan.follow:
            out += [head("你计划里的价位", "The levels in your plan"), "· " + L("；", "; ").join(mine)]
    main = next(iter((indices or {}).values()), None)
    pos = position_lines(symbol, bars, price, day_pct=day_pct, analysis=analysis, index=main, index_bars=index_bars, usd=usd)
    if pos:
        out += [head(f"{symbol} 现在的位置", f"Where {symbol} stands now")] + pos
    mk = market_lines(indices or {}, vix)
    if mk:
        out += [head("同一时间的大盘", "The market at the same time")] + mk
    out.append(head("怎么看", "How to read it"))
    if sell:
        out.append(L("· 这是区间规则的高抛信号：价格到了过去三个月反复受阻的位置。规则只在横盘时适用，一旦收盘突破作废位，就不再是“高抛”而是突破。",
                     "· This is the range rule's sell-high signal: the price is where it has turned back repeatedly over the past three months. The rule only applies while "
                     "the stock moves sideways; once a close passes the void level it is a breakout, not a sell-high."))
        out.append(L("· 到了上沿不等于马上回落：看上面“过去每次进入高抛区之后”的统计，以及它相对大盘的强弱。",
                     "· Reaching the top is not the same as turning down at once: see the count of what followed earlier entries above, and its strength against the market."))
        out.append(L("· 是否卖、卖多少，对照你计划里写的减仓价位和比例；程序只提示条件到了，不会下单。",
                     "· Whether to sell and how much is a matter of the trim levels and proportions in your plan; the program only says the condition is met and never places orders."))
    else:
        out.append(L("· 两件事同时成立才提醒：价格在区间下沿，并且止跌形态的四项条件都满足。只到下沿、还在下跌的，不提醒。",
                     "· The alert needs both: the price at the bottom of the range and all four conditions of the stop-falling pattern. A price that is at the bottom but still falling raises nothing."))
        out.append(L("· 形态成立说明跌势暂时停了，不保证不再跌：作废条件和区间作废位都写在上面。是否买、买多少由你决定；程序不会下单。",
                     "· A formed pattern says the fall has paused, not that it cannot resume: its void condition and the range's void level are given above. "
                     "Whether to buy and how much is yours to decide; the program never places orders."))
    out.append(FOOT)
    return tidy(title), tidy(first) + "\n" + tidy("\n".join(out))


def index_stop_alert(st: kl.IndexState, sf, rec=None, *, others: dict | None = None, vix: dict | None = None) -> tuple[str, str]:
    """(title, body) when an index that had broken down shows the stop-falling pattern."""
    title = L(f"{st.name}出现止跌形态", f"{st.name}: stop-falling pattern formed")
    out = [L(f"{st.name}（{st.symbol}）{'盘中' if st.live else f'{st.asof:%m-%d} 收盘'} {num(st.price)}（当日 {st.day_pct:+.2f}%），止跌形态的四项条件都已成立，"
             f"这一轮的低点是 {num(sf.low)}（{sf.low_date:%m-%d}）。",
             f"{st.name} ({st.symbol}) {'intraday' if st.live else f'close of {st.asof:%m-%d}'} {num(st.price)} ({st.day_pct:+.2f}% on the day): all four conditions of the "
             f"stop-falling pattern hold; the low of this leg is {num(sf.low)} ({sf.low_date:%m-%d}).")]
    out += [head("止跌形态", "The stop-falling pattern")] + stopfall_lines(sf, rec, st.live)
    out += [head("现在的位置", "Where it stands now")] + index_position(st) + _standing(st, set()) + market_lines(others or {}, vix, skip=st.symbol)
    up = next_levels(st, "up", 4)
    if up:
        out += [head("上方要收复的关键位", "Key levels to regain above")] + up
    out.append(head("接下来看什么", "What to watch next"))
    out.append(L(f"· 低点 {num(sf.low)} 是这次形态的底线：收盘跌破就作废，之前的破位仍然有效。",
                 f"· The low {num(sf.low)} is the line for this pattern: a close under it voids the pattern and the earlier break stands."))
    above = st.above()
    if above:
        out.append(L(f"· 止跌之后的第一道关口是{label(above[0])}（距现价 {gap(above[0].value, st.price)}）；站不回去的止跌多半只是下跌中的停顿。",
                     f"· The first hurdle after a stop is {_the_label(above[0])} ({gap(above[0].value, st.price)} from the price); a stop that cannot regain it is usually a pause in the decline."))
    out.append(FOOT)
    return tidy(title), tidy("\n".join(out))


def digest_lines(indices: dict, vix: dict | None = None) -> list[str]:
    """The market section of the daily digest: each index, its key levels and any break."""
    out = []
    for st in (indices or {}).values():
        out.append("  " + index_brief(st))
        out += ["    " + x[2:] for x in _standing(st, set())]
        below, above = st.below()[:3], st.above()[:2]
        if below:
            out.append(L("    下方关键位：", "    Key levels below: ") + L("；", "; ").join(
                L(f"{label(x)}（{gap(x.value, st.price)}）", f"{label(x)} ({gap(x.value, st.price)})") for x in below))
        if above:
            out.append(L("    上方：", "    Above: ") + L("；", "; ").join(
                L(f"{label(x)}（{gap(x.value, st.price)}）", f"{label(x)} ({gap(x.value, st.price)})") for x in above))
    if out and vix_text(vix):
        out.append("  " + vix_text(vix))
    return [tidy(x) for x in out]
