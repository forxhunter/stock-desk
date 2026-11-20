"""HTML builders for the rich-text panels (Qt rich text subset). Pure functions, no widgets."""
from __future__ import annotations

import html
import math
import time
from datetime import datetime

from ..engine import Snapshot, TickerView
from ..i18n import L
from ..insight import is_head
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
    return _note(L(f"⚠ {esc(DISCLAIMER)} 本程序只读取数据，不会也不能下单。",
                   f"⚠ {esc(DISCLAIMER)} This program only reads data; it does not and cannot place orders."), T.AMBER)


# ---- ticker detail ---------------------------------------------------------------------------

def analysis_html(v: TickerView, pal: T.Palette, ref=None) -> str:
    a = v.analysis
    out = [disclaimer_html()]
    if not a or not a.ok:
        out.append(_note(esc(a.headline if a else L("暂无数据（行情源还没有返回这只股票的历史）。", "No data (the quote source has not returned history for this stock yet)."))))
        return "".join(out)
    col = pal.stance(a.stance)
    out.append(f'<p style="margin:10px 0 2px 0; font-size:15pt; font-weight:bold; color:{col};">{a.stance}'
               f'<span style="font-size:10pt; color:{T.SUB}; font-weight:normal;">{L(f"　综合分 {a.composite:+.2f}（−1 ~ +1）", f"  composite score {a.composite:+.2f} (−1 to +1)")}</span></p>')
    out.append(_note(esc(a.headline), T.TEXT))

    out.append(_h(L("信号拆解", "Signal breakdown"), L("每一项的输入数据都列在右侧", "the inputs of each item are listed on the right")))
    rows = [L(f"<tr><th {_TH}>信号</th><th {_TH}>判断</th><th {_TH}>强度</th><th {_TH}>依据</th></tr>",
              f"<tr><th {_TH}>Signal</th><th {_TH}>Reading</th><th {_TH}>Strength</th><th {_TH}>Basis</th></tr>")]
    for s in a.signals:
        rows.append(f"<tr><td {_TD}>{esc(s.name)}</td><td {_TD}>{_span(esc(s.label), pal.sign(s.score), True)}</td>"
                    f"<td {_TD}>{_bar(s.score, pal)} <span style='color:{T.SUB}; font-size:8.5pt;'>{s.score:+.2f}</span></td>"
                    f"<td {_TD}><span style='color:{T.SUB};'>{esc(s.detail)}</span></td></tr>")
    out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')

    if a.range is not None:
        out.append(range_html(a.range, pal))
    if ref is not None:
        out.append(reference_html({v.symbol: ref}, pal))

    out.append(_h(L("参考思路与风险位", "Reference view and risk levels")))
    for line in a.rationale:
        if line.startswith((L("【持仓】", "[Position]"), L("【跟踪止损", "[Trailing-stop"), L("【参考思路】", "[Reference view]"),
                            L("【若新建仓", "[Reference levels if"), L("【仓位上限】", "[Position cap]"))):
            tag, rest = line.split(L("】", "]"), 1)
            if T.PRIVATE and tag in (L("【持仓", "[Position"), L("【跟踪止损参考", "[Trailing-stop reference"), L("【仓位上限", "[Position cap")):
                rest = L("隐私模式下已隐藏。", "Hidden in privacy mode.")
            out.append(f'<p style="margin:5px 0; line-height:145%;">{_span(esc(tag + L("】", "]")), T.BLUE, True)}{esc(rest)}</p>')
    if v.levels_src == "user" and not T.PRIVATE:
        out.append(_note(L(f"小组件进度条使用的是你自己设定的止损 {T.price(v.stop)} / 目标 {T.price(v.target)}。",
                           f"The widget progress bar uses the stop {T.price(v.stop)} / target {T.price(v.target)} that you set yourself.")))

    out.append(_h(L("历史回测", "Historical backtest"), L("同一只股票 · 默认参数 · 信号次日开盘成交 · 已扣成本", "same stock · default parameters · filled at next open after the signal · costs deducted")))
    rows = [L(f"<tr><th {_TH}>规则</th><th {_TH}>现在</th><th {_TH} align='right'>交易数</th><th {_TH} align='right'>胜率 (95% 区间)</th>"
              f"<th {_TH} align='right'>累计</th><th {_TH} align='right'>买入持有</th><th {_TH} align='right'>最大回撤</th>"
              f"<th {_TH} align='right'>夏普</th><th {_TH}>提示</th></tr>",
              f"<tr><th {_TH}>Rule</th><th {_TH}>Now</th><th {_TH} align='right'>Trades</th><th {_TH} align='right'>Win rate (95% CI)</th>"
              f"<th {_TH} align='right'>Total</th><th {_TH} align='right'>Buy &amp; hold</th><th {_TH} align='right'>Max drawdown</th>"
              f"<th {_TH} align='right'>Sharpe</th><th {_TH}>Note</th></tr>")]
    for b in a.backtests:
        if not b.n_bars:
            rows.append(f"<tr><td {_TD}>{esc(b.label)}</td><td {_TD} colspan='8'><span style='color:{T.DIM};'>{esc(' '.join(b.notes))}</span></td></tr>")
            continue
        hr = "—" if b.hit_rate is None else L(f"{b.hit_rate*100:.0f}%（{b.hit_ci[0]*100:.0f}–{b.hit_ci[1]*100:.0f}%）",
                                              f"{b.hit_rate*100:.0f}% ({b.hit_ci[0]*100:.0f}–{b.hit_ci[1]*100:.0f}%)")
        star = " ★" if a.evidence is b else ""
        now = _span(L("持有", "Holding"), pal.up) if b.in_position_now else _span(L("空仓", "Flat"), T.DIM)
        rows.append(
            f"<tr><td {_TD}><b>{esc(b.label)}{star}</b><br><span style='color:{T.DIM}; font-size:8.5pt;'>{esc(b.rule)}</span></td>"
            f"<td {_TD}>{now}</td><td {_TD} align='right'>{b.n_trades}</td><td {_TD} align='right'>{hr}</td>"
            f"<td {_TD} align='right'>{_span(T.pct(b.total_return*100, 0), pal.sign(b.total_return))}</td>"
            f"<td {_TD} align='right'>{T.pct(b.bh_return*100, 0)}</td>"
            f"<td {_TD} align='right'>{b.max_dd*100:.0f}%<span style='color:{T.DIM};'> / {b.bh_max_dd*100:.0f}%</span></td>"
            f"<td {_TD} align='right'>{b.sharpe:.2f}</td>"
            f"<td {_TD}><span style='color:{T.AMBER}; font-size:8.5pt;'>{esc(' '.join(b.notes))}</span></td></tr>")
    out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
    out.append(_note(L("★ = 与当前信号最接近、被用来估算凯利仓位的规则。最大回撤一栏为 策略 / 买入持有。",
                       "★ = the rule closest to the current signal, used to estimate the Kelly position size. The max drawdown column shows strategy / buy & hold.")))

    if v.news or v.odds:
        out.append(research_html(v.news, v.odds, compact=True))

    out.append(_h(L("请记住这些局限", "Keep these limitations in mind")))
    out.append("".join(_note("· " + esc(c)) for c in a.caveats))
    return "".join(out)


_RANGE_COL = {"buy": "up", "sell": "down", "breakdown": "down", "breakout_up": "up"}
_EV = {"buy": L("低吸", "buy"), "sell": L("高抛", "sell"), "stop": L("跌破止损", "stop broken"), "time": L("超时离场", "time exit")}


def range_html(r, pal: T.Palette) -> str:
    """Section for the phase-based range-trading (高抛低吸) rule."""
    out = [_h(L("震荡区间 · 高抛低吸", "Trading range · sell high, buy low"),
           L("横盘时在支撑附近低吸、阻力附近高抛；区间被突破即停用", "buy near support and sell near resistance while sideways; stop using it once the range breaks"))]
    if r.status == "short":
        out.append(_note(esc(r.detail)))
        return "".join(out)
    key = _RANGE_COL.get(r.status)
    col = getattr(pal, key) if key else (T.AMBER if r.status == "loose" else T.SUB)
    out.append(f'<p style="margin:4px 0; font-size:12pt; font-weight:bold; color:{col};">{esc(r.label)}'
               + (f'<span style="font-size:9pt; color:{T.SUB}; font-weight:normal;">{L("　规则当前持有低吸仓位", "  the rule currently holds a buy-zone position")}</span>'
                  if r.in_trade else "") + "</p>")
    out.append(f'<p style="margin:4px 0; line-height:145%;">{esc(r.detail)}</p>')
    if r.buy_band:
        rows = [L(f"<tr><th {_TH}>位置</th><th {_TH} align='right'>价格</th><th {_TH}>含义</th></tr>",
                  f"<tr><th {_TH}>Level</th><th {_TH} align='right'>Price</th><th {_TH}>Meaning</th></tr>")]
        for name, val, meaning, c in (
                (L("突破位", "Breakout"), f"{r.breakout:.2f}", L("收盘站上 = 向上突破，区间作废", "close above = upside breakout, range void"), T.SUB),
                (L("高抛区", "Sell zone"), f"{r.sell_band[0]:.2f} – {r.sell_band[1]:.2f}", L("区间上沿 20%：卖出 / 减仓参考", "top 20% of the range: reference for selling / trimming"), pal.down),
                (L("低吸区", "Buy zone"), f"{r.buy_band[0]:.2f} – {r.buy_band[1]:.2f}", L("区间下沿 20%：买入参考", "bottom 20% of the range: reference for buying"), pal.up),
                (L("失效位", "Invalidation"), f"{r.stop:.2f}", L("收盘跌破 = 区间跌破，止损参考", "close below = range breakdown, stop reference"), T.SUB)):
            rows.append(f"<tr><td {_TD}>{_span(name, c, True)}</td><td {_TD} align='right'>{val}</td>"
                        f"<td {_TD}><span style='color:{T.SUB};'>{meaning}</span></td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
    p = r.params
    out.append(_note(L(f"检测依据：最近 {int(p.get('lookback', 60))} 日 · 走势效率 {r.er:.2f}（≤{p.get('max_er', 0.3):g} 才算震荡）"
                       f" · 回归漂移 {r.drift*100:.0f}% 区间宽度（≤{p.get('max_drift', 0.5)*100:.0f}%）"
                       f" · 下沿 / 上沿测试 {r.touches[0]} / {r.touches[1]} 次 · ATR {r.atr:.2f}",
                       f"Detection basis: last {int(p.get('lookback', 60))} days · efficiency ratio {r.er:.2f} (≤{p.get('max_er', 0.3):g} counts as ranging)"
                       f" · regression drift {r.drift*100:.0f}% of range width (≤{p.get('max_drift', 0.5)*100:.0f}%)"
                       f" · lower / upper edge tests {r.touches[0]} / {r.touches[1]} · ATR {r.atr:.2f}")))
    recent = r.events[-4:]
    if recent:
        out.append(_note(L("规则最近的信号：", "Recent rule signals: ") + L("；", "; ").join(f"{_EV.get(e.kind, e.kind)} @ {e.price:.2f}" for e in recent)
                         + L("（图上 ▲ 低吸、▼ 高抛 / 离场；完整历史表现见下方回测表“震荡区间高抛低吸”一行）",
                             " (on the chart ▲ = buy, ▼ = sell / exit; for the full history see the \"Trading range · sell high, buy low\" row of the backtest table below)")))
    out.append(_note(L("高抛低吸只在横盘阶段有效；趋势行情里它会反复在“支撑”处买入下跌的股票。"
                       "所以区间一旦被跌破，规则要求止损并停用，而不是继续摊低成本。",
                       "Selling high and buying low only works while the price moves sideways; in a trend it keeps buying a falling stock at \"support\". "
                       "So once the range breaks, the rule calls for a stop-out and stops itself, rather than averaging down."), T.AMBER))
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
        out.append(_note(L("这些是背景信息，和量化建议并列展示，供你自己判断；它们不会自动触发任何建议或提醒。"
                           "预测市场价格反映的是下注者的定价，流动性差的市场尤其不可靠。",
                           "This is background information shown next to the quantitative signals for your own judgement; it never triggers any suggestion or alert by itself. "
                           "Prediction-market prices reflect what bettors are paying, and thin markets are especially unreliable."), T.AMBER))
    out.append(_h(L("预测市场概率", "Prediction-market odds"), L("Polymarket 公开行情 · 只读", "Polymarket public quotes · read-only")))
    if odds:
        rows = [L(f"<tr><th {_TH}>主题</th><th {_TH}>市场</th><th {_TH} align='right'>概率 (Yes)</th>"
                  f"<th {_TH} align='right'>1日</th><th {_TH} align='right'>1周</th><th {_TH} align='right'>1月</th>"
                  f"<th {_TH} align='right'>24h 成交</th><th {_TH} align='right'>总成交</th><th {_TH}>到期</th></tr>",
                  f"<tr><th {_TH}>Topic</th><th {_TH}>Market</th><th {_TH} align='right'>Probability (Yes)</th>"
                  f"<th {_TH} align='right'>1d</th><th {_TH} align='right'>1w</th><th {_TH} align='right'>1m</th>"
                  f"<th {_TH} align='right'>24h volume</th><th {_TH} align='right'>Total volume</th><th {_TH}>Ends</th></tr>")]
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
        out.append(_note(L("变化列的单位是百分点。可在 data/config.yaml 的 research.polymarket.keywords / slugs 里固定你关心的主题。",
                           "The change columns are in percentage points. Pin the topics you care about in research.polymarket.keywords / slugs in data/config.yaml.")))
    else:
        out.append(_note(L("暂无数据（未启用、离线，或没有匹配的市场）。", "No data (disabled, offline, or no matching markets).")))

    out.append(_h("SemiAnalysis", L("semianalysis.com 公开 RSS · 仅标题与摘要", "semianalysis.com public RSS · titles and summaries only")))
    if news:
        for n in news[: 6 if compact else 40]:
            day = datetime.fromtimestamp(n.published).strftime("%Y-%m-%d") if n.published else ""
            tick = "".join(f' <span style="color:{T.BLUE}; font-size:8.5pt;">[{esc(t)}]</span>' for t in n.tickers)
            out.append(f'<p style="margin:9px 0 1px 0;"><a href="{esc(n.link)}" style="color:{T.TEXT}; text-decoration:none; '
                       f'font-weight:bold;">{esc(n.title)}</a>{tick}'
                       f'<span style="color:{T.DIM}; font-size:8.5pt;">　{day}</span></p>')
            if n.summary and not compact:
                out.append(_note(esc(n.summary)))
        out.append(_note(L("点击标题在浏览器打开原文。付费内容需要你自己的订阅，本程序不会绕过付费墙。",
                           "Click a title to open the original in your browser. Paid content needs your own subscription; this program does not bypass paywalls.")))
    else:
        out.append(_note(L("暂无相关文章。", "No related articles.") if compact else L("暂无数据（未启用或离线）。", "No data (disabled or offline).")))
    return "".join(out)


# ---- unusual-activity scan ---------------------------------------------------------------------

def flow_html(rep, pal: T.Palette) -> str:
    from ..flow import TIER_LABEL
    out = [_note(L("按固定规则筛出的<b>异常成交</b>：Polymarket 大额下注、期权放量、期货大幅波动。每条都写明触发了哪条规则和具体数字，"
                   "“怎么看”是核对思路，不是预测，也不是买卖建议。程序只读公开数据，不能下注或下单。",
                   "<b>Unusual trading</b> picked out by fixed rules: large Polymarket bets, option volume spikes, big futures moves. Each item states which rule fired and the actual numbers; "
                   "\"How to read it\" is a way to check things, not a prediction and not a buy / sell recommendation. The program only reads public data and cannot bet or place orders."), T.AMBER)]
    if rep is None:
        return "".join(out) + _note(L("异动扫描已关闭（data/config.yaml 里 flow.enabled）。", "The unusual-activity scan is off (flow.enabled in data/config.yaml)."))
    if not rep.ts:
        return "".join(out) + _note(L("还没有扫描结果，第一次扫描会在启动后一两分钟内完成。", "No scan results yet; the first scan finishes within a minute or two of starting."))
    out.append(_note(L(f"上次扫描 {datetime.fromtimestamp(rep.ts):%m-%d %H:%M}，每 10 分钟自动扫一次（期权每 30 分钟）。",
                       f"Last scan {datetime.fromtimestamp(rep.ts):%m-%d %H:%M}; it scans automatically every 10 minutes (options every 30 minutes).")
                     + (L("　出错：", "  Errors: ") + esc(L("；", "; ").join(rep.errors)) if rep.errors else "")))
    notes: dict[str, list[str]] = {}
    for n in rep.notes:
        k, _, text = n.partition("|")
        notes.setdefault(k, []).append(text)
    sections = (("polymarket", L("Polymarket 大额下注", "Polymarket large bets"), L("单笔 ≥ $1 万的成交 · 近 60 分钟", "single trades ≥ $10k · last 60 minutes")),
                ("options", L("期权异动", "Unusual options activity"), L("Yahoo 期权链 · 延迟行情 · 成交量为当日累计", "Yahoo option chain · delayed quotes · volume is the day's running total")),
                ("futures", L("期货 / 波动率", "Futures / volatility"), L("Yahoo 日线 · 盘中为未走完的一天", "Yahoo daily bars · the current day is still in progress")))
    for key, title, sub in sections:
        items = rep.by(key)
        out.append(_h(title, sub))
        if not items:
            out.append(_note(L("这一轮没有达到门槛的异动。", "Nothing reached the threshold in this round.")))
        for i in items:
            col, dot = _FLAG[i.severity]
            tier = f'<span style="color:{T.BLUE if i.tier == "watch" else T.SUB}; font-size:8.5pt;">　{esc(TIER_LABEL[i.tier])}'                    f'{" [" + esc(",".join(i.tickers)) + "]" if i.tickers else ""}</span>'
            link = f'<a href="{esc(i.url)}" style="color:{T.TEXT}; text-decoration:none; font-weight:bold;">{esc(i.title)}</a>'                 if i.url else f"<b>{esc(i.title)}</b>"
            out.append(f'<p style="margin:12px 0 2px 0;"><span style="color:{col};">{dot}</span> {link}{tier}</p>')
            out.append(f'<p style="margin:0 0 2px 16px;">{esc(i.headline)}</p>')
            out.append("".join(f'<p style="margin:0 0 1px 16px; color:{T.SUB}; font-size:9pt;">· {esc(f)}</p>' for f in i.facts))
            out.append("".join(f'<p style="margin:1px 0 1px 16px; color:{T.TEXT}; font-size:9pt; line-height:135%;">'
                               f'{L("具体：", "Details: ") if n == 0 else L("　　　", "")}{esc(d)}</p>' for n, d in enumerate(i.detail)))
            out.append("".join(f'<p style="margin:1px 0 1px 16px; color:{T.DIM}; font-size:9pt; line-height:135%;">'
                               f'{L("怎么看：", "How to read it: ") if n == 0 else L("　　　　", "")}{esc(r)}</p>' for n, r in enumerate(i.reading)))
        for text in notes.get(key, []):
            out.append(_note(esc(text)))
    out.append(_note(L("门槛都可以在 data/config.yaml 的 flow 一节里改（big_usd、wallet_usd、vol_oi、z 等）。"
                       "大单、放量和大波动经常出现而后续什么也没发生；把它们当作“值得看一眼”的线索，而不是结论。",
                       "All thresholds can be changed in the flow section of data/config.yaml (big_usd, wallet_usd, vol_oi, z, etc.). "
                       "Large orders, volume spikes and big moves often show up with nothing following; treat them as leads worth a look, not conclusions.")))
    return "".join(out)


def congress_html(rep, pal: T.Palette, watch: set[str] | None = None) -> str:
    """Trades disclosed by members of Congress: the watchlist, who has been right, the latest reports."""
    from .. import congress as cg
    watch = watch or set()
    both = rep is not None and rep.senate
    out = [_note(L("两院议员按《股票法案》公开申报的<b>股票买卖</b>（" + ("读取众议院书记官和参议院秘书处两个官方站点的申报" if both else "目前只读取众议院书记官网站的官方申报") + "）。每笔交易按“交易日之后的股价相对标普”评估方向对不对，"
                   "并据此给议员评级。申报最晚可以在交易后 45 天才公开、金额只有区间；评级是“值得看一眼”的线索，不是预测，也不是买卖建议。",
                   "<b>Stock trades</b> that members of Congress disclose under the STOCK Act ("
                   + ("the official reports on the House Clerk's and the Secretary of the Senate's sites" if both else "currently only the official reports on the House Clerk's site") + "). "
                   "Each trade is judged by the stock's move against the S&P after the trade date, and members are rated on that. "
                   "Reports may come up to 45 days after the trade and give amount ranges only; a rating is a lead worth a look, "
                   "not a prediction and not a buy / sell recommendation."), T.AMBER)]
    if rep is None:
        return "".join(out) + _note(L("议员交易追踪已关闭（data/config.yaml 里 congress.enabled）。", "Congress trade tracking is off (congress.enabled in data/config.yaml)."))
    if not rep.ts:
        return "".join(out) + _note(L("还没有数据，第一次读取会在启动后几分钟内开始。", "No data yet; the first read starts within a few minutes of launch."))
    h = rep.horizon
    queue = L(f"，还有 {rep.backlog} 份在排队读取（读完之前评级还不完整，暂不发提醒）", f"; {rep.backlog} more queued (ratings are incomplete and no alerts are sent until they are read)") if rep.backlog else ""
    out.append(_note(L(f"上次更新 {cg.as_of(rep.ts)}　·　{rep.since} 以来已读取 {rep.docs} 份申报{queue}　·　{rep.scans} 份是扫描的纸质申报，无法自动读取"
                       f"　·　{rep.priced}/{rep.tickers} 只股票有行情　·　评估周期：交易日后 {h} 个交易日",
                       f"Updated {cg.as_of(rep.ts)}  ·  {rep.docs} reports read since {rep.since}{queue}  ·  {rep.scans} are scanned paper reports that cannot be read"
                       f"  ·  {rep.priced}/{rep.tickers} stocks have price history  ·  judged {h} sessions after the trade date")
                     + (L("　出错：", "  Errors: ") + esc(L("；", "; ").join(rep.errors)) if rep.errors else "")))

    def tier(m) -> str:
        if m is None:
            return ""
        col = T.AMBER if m.tier == "star" else T.BLUE if m.tier == "good" else T.RED if m.tier == "poor" else T.DIM
        return f'<span style="color:{col}; font-size:8.5pt;"> {esc(cg.TIER[m.tier])}</span>'

    def recent(m) -> str:
        if not m.recent[0]:
            return "—"
        arrow = (f' <span style="color:{T.RED};">↓</span>' if m.trend == "down" else f' <span style="color:{T.GREEN};">↑</span>' if m.trend == "up" else "")
        return f"{m.recent[1] * 100:.0f}% · {cg.pct(m.recent[2])} ({m.recent[0]}){arrow}"

    def names(d: dict) -> str:
        return L("、", ", ").join(esc(n) + tier(rep.score(next((c.who for c in rep.calls if c.member == n), ""))) for n in list(d)[:6]) or "—"

    rows = []
    for sym in sorted(watch):
        k = rep.consensus(sym)
        if k["buyers"] or k["sellers"]:
            ab, asell = {n: 1 for n in k["acc_buy"]}, {n: 1 for n in k["acc_sell"]}
            rest = L(f"买 {len(k['buyers']) - len(ab)} 人 / 卖 {len(k['sellers']) - len(asell)} 人",
                     f"{len(k['buyers']) - len(ab)} bought / {len(k['sellers']) - len(asell)} sold")
            rows.append(f"<tr><td {_TD}><b>{esc(sym)}</b></td><td {_TD}>{names(ab)}</td><td {_TD}>{names(asell)}</td>"
                        f"<td {_TD} align='right'>{k['net']:+.1f}</td><td {_TD} align='right' style='color:{T.DIM};'>{rest}</td></tr>")
    out.append(_h(L("你的自选", "Your watchlist"), L("近 90 天按交易日统计 · 只参考评级偏准以上的议员 · 加权净值 = 这些买方的系数之和 − 卖方之和",
                                                    "by trade date over the last 90 days · only members rated fairly accurate or better are referred to · weighted net = their buying weights − selling weights")))
    if rows:
        head = "".join(f"<th {_TH}>{x}</th>" for x in (L("股票", "Stock"), L("买入（评级偏准以上）", "Buying (rated accurate)"), L("卖出（评级偏准以上）", "Selling (rated accurate)"),
                                                       L("加权净值", "Weighted net"), L("其他议员（不作参考）", "Other members (not referred to)")))
        out.append(f'<table cellspacing="0" width="100%"><tr>{head}</tr>{"".join(rows)}</table>')
    else:
        out.append(_note(L("近 90 天没有议员申报买卖你自选里的股票。", "No member has reported trading a stock on your watchlist in the last 90 days.")))

    out.append(_h(L("谁买卖得准", "Who has been right"), L(f"按交易日后 {h} 个交易日相对标普的表现 · 买入跑赢、卖出后跑输算对", f"by the move against the S&P {h} sessions after the trade date · a purchase that beats it or a sale that lags it is right")))
    top = [m for m in rep.members if m.tier in ("star", "good")][:20] or [m for m in rep.members if m.n][:10]
    if top:
        head = "".join(f"<th {_TH} align='{'left' if n < 2 else 'right'}'>{x}</th>" for n, x in enumerate(
            (L("议员", "Member"), L("评级", "Rating"), L("可评估 / 全部", "Judged / all"), L("方向正确", "Right"), L("95% 区间", "95% interval"),
             L("平均超额", "Avg excess"), L("买入", "Purchases"), L("卖出", "Sales"), L("披露后才跟", "Following after disclosure"),
             L("近期表现", "Recent form"), L("最近申报", "Last report"))))
        body = []
        for m in top:
            ci = f"{m.ci[0] * 100:.0f}–{m.ci[1] * 100:.0f}%" if m.ci else "—"
            fo = f"{cg.pct(m.follow[2])} ({m.follow[0]})" if m.follow[0] else "—"
            body.append(f"<tr><td {_TD}>{esc(m.member)} <span style='color:{T.DIM}; font-size:8.5pt;'>{esc(m.state or cg.CHAMBER.get(m.chamber, ''))}</span></td>"
                        f"<td {_TD}>{tier(m)}</td><td {_TD} align='right'>{m.n} / {m.trades}</td>"
                        f"<td {_TD} align='right'>{m.hit_rate * 100:.0f}%</td><td {_TD} align='right'>{ci}</td>"
                        f"<td {_TD} align='right'>{cg.pct(m.mean)}</td>"
                        f"<td {_TD} align='right'>{cg.pct(m.buys[1])} ({m.buys[0]})</td><td {_TD} align='right'>{cg.pct(m.sells[1])} ({m.sells[0]})</td>"
                        f"<td {_TD} align='right'>{fo}</td><td {_TD} align='right'>{recent(m)}</td>"
                        f"<td {_TD} align='right'>{esc(m.last_filed[5:])}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%"><tr>{head}</tr>{"".join(body)}</table>')
        out.append(_note(L(f"评级按最近 {rep.window_days} 天的交易滚动计算，每次扫描都重算；“近期表现”只看近 {rep.recent_days} 天的交易、按 {rep.short} 个交易日评估，"
                           "近期转差（↓）的会被下调一级，近期明显转好（↑）的会被上调。只有评级偏准以上的议员会触发提醒、计入加权。"
                           "“平均超额”都是带方向的：正数表示判断对了（买入后跑赢，或卖出后跑输），括号里是笔数；“披露后才跟”从披露后的下一个交易日算起，"
                           "也就是读申报的人实际能拿到的结果。几百名议员里总有人因为运气上榜。",
                           f"Ratings roll over the trades of the last {rep.window_days} days and are recomputed on every scan; 'recent form' looks only at the trades of the "
                           f"last {rep.recent_days} days, judged at {rep.short} sessions: a record that has turned worse (↓) is moved down a tier, one that has clearly "
                           "improved (↑) is moved up. Only members rated fairly accurate or better raise alerts and carry weight. "
                           "Every 'average excess' is signed: positive means the call was right (a purchase that beat the market, or a sale that lagged it); counts are "
                           "in brackets. 'Following after disclosure' starts on the session after the report, which is what a reader could actually have had. "
                           "Among several hundred members some make this list by luck.")))
        if rep.changes:
            out.append(_h(L("评级变动", "Rating changes"), L("新的在前", "newest first")))
            out.append("".join(f'<p style="margin:2px 0 2px 10px;">{esc(cg.change_text(c))}</p>' for c in rep.changes[:12]))
    else:
        out.append(_note(L("还没有满足评估条件的交易。", "No trades can be judged yet.")))

    out.append(_h(L("最近披露", "Latest disclosures"), L("新的在前 · 点议员名打开原始申报", "newest first · click a name to open the original report")))
    if rep.recent:
        head = "".join(f"<th {_TH} align='{'left' if n < 4 else 'right'}'>{x}</th>" for n, x in enumerate(
            (L("披露日", "Filed"), L("议员", "Member"), L("操作", "Action"), L("股票", "Stock"), L("交易日", "Traded"), L("滞后", "Delay"),
             L("金额", "Amount"), L("交易日至今", "Since the trade"), L("相对标普", "Vs S&P"))))
        body = []
        for c in rep.recent[:60]:
            who = f'<a href="{esc(c.url)}" style="color:{T.TEXT}; text-decoration:none;">{esc(c.member)}</a>' if c.url else esc(c.member)
            rel = None if c.ex_now is None else (c.ex_now if c.side == "buy" else -c.ex_now)
            mark = "" if c.ex_now is None else (f' <span style="color:{T.GREEN};">✓</span>' if c.ex_now > 0 else f' <span style="color:{T.RED};">✗</span>')
            tk = f"<b>{esc(c.ticker)}</b>" if c.ticker in watch else esc(c.ticker)
            body.append(f"<tr><td {_TD}>{esc(c.filed[5:])}</td><td {_TD}>{who}{tier(rep.score(c.who))}</td>"
                        f"<td {_TD}>{esc(cg.side_text(c))}</td><td {_TD}>{tk}</td><td {_TD} align='right'>{esc(c.traded[5:])}</td>"
                        f"<td {_TD} align='right'>{c.delay}{L(' 天', 'd')}</td><td {_TD} align='right'>{esc(cg.money(c.lo, c.hi))}</td>"
                        f"<td {_TD} align='right'>{_span(cg.pct(c.ret_now), pal.sign(c.ret_now or 0))}</td>"
                        f"<td {_TD} align='right'>{cg.pct(rel)}{mark}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%"><tr>{head}</tr>{"".join(body)}</table>')
        out.append(_note(L("✓ / ✗：到目前为止这笔的方向是对是错（买入后跑赢标普、或卖出后跑输标普为对）。粗体是你自选里的股票。",
                           "✓ / ✗: whether the call is right so far (a purchase beating the S&P, or a sale lagging it). Bold marks stocks on your watchlist.")))
    else:
        out.append(_note(L("还没有读到申报。", "No reports read yet.")))
    out.append(_note(L("数据来自众议院书记官和参议院秘书处公开的申报文件（参议院需要你在配置里确认过该站点的使用协议才会读取）。按法律（5 U.S.C. app. 105(c)），这些申报不得用于商业目的（新闻媒体除外）、评定信用或募款。"
                       "参数在 data/config.yaml 的 congress 一节。",
                       "Data comes from the reports published by the Clerk of the House and the Secretary of the Senate (the Senate's are read only once you have confirmed that site's terms in the settings). By law (5 U.S.C. app. 105(c)) these reports may not be used "
                       "for commercial purposes (other than by news media), for credit ratings or to solicit money. Settings are under congress in data/config.yaml.")))
    return "".join(out)


# ---- daily briefing ---------------------------------------------------------------------------

_FLAG = {"high": (T.RED, "●"), "warn": (T.AMBER, "●"), "info": (T.BLUE, "●")}


def private_html(what: str) -> str:
    return (f'<p style="margin:60px 0; font-size:12pt; color:{T.SUB};" align="center">{L(f"隐私模式已开启，{esc(what)}已隐藏。", f"Privacy mode is on; {esc(what)} is hidden.")}'
            f'<br><span style="font-size:9.5pt;">{L("点小组件顶部的眼睛图标，或右键小组件 → “隐藏关键数据”，可恢复显示。", "Click the eye icon at the top of the widget, or right-click the widget → \"Hide key figures\", to show them again.")}</span></p>')


def reference_html(refs: dict, pal: T.Palette) -> str:
    """Home-market share next to its foreign listing (e.g. SK Hynix in Seoul vs the HY9H GDR)."""
    out = []
    for r in refs.values():
        out.append(_h(L(f"{r.symbol} 对应的本土股票：{r.name}", f"Home-market share of {r.symbol}: {r.name}"),
                 L(f"{r.ref_symbol} · 截至 {r.ref_asof:%Y-%m-%d} 收盘", f"{r.ref_symbol} · close of {r.ref_asof:%Y-%m-%d}")))
        cells = [(L("收盘", "Close"), f"{r.ref_price:,.0f} {esc(r.ref_currency)}", T.TEXT),
                 (L("当日", "Day"), T.pct(r.ref_day_pct), pal.sign(r.ref_day_pct)),
                 (L("5 日", "5d"), T.pct(r.ref_chg_5d, 1), pal.sign(r.ref_chg_5d)),
                 (L("1 月", "1m"), T.pct(r.ref_chg_21d, 1), pal.sign(r.ref_chg_21d)),
                 (L("半年", "6m"), T.pct(r.ref_chg_126d, 1), pal.sign(r.ref_chg_126d)),
                 (L("距 52 周高点", "From 52w high"), T.pct(r.ref_dd_252, 1), pal.sign(r.ref_dd_252))]
        if r.ref_stance:
            cells.append((L("综合信号", "Composite signal"), f"{esc(r.ref_stance)} {r.ref_composite:+.2f}", pal.stance(r.ref_stance)))
            cells.append((L("震荡区间", "Trading range"), esc(r.ref_range or "—"), T.SUB))
        head = "".join(f"<th {_TH} align='right'>{k}</th>" for k, _, _ in cells)
        body = "".join(f"<td {_TD} align='right'>{_span(v, c)}</td>" for _, v, c in cells)
        out.append(f'<table cellspacing="0" width="100%"><tr>{head}</tr><tr>{body}</tr></table>')
        if r.premium_pct == r.premium_pct:
            z = (r.premium_pct - r.premium_avg) / r.premium_std if r.premium_std else 0.0
            where = L("明显偏高", "clearly high") if z > 1.5 else L("明显偏低", "clearly low") if z < -1.5 else L("在平时的范围内", "within its usual range")
            stale = L(f"（{r.symbol} 在这之后又交易了 {r.stale_days} 天，差价里包含这段时间的涨跌）",
                      f" ({r.symbol} has traded {r.stale_days} more days since then; the gap includes those moves)") if r.stale_days else ""
            out.append(L(f'<p style="margin:6px 0; line-height:150%;">按汇率折算，本土收盘价相当于 '
                         f'<b>{r.implied:,.2f} {esc(r.currency)}</b>；{esc(r.symbol)} 现价相对它 '
                         f'{_span(T.pct(r.premium_pct, 1), T.TEXT, True)}{stale}。'
                         f'近 60 日这个差价平均 {T.pct(r.premium_avg, 1)}、波动 ±{r.premium_std:.1f}%，目前{where}。</p>',
                         f'<p style="margin:6px 0; line-height:150%;">Converted at the exchange rate, the home-market close is equivalent to '
                         f'<b>{r.implied:,.2f} {esc(r.currency)}</b>; {esc(r.symbol)} now trades at '
                         f'{_span(T.pct(r.premium_pct, 1), T.TEXT, True)} relative to it{stale}. '
                         f'Over the last 60 days this gap averaged {T.pct(r.premium_avg, 1)} with a spread of ±{r.premium_std:.1f}%; it is currently {where}.</p>'))
        notes = []
        if r.corr == r.corr:
            notes.append(L(f"两者近 120 日的日涨跌相关系数 {r.corr:.2f}", f"correlation of daily moves over the last 120 days: {r.corr:.2f}"))
        if r.ref_below_falling_sma50:
            notes.append(L("本土股价也在下行的 50 日均线下方", "the home-market price is also below its falling 50-day average"))
        notes.append(L("首尔比法兰克福先收盘，本土当日的涨跌通常会先反映出来，可作为当天的参考，但不保证同向",
                   "Seoul closes before Frankfurt, so the home-market move of the day usually shows up first; it can serve as a reference for the day, but there is no guarantee the other follows"))
        out.append(_note(L("；", "; ").join(notes) + L("。", ".")))
    return "".join(out)


_PLAN_MISSING = L("未填写", "not set")
from ..plans import fmt as plan_fmt, fmt_px as plan_px  # noqa: E402


def _progress(frac: float, color: str, width: int = 420) -> str:
    """A thin horizontal bar (Qt rich text has no CSS bars, so use a two-cell table)."""
    a = max(1, min(width - 1, int(round(width * max(0.0, min(1.0, frac))))))
    return (f'<table cellspacing="0" cellpadding="0" width="{width}" style="margin:3px 0;"><tr>'
            f'<td width="{a}" height="6" bgcolor="{color}"></td><td width="{width - a}" height="6" bgcolor="{T.LINE}"></td>'
            f'</tr></table>')


def plans_html(plans: list, pal: T.Palette) -> str:
    out = [_h(L("我的计划", "My plans"), L("数字由你自己写在 data/plans.yaml；程序只负责每天对照", "you write the numbers yourself in data/plans.yaml; the program only checks against them every day"))]
    if not plans:
        out.append(_note(L("还没有计划。右键某只股票 → “编辑我的计划…” 可以写一份。", "No plans yet. Right-click a stock → \"Edit my plan…\" to write one.")))
        return "".join(out)
    for p in plans:
        span = L(f"{p.start} → {p.end}，已过 {p.days_done} 天，还剩 {p.days_left} 天", f"{p.start} → {p.end}, {p.days_done} days elapsed, {p.days_left} left") if p.days_total else L("起止日期未填写", "start / end dates not set")
        out.append(f'<p style="margin:8px 0 2px 0;"><b>{esc(p.symbol)}</b>　<span style="color:{T.SUB};">{span}'
                   + (L(f"　·　下次复查 {p.next_review}", f"  ·  next review {p.next_review}") if p.next_review else "") + "</span>"
                   f'　<a href="plan:{esc(p.symbol)}" style="color:{T.BLUE}; text-decoration:none;">{L("编辑计划…", "Edit plan…")}</a></p>')
        F = plan_fmt
        ccy = f" {esc(p.level_ccy)}" if p.follow and p.level_ccy else ""

        def loc(x):         # a followed level, also shown in the held stock's own currency
            return L(f"<span style='color:{T.DIM};'>（≈ {x * p.to_local:,.0f} {esc(p.local_ccy)}）</span>",
                  f"<span style='color:{T.DIM};'>(≈ {x * p.to_local:,.0f} {esc(p.local_ccy)})</span>") \
                if p.follow and p.to_local else ""
        if p.follow:
            px = L(f"现价 {plan_px(p.check_price)}{ccy}", f"price {plan_px(p.check_price)}{ccy}") if p.check_price == p.check_price else L("暂无行情", "no quote")
            out.append(_note(L(f"价位按 <b>{esc(p.follow_name)}</b>（{esc(p.follow)}）的股价填写和判断　·　{px}{loc(p.check_price) if p.check_price == p.check_price else ''}",
                               f"Levels are entered and judged against the price of <b>{esc(p.follow_name)}</b> ({esc(p.follow)})  ·  {px}{loc(p.check_price) if p.check_price == p.check_price else ''}"), T.BLUE))
        if p.days_total:
            out.append(_note(L("时间进度", "Time elapsed")) + _progress(p.days_done / p.days_total, T.BLUE))
        if p.target_weight_pct is not None and p.start_weight_pct and p.start_weight_pct != p.target_weight_pct:
            done = (p.start_weight_pct - p.weight_pct) / (p.start_weight_pct - p.target_weight_pct)
            out.append(_note(L(f"占比进度（{p.start_weight_pct:g}% → {p.target_weight_pct:g}%，现在 {p.weight_pct:.1f}%）",
                           f"Weight progress ({p.start_weight_pct:g}% → {p.target_weight_pct:g}%, now {p.weight_pct:.1f}%)"))
                       + _progress(done, T.GREEN))
        tgt = f"{p.target_weight_pct:g}%" if p.target_weight_pct is not None else _PLAN_MISSING
        path = L(f"（按时间均匀推进，今天应在 {p.path_weight_pct:.1f}% 附近）", f" (even pace over time: about {p.path_weight_pct:.1f}% today)") if p.path_weight_pct is not None else ""
        lv = " / ".join(F(x) for x in p.reduce_levels) or _PLAN_MISSING
        nxt = L(f"，下一个 {F(p.next_level)}", f", next {F(p.next_level)}") if p.next_level else ""
        exit_txt = f"{F(p.exit_below)}{ccy} {loc(p.exit_below)}" if p.exit_below else _PLAN_MISSING
        if p.building:                                               # a build-up plan
            cap = L(f"　上限 {p.max_weight_pct:g}%", f"  cap {p.max_weight_pct:g}%") if p.max_weight_pct else L("　上限 ", "  cap ") + _PLAN_MISSING
            rows = [(L("占组合", "Share of portfolio"), L(f"现在 {p.weight_pct:.1f}%{cap}", f"Now {p.weight_pct:.1f}%{cap}"))]
            done = L(f"（已到 {' / '.join(F(x) for x in p.buy_reached)}）", f" (reached {' / '.join(F(x) for x in p.buy_reached)})") if p.buy_reached else ""
            nb = L(f"，下一个 {F(p.next_buy)}", f", next {F(p.next_buy)}") if p.next_buy else ""
            rows.append((L("分批买入价位", "Staged buy levels"), (" / ".join(F(x) for x in p.buy_levels) + ccy + done + nb)
                         if p.buy_levels else _PLAN_MISSING))
            cp = p.check_price
            gap = L(f"（现价 {plan_px(cp)}，还差 {(p.target_price / cp - 1) * 100:+.1f}%）", f" (price {plan_px(cp)}, {(p.target_price / cp - 1) * 100:+.1f}% to go)") if p.target_price and cp == cp and cp else ""
            rows.append((L("目标价", "Target"), f"{F(p.target_price)}{ccy}{gap}" if p.target_price else _PLAN_MISSING))
            rows.append((L("底线价", "Exit-below price"), exit_txt))
            if p.reduce_levels:
                rows.append((L("分批减仓价位", "Staged trim levels"), f"{lv}{ccy}{nxt}"))
        else:
            rows = [(L("占组合", "Share of portfolio"), L(f"现在 {p.weight_pct:.1f}%　→　目标 {tgt}{path}", f"Now {p.weight_pct:.1f}%  →  target {tgt}{path}")),
                    (L("底线价", "Exit-below price"), exit_txt),
                    (L("分批减仓价位", "Staged trim levels"), f"{lv}{ccy if p.reduce_levels else ''}{nxt}")]
            if p.buy_levels:
                rows.append((L("分批买入价位", "Staged buy levels"), " / ".join(F(x) for x in p.buy_levels) + ccy))
            if p.target_price:
                rows.append((L("目标价", "Target"), f"{F(p.target_price)}{ccy}"))
        if p.notes:
            rows.append((L("写给自己的话", "Note to self"), esc(p.notes)))
        out.append('<table cellspacing="0">' + "".join(
            f"<tr><td {_TD}><span style='color:{T.SUB};'>{k}</span></td><td {_TD}>{v}</td></tr>" for k, v in rows) + "</table>")
        for lvl, text in p.flags:
            col, dot = _FLAG.get(lvl, _FLAG["info"])
            out.append(f'<p style="margin:4px 0;">{_span(dot, col)} {esc(text)}</p>')
        if p.missing:
            out.append(_note(L(f"还没填：{'、'.join(p.missing)}。这些数字需要你自己决定，程序不会替你填。",
                               f"Not filled in yet: {', '.join(p.missing)}. You need to decide these numbers yourself; the program will not fill them in."), T.AMBER))
    return "".join(out)


def bottoms_html(bottoms: dict, pal: T.Palette) -> str:
    out = [_h(L("筑底观察", "Bottoming watch"), L("价格条件和成交量条件都达到门槛才提醒；只是形态统计，不是预测", "alerts only when both the price and the volume conditions reach their thresholds; pattern statistics, not a prediction"))]
    for sym, b in bottoms.items():
        if not b.eligible:
            out.append(f'<p style="margin:5px 0;"><b>{esc(sym)}</b>　<span style="color:{T.SUB};">'
                       f'{L(f"距 52 周高点 {b.drawdown_pct:.1f}%，跌幅不够大，不在筑底观察范围内。", f"{b.drawdown_pct:.1f}% below the 52-week high; the drop is not large enough for the bottoming watch.")}</span></p>')
            continue
        col = pal.up if b.triggered else T.SUB
        state = L("已达到提醒门槛", "alert threshold reached") if b.triggered else L("未达到", "not reached")
        out.append(f'<p style="margin:8px 0 2px 0;"><b>{esc(sym)}</b>　{_span(b.summary, col, True)}'
                   f'<span style="color:{T.SUB};">{L(f"　门槛 价格 {b.min_score}、成交量 {b.min_vol_score} · {state} · 低点 {b.base_low:,.2f}（{b.base_low_date:%m-%d}）", f"  thresholds: price {b.min_score}, volume {b.min_vol_score} · {state} · low {b.base_low:,.2f} ({b.base_low_date:%m-%d})")}</span></p>')
        from ..quant.bottom import LABELS
        for key, ok, detail in b.checks:
            mark = _span("✓", pal.up, True) if ok else _span("✗", T.DIM)
            out.append(f'<p style="margin:1px 0 1px 14px; color:{T.TEXT if ok else T.SUB};">{mark} {esc(LABELS[key])}'
                       f'<span style="color:{T.SUB}; font-size:9pt;">　{esc(detail)}</span></p>')
    return "".join(out)


def briefing_html(b, pal: T.Palette, refs: dict | None = None, plans: list | None = None,
                  bottoms: dict | None = None) -> str:
    if b is None:
        return _note(L("还没有生成简报（等待第一次行情刷新）。", "No briefing yet (waiting for the first quote refresh)."))
    out = [disclaimer_html(),
           f'<p style="margin:10px 0 2px 0; font-size:15pt; font-weight:bold;">{L("每日简报", "Daily briefing")}'
           f'<span style="font-size:10pt; color:{T.SUB}; font-weight:normal;">{L(f"　{esc(b.day)}　每次刷新自动更新", f"  {esc(b.day)}  updated on every refresh")}</span></p>',
           _note(L("这里汇总的是按固定规则算出的信号、参考位和你自己的纪律规则的检查结果，用来帮你每天对照，"
                   "不是买卖指令；怎么操作由你自己决定。",
                   "This page collects signals and reference levels computed by fixed rules, plus the result of checking your own discipline rules, so you can compare against them every day. "
                   "They are not buy / sell orders; what to do is up to you."))]

    out.append(_h(L("和上一次相比的变化", "Changes since last time"), L(f"对比 {b.prev_day}", f"compared with {b.prev_day}") if b.prev_day else L("今天是第一天记录", "today is the first day recorded")))
    if b.prev_day is None:
        out.append(_note(L("从下一个交易日开始，这里会列出信号翻转、跌破区间、触及止损 / 目标等变化。", "From the next trading day on, this lists changes such as signal flips, range breakdowns and stop / target hits.")))
    elif not b.changes:
        out.append(_note(L("没有状态变化。", "No status changes."), T.TEXT))
    else:
        for sym, text in b.changes:
            out.append(f'<p style="margin:3px 0;">{_span("●", T.BLUE)} <b>{esc(sym)}</b>　{esc(text)}</p>')

    out.append(_h(L("持仓状态", "Position status"), L("金额已折算成美元；价格为各自的计价货币", "amounts converted to USD; prices are in each stock's own currency")))
    if not b.held:
        out.append(_note(L("当前没有持仓。", "No positions at the moment.")))
    else:
        rows = [L(f"<tr><th {_TH}>股票</th><th {_TH} align='right'>现价</th><th {_TH} align='right'>今日</th>"
                  f"<th {_TH} align='right'>占比 / 上限</th><th {_TH} align='right'>浮动盈亏</th><th {_TH}>综合信号</th>"
                  f"<th {_TH}>震荡区间</th><th {_TH} align='right'>止损参考（距离）</th><th {_TH} align='right'>目标参考</th></tr>",
                  f"<tr><th {_TH}>Stock</th><th {_TH} align='right'>Price</th><th {_TH} align='right'>Today</th>"
                  f"<th {_TH} align='right'>Weight / cap</th><th {_TH} align='right'>Unrealised P&amp;L</th><th {_TH}>Composite signal</th>"
                  f"<th {_TH}>Trading range</th><th {_TH} align='right'>Stop ref. (distance)</th><th {_TH} align='right'>Target ref.</th></tr>")]
        for i in b.held:
            cur = "" if i.currency == "USD" else f" <span style='color:{T.DIM}; font-size:8.5pt;'>{esc(i.currency)}</span>"
            over = i.weight * 100 > i.cap_pct
            wtxt = _span(f"{i.weight*100:.1f}%", T.AMBER if over else T.TEXT, over) + \
                f"<span style='color:{T.DIM};'> / {i.cap_pct:g}%</span>"
            src = "" if i.levels_src == "user" else "≈"
            if i.stop:
                below = i.stop_dist_pct is not None and i.stop_dist_pct < 0
                dist = "" if i.stop_dist_pct is None else L(f"（{i.stop_dist_pct:+.1f}%）", f" ({i.stop_dist_pct:+.1f}%)")
                stop = _span(f"{src}{T.price(i.stop)}{dist}", T.RED if below else T.TEXT, below)
            else:
                stop = "—"
            sig = _span(f"{esc(i.stance)} {i.composite:+.2f}", pal.stance(i.stance), True) if i.has_data else \
                _span(L("无数据", "No data"), T.DIM)
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
        out.append(_note(L("≈ = 按 ATR 自动估算的参考位（右键股票可以设成你自己的止损 / 目标 / 仓位上限）。"
                           "距离 = 现价高于止损参考位的幅度，负数表示已在其下方。",
                           "≈ = reference level estimated automatically from ATR (right-click a stock to set your own stop / target / position cap). "
                           "Distance = how far the price is above the stop reference; a negative number means it is already below it.")))

    if refs:
        out.append(reference_html(refs, pal))
    if bottoms:
        out.append(bottoms_html(bottoms, pal))
    if plans is not None:
        out.append(plans_html(plans, pal))

    out.append(_h(L("规则检查", "Rule check"), L("对照你的纪律规则，今天哪些条件成立", "which of your discipline-rule conditions hold today")))
    any_flag = False
    for i in b.held + b.watch:
        for lvl, text in i.flags:
            any_flag = True
            col, dot = _FLAG.get(lvl, _FLAG["info"])
            out.append(f'<p style="margin:4px 0; line-height:145%;">{_span(dot, col)} <b>{esc(i.symbol)}</b>　{esc(text)}</p>')
    if not any_flag:
        out.append(_note(L("今天没有规则条件成立。", "No rule conditions hold today."), T.TEXT))
    out.append(_note(L(f"本月成交 {b.month_trades} 笔，上月 {b.prev_month_trades} 笔。", f"Trades this month: {b.month_trades}; last month: {b.prev_month_trades}.")))

    if b.rebalance:
        out.append(_h(L(f"与参考配置的差额：{b.rebalance_method}", f"Gap to the reference allocation: {b.rebalance_method}"), L("目标 − 当前；只是计算结果", "target − current; just a calculation result")))
        rows = [L(f"<tr><th {_TH}>股票</th><th {_TH} align='right'>当前占比</th><th {_TH} align='right'>参考占比</th>"
                  f"<th {_TH} align='right'>差额（美元）</th><th {_TH} align='right'>约合股数</th></tr>",
                  f"<tr><th {_TH}>Stock</th><th {_TH} align='right'>Current weight</th><th {_TH} align='right'>Reference weight</th>"
                  f"<th {_TH} align='right'>Gap (USD)</th><th {_TH} align='right'>≈ Shares</th></tr>")]
        for r in b.rebalance:
            sh = f"{r['delta_shares']:+.0f}" if r["delta_shares"] else "—"
            rows.append(f"<tr><td {_TD}><b>{esc(r['symbol'])}</b></td><td {_TD} align='right'>{r['current']*100:.1f}%</td>"
                        f"<td {_TD} align='right'>{r['target']*100:.1f}%</td>"
                        f"<td {_TD} align='right'>{_span(T.money(r['delta_value'], True, 0), pal.sign(r['delta_value']), True)}</td>"
                        f"<td {_TD} align='right'>{sh}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
        out.append(_note(L("参考配置只按历史波动和相关性分散风险，不预测收益；范围是全部自选股。它不是调仓指令，"
                           "税费、交易成本和你自己的判断都没有算在内。方法可在“组合优化”页切换比较。",
                           "The reference allocation only spreads risk using historical volatility and correlation; it does not predict returns, and it covers the whole watchlist. It is not a rebalancing order; "
                           "taxes, trading costs and your own judgement are not included. You can switch and compare methods on the portfolio optimisation page.")))

    if b.watch:
        out.append(_h(L("自选（未持仓）", "Watchlist (not held)")))
        rows = [L(f"<tr><th {_TH}>股票</th><th {_TH} align='right'>现价</th><th {_TH} align='right'>今日</th>"
                  f"<th {_TH}>综合信号</th><th {_TH}>震荡区间</th><th {_TH}>变化</th></tr>",
                  f"<tr><th {_TH}>Stock</th><th {_TH} align='right'>Price</th><th {_TH} align='right'>Today</th>"
                  f"<th {_TH}>Composite signal</th><th {_TH}>Trading range</th><th {_TH}>Change</th></tr>")]
        for i in b.watch:
            sig = _span(f"{esc(i.stance)} {i.composite:+.2f}", pal.stance(i.stance), True) if i.has_data else _span(L("无数据", "No data"), T.DIM)
            rows.append(f"<tr><td {_TD}><b>{esc(i.symbol)}</b></td><td {_TD} align='right'>{T.price(i.price)}</td>"
                        f"<td {_TD} align='right'>{_span(T.pct(i.day_pct), pal.sign(i.day_pct))}</td><td {_TD}>{sig}</td>"
                        f"<td {_TD}><span style='color:{T.SUB};'>{esc(i.range_label or '—')}</span></td>"
                        f"<td {_TD}><span style='color:{T.SUB};'>{esc(L('；', '; ').join(i.changes)) or '—'}</span></td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')

    for n in b.notes:
        out.append(_note("· " + esc(n), T.AMBER))
    return "".join(out)


# ---- portfolio optimisation ------------------------------------------------------------------

def portfolio_html(snap: Snapshot, method: str, pal: T.Palette, min_trade_pct: float = 1.0) -> str:
    out = [disclaimer_html()]
    held = [v for v in snap.tickers if v.value > 0]
    out.append(_h(L("组合概览", "Portfolio overview")))
    out.append(f'<p style="margin:4px 0; line-height:150%;">{L("持仓市值", "Position value")} <b>{T.money(snap.total_value)}</b>{L("　·　今日 ", "  ·  today ")}'
               f'{_span(T.money(snap.day_pnl, True) + L("（", " (") + T.pct(snap.day_pct) + L("）", ")"), pal.sign(snap.day_pnl), True)}{L("　·　浮动盈亏 ", "  ·  unrealised P&amp;L ")}'
               f'{_span(T.money(snap.total_upl, True), pal.sign(snap.total_upl), True)}'
               + (L(f"　·　现金 {T.money(snap.cash)}", f"  ·  cash {T.money(snap.cash)}") if snap.cash is not None else "")
               + L(f"　·　用于仓位计算的账户规模 {T.money(snap.equity)}", f"  ·  account size used for position sizing {T.money(snap.equity)}") + "</p>")
    c = snap.conc
    if c and c.weights:
        out.append(_note(L(f"最大持仓 {esc(c.max_symbol)} {c.max_weight*100:.1f}%　·　前三合计 {c.top3*100:.1f}%　·　"
                           f"有效分散度 ≈ {c.effective_n:.1f} 只等权股票",
                           f"Largest holding {esc(c.max_symbol)} {c.max_weight*100:.1f}%  ·  top three combined {c.top3*100:.1f}%  ·  "
                           f"effective diversification ≈ {c.effective_n:.1f} equal-weight stocks"), T.TEXT))
        for w in c.warnings:
            out.append(_note("⚠ " + esc(w), T.AMBER))
    if not held:
        out.append(_note(L("当前没有持仓。下面的权重是在自选股范围内的理论配置。", "No positions at the moment. The weights below are a theoretical allocation across the watchlist.")))

    opt = snap.opt
    if not opt:
        out.append(_note(L("至少需要 2 只有足够历史数据的股票才能做组合优化。", "Portfolio optimisation needs at least 2 stocks with enough history.")))
        return "".join(out)
    out.append(_h(L("目标权重对比", "Target weight comparison"), L("只做多 · 不加杠杆", "long only · no leverage")))
    total = snap.total_value
    cur = {v.symbol: (v.value / total if total else 0.0) for v in snap.tickers}
    head = "".join(f"<th {_TH} align='right'>{esc(optimizer.METHODS[m])}{' ◀' if m == method else ''}</th>" for m in optimizer.METHODS)
    rows = [L(f"<tr><th {_TH}>股票</th><th {_TH} align='right'>当前</th>{head}</tr>", f"<tr><th {_TH}>Stock</th><th {_TH} align='right'>Current</th>{head}</tr>")]
    for s in opt.symbols:
        cells = "".join(
            f"<td {_TD} align='right'>{_span(f'{opt.weights[m][s]*100:.1f}%', T.TEXT if m == method else T.SUB, m == method)}</td>"
            for m in optimizer.METHODS)
        rows.append(f"<tr><td {_TD}><b>{esc(s)}</b></td><td {_TD} align='right'>{cur.get(s, 0)*100:.1f}%</td>{cells}</tr>")
    for label, key, fmt in ((L("预期年化收益*", "Expected annual return*"), "ret", "{:+.1f}%"), (L("年化波动", "Annualised volatility"), "vol", "{:.1f}%"), (L("夏普*", "Sharpe*"), "sharpe", "{:.2f}")):
        cells = "".join(
            f"<td {_TD} align='right'><span style='color:{T.SUB};'>"
            f"{fmt.format(opt.stats[m][key] * (1 if key == 'sharpe' else 100))}</span></td>" for m in optimizer.METHODS)
        rows.append(f"<tr><td {_TD}><span style='color:{T.SUB};'>{label}</span></td><td {_TD}></td>{cells}</tr>")
    out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
    out.append(_note(L("* 预期收益 / 夏普来自历史均值，只是样本内的数字，不是预测。", "* Expected return / Sharpe come from historical averages; they are in-sample numbers, not a prediction.")))

    out.append(_h(L(f"再平衡差额：{optimizer.METHODS[method]}", f"Rebalancing gap: {optimizer.METHODS[method]}"), L("目标 − 当前；正数表示低配", "target − current; a positive number means underweight")))
    if total > 0:
        prices = {v.symbol: v.price * (v.fx or 1.0) for v in snap.tickers if not math.isnan(v.price)}
        # stocks without enough history are not part of the optimisation: leave them out instead of
        # showing a target of 0 (which would read as "sell everything")
        skipped = [v.symbol for v in snap.tickers if v.value > 0 and v.symbol not in opt.symbols]
        inside = {v.symbol: v.value for v in snap.tickers if v.symbol in opt.symbols}
        if skipped:
            out.append(_note(L(f"{'、'.join(esc(x) for x in skipped)} 的历史数据不足，没有参与计算；"
                               "下面的差额只在其余股票之间计算，不代表对它的任何判断。",
                               f"{', '.join(esc(x) for x in skipped)}: not enough history, so left out of the calculation; "
                               "the gaps below are computed among the remaining stocks only and say nothing about it."), T.AMBER))
        d = optimizer.rebalance_deltas(inside, opt.weights[method], prices, sum(inside.values()), min_trade_pct)
        rows = [L(f"<tr><th {_TH}>股票</th><th {_TH} align='right'>当前市值</th><th {_TH} align='right'>目标市值</th>"
                  f"<th {_TH} align='right'>差额</th><th {_TH} align='right'>约合股数</th></tr>",
                  f"<tr><th {_TH}>Stock</th><th {_TH} align='right'>Current value</th><th {_TH} align='right'>Target value</th>"
                  f"<th {_TH} align='right'>Gap</th><th {_TH} align='right'>≈ Shares</th></tr>")]
        for sym, r in d.sort_values("delta_value").iterrows():
            dv = r["delta_value"]
            txt = _span(T.money(dv, True, 0), pal.sign(dv), True) if dv else f"<span style='color:{T.DIM};'>{L('差额很小，忽略', 'gap too small, ignored')}</span>"
            sh = f"{r['delta_shares']:+.0f}" if r["delta_shares"] else "—"
            rows.append(f"<tr><td {_TD}><b>{esc(sym)}</b></td><td {_TD} align='right'>{T.money(r['current_value'], decimals=0)}</td>"
                        f"<td {_TD} align='right'>{T.money(r['target_value'], decimals=0)}</td>"
                        f"<td {_TD} align='right'>{txt}</td><td {_TD} align='right'>{sh}</td></tr>")
        out.append(f'<table cellspacing="0" width="100%">{"".join(rows)}</table>')
        out.append(_note(L(f"小于组合 {min_trade_pct:g}% 的差额不提示。这里只是算出差额，是否调整、何时调整、"
                           "税费和交易成本都需要你自己考虑；本程序不会下单。",
                           f"Gaps smaller than {min_trade_pct:g}% of the portfolio are not shown. This only calculates the gap; whether and when to adjust, "
                           "plus taxes and trading costs, are for you to consider. This program does not place orders.")))
    else:
        out.append(_note(L("没有持仓，无法计算差额。", "No positions, so the gap cannot be calculated.")))
    out.append(_h(L("关于“收益最大化”的实话", "An honest word on \"maximising returns\"")))
    for n in opt.notes:
        out.append(_note("· " + esc(n)))
    out.append(_note(L("· 没有任何方法能可靠地“最大化收益”。能控制的是风险：仓位大小、分散程度、止损纪律。"
                       "建议优先参考风险平价 / 最小波动的方向，而不是照搬最大夏普的数字。",
                       "· No method can reliably \"maximise returns\". What you can control is risk: position size, diversification, stop discipline. "
                       "Consider leaning towards risk parity / minimum volatility rather than copying the maximum-Sharpe numbers.")))
    return "".join(out)


# ---- behaviour alerts ------------------------------------------------------------------------

_SEV = {"high": (T.RED, L("高", "High")), "warn": (T.AMBER, L("中", "Medium")), "info": (T.BLUE, L("提示", "Note"))}


def alerts_html(alerts: list[Alert], snap: Snapshot | None, snooze_days: float = 7) -> str:
    out = [_note(L("提醒在每次同步之后生成（程序看不到你尚未下的单），针对的是你自己的操作模式，而不是行情预测。"
                   "想在下单前检查，请用“交易前检查”页。",
                   "Alerts are generated after each sync (the program cannot see orders you have not placed yet) and are about your own trading patterns, not market predictions. "
                   "To check before placing an order, use the pre-trade check page."), T.SUB)]
    if snap is not None and snap.demo_trades:
        out.append(_note(L("当前没有券商成交记录，下面带【演示数据】的提醒来自程序生成的示例成交，"
                           "只是为了展示功能。接入券商或导入 CSV 后它们会自动消失。",
                           "There are no broker trade records at the moment; the alerts below marked [Demo data] come from sample trades generated by the program, "
                           "only to show the feature. They disappear once you connect a broker or import a CSV."), T.AMBER))
    if not alerts:
        out.append(L('<p style="margin:30px 0; font-size:12pt; color:%s;" align="center">目前没有需要提醒的行为。保持纪律 👍</p>',
                     '<p style="margin:30px 0; font-size:12pt; color:%s;" align="center">No behaviour to flag right now. Keep up the discipline 👍</p>') % T.SUB)
        return "".join(out)
    for a in alerts:
        col, sev = _SEV.get(a.severity, _SEV["info"])
        when = datetime.fromtimestamp(a.created).strftime("%m-%d %H:%M") if a.created else ""
        body = "<br>".join(f'<b style="color:{T.SUB};">{esc(x)}</b>' if is_head(x) else esc(x) for x in a.body.split("\n"))
        out.append(
            f'<table cellspacing="0" cellpadding="0" width="100%" style="margin:10px 0;"><tr>'
            f'<td width="4" bgcolor="{col}"></td>'
            f'<td bgcolor="{T.PANEL}" style="padding:10px 14px;">'
            f'<span style="font-size:11pt; font-weight:bold;">{esc(a.title)}</span>'
            f'<span style="color:{col}; font-size:8.5pt;">　● {sev}</span>'
            f'<span style="color:{T.DIM}; font-size:8.5pt;">　{when}</span>'
            f'<p style="margin:6px 0; line-height:150%; color:{T.TEXT};">{body}</p>'
            f'<a href="snooze:{esc(a.id)}" style="color:{T.BLUE}; text-decoration:none;">{L(f"稍后提醒（{snooze_days:g} 天）", f"Remind me later ({snooze_days:g} days)")}</a>'
            f'{L("　　", "  ")}<a href="dismiss:{esc(a.id)}" style="color:{T.SUB}; text-decoration:none;">{L("知道了，不再显示", "Got it, do not show again")}</a>'
            f'</td></tr></table>')
    return "".join(out)


_LVL = {"ok": (T.GREEN, "✓"), "warn": (T.AMBER, "!"), "high": (T.RED, "✕"), "info": (T.BLUE, "i")}


def pretrade_html(checks, symbol: str, side: str, qty: float, px: float) -> str:
    worst = "high" if any(c.level == "high" for c in checks) else "warn" if any(c.level == "warn" for c in checks) else "ok"
    verdict = {"high": (L("先停一下：有明显的情绪化 / 违反计划的迹象", "Pause first: clear signs of emotional trading / breaking your plan"), T.RED),
               "warn": (L("有几处需要想清楚", "A few points to think through"), T.AMBER),
               "ok": (L("没有发现情绪化交易的迹象", "No signs of emotional trading found"), T.GREEN)}[worst]
    out = [f'<p style="margin:6px 0; font-size:12pt; font-weight:bold; color:{verdict[1]};">{verdict[0]}</p>',
           _note(L(f"假设：{'买入' if side == 'BUY' else '卖出'} {qty:g} 股 {esc(symbol)} @ {T.price(px)}"
                 f"（约 {T.money(qty * px, decimals=0)}）",
                 f"Assumption: {'buy' if side == 'BUY' else 'sell'} {qty:g} shares of {esc(symbol)} @ {T.price(px)}"
                 f" (about {T.money(qty * px, decimals=0)})"), T.TEXT)]
    for c in checks:
        col, mark = _LVL.get(c.level, _LVL["info"])
        out.append(f'<table cellspacing="0" cellpadding="0" width="100%" style="margin:6px 0;"><tr>'
                   f'<td width="26" align="center" valign="top" style="padding-top:8px;">{_span(mark, col, True)}</td>'
                   f'<td bgcolor="{T.PANEL}" style="padding:8px 12px;"><b>{esc(c.title)}</b>'
                   f'<p style="margin:3px 0; color:{T.SUB}; line-height:145%;">{esc(c.detail)}</p></td></tr></table>')
    out.append(_note(L("这只是一次检查，本程序不会下单。真正的决定和操作在你的券商那里完成。", "This is only a check; the program does not place orders. The actual decision and order happen at your broker."), T.AMBER))
    return "".join(out)


def sources_html(snap: Snapshot | None) -> str:
    if snap is None:
        return _note(L("还没有同步过。", "Not synced yet."))
    col = {"ok": T.GREEN, "off": T.AMBER, "error": T.RED}
    rows = []
    for name, (state, msg) in snap.sources.items():
        rows.append(f'<p style="margin:3px 0;">{_span("●", col.get(state, T.DIM))} {esc(msg)}</p>')
    if not any(n.startswith(("ibkr", "robinhood")) for n in snap.sources):
        rows.append(_note(L("没有启用任何券商数据源：行情是 Yahoo 的真实数据，持仓来自 watchlist.yaml 里手动填写的数量", "No broker data source is enabled: quotes are real Yahoo data, and positions come from the quantities entered by hand in watchlist.yaml")
                          + (L("——目前还是程序自带的示例数字，请改成你自己的，或连接 IBKR / 导入 Robinhood CSV。", " (currently still the sample numbers that ship with the program; change them to your own, or connect IBKR / import a Robinhood CSV).")
                             if snap.sample_holdings else L("。", "."))))
    rows.append(_note(L(f"上次刷新 {time.strftime('%H:%M:%S', time.localtime(snap.ts))}　·　成交记录 {snap.n_trades} 笔"
                        f"{'（演示数据）' if snap.demo_trades else ''}",
                        f"Last refresh {time.strftime('%H:%M:%S', time.localtime(snap.ts))}  ·  {snap.n_trades} trade records"
                        f"{' (demo data)' if snap.demo_trades else ''}")))
    return "".join(rows)
