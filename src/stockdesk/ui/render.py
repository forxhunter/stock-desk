"""HTML builders for the rich-text panels (Qt rich text subset). Pure functions, no widgets."""
from __future__ import annotations

import html
import math
import time
from datetime import datetime

from ..engine import Snapshot, TickerView
from ..models import Alert, MarketOdds, NewsItem
from ..quant import optimizer
from ..quant.analysis import DISCLAIMER
from . import theme as T

esc = html.escape
_TD = f'style="padding:5px 10px; border-bottom:1px solid {T.LINE};"'
_TH = f'style="padding:5px 10px; color:{T.SUB}; font-weight:normal; border-bottom:1px solid {T.LINE};" align="left"'


def _h(title: str, sub: str = "") -> str:
    s = f'<span style="color:{T.SUB}; font-size:9pt;">　{esc(sub)}</span>' if sub else ""
    return f'<p style="margin:16px 0 6px 0; font-size:11.5pt; font-weight:bold;">{esc(title)}{s}</p>'


def _note(text: str, color: str = T.SUB) -> str:
    return f'<p style="margin:4px 0; color:{color}; font-size:9pt; line-height:140%;">{text}</p>'


def _span(text: str, color: str, bold: bool = False) -> str:
    return f'<span style="color:{color};{" font-weight:bold;" if bold else ""}">{text}</span>'


def _bar(score: float, pal: T.Palette, cells: int = 5) -> str:
    """A five-cell strength gauge, coloured by direction."""
    n = max(0, min(cells, int(round(abs(score) * cells))))
    return (f'<span style="color:{pal.sign(score)};">{"■" * n}</span>'
            f'<span style="color:{T.LINE};">{"■" * (cells - n)}</span>')


def disclaimer_html() -> str:
    return _note(f"⚠ {esc(DISCLAIMER)} 本程序只读取数据，不会也不能下单。", T.AMBER)


# ---- ticker detail ---------------------------------------------------------------------------

def analysis_html(v: TickerView, pal: T.Palette, ref=None) -> str:
    a = v.analysis
    out = [disclaimer_html()]
    if not a or not a.ok:
        out.append(_note(esc(a.headline if a else "暂无数据（行情源还没有返回这只股票的历史）。")))
        return "".join(out)
    col = pal.stance(a.stance)
    out.append(f'<p style="margin:10px 0 2px 0; font-size:15pt; font-weight:bold; color:{col};">{a.stance}'
               f'<span style="font-size:10pt; color:{T.SUB}; font-weight:normal;">　综合分 {a.composite:+.2f}（−1 ~ +1）</span></p>')
    out.append(_note(esc(a.headline), T.TEXT))

    out.append(_h("信号拆解", "每一项的输入数据都列在右侧"))
    rows = [f"<tr><th {_TH}>信号</th><th {_TH}>判断</th><th {_TH}>强度</th><th {_TH}>依据</th></tr>"]
    for s in a.signals:
        rows.append(f"<tr><td {_TD}>{esc(s.name)}</td><td {_TD}>{_span(esc(s.label), pal.sign(s.score), True)}</td>"
                    f"<td {_TD}>{_bar(s.score, pal)} <span style='color:{T.SUB}; font-size:8.5pt;'>{s.score:+.2f}</span></td>"
                    f"<td {_TD}><span style='color:{T.SUB};'>{esc(s.detail)}</span></td></tr>")
    out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')

    if a.range is not None:
        out.append(range_html(a.range, pal))
    if ref is not None:
        out.append(reference_html({v.symbol: ref}, pal))

    out.append(_h("参考思路与风险位"))
    for line in a.rationale:
        if line.startswith(("【持仓】", "【跟踪止损", "【参考思路】", "【若新建仓", "【仓位上限】")):
            tag, rest = line.split("】", 1)
            if T.PRIVATE and tag in ("【持仓", "【跟踪止损参考", "【仓位上限"):
                rest = "隐私模式下已隐藏。"
            out.append(f'<p style="margin:5px 0; line-height:145%;">{_span(esc(tag + "】"), T.BLUE, True)}{esc(rest)}</p>')
    if v.levels_src == "user" and not T.PRIVATE:
        out.append(_note(f"小组件进度条使用的是你自己设定的止损 {T.price(v.stop)} / 目标 {T.price(v.target)}。"))

    out.append(_h("历史回测", "同一只股票 · 默认参数 · 信号次日开盘成交 · 已扣成本"))
    rows = [f"<tr><th {_TH}>规则</th><th {_TH}>现在</th><th {_TH} align='right'>交易数</th><th {_TH} align='right'>胜率 (95% 区间)</th>"
            f"<th {_TH} align='right'>累计</th><th {_TH} align='right'>买入持有</th><th {_TH} align='right'>最大回撤</th>"
            f"<th {_TH} align='right'>夏普</th><th {_TH}>提示</th></tr>"]
    for b in a.backtests:
        if not b.n_bars:
            rows.append(f"<tr><td {_TD}>{esc(b.label)}</td><td {_TD} colspan='8'><span style='color:{T.DIM};'>{esc(' '.join(b.notes))}</span></td></tr>")
            continue
        hr = "—" if b.hit_rate is None else f"{b.hit_rate*100:.0f}%（{b.hit_ci[0]*100:.0f}–{b.hit_ci[1]*100:.0f}%）"
        star = " ★" if a.evidence is b else ""
        now = _span("持有", pal.up) if b.in_position_now else _span("空仓", T.DIM)
        rows.append(
            f"<tr><td {_TD}><b>{esc(b.label)}{star}</b><br><span style='color:{T.DIM}; font-size:8.5pt;'>{esc(b.rule)}</span></td>"
            f"<td {_TD}>{now}</td><td {_TD} align='right'>{b.n_trades}</td><td {_TD} align='right'>{hr}</td>"
            f"<td {_TD} align='right'>{_span(T.pct(b.total_return*100, 0), pal.sign(b.total_return))}</td>"
            f"<td {_TD} align='right'>{T.pct(b.bh_return*100, 0)}</td>"
            f"<td {_TD} align='right'>{b.max_dd*100:.0f}%<span style='color:{T.DIM};'> / {b.bh_max_dd*100:.0f}%</span></td>"
            f"<td {_TD} align='right'>{b.sharpe:.2f}</td>"
            f"<td {_TD}><span style='color:{T.AMBER}; font-size:8.5pt;'>{esc(' '.join(b.notes))}</span></td></tr>")
    out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
    out.append(_note("★ = 与当前信号最接近、被用来估算凯利仓位的规则。最大回撤一栏为 策略 / 买入持有。"))

    if v.news or v.odds:
        out.append(research_html(v.news, v.odds, compact=True))

    out.append(_h("请记住这些局限"))
    out.append("".join(_note("· " + esc(c)) for c in a.caveats))
    return "".join(out)


_RANGE_COL = {"buy": "up", "sell": "down", "breakdown": "down", "breakout_up": "up"}
_EV = {"buy": "低吸", "sell": "高抛", "stop": "跌破止损", "time": "超时离场"}


def range_html(r, pal: T.Palette) -> str:
    """Section for the phase-based range-trading (高抛低吸) rule."""
    out = [_h("震荡区间 · 高抛低吸", "横盘时在支撑附近低吸、阻力附近高抛；区间被突破即停用")]
    if r.status == "short":
        out.append(_note(esc(r.detail)))
        return "".join(out)
    key = _RANGE_COL.get(r.status)
    col = getattr(pal, key) if key else (T.AMBER if r.status == "loose" else T.SUB)
    out.append(f'<p style="margin:4px 0; font-size:12pt; font-weight:bold; color:{col};">{esc(r.label)}'
               + (f'<span style="font-size:9pt; color:{T.SUB}; font-weight:normal;">　规则当前持有低吸仓位</span>'
                  if r.in_trade else "") + "</p>")
    out.append(f'<p style="margin:4px 0; line-height:145%;">{esc(r.detail)}</p>')
    if r.buy_band:
        rows = [f"<tr><th {_TH}>位置</th><th {_TH} align='right'>价格</th><th {_TH}>含义</th></tr>"]
        for name, val, meaning, c in (
                ("突破位", f"{r.breakout:.2f}", "收盘站上 = 向上突破，区间作废", T.SUB),
                ("高抛区", f"{r.sell_band[0]:.2f} – {r.sell_band[1]:.2f}", "区间上沿 20%：卖出 / 减仓参考", pal.down),
                ("低吸区", f"{r.buy_band[0]:.2f} – {r.buy_band[1]:.2f}", "区间下沿 20%：买入参考", pal.up),
                ("失效位", f"{r.stop:.2f}", "收盘跌破 = 区间跌破，止损参考", T.SUB)):
            rows.append(f"<tr><td {_TD}>{_span(name, c, True)}</td><td {_TD} align='right'>{val}</td>"
                        f"<td {_TD}><span style='color:{T.SUB};'>{meaning}</span></td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
    p = r.params
    out.append(_note(f"检测依据：最近 {int(p.get('lookback', 60))} 日 · 走势效率 {r.er:.2f}（≤{p.get('max_er', 0.3):g} 才算震荡）"
                     f" · 回归漂移 {r.drift*100:.0f}% 区间宽度（≤{p.get('max_drift', 0.5)*100:.0f}%）"
                     f" · 下沿 / 上沿测试 {r.touches[0]} / {r.touches[1]} 次 · ATR {r.atr:.2f}"))
    recent = r.events[-4:]
    if recent:
        out.append(_note("规则最近的信号：" + "；".join(f"{_EV.get(e.kind, e.kind)} @ {e.price:.2f}" for e in recent)
                         + "（图上 ▲ 低吸、▼ 高抛 / 离场；完整历史表现见下方回测表“震荡区间高抛低吸”一行）"))
    out.append(_note("高抛低吸只在横盘阶段有效；趋势行情里它会反复在“支撑”处买入下跌的股票。"
                     "所以区间一旦被跌破，规则要求止损并停用，而不是继续摊低成本。", T.AMBER))
    return "".join(out)


# ---- research / prediction markets -----------------------------------------------------------

def _chg(x: float | None, pal: T.Palette | None = None) -> str:
    if x is None:
        return f'<span style="color:{T.DIM};">—</span>'
    pts = x * 100
    col = T.SUB if abs(pts) < 0.05 else (T.BLUE if pts > 0 else T.AMBER)
    return f'<span style="color:{col};">{"+" if pts >= 0 else "−"}{abs(pts):.1f}</span>'


def research_html(news: list[NewsItem], odds: list[MarketOdds], compact: bool = False, status: dict | None = None) -> str:
    out = []
    if not compact:
        out.append(_note("这些是背景信息，和量化建议并列展示，供你自己判断；它们不会自动触发任何建议或提醒。"
                         "预测市场价格反映的是下注者的定价，流动性差的市场尤其不可靠。", T.AMBER))
    out.append(_h("预测市场概率", "Polymarket 公开行情 · 只读"))
    if odds:
        rows = [f"<tr><th {_TH}>主题</th><th {_TH}>市场</th><th {_TH} align='right'>概率 (Yes)</th>"
                f"<th {_TH} align='right'>1日</th><th {_TH} align='right'>1周</th><th {_TH} align='right'>1月</th>"
                f"<th {_TH} align='right'>24h 成交</th><th {_TH} align='right'>总成交</th><th {_TH}>到期</th></tr>"]
        for o in odds[: 6 if compact else 40]:
            tick = f' <span style="color:{T.BLUE}; font-size:8.5pt;">[{esc(",".join(o.tickers))}]</span>' if o.tickers else ""
            rows.append(
                f"<tr><td {_TD}><span style='color:{T.SUB};'>{esc(o.label)}</span></td>"
                f"<td {_TD}><a href='{esc(o.url)}' style='color:{T.TEXT}; text-decoration:none;'>{esc(o.question)}</a>{tick}</td>"
                f"<td {_TD} align='right'><b>{o.prob*100:.1f}%</b></td><td {_TD} align='right'>{_chg(o.change_1d)}</td>"
                f"<td {_TD} align='right'>{_chg(o.change_1w)}</td><td {_TD} align='right'>{_chg(o.change_1m)}</td>"
                f"<td {_TD} align='right'>{T.compact(o.volume_24h)}</td><td {_TD} align='right'>{T.compact(o.volume)}</td>"
                f"<td {_TD}><span style='color:{T.SUB};'>{esc(o.end_date)}</span></td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
        out.append(_note("变化列的单位是百分点。可在 data/config.yaml 的 research.polymarket.keywords / slugs 里固定你关心的主题。"))
    else:
        out.append(_note("暂无数据（未启用、离线，或没有匹配的市场）。"))

    out.append(_h("SemiAnalysis", "semianalysis.com 公开 RSS · 仅标题与摘要"))
    if news:
        for n in news[: 6 if compact else 40]:
            day = datetime.fromtimestamp(n.published).strftime("%Y-%m-%d") if n.published else ""
            tick = "".join(f' <span style="color:{T.BLUE}; font-size:8.5pt;">[{esc(t)}]</span>' for t in n.tickers)
            out.append(f'<p style="margin:9px 0 1px 0;"><a href="{esc(n.link)}" style="color:{T.TEXT}; text-decoration:none; '
                       f'font-weight:bold;">{esc(n.title)}</a>{tick}'
                       f'<span style="color:{T.DIM}; font-size:8.5pt;">　{day}</span></p>')
            if n.summary and not compact:
                out.append(_note(esc(n.summary)))
        out.append(_note("点击标题在浏览器打开原文。付费内容需要你自己的订阅，本程序不会绕过付费墙。"))
    else:
        out.append(_note("暂无相关文章。" if compact else "暂无数据（未启用或离线）。"))
    return "".join(out)


# ---- unusual-activity scan ---------------------------------------------------------------------

def flow_html(rep, pal: T.Palette) -> str:
    from ..flow import TIER_LABEL
    out = [_note("按固定规则筛出的<b>异常成交</b>：Polymarket 大额下注、期权放量、期货大幅波动。每条都写明触发了哪条规则和具体数字，"
                 "“怎么看”是核对思路，不是预测，也不是买卖建议。程序只读公开数据，不能下注或下单。", T.AMBER)]
    if rep is None:
        return "".join(out) + _note("异动扫描已关闭（data/config.yaml 里 flow.enabled）。")
    if not rep.ts:
        return "".join(out) + _note("还没有扫描结果，第一次扫描会在启动后一两分钟内完成。")
    out.append(_note(f"上次扫描 {datetime.fromtimestamp(rep.ts):%m-%d %H:%M}，每 10 分钟自动扫一次（期权每 30 分钟）。"
                     + ("　出错：" + esc("；".join(rep.errors)) if rep.errors else "")))
    notes: dict[str, list[str]] = {}
    for n in rep.notes:
        k, _, text = n.partition("|")
        notes.setdefault(k, []).append(text)
    sections = (("polymarket", "Polymarket 大额下注", "单笔 ≥ $1 万的成交 · 近 60 分钟"),
                ("options", "期权异动", "Yahoo 期权链 · 延迟行情 · 成交量为当日累计"),
                ("futures", "期货 / 波动率", "Yahoo 日线 · 盘中为未走完的一天"))
    for key, title, sub in sections:
        items = rep.by(key)
        out.append(_h(title, sub))
        if not items:
            out.append(_note("这一轮没有达到门槛的异动。"))
        for i in items:
            col, dot = _FLAG[i.severity]
            tier = f'<span style="color:{T.BLUE if i.tier == "watch" else T.SUB}; font-size:8.5pt;">　{esc(TIER_LABEL[i.tier])}'                    f'{" [" + esc(",".join(i.tickers)) + "]" if i.tickers else ""}</span>'
            link = f'<a href="{esc(i.url)}" style="color:{T.TEXT}; text-decoration:none; font-weight:bold;">{esc(i.title)}</a>'                 if i.url else f"<b>{esc(i.title)}</b>"
            out.append(f'<p style="margin:12px 0 2px 0;"><span style="color:{col};">{dot}</span> {link}{tier}</p>')
            out.append(f'<p style="margin:0 0 2px 16px;">{esc(i.headline)}</p>')
            out.append("".join(f'<p style="margin:0 0 1px 16px; color:{T.SUB}; font-size:9pt;">· {esc(f)}</p>' for f in i.facts))
            out.append("".join(f'<p style="margin:1px 0 1px 16px; color:{T.DIM}; font-size:9pt; line-height:135%;">'
                               f'{"怎么看：" if n == 0 else "　　　　"}{esc(r)}</p>' for n, r in enumerate(i.reading)))
        for text in notes.get(key, []):
            out.append(_note(esc(text)))
    out.append(_note("门槛都可以在 data/config.yaml 的 flow 一节里改（big_usd、wallet_usd、vol_oi、z 等）。"
                     "大单、放量和大波动经常出现而后续什么也没发生；把它们当作“值得看一眼”的线索，而不是结论。"))
    return "".join(out)


# ---- daily briefing ---------------------------------------------------------------------------

_FLAG = {"high": (T.RED, "●"), "warn": (T.AMBER, "●"), "info": (T.BLUE, "●")}


def private_html(what: str) -> str:
    return (f'<p style="margin:60px 0; font-size:12pt; color:{T.SUB};" align="center">隐私模式已开启，{esc(what)}已隐藏。'
            f'<br><span style="font-size:9.5pt;">点小组件顶部的眼睛图标，或右键小组件 → “隐藏关键数据”，可恢复显示。</span></p>')


def reference_html(refs: dict, pal: T.Palette) -> str:
    """Home-market share next to its foreign listing (e.g. SK Hynix in Seoul vs the HY9H GDR)."""
    out = []
    for r in refs.values():
        out.append(_h(f"{r.symbol} 对应的本土股票：{r.name}", f"{r.ref_symbol} · 截至 {r.ref_asof:%Y-%m-%d} 收盘"))
        cells = [("收盘", f"{r.ref_price:,.0f} {esc(r.ref_currency)}", T.TEXT),
                 ("当日", T.pct(r.ref_day_pct), pal.sign(r.ref_day_pct)),
                 ("5 日", T.pct(r.ref_chg_5d, 1), pal.sign(r.ref_chg_5d)),
                 ("1 月", T.pct(r.ref_chg_21d, 1), pal.sign(r.ref_chg_21d)),
                 ("半年", T.pct(r.ref_chg_126d, 1), pal.sign(r.ref_chg_126d)),
                 ("距 52 周高点", T.pct(r.ref_dd_252, 1), pal.sign(r.ref_dd_252))]
        if r.ref_stance:
            cells.append(("综合信号", f"{esc(r.ref_stance)} {r.ref_composite:+.2f}", pal.stance(r.ref_stance)))
            cells.append(("震荡区间", esc(r.ref_range or "—"), T.SUB))
        head = "".join(f"<th {_TH} align='right'>{k}</th>" for k, _, _ in cells)
        body = "".join(f"<td {_TD} align='right'>{_span(v, c)}</td>" for _, v, c in cells)
        out.append(f'<table cellspacing="0" width="100%"><tr>{head}</tr><tr>{body}</tr></table>')
        if r.premium_pct == r.premium_pct:
            z = (r.premium_pct - r.premium_avg) / r.premium_std if r.premium_std else 0.0
            where = "明显偏高" if z > 1.5 else "明显偏低" if z < -1.5 else "在平时的范围内"
            stale = f"（{r.symbol} 在这之后又交易了 {r.stale_days} 天，差价里包含这段时间的涨跌）" if r.stale_days else ""
            out.append(f'<p style="margin:6px 0; line-height:150%;">按汇率折算，本土收盘价相当于 '
                       f'<b>{r.implied:,.2f} {esc(r.currency)}</b>；{esc(r.symbol)} 现价相对它 '
                       f'{_span(T.pct(r.premium_pct, 1), T.TEXT, True)}{stale}。'
                       f'近 60 日这个差价平均 {T.pct(r.premium_avg, 1)}、波动 ±{r.premium_std:.1f}%，目前{where}。</p>')
        notes = []
        if r.corr == r.corr:
            notes.append(f"两者近 120 日的日涨跌相关系数 {r.corr:.2f}")
        if r.ref_below_falling_sma50:
            notes.append("本土股价也在下行的 50 日均线下方")
        notes.append("首尔比法兰克福先收盘，本土当日的涨跌通常会先反映出来，可作为当天的参考，但不保证同向")
        out.append(_note("；".join(notes) + "。"))
    return "".join(out)


_PLAN_MISSING = "未填写"
from ..plans import fmt as plan_fmt, fmt_px as plan_px  # noqa: E402


def _progress(frac: float, color: str, width: int = 420) -> str:
    """A thin horizontal bar (Qt rich text has no CSS bars, so use a two-cell table)."""
    a = max(1, min(width - 1, int(round(width * max(0.0, min(1.0, frac))))))
    return (f'<table cellspacing="0" cellpadding="0" width="{width}" style="margin:3px 0;"><tr>'
            f'<td width="{a}" height="6" bgcolor="{color}"></td><td width="{width - a}" height="6" bgcolor="{T.LINE}"></td>'
            f'</tr></table>')


def plans_html(plans: list, pal: T.Palette) -> str:
    out = [_h("我的计划", "数字由你自己写在 data/plans.yaml；程序只负责每天对照")]
    if not plans:
        out.append(_note("还没有计划。右键某只股票 → “编辑我的计划…” 可以写一份。"))
        return "".join(out)
    for p in plans:
        span = f"{p.start} → {p.end}，已过 {p.days_done} 天，还剩 {p.days_left} 天" if p.days_total else "起止日期未填写"
        out.append(f'<p style="margin:8px 0 2px 0;"><b>{esc(p.symbol)}</b>　<span style="color:{T.SUB};">{span}'
                   + (f"　·　下次复查 {p.next_review}" if p.next_review else "") + "</span>"
                   f'　<a href="plan:{esc(p.symbol)}" style="color:{T.BLUE}; text-decoration:none;">编辑计划…</a></p>')
        F = plan_fmt
        ccy = f" {esc(p.level_ccy)}" if p.follow and p.level_ccy else ""

        def loc(x):         # a followed level, also shown in the held stock's own currency
            return f"<span style='color:{T.DIM};'>（≈ {x * p.to_local:,.0f} {esc(p.local_ccy)}）</span>" \
                if p.follow and p.to_local else ""
        if p.follow:
            px = f"现价 {plan_px(p.check_price)}{ccy}" if p.check_price == p.check_price else "暂无行情"
            out.append(_note(f"价位按 <b>{esc(p.follow_name)}</b>（{esc(p.follow)}）的股价填写和判断　·　{px}{loc(p.check_price) if p.check_price == p.check_price else ''}", T.BLUE))
        if p.days_total:
            out.append(_note("时间进度") + _progress(p.days_done / p.days_total, T.BLUE))
        if p.target_weight_pct is not None and p.start_weight_pct and p.start_weight_pct != p.target_weight_pct:
            done = (p.start_weight_pct - p.weight_pct) / (p.start_weight_pct - p.target_weight_pct)
            out.append(_note(f"占比进度（{p.start_weight_pct:g}% → {p.target_weight_pct:g}%，现在 {p.weight_pct:.1f}%）")
                       + _progress(done, T.GREEN))
        tgt = f"{p.target_weight_pct:g}%" if p.target_weight_pct is not None else _PLAN_MISSING
        path = f"（按时间均匀推进，今天应在 {p.path_weight_pct:.1f}% 附近）" if p.path_weight_pct is not None else ""
        lv = " / ".join(F(x) for x in p.reduce_levels) or _PLAN_MISSING
        nxt = f"，下一个 {F(p.next_level)}" if p.next_level else ""
        exit_txt = f"{F(p.exit_below)}{ccy} {loc(p.exit_below)}" if p.exit_below else _PLAN_MISSING
        if p.building:                                               # a build-up plan
            cap = f"　上限 {p.max_weight_pct:g}%" if p.max_weight_pct else "　上限 " + _PLAN_MISSING
            rows = [("占组合", f"现在 {p.weight_pct:.1f}%{cap}")]
            done = f"（已到 {' / '.join(F(x) for x in p.buy_reached)}）" if p.buy_reached else ""
            nb = f"，下一个 {F(p.next_buy)}" if p.next_buy else ""
            rows.append(("分批买入价位", (" / ".join(F(x) for x in p.buy_levels) + ccy + done + nb)
                         if p.buy_levels else _PLAN_MISSING))
            cp = p.check_price
            gap = f"（现价 {plan_px(cp)}，还差 {(p.target_price / cp - 1) * 100:+.1f}%）" if p.target_price and cp == cp and cp else ""
            rows.append(("目标价", f"{F(p.target_price)}{ccy}{gap}" if p.target_price else _PLAN_MISSING))
            rows.append(("底线价", exit_txt))
            if p.reduce_levels:
                rows.append(("分批减仓价位", f"{lv}{ccy}{nxt}"))
        else:
            rows = [("占组合", f"现在 {p.weight_pct:.1f}%　→　目标 {tgt}{path}"),
                    ("底线价", exit_txt),
                    ("分批减仓价位", f"{lv}{ccy if p.reduce_levels else ''}{nxt}")]
            if p.buy_levels:
                rows.append(("分批买入价位", " / ".join(F(x) for x in p.buy_levels) + ccy))
            if p.target_price:
                rows.append(("目标价", f"{F(p.target_price)}{ccy}"))
        if p.notes:
            rows.append(("写给自己的话", esc(p.notes)))
        out.append('<table cellspacing="0">' + "".join(
            f"<tr><td {_TD}><span style='color:{T.SUB};'>{k}</span></td><td {_TD}>{v}</td></tr>" for k, v in rows) + "</table>")
        for lvl, text in p.flags:
            col, dot = _FLAG.get(lvl, _FLAG["info"])
            out.append(f'<p style="margin:4px 0;">{_span(dot, col)} {esc(text)}</p>')
        if p.missing:
            out.append(_note(f"还没填：{'、'.join(p.missing)}。这些数字需要你自己决定，程序不会替你填。", T.AMBER))
    return "".join(out)


def bottoms_html(bottoms: dict, pal: T.Palette) -> str:
    out = [_h("筑底观察", "价格条件和成交量条件都达到门槛才提醒；只是形态统计，不是预测")]
    for sym, b in bottoms.items():
        if not b.eligible:
            out.append(f'<p style="margin:5px 0;"><b>{esc(sym)}</b>　<span style="color:{T.SUB};">距 52 周高点 '
                       f'{b.drawdown_pct:.1f}%，跌幅不够大，不在筑底观察范围内。</span></p>')
            continue
        col = pal.up if b.triggered else T.SUB
        state = "已达到提醒门槛" if b.triggered else "未达到"
        out.append(f'<p style="margin:8px 0 2px 0;"><b>{esc(sym)}</b>　{_span(b.summary, col, True)}'
                   f'<span style="color:{T.SUB};">　门槛 价格 {b.min_score}、成交量 {b.min_vol_score} · {state} · 低点 {b.base_low:,.2f}'
                   f'（{b.base_low_date:%m-%d}）</span></p>')
        from ..quant.bottom import LABELS
        for key, ok, detail in b.checks:
            mark = _span("✓", pal.up, True) if ok else _span("✗", T.DIM)
            out.append(f'<p style="margin:1px 0 1px 14px; color:{T.TEXT if ok else T.SUB};">{mark} {esc(LABELS[key])}'
                       f'<span style="color:{T.SUB}; font-size:9pt;">　{esc(detail)}</span></p>')
    return "".join(out)


def briefing_html(b, pal: T.Palette, refs: dict | None = None, plans: list | None = None,
                  bottoms: dict | None = None) -> str:
    if b is None:
        return _note("还没有生成简报（等待第一次行情刷新）。")
    out = [disclaimer_html(),
           f'<p style="margin:10px 0 2px 0; font-size:15pt; font-weight:bold;">每日简报'
           f'<span style="font-size:10pt; color:{T.SUB}; font-weight:normal;">　{esc(b.day)}　每次刷新自动更新</span></p>',
           _note("这里汇总的是按固定规则算出的信号、参考位和你自己的纪律规则的检查结果，用来帮你每天对照，"
                 "不是买卖指令；怎么操作由你自己决定。")]

    out.append(_h("和上一次相比的变化", f"对比 {b.prev_day}" if b.prev_day else "今天是第一天记录"))
    if b.prev_day is None:
        out.append(_note("从下一个交易日开始，这里会列出信号翻转、跌破区间、触及止损 / 目标等变化。"))
    elif not b.changes:
        out.append(_note("没有状态变化。", T.TEXT))
    else:
        for sym, text in b.changes:
            out.append(f'<p style="margin:3px 0;">{_span("●", T.BLUE)} <b>{esc(sym)}</b>　{esc(text)}</p>')

    out.append(_h("持仓状态", "金额已折算成美元；价格为各自的计价货币"))
    if not b.held:
        out.append(_note("当前没有持仓。"))
    else:
        rows = [f"<tr><th {_TH}>股票</th><th {_TH} align='right'>现价</th><th {_TH} align='right'>今日</th>"
                f"<th {_TH} align='right'>占比 / 上限</th><th {_TH} align='right'>浮动盈亏</th><th {_TH}>综合信号</th>"
                f"<th {_TH}>震荡区间</th><th {_TH} align='right'>止损参考（距离）</th><th {_TH} align='right'>目标参考</th></tr>"]
        for i in b.held:
            cur = "" if i.currency == "USD" else f" <span style='color:{T.DIM}; font-size:8.5pt;'>{esc(i.currency)}</span>"
            over = i.weight * 100 > i.cap_pct
            wtxt = _span(f"{i.weight*100:.1f}%", T.AMBER if over else T.TEXT, over) + \
                f"<span style='color:{T.DIM};'> / {i.cap_pct:g}%</span>"
            src = "" if i.levels_src == "user" else "≈"
            if i.stop:
                below = i.stop_dist_pct is not None and i.stop_dist_pct < 0
                dist = "" if i.stop_dist_pct is None else f"（{i.stop_dist_pct:+.1f}%）"
                stop = _span(f"{src}{T.price(i.stop)}{dist}", T.RED if below else T.TEXT, below)
            else:
                stop = "—"
            sig = _span(f"{esc(i.stance)} {i.composite:+.2f}", pal.stance(i.stance), True) if i.has_data else \
                _span("无数据", T.DIM)
            rcol = {"buy": pal.up, "sell": pal.down, "breakdown": pal.down, "breakout_up": pal.up}.get(i.range_status, T.SUB)
            rows.append(
                f"<tr><td {_TD}><b>{esc(i.symbol)}</b></td><td {_TD} align='right'>{T.price(i.price)}{cur}</td>"
                f"<td {_TD} align='right'>{_span(T.pct(i.day_pct), pal.sign(i.day_pct))}</td>"
                f"<td {_TD} align='right'>{wtxt}</td>"
                f"<td {_TD} align='right'>{_span(T.pct(i.upl_pct, 1), pal.sign(i.upl_pct))}</td>"
                f"<td {_TD}>{sig}</td><td {_TD}>{_span(esc(i.range_label or '—'), rcol)}</td>"
                f"<td {_TD} align='right'>{stop}</td>"
                f"<td {_TD} align='right'>{(src + T.price(i.target)) if i.target else '—'}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
        out.append(_note("≈ = 按 ATR 自动估算的参考位（右键股票可以设成你自己的止损 / 目标 / 仓位上限）。"
                         "距离 = 现价高于止损参考位的幅度，负数表示已在其下方。"))

    if refs:
        out.append(reference_html(refs, pal))
    if bottoms:
        out.append(bottoms_html(bottoms, pal))
    if plans is not None:
        out.append(plans_html(plans, pal))

    out.append(_h("规则检查", "对照你的纪律规则，今天哪些条件成立"))
    any_flag = False
    for i in b.held + b.watch:
        for lvl, text in i.flags:
            any_flag = True
            col, dot = _FLAG.get(lvl, _FLAG["info"])
            out.append(f'<p style="margin:4px 0; line-height:145%;">{_span(dot, col)} <b>{esc(i.symbol)}</b>　{esc(text)}</p>')
    if not any_flag:
        out.append(_note("今天没有规则条件成立。", T.TEXT))
    out.append(_note(f"本月成交 {b.month_trades} 笔，上月 {b.prev_month_trades} 笔。"))

    if b.rebalance:
        out.append(_h(f"与参考配置的差额：{b.rebalance_method}", "目标 − 当前；只是计算结果"))
        rows = [f"<tr><th {_TH}>股票</th><th {_TH} align='right'>当前占比</th><th {_TH} align='right'>参考占比</th>"
                f"<th {_TH} align='right'>差额（美元）</th><th {_TH} align='right'>约合股数</th></tr>"]
        for r in b.rebalance:
            sh = f"{r['delta_shares']:+.0f}" if r["delta_shares"] else "—"
            rows.append(f"<tr><td {_TD}><b>{esc(r['symbol'])}</b></td><td {_TD} align='right'>{r['current']*100:.1f}%</td>"
                        f"<td {_TD} align='right'>{r['target']*100:.1f}%</td>"
                        f"<td {_TD} align='right'>{_span(T.money(r['delta_value'], True, 0), pal.sign(r['delta_value']), True)}</td>"
                        f"<td {_TD} align='right'>{sh}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
        out.append(_note("参考配置只按历史波动和相关性分散风险，不预测收益；范围是全部自选股。它不是调仓指令，"
                         "税费、交易成本和你自己的判断都没有算在内。方法可在“组合优化”页切换比较。"))

    if b.watch:
        out.append(_h("自选（未持仓）"))
        rows = [f"<tr><th {_TH}>股票</th><th {_TH} align='right'>现价</th><th {_TH} align='right'>今日</th>"
                f"<th {_TH}>综合信号</th><th {_TH}>震荡区间</th><th {_TH}>变化</th></tr>"]
        for i in b.watch:
            sig = _span(f"{esc(i.stance)} {i.composite:+.2f}", pal.stance(i.stance), True) if i.has_data else _span("无数据", T.DIM)
            rows.append(f"<tr><td {_TD}><b>{esc(i.symbol)}</b></td><td {_TD} align='right'>{T.price(i.price)}</td>"
                        f"<td {_TD} align='right'>{_span(T.pct(i.day_pct), pal.sign(i.day_pct))}</td><td {_TD}>{sig}</td>"
                        f"<td {_TD}><span style='color:{T.SUB};'>{esc(i.range_label or '—')}</span></td>"
                        f"<td {_TD}><span style='color:{T.SUB};'>{esc('；'.join(i.changes)) or '—'}</span></td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')

    for n in b.notes:
        out.append(_note("· " + esc(n), T.AMBER))
    return "".join(out)


# ---- portfolio optimisation ------------------------------------------------------------------

def portfolio_html(snap: Snapshot, method: str, pal: T.Palette, min_trade_pct: float = 1.0) -> str:
    out = [disclaimer_html()]
    held = [v for v in snap.tickers if v.value > 0]
    out.append(_h("组合概览"))
    out.append(f'<p style="margin:4px 0; line-height:150%;">持仓市值 <b>{T.money(snap.total_value)}</b>　·　今日 '
               f'{_span(T.money(snap.day_pnl, True) + "（" + T.pct(snap.day_pct) + "）", pal.sign(snap.day_pnl), True)}　·　浮动盈亏 '
               f'{_span(T.money(snap.total_upl, True), pal.sign(snap.total_upl), True)}'
               + (f"　·　现金 {T.money(snap.cash)}" if snap.cash is not None else "")
               + f"　·　用于仓位计算的账户规模 {T.money(snap.equity)}</p>")
    c = snap.conc
    if c and c.weights:
        out.append(_note(f"最大持仓 {esc(c.max_symbol)} {c.max_weight*100:.1f}%　·　前三合计 {c.top3*100:.1f}%　·　"
                         f"有效分散度 ≈ {c.effective_n:.1f} 只等权股票", T.TEXT))
        for w in c.warnings:
            out.append(_note("⚠ " + esc(w), T.AMBER))
    if not held:
        out.append(_note("当前没有持仓。下面的权重是在自选股范围内的理论配置。"))

    opt = snap.opt
    if not opt:
        out.append(_note("至少需要 2 只有足够历史数据的股票才能做组合优化。"))
        return "".join(out)
    out.append(_h("目标权重对比", "只做多 · 不加杠杆"))
    total = snap.total_value
    cur = {v.symbol: (v.value / total if total else 0.0) for v in snap.tickers}
    head = "".join(f"<th {_TH} align='right'>{esc(optimizer.METHODS[m])}{' ◀' if m == method else ''}</th>" for m in optimizer.METHODS)
    rows = [f"<tr><th {_TH}>股票</th><th {_TH} align='right'>当前</th>{head}</tr>"]
    for s in opt.symbols:
        cells = "".join(
            f"<td {_TD} align='right'>{_span(f'{opt.weights[m][s]*100:.1f}%', T.TEXT if m == method else T.SUB, m == method)}</td>"
            for m in optimizer.METHODS)
        rows.append(f"<tr><td {_TD}><b>{esc(s)}</b></td><td {_TD} align='right'>{cur.get(s, 0)*100:.1f}%</td>{cells}</tr>")
    for label, key, fmt in (("预期年化收益*", "ret", "{:+.1f}%"), ("年化波动", "vol", "{:.1f}%"), ("夏普*", "sharpe", "{:.2f}")):
        cells = "".join(
            f"<td {_TD} align='right'><span style='color:{T.SUB};'>"
            f"{fmt.format(opt.stats[m][key] * (1 if key == 'sharpe' else 100))}</span></td>" for m in optimizer.METHODS)
        rows.append(f"<tr><td {_TD}><span style='color:{T.SUB};'>{label}</span></td><td {_TD}></td>{cells}</tr>")
    out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
    out.append(_note("* 预期收益 / 夏普来自历史均值，只是样本内的数字，不是预测。"))

    out.append(_h(f"再平衡差额：{optimizer.METHODS[method]}", "目标 − 当前；正数表示低配"))
    if total > 0:
        prices = {v.symbol: v.price * (v.fx or 1.0) for v in snap.tickers if not math.isnan(v.price)}
        # stocks without enough history are not part of the optimisation: leave them out instead of
        # showing a target of 0 (which would read as "sell everything")
        skipped = [v.symbol for v in snap.tickers if v.value > 0 and v.symbol not in opt.symbols]
        inside = {v.symbol: v.value for v in snap.tickers if v.symbol in opt.symbols}
        if skipped:
            out.append(_note(f"{'、'.join(esc(x) for x in skipped)} 的历史数据不足，没有参与计算；"
                             "下面的差额只在其余股票之间计算，不代表对它的任何判断。", T.AMBER))
        d = optimizer.rebalance_deltas(inside, opt.weights[method], prices, sum(inside.values()), min_trade_pct)
        rows = [f"<tr><th {_TH}>股票</th><th {_TH} align='right'>当前市值</th><th {_TH} align='right'>目标市值</th>"
                f"<th {_TH} align='right'>差额</th><th {_TH} align='right'>约合股数</th></tr>"]
        for sym, r in d.sort_values("delta_value").iterrows():
            dv = r["delta_value"]
            txt = _span(T.money(dv, True, 0), pal.sign(dv), True) if dv else f"<span style='color:{T.DIM};'>差额很小，忽略</span>"
            sh = f"{r['delta_shares']:+.0f}" if r["delta_shares"] else "—"
            rows.append(f"<tr><td {_TD}><b>{esc(sym)}</b></td><td {_TD} align='right'>{T.money(r['current_value'], decimals=0)}</td>"
                        f"<td {_TD} align='right'>{T.money(r['target_value'], decimals=0)}</td>"
                        f"<td {_TD} align='right'>{txt}</td><td {_TD} align='right'>{sh}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
        out.append(_note(f"小于组合 {min_trade_pct:g}% 的差额不提示。这里只是算出差额，是否调整、何时调整、"
                         "税费和交易成本都需要你自己考虑；本程序不会下单。"))
    else:
        out.append(_note("没有持仓，无法计算差额。"))
    out.append(_h("关于“收益最大化”的实话"))
    for n in opt.notes:
        out.append(_note("· " + esc(n)))
    out.append(_note("· 没有任何方法能可靠地“最大化收益”。能控制的是风险：仓位大小、分散程度、止损纪律。"
                     "建议优先参考风险平价 / 最小波动的方向，而不是照搬最大夏普的数字。"))
    return "".join(out)


# ---- behaviour alerts ------------------------------------------------------------------------

_SEV = {"high": (T.RED, "高"), "warn": (T.AMBER, "中"), "info": (T.BLUE, "提示")}


def alerts_html(alerts: list[Alert], snap: Snapshot | None, snooze_days: float = 7) -> str:
    out = [_note("提醒在每次同步之后生成（程序看不到你尚未下的单），针对的是你自己的操作模式，而不是行情预测。"
                 "想在下单前检查，请用“交易前检查”页。", T.SUB)]
    if snap is not None and snap.demo_trades:
        out.append(_note("当前没有券商成交记录，下面带【演示数据】的提醒来自程序生成的示例成交，"
                         "只是为了展示功能。接入券商或导入 CSV 后它们会自动消失。", T.AMBER))
    if not alerts:
        out.append('<p style="margin:30px 0; font-size:12pt; color:%s;" align="center">目前没有需要提醒的行为。保持纪律 👍</p>' % T.SUB)
        return "".join(out)
    for a in alerts:
        col, sev = _SEV.get(a.severity, _SEV["info"])
        when = datetime.fromtimestamp(a.created).strftime("%m-%d %H:%M") if a.created else ""
        body = "<br>".join(esc(x) for x in a.body.split("\n"))
        out.append(
            f'<table cellspacing="0" cellpadding="0" width="100%" style="margin:10px 0;"><tr>'
            f'<td width="4" bgcolor="{col}"></td>'
            f'<td bgcolor="{T.PANEL}" style="padding:10px 14px;">'
            f'<span style="font-size:11pt; font-weight:bold;">{esc(a.title)}</span>'
            f'<span style="color:{col}; font-size:8.5pt;">　● {sev}</span>'
            f'<span style="color:{T.DIM}; font-size:8.5pt;">　{when}</span>'
            f'<p style="margin:6px 0; line-height:150%; color:{T.TEXT};">{body}</p>'
            f'<a href="snooze:{esc(a.id)}" style="color:{T.BLUE}; text-decoration:none;">稍后提醒（{snooze_days:g} 天）</a>'
            f'　　<a href="dismiss:{esc(a.id)}" style="color:{T.SUB}; text-decoration:none;">知道了，不再显示</a>'
            f'</td></tr></table>')
    return "".join(out)


_LVL = {"ok": (T.GREEN, "✓"), "warn": (T.AMBER, "!"), "high": (T.RED, "✕"), "info": (T.BLUE, "i")}


def pretrade_html(checks, symbol: str, side: str, qty: float, px: float) -> str:
    worst = "high" if any(c.level == "high" for c in checks) else "warn" if any(c.level == "warn" for c in checks) else "ok"
    verdict = {"high": ("先停一下：有明显的情绪化 / 违反计划的迹象", T.RED),
               "warn": ("有几处需要想清楚", T.AMBER), "ok": ("没有发现情绪化交易的迹象", T.GREEN)}[worst]
    out = [f'<p style="margin:6px 0; font-size:12pt; font-weight:bold; color:{verdict[1]};">{verdict[0]}</p>',
           _note(f"假设：{'买入' if side == 'BUY' else '卖出'} {qty:g} 股 {esc(symbol)} @ {T.price(px)}"
                 f"（约 {T.money(qty * px, decimals=0)}）", T.TEXT)]
    for c in checks:
        col, mark = _LVL.get(c.level, _LVL["info"])
        out.append(f'<table cellspacing="0" cellpadding="0" width="100%" style="margin:6px 0;"><tr>'
                   f'<td width="26" align="center" valign="top" style="padding-top:8px;">{_span(mark, col, True)}</td>'
                   f'<td bgcolor="{T.PANEL}" style="padding:8px 12px;"><b>{esc(c.title)}</b>'
                   f'<p style="margin:3px 0; color:{T.SUB}; line-height:145%;">{esc(c.detail)}</p></td></tr></table>')
    out.append(_note("这只是一次检查，本程序不会下单。真正的决定和操作在你的券商那里完成。", T.AMBER))
    return "".join(out)


def sources_html(snap: Snapshot | None) -> str:
    if snap is None:
        return _note("还没有同步过。")
    col = {"ok": T.GREEN, "off": T.AMBER, "error": T.RED}
    rows = []
    for name, (state, msg) in snap.sources.items():
        rows.append(f'<p style="margin:3px 0;">{_span("●", col.get(state, T.DIM))} {esc(msg)}</p>')
    if not any(n.startswith(("ibkr", "robinhood")) for n in snap.sources):
        rows.append(_note("没有启用任何券商数据源：行情是 Yahoo 的真实数据，持仓来自 watchlist.yaml 里手动填写的数量"
                          + ("——目前还是程序自带的示例数字，请改成你自己的，或连接 IBKR / 导入 Robinhood CSV。"
                             if snap.sample_holdings else "。")))
    rows.append(_note(f"上次刷新 {time.strftime('%H:%M:%S', time.localtime(snap.ts))}　·　成交记录 {snap.n_trades} 笔"
                      f"{'（演示数据）' if snap.demo_trades else ''}"))
    return "".join(rows)
