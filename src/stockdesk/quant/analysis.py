"""Per-ticker analysis: indicators -> scored signals -> an explained, informational suggestion.

Nothing here is a trade instruction. Every conclusion carries the inputs it was derived
from and the historical record of the matching rule on the same ticker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..i18n import L
from . import indicators as ind
from . import range_trading as rt
from .backtest import BacktestResult, backtest_all
from .risk import Kelly, Levels, atr_levels, kelly_from_trades, shares_for_risk, vol_budget_weight

WEIGHTS = {"trend": 0.35, "momentum": 0.25, "rel_strength": 0.20, "drawdown": 0.10, "volatility": 0.10}
DISCLAIMER = L("以下内容仅为基于历史数据的量化参考，不构成投资建议；历史表现不代表未来。",
               "The following is a quantitative reference based on historical data only; it is not "
               "investment advice, and past performance does not predict future results.")


@dataclass
class Signal:
    key: str
    name: str
    score: float           # -1 (bearish) .. +1 (bullish)
    label: str
    detail: str
    inputs: dict = field(default_factory=dict)


@dataclass
class Sizing:
    equity: float = 0.0
    risk_shares: float = 0.0
    risk_weight: float = 0.0
    vol_weight: float = 0.0
    kelly: Kelly | None = None
    max_weight: float = 0.25
    suggested_weight: float = 0.0
    suggested_value: float = 0.0
    suggested_shares: float = 0.0
    current_weight: float = 0.0
    binding: str = ""


@dataclass
class Analysis:
    symbol: str
    ok: bool = False
    asof: pd.Timestamp | None = None
    price: float = float("nan")
    ind: dict = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    composite: float = 0.0
    stance: str = L("数据不足", "No data")
    levels: Levels | None = None
    sizing: Sizing | None = None
    backtests: list[BacktestResult] = field(default_factory=list)
    evidence: BacktestResult | None = None
    range: rt.RangeState | None = None
    headline: str = ""
    rationale: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


def _f(x) -> float:
    try:
        x = float(x)
        return x if math.isfinite(x) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def compute_indicators(bars: pd.DataFrame, bench: pd.Series | None = None) -> dict:
    c, h, l = bars["close"], bars["high"], bars["low"]
    m = ind.macd(c)
    b = ind.bollinger(c)
    out = {
        "close": _f(c.iloc[-1]),
        "sma20": _f(ind.sma(c, 20).iloc[-1]), "sma50": _f(ind.sma(c, 50).iloc[-1]),
        "sma200": _f(ind.sma(c, 200).iloc[-1]),
        "sma50_prev": _f(ind.sma(c, 50).iloc[-21]) if len(c) > 70 else float("nan"),
        "rsi": _f(ind.rsi(c).iloc[-1]),
        "macd": _f(m["macd"].iloc[-1]), "macd_signal": _f(m["signal"].iloc[-1]),
        "macd_hist": _f(m["hist"].iloc[-1]),
        "macd_hist_prev": _f(m["hist"].iloc[-2]) if len(c) > 2 else float("nan"),
        "atr": _f(ind.atr(h, l, c).iloc[-1]),
        "bb_upper": _f(b["upper"].iloc[-1]), "bb_lower": _f(b["lower"].iloc[-1]),
        "bb_mid": _f(b["mid"].iloc[-1]), "pct_b": _f(b["pct_b"].iloc[-1]),
        "bandwidth": _f(b["bandwidth"].iloc[-1]),
        "vol20": _f(ind.realized_vol(c, 20).iloc[-1]),
        "vol60": _f(ind.realized_vol(c, 60).iloc[-1]),
        "dd_252": _f(ind.drawdown(c, 252).iloc[-1]),
        "max_dd": _f(ind.max_drawdown(c)),
        "high_252": _f(c.tail(252).max()), "low_252": _f(c.tail(252).min()),
        "chg_5d": _f(ind.pct_change_n(c, 5).iloc[-1]) if len(c) > 5 else float("nan"),
        "chg_21d": _f(ind.pct_change_n(c, 21).iloc[-1]) if len(c) > 21 else float("nan"),
        "rs_63": None, "rs_126": None,
    }
    out["atr_pct"] = out["atr"] / out["close"] if out["close"] else float("nan")
    if bench is not None and len(bench) > 0:
        out["rs_63"] = ind.relative_strength(c, bench, 63)
        out["rs_126"] = ind.relative_strength(c, bench, 126)
    return out


def score_signals(i: dict, benchmark: str = "SPY") -> list[Signal]:
    sig: list[Signal] = []
    px = i["close"]

    # --- trend: moving-average structure
    parts, votes = [], []
    for name, a, b in ((L("价格 vs 200日线", "Price vs 200-day MA"), px, i["sma200"]),
                       (L("50日线 vs 200日线", "50-day MA vs 200-day MA"), i["sma50"], i["sma200"]),
                       (L("20日线 vs 50日线", "20-day MA vs 50-day MA"), i["sma20"], i["sma50"])):
        if not (math.isnan(a) or math.isnan(b)):
            votes.append(1.0 if a > b else -1.0)
            parts.append(L(f"{name}：{'上方' if a > b else '下方'}（{a:.2f} / {b:.2f}）",
                           f"{name}: {'above' if a > b else 'below'} ({a:.2f} / {b:.2f})"))
    if votes:
        s = float(np.mean(votes))
        label = (L("多头排列", "Bullish alignment") if s > 0.9 else L("空头排列", "Bearish alignment") if s < -0.9
                 else L("趋势偏多", "Trend leaning up") if s > 0 else L("趋势偏空", "Trend leaning down"))
        sig.append(Signal("trend", L("趋势（均线）", "Trend (moving averages)"), s, label, L("；", "; ").join(parts),
                          {"SMA20": i["sma20"], "SMA50": i["sma50"], "SMA200": i["sma200"], L("价格", "Price"): px}))

    # --- momentum: RSI + MACD
    rsi, hist, hprev = i["rsi"], i["macd_hist"], i["macd_hist_prev"]
    if not math.isnan(rsi) and not math.isnan(hist):
        rs = max(-1.0, min(1.0, (rsi - 50.0) / 20.0))
        note = ""
        if rsi >= 75:
            rs, note = 0.2, L("（超买，追高风险上升）", " (overbought, higher risk of chasing)")
        elif rsi <= 25:
            rs, note = -0.2, L("（超卖，可能反弹但也可能继续下跌）", " (oversold, may bounce but may also keep falling)")
        rising = not math.isnan(hprev) and hist > hprev
        ms = (1.0 if rising else 0.3) if hist > 0 else (-0.3 if rising else -1.0)
        s = 0.5 * rs + 0.5 * ms
        label = (L("动量强", "Strong momentum") if s > 0.4 else L("动量弱", "Weak momentum") if s < -0.4
                 else L("动量中性", "Neutral momentum"))
        sig.append(Signal("momentum", L("动量（RSI / MACD）", "Momentum (RSI / MACD)"), s, label,
                          L(f"RSI(14)={rsi:.1f}{note}；MACD 柱 {hist:+.3f}，较前一日{'走强' if rising else '走弱'}",
                            f"RSI(14)={rsi:.1f}{note}; MACD histogram {hist:+.3f}, "
                            f"{'stronger' if rising else 'weaker'} than the previous day"),
                          {"RSI14": rsi, "MACD": i["macd"], "Signal": i["macd_signal"], "Hist": hist}))

    # --- relative strength vs benchmark
    if i.get("rs_63") is not None:
        r63, r126 = i["rs_63"], i.get("rs_126")
        s = max(-1.0, min(1.0, r63 / 0.10))
        extra = L(f"，近126日 {r126*100:+.1f}%", f", 126-day {r126*100:+.1f}%") if r126 is not None else ""
        sig.append(Signal("rel_strength", L(f"相对强弱（对比 {benchmark}）", f"Relative strength (vs {benchmark})"), s,
                          L("强于大盘", "Stronger than market") if s > 0.2
                          else L("弱于大盘", "Weaker than market") if s < -0.2 else L("与大盘相当", "In line with market"),
                          L(f"近63日超额收益 {r63*100:+.1f}%{extra}", f"63-day excess return {r63*100:+.1f}%{extra}"),
                          {"RS63": r63, "RS126": r126}))

    # --- drawdown from the 52-week high
    dd = i["dd_252"]
    if not math.isnan(dd):
        s = 0.5 if dd > -0.05 else 0.0 if dd > -0.15 else -0.4 if dd > -0.25 else -0.8
        label = (L("接近52周高点", "Near 52-week high") if dd > -0.05
                 else L("正常回撤", "Normal pullback") if dd > -0.15 else L("深度回撤", "Deep drawdown"))
        sig.append(Signal("drawdown", L("回撤", "Drawdown"), s, label,
                          L(f"距52周高点 {dd*100:.1f}%；历史最大回撤 {i['max_dd']*100:.1f}%",
                            f"{dd*100:.1f}% from the 52-week high; max drawdown on record {i['max_dd']*100:.1f}%"),
                          {"DD52w": dd, "MaxDD": i["max_dd"]}))

    # --- volatility / stretch
    pb, atrp = i["pct_b"], i["atr_pct"]
    if not math.isnan(pb) and not math.isnan(atrp):
        s = -0.4 if pb > 1.0 else 0.4 if pb < 0.0 else 0.0
        label = (L("突破上轨（偏热）", "Above upper band (running hot)") if pb > 1
                 else L("跌破下轨（偏冷）", "Below lower band (running cold)") if pb < 0
                 else L("波动正常", "Normal volatility"))
        sig.append(Signal("volatility", L("波动（ATR / 布林带）", "Volatility (ATR / Bollinger)"), s, label,
                          L(f"ATR(14)={i['atr']:.2f}（{atrp*100:.1f}%/日）；布林 %B={pb:.2f}；"
                            f"20日年化波动 {i['vol20']*100:.0f}%",
                            f"ATR(14)={i['atr']:.2f} ({atrp*100:.1f}%/day); Bollinger %B={pb:.2f}; "
                            f"20-day annualised volatility {i['vol20']*100:.0f}%"),
                          {"ATR14": i["atr"], "%B": pb, "Vol20": i["vol20"]}))
    return sig


def composite_score(signals: list[Signal]) -> float:
    tot = sum(WEIGHTS.get(s.key, 0) for s in signals)
    return sum(WEIGHTS.get(s.key, 0) * s.score for s in signals) / tot if tot else 0.0


def stance_of(score: float) -> str:
    return L("偏多", "Bullish") if score >= 0.35 else L("偏空", "Bearish") if score <= -0.35 else L("中性", "Neutral")


def _pick_evidence(stance: str, signals: list[Signal], bts: list[BacktestResult],
                   rng: rt.RangeState | None = None) -> BacktestResult | None:
    """The backtest whose rule is closest to what is driving the current stance."""
    by = {b.key: b for b in bts if b.n_bars}
    if rng is not None and "range_trading" in by and (rng.status == "buy" or by["range_trading"].in_position_now):
        return by["range_trading"]
    mom = next((s for s in signals if s.key == "momentum"), None)
    vol = next((s for s in signals if s.key == "volatility"), None)
    if vol and vol.score > 0 and "boll_reversion" in by and by["boll_reversion"].in_position_now:
        return by["boll_reversion"]
    if mom and mom.inputs.get("RSI14", 50) < 32 and "rsi_reversion" in by:
        return by["rsi_reversion"]
    for key in ("trend_filter", "ma_50_200", "macd", "ma_20_50"):
        if key in by:
            return by[key]
    return next(iter(by.values()), None)


def analyze(symbol: str, bars: pd.DataFrame, bench: pd.Series | None = None, *,
            qty: float = 0.0, avg_cost: float = 0.0, equity: float = 0.0,
            position_value: float = 0.0, cfg: dict | None = None, benchmark: str = "SPY",
            price: float | None = None) -> Analysis:
    q = {"atr_stop_mult": 2.0, "reward_risk": 2.0, "risk_per_trade_pct": 1.0, "vol_budget_pct": 3.0,
         "kelly_fraction": 0.25, "kelly_cap_pct": 15.0, "max_position_pct": 25.0, "cost_bps": 10}
    q.update(cfg or {})
    a = Analysis(symbol)
    if bars is None or len(bars) < 60:
        a.headline = L("历史数据不足（少于 60 个交易日），暂不给出分析。",
                       "Not enough history (fewer than 60 trading days); no analysis for now.")
        return a
    a.ok, a.asof = True, bars.index[-1]
    a.ind = compute_indicators(bars, bench)
    a.price = price if price else a.ind["close"]
    a.signals = score_signals(a.ind, benchmark)
    a.composite = composite_score(a.signals)
    a.stance = stance_of(a.composite)
    rp = dict(q.get("range") or {})
    range_on = rp.pop("enabled", True)
    a.backtests = backtest_all(bars, q["cost_bps"], rp or None)
    if not range_on:
        a.backtests = [b for b in a.backtests if b.key != "range_trading"]
    else:
        try:
            a.range = rt.range_state(bars, rp, price=a.price)
        except Exception:      # the range view must never break the main analysis
            a.range = None
    a.evidence = _pick_evidence(a.stance, a.signals, a.backtests, a.range)

    held = qty > 0 and avg_cost > 0
    a.levels = atr_levels(avg_cost if held else a.price, a.ind["atr"], q["atr_stop_mult"],
                          q["reward_risk"], "cost" if held else "price")
    fresh = atr_levels(a.price, a.ind["atr"], q["atr_stop_mult"], q["reward_risk"])

    # ---- sizing: the smallest of three independent caps
    sz = Sizing(equity=equity, max_weight=q["max_position_pct"] / 100.0)
    vol = a.ind["vol60"] if not math.isnan(a.ind["vol60"]) else a.ind["vol20"]
    sz.vol_weight = vol_budget_weight(vol, q["vol_budget_pct"], sz.max_weight)
    sz.kelly = kelly_from_trades(a.evidence.trade_returns if a.evidence else [],
                                 q["kelly_fraction"], q["kelly_cap_pct"] / 100.0)
    if fresh and equity > 0:
        sz.risk_shares = shares_for_risk(equity, q["risk_per_trade_pct"], fresh.entry, fresh.stop)
        sz.risk_weight = sz.risk_shares * a.price / equity
    caps = {L("波动预算", "Volatility budget"): sz.vol_weight, L("单只上限", "Per-stock cap"): sz.max_weight}
    if equity > 0 and fresh:
        caps[L("单笔风险", "Per-trade risk")] = sz.risk_weight
    if sz.kelly.n >= 10:
        caps[L("分数凯利", "Fractional Kelly")] = sz.kelly.suggested
    sz.binding, sz.suggested_weight = min(caps.items(), key=lambda kv: kv[1])
    if equity > 0:
        sz.suggested_value = sz.suggested_weight * equity
        sz.suggested_shares = math.floor(sz.suggested_value / a.price) if a.price > 0 else 0
        sz.current_weight = position_value / equity
    a.sizing = sz

    _write_text(a, held, qty, avg_cost, q, fresh)
    return a


def _write_text(a: Analysis, held: bool, qty: float, avg_cost: float, q: dict, fresh: Levels | None) -> None:
    drivers = sorted(a.signals, key=lambda s: -abs(WEIGHTS.get(s.key, 0) * s.score))
    top = L("、", ", ").join(s.label for s in drivers[:2])
    a.headline = L(f"综合信号 {a.stance}（{a.composite:+.2f}，范围 -1~+1）：{top}",
                   f"Composite signal {a.stance} ({a.composite:+.2f}, range -1 to +1): {top}")
    r = a.rationale
    for s in a.signals:
        r.append(L(f"【{s.name}】{s.label}（{s.score:+.2f}）— {s.detail}",
                   f"[{s.name}] {s.label} ({s.score:+.2f}) — {s.detail}"))

    rg = a.range
    if rg is not None and rg.status != "short":
        r.append(L(f"【震荡区间】{rg.label} — {rg.detail}", f"[Trading range] {rg.label} — {rg.detail}"))

    sz, lv = a.sizing, a.levels
    if held and lv:
        pnl = a.price / avg_cost - 1
        r.append(L(f"【持仓】{qty:g} 股，成本 {avg_cost:.2f}，浮动盈亏 {pnl*100:+.1f}%。"
                   f"以成本为锚的 ATR 参考位：止损 {lv.stop:.2f}（成本 - {q['atr_stop_mult']:g}×ATR），"
                   f"止盈 {lv.target:.2f}（盈亏比 {q['reward_risk']:g}:1）。",
                   f"[Position] {qty:g} shares, cost {avg_cost:.2f}, unrealised P&L {pnl*100:+.1f}%. "
                   f"ATR reference levels anchored on cost: stop {lv.stop:.2f} (cost - {q['atr_stop_mult']:g}×ATR), "
                   f"target {lv.target:.2f} (reward/risk {q['reward_risk']:g}:1)."))
        if fresh:
            r.append(L(f"【跟踪止损参考】按现价计算的 {q['atr_stop_mult']:g}×ATR 位置为 {fresh.stop:.2f}。",
                       f"[Trailing-stop reference] The {q['atr_stop_mult']:g}×ATR level at the current price "
                       f"is {fresh.stop:.2f}."))
        if a.stance == L("偏空", "Bearish"):
            r.append(L("【参考思路】信号转弱且你持有该股：可以对照自己的止损计划检查仓位，而不是凭情绪加仓或恐慌卖出。",
                       "[Reference view] The signal has weakened and you hold this stock: you can check the position "
                       "against your own stop plan, rather than adding on emotion or panic selling."))
        elif a.stance == L("偏多", "Bullish"):
            r.append(L("【参考思路】趋势与动量仍支持持有；如要加仓，请先看下面的仓位上限。",
                       "[Reference view] Trend and momentum still support holding; if you want to add, "
                       "look at the position cap below first."))
        else:
            r.append(L("【参考思路】信号不明确，量化上没有调整仓位的依据。",
                       "[Reference view] The signal is unclear; quantitatively there is no basis to change the position."))
    elif fresh:
        r.append(L(f"【若新建仓的参考位】入场 {fresh.entry:.2f}，ATR 止损 {fresh.stop:.2f}"
                   f"（每股风险 {fresh.risk_per_share:.2f}），止盈 {fresh.target:.2f}。",
                   f"[Reference levels if building a position] entry {fresh.entry:.2f}, ATR stop {fresh.stop:.2f} "
                   f"(risk per share {fresh.risk_per_share:.2f}), target {fresh.target:.2f}."))
        if a.stance == L("偏多", "Bullish"):
            r.append(L("【参考思路】信号偏多，但请留意是否已经短期涨幅过大（见动量 / 波动项），避免追高。",
                       "[Reference view] The signal is bullish, but check whether the price has already risen too much "
                       "in the short term (see the momentum / volatility items) to avoid chasing."))
        elif a.stance == L("偏空", "Bearish"):
            r.append(L("【参考思路】信号偏空，量化上没有新建仓的依据。",
                       "[Reference view] The signal is bearish; quantitatively there is no basis to build a position."))
        else:
            r.append(L("【参考思路】信号中性，可继续观察。", "[Reference view] The signal is neutral; keep watching."))

    if sz:
        k = sz.kelly
        parts = [L(f"波动预算 {sz.vol_weight*100:.1f}%", f"Volatility budget {sz.vol_weight*100:.1f}%"),
                 L(f"单只上限 {sz.max_weight*100:.0f}%", f"Per-stock cap {sz.max_weight*100:.0f}%")]
        if sz.equity > 0 and sz.risk_shares:
            parts.insert(0, L(f"单笔风险 {q['risk_per_trade_pct']:g}% → 最多 {sz.risk_shares:.0f} 股"
                              f"（{sz.risk_weight*100:.1f}%）",
                              f"Per-trade risk {q['risk_per_trade_pct']:g}% → at most {sz.risk_shares:.0f} shares "
                              f"({sz.risk_weight*100:.1f}%)"))
        if k and k.win_rate is not None and k.payoff:
            parts.append(L(f"{q['kelly_fraction']:g}×凯利 {k.suggested*100:.1f}%"
                           f"（胜率 {k.win_rate*100:.0f}%、盈亏比 {k.payoff:.2f}、{k.n} 笔"
                           f"{'；' + k.note if k.note else ''}）",
                           f"{q['kelly_fraction']:g}×Kelly {k.suggested*100:.1f}% "
                           f"(win rate {k.win_rate*100:.0f}%, payoff ratio {k.payoff:.2f}, {k.n} trades"
                           f"{'; ' + k.note if k.note else ''})"))
        elif k and k.note:
            parts.append(L(f"凯利：{k.note}", f"Kelly: {k.note}"))
        line = (L("【仓位上限】", "[Position cap] ") + L("；", "; ").join(parts)
                + L(f"。取最小值：{sz.suggested_weight*100:.1f}%（受“{sz.binding}”约束）",
                    f". Smallest of these: {sz.suggested_weight*100:.1f}% (limited by '{sz.binding}')"))
        if sz.equity > 0:
            line += L(f" ≈ {sz.suggested_value:,.0f} 美元 / {sz.suggested_shares:.0f} 股；当前仓位 {sz.current_weight*100:.1f}%",
                      f" ≈ {sz.suggested_value:,.0f} USD / {sz.suggested_shares:.0f} shares; "
                      f"current position {sz.current_weight*100:.1f}%")
            if sz.current_weight > sz.suggested_weight + 0.02:
                line += L("，高于参考上限", ", above the reference cap")
        r.append(line + L("。", "."))

    ev = a.evidence
    if ev and ev.n_bars:
        hr = L("无完整交易", "no completed trades") if ev.hit_rate is None else L(
            f"胜率 {ev.hit_rate*100:.0f}%（95% 区间 {ev.hit_ci[0]*100:.0f}%~{ev.hit_ci[1]*100:.0f}%，{ev.n_trades} 笔）",
            f"win rate {ev.hit_rate*100:.0f}% (95% interval {ev.hit_ci[0]*100:.0f}%-{ev.hit_ci[1]*100:.0f}%, "
            f"{ev.n_trades} trades)")
        r.append(L(f"【历史检验】规则“{ev.label}”（{ev.rule}）在本股过去 {ev.years:.1f} 年：{hr}，"
                   f"累计 {ev.total_return*100:+.0f}%（买入持有 {ev.bh_return*100:+.0f}%），"
                   f"最大回撤 {ev.max_dd*100:.0f}%（买入持有 {ev.bh_max_dd*100:.0f}%），"
                   f"已扣单边 {q['cost_bps']:g}bp 成本，信号次日开盘成交。"
                   + ("".join(ev.notes)),
                   f"[Historical test] Rule '{ev.label}' ({ev.rule}) over the past {ev.years:.1f} years on this stock: "
                   f"{hr}, cumulative {ev.total_return*100:+.0f}% (buy-and-hold {ev.bh_return*100:+.0f}%), "
                   f"max drawdown {ev.max_dd*100:.0f}% (buy-and-hold {ev.bh_max_dd*100:.0f}%), "
                   f"after deducting {q['cost_bps']:g}bp one-way cost, signals filled at the next day's open."
                   + ("".join(" " + n for n in ev.notes))))

    a.caveats = [
        DISCLAIMER,
        L("回测只用了这一只股票的一段历史，规则参数是通用默认值、未针对该股调参；即便如此，样本少、"
          "幸存者偏差和市场环境变化都会让历史胜率高估未来表现。",
          "The backtest uses only one stretch of history for this single stock, and the rule parameters are generic "
          "defaults, not tuned to it; even so, a small sample, survivorship bias and changing market conditions can "
          "all make the historical win rate overstate future performance."),
        L("指标之间高度相关（都来自同一条价格序列），综合分数不是概率。",
          "The indicators are highly correlated (all derived from the same price series); the composite score "
          "is not a probability."),
        L("未考虑财报、新闻、税费、汇率与流动性；数据来自免费行情源，可能延迟或有误。",
          "Earnings, news, taxes and fees, FX and liquidity are not considered; data comes from free market-data "
          "sources and may be delayed or wrong."),
    ]
