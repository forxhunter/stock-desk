"""Short, plain summaries of alerts for e-mail: what happened, what it means, the risk, what to watch.

Each function returns a handful of lines, one idea per line, with the numbers that matter. The full
write-up stays in the app's alerts page; mail carries these lines. No holdings, amounts or P&L.
"""
from __future__ import annotations

import math

from .i18n import L
from .insight import gap, num


def vix_line(vix: dict | None) -> str:
    """The VIX level, what zone it is in and the S&P move it prices in (VIX / sqrt(252) per day, / sqrt(12) per month)."""
    if not vix or vix.get("price") != vix.get("price"):
        return ""
    v = float(vix["price"])
    zone = (L("平静", "calm") if v < 15 else L("正常", "normal") if v < 20 else L("紧张", "tense") if v < 30
            else L("恐慌", "panic") if v < 40 else L("极度恐慌", "extreme panic"))
    day, month = v / math.sqrt(252), v / math.sqrt(12)
    return L(f"VIX {v:.1f}（今日 {vix.get('pct', 0):+.0f}%）：{zone}区。期权定价的标普波动约每天 ±{day:.1f}%、一个月 ±{month:.0f}%",
             f"VIX {v:.1f} ({vix.get('pct', 0):+.0f}% today): {zone} zone. Options price the S&P at about ±{day:.1f}% a day, ±{month:.0f}% a month")


_MEANING = {
    "ma": lambda lv: L("长期趋势转弱的常用标志" if lv.n >= 200 else "中期趋势转弱", "a common sign of a weaker long-term trend" if lv.n >= 200 else "the medium-term trend weakens"),
    "dd": lambda lv: L(f"距 52 周高点 −{lv.n:g}%，" + ("进入“熊市”区间" if lv.n >= 20 else "进入“回调”区间"),
                       f"−{lv.n:g}% from the 52-week high: " + ("bear-market territory" if lv.n >= 20 else "correction territory")),
    "low": lambda lv: L(f"创 {lv.n:g} 日收盘新低", f"a new {lv.n:g}-day closing low"),
    "zone": lambda lv: L("过去一年多次止跌的区域失守", "a zone where falls stopped several times in the past year has given way"),
    "mom": lambda lv: L("趋势跟踪基金（CTA）可能转为卖出的估算点", "an estimated point where trend-following funds (CTAs) may turn sellers"),
    "user": lambda lv: L("你设的关键位", "a level you set"),
    "high": lambda lv: L("创 52 周新高", "a new 52-week high"),
}


def index_down(st, direction: str, hits: list, vix: dict | None, plan_text: str = "", depth_text: str = "") -> list[str]:
    top = hits[0]
    down = direction == "down"
    i = st.ind
    vol = i.get("vol_ratio")
    now = L(f"现在 {num(st.price)}（今日 {st.day_pct:+.1f}%），距 52 周高点 {i['dd']:+.1f}%",
            f"Now {num(st.price)} ({st.day_pct:+.1f}% today), {i['dd']:+.1f}% from the 52-week high")
    if vol and not st.live:
        now += L(f"；成交量为 20 日均量 {vol:.1f} 倍（{'放量' if vol >= 1.2 else '缩量' if vol < 0.9 else '平常'}）",
                 f"; volume {vol:.1f}x its 20-day average ({'heavy' if vol >= 1.2 else 'light' if vol < 0.9 else 'ordinary'})")
    out = [now]
    mean = []
    if top.kind == "user" and top.source:
        mean.append(top.source.split("；")[0].split(";")[0][:70])
    mean += [_MEANING.get(x.kind, lambda lv: "")(x) for x in hits[:3] if not (x.kind == "user" and mean)][:2]
    out.append(L("意味着：", "What it means: ") + L("；", "; ").join(m for m in mean if m))
    risk = vix_line(vix)
    rec = st.records.get(f"{top.key}:{direction}")
    if rec is not None and 21 in rec.fwd and down:
        risk_hist = L(f"历史上同类破位 {rec.n} 次：之后 21 日中位数 {rec.fwd[21][1] * 100:+.1f}%，最差一成 {rec.fwd[21][3] * 100:+.1f}%",
                      f"{rec.n} like breaks in the past: median {rec.fwd[21][1] * 100:+.1f}% after 21 sessions, worst tenth {rec.fwd[21][3] * 100:+.1f}%")
        risk = f"{risk}\n{risk_hist}" if risk else risk_hist
    if risk:
        out += [L("风险：", "Risk: ") + x for x in risk.split("\n")]
    if depth_text:
        out.append(depth_text)
    cur = st.level(top.key)
    ref = cur.value if cur is not None else top.value
    if down:
        nxt = st.below()[:1]
        watch = L(f"收复 {num(ref)} = 假破位", f"back above {num(ref)} = a false break")
        if nxt:
            watch += L(f"；继续跌的下一关键位 {num(nxt[0].value)}（{gap(nxt[0].value, st.price)}）",
                       f"; next key level below {num(nxt[0].value)} ({gap(nxt[0].value, st.price)})")
    else:
        watch = L(f"能否守住 {num(ref)}", f"whether {num(ref)} holds")
    out.append(L("接下来看：", "Watch next: ") + watch)
    if st.live:
        out.append(L("状态：盘中，未收盘确认", "Status: intraday, not confirmed by a close"))
    if plan_text:
        out.append(L("你的预案：", "Your playbook: ") + plan_text)
    return out


def index_stop(st, sf, vix: dict | None) -> list[str]:
    out = [L(f"现在 {num(st.price)}（今日 {st.day_pct:+.1f}%），这一轮低点 {num(sf.low)}（{sf.low_date:%m-%d}）",
             f"Now {num(st.price)} ({st.day_pct:+.1f}% today), low of this leg {num(sf.low)} ({sf.low_date:%m-%d})"),
           L("意味着：跌势暂时停住（四项条件都成立），不等于见底", "What it means: the fall has paused (all four conditions hold); not the same as a bottom")]
    if vix_line(vix):
        out.append(L("风险：", "Risk: ") + vix_line(vix))
    up = st.above()[:1]
    out.append(L("接下来看：", "Watch next: ") + L(f"跌破 {num(sf.low)} 就作废", f"a close under {num(sf.low)} voids it")
               + (L(f"；第一道关口 {num(up[0].value)}（{gap(up[0].value, st.price)}）", f"; first hurdle {num(up[0].value)} ({gap(up[0].value, st.price)})") if up else ""))
    return out


def plan(key: str, level: float, st, text: str) -> list[str]:
    px = st.check_price
    who = st.follow or st.symbol
    out = [text.split("\n")[0][:160]]
    rest = []
    for lvl, w in getattr(st, "steps", []):
        if lvl < level and px == px:
            rest.append(L(f"{num(lvl)} → {w:g}%（{gap(lvl, px)}）", f"{num(lvl)} → {w:g}% ({gap(lvl, px)})"))
    if st.next_buy is not None and key == "buy":
        rest.append(L(f"下一档买入价 {num(st.next_buy)}（{gap(st.next_buy, px)}）", f"next buy level {num(st.next_buy)} ({gap(st.next_buy, px)})"))
    if rest:
        out.append(L("后面几步：", "Next steps: ") + L("；", "; ").join(rest))
    out.append(L(f"依据：价位是你自己在 {who} 计划里写的", f"Basis: levels you wrote in the {who} plan"))
    return out


def bottom(sym: str, st, need: set) -> list[str]:
    out = [L(f"{sym} 收盘 {num(st.close)}，距 52 周高点 {st.drawdown_pct:.0f}%；{st.summary} 成立", f"{sym} closed at {num(st.close)}, {st.drawdown_pct:.0f}% from the 52-week high; {st.summary} hold")]
    follow = next((d for k, ok, d in st.checks if k == "vol_follow" and ok), "")
    if follow:
        out.append(L("放量上涨：", "Volume-confirmed rally: ") + follow)
    out.append(L("意味着：有成交量确认的筑底迹象，不是预测", "What it means: signs of a base backed by volume; not a prediction"))
    out.append(L(f"作废位：收盘跌破这一轮低点 {num(st.base_low)}（{gap(st.base_low, st.close)}）", f"Void if a close falls under the low of this leg, {num(st.base_low)} ({gap(st.base_low, st.close)})"))
    return out


def range_edge(sym: str, rg, edge: str, price: float, rec: dict | None) -> list[str]:
    sell = edge == "sell"
    out = [L(f"{sym} 现价 {num(price)}，在区间 {rg.pos * 100:.0f}% 位置（支撑 {num(rg.support)} / 阻力 {num(rg.resistance)}）",
             f"{sym} at {num(price)}, {rg.pos * 100:.0f}% of its range (support {num(rg.support)} / resistance {num(rg.resistance)})")]
    if rec:
        out.append(L(f"过去进入{'高抛区' if sell else '低吸区'} {rec['n']} 次：{'回落' if sell else '反弹'} {rec['turned']} 次、{'向上突破' if sell else '跌破'} {rec['broke']} 次",
                     f"Earlier entries into this zone: {rec['n']}; {'fell back' if sell else 'bounced'} {rec['turned']}, {'broke out' if sell else 'broke down'} {rec['broke']}"))
    lvl = rg.breakout if sell else rg.stop
    out.append(L(f"作废位：收盘{'高于' if sell else '低于'} {num(lvl)}（{gap(lvl, price)}）区间失效", f"Void: a close {'above' if sell else 'below'} {num(lvl)} ({gap(lvl, price)}) ends the range"))
    return out


def _sentence(text: str) -> str:
    for mark in ("。", ". "):
        if mark in text:
            return text.split(mark)[0] + ("。" if mark == "。" else ".")
    return text


def flow_item(i) -> list[str]:
    out = [i.headline]
    if i.facts:
        out.append(i.facts[0])
    if i.reading:
        out.append(L("意味着：", "What it means: ") + _sentence(i.reading[-1] if i.source == "polymarket" and len(i.reading) > 1 else i.reading[0]))
    depth = next((d for d in i.detail if "挂单深度" in d or "order-book depth" in d), "")
    if depth:
        seg = next((x for x in depth.replace("，", "；").replace(", ", "; ").split("；") if "挂单深度" in x or "order-book depth" in x), "")
        vol = next((x for x in depth.replace("，", "；").replace(", ", "; ").split("；") if "24 小时成交" in x or "24h volume" in x), "")
        out.append(L("交易深度：", "Market depth: ") + L("，", ", ").join(x.strip() for x in (vol, seg) if x))
    return [x[:180] for x in out]


def day_points(snap) -> list[str]:
    """The top of the daily digest: the market in a line, the VIX, today's important alerts, each plan's next step."""
    out = []
    for st in (snap.indices or {}).values():
        below, above = st.below()[:1], st.above()[:1]
        line = L(f"{st.name} {num(st.price)}（今日 {st.day_pct:+.1f}%），距 52 周高点 {st.ind['dd']:+.1f}%",
                 f"{st.name} {num(st.price)} ({st.day_pct:+.1f}% today), {st.ind['dd']:+.1f}% from the 52-week high")
        if below:
            line += L(f"；下方 {_lname(below[0])} {gap(below[0].value, st.price)}", f"; below: {_lname(below[0])} {gap(below[0].value, st.price)}")
        if above:
            line += L(f"，上方 {_lname(above[0])} {gap(above[0].value, st.price)}", f", above: {_lname(above[0])} {gap(above[0].value, st.price)}")
        out.append(line)
    if vix_line(snap.vix):
        out.append(vix_line(snap.vix))
    from . import depth as _depth
    if _depth.line(getattr(snap, "depth", None)):
        out.append(_depth.line(snap.depth))
    rates = getattr(snap, "rates", None) or {}
    from . import treasury
    if rates.get("summary"):
        out.append(treasury.curve_line(rates["summary"]))
    last = (rates.get("auctions") or [])[-1:]
    if last:
        a = last[0]
        out.append(L(f"最近一次国债拍卖：{a['term']} {a['date'][5:]}，倍数 {a.get('btc') or 0:.2f}，{treasury.VERDICT.get(a.get('verdict'), '')}",
                     f"Latest Treasury auction: {a['term']} {a['date'][5:]}, bid-to-cover {a.get('btc') or 0:.2f}, {treasury.VERDICT.get(a.get('verdict'), '')}"))
    import time as _t
    hot = [a for a in snap.alerts if a.severity in ("high", "warn") and not a.demo and a.created >= _t.time() - 86400
           and a.kind in ("index_level", "plan_level", "rates", "bottoming", "range", "congress", "flow")]
    hot.sort(key=lambda a: (0 if a.severity == "high" else 1, a.kind != "index_level"))
    for a in hot[:3]:
        out.append(L(f"{'重要' if a.severity == 'high' else '留意'}：{a.title}", f"{'Important' if a.severity == 'high' else 'Note'}: {a.title}"))
    for p in snap.plans:
        px = p.check_price
        if px != px:
            continue
        step = next(((lvl, w) for lvl, w in getattr(p, "steps", []) if px >= lvl), None)
        if step:
            out.append(L(f"{p.symbol} 计划：下一步减仓价 {num(step[0])}（{gap(step[0], px)}）→ 占比 {step[1]:g}%",
                         f"{p.symbol} plan: next trim step {num(step[0])} ({gap(step[0], px)}) → weight {step[1]:g}%"))
        elif p.next_buy is not None:
            out.append(L(f"{p.symbol} 计划：下一档买入价 {num(p.next_buy)}（{gap(p.next_buy, px)}），需止跌形态成立才提醒",
                         f"{p.symbol} plan: next buy level {num(p.next_buy)} ({gap(p.next_buy, px)}); alerts only once the stop-falling pattern forms"))
    return out


def _lname(lv) -> str:
    from .insight import label
    return label(lv)


def congress_note(note, m, tier_text: str) -> list[str]:
    rows = L("；", "; ").join(L(f"{'买入' if c.side == 'buy' else '卖出'} {c.ticker}", f"{'bought' if c.side == 'buy' else 'sold'} {c.ticker}")
                             for c in note.calls[:6])
    out = [L(f"{note.member}：{rows}（交易日 {min(c.traded for c in note.calls)[5:]}，{note.filed[5:]} 披露）",
             f"{note.member}: {rows} (traded {min(c.traded for c in note.calls)[5:]}, disclosed {note.filed[5:]})")]
    if m is not None and m.n:
        out.append(L(f"记录：{tier_text}，{m.n} 笔可评估，方向正确 {m.hit_rate * 100:.0f}%，平均超额 {m.mean * 100:+.1f}%",
                     f"Record: {tier_text}, {m.n} judged calls, {m.hit_rate * 100:.0f}% right, average excess {m.mean * 100:+.1f}%"))
    return out
