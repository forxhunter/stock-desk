"""Daily briefing: today's rule-based state of every position, and what changed since the last day.

This is a mechanical summary of the signals, reference levels and the user's own discipline rules.
It never says what to buy or sell, and nothing here can place an order.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np

from .quant import optimizer

DEFAULTS = {
    "cooloff_days": 5,          # trading days after a sale during which a re-buy is flagged
    "panic_drop_pct": 15.0,     # 5-day drop that counts as a sharp fall...
    "panic_rsi": 30.0,          # ...together with RSI at or below this
    "rebalance_method": "risk_parity",
    "keep_days": 40,
}

RANGE_TEXT = {
    "buy": "接近支撑（低吸区）", "sell": "接近阻力（高抛区）", "mid": "区间中部",
    "breakout_up": "向上突破区间", "breakdown": "向下跌破区间", "loose": "区间松动",
    "none": "非震荡", "short": "数据不足",
}


@dataclass
class BriefItem:
    symbol: str
    currency: str = "USD"
    price: float = float("nan")
    day_pct: float = 0.0
    qty: float = 0.0
    weight: float = 0.0
    upl_pct: float = 0.0
    stance: str = "数据不足"
    composite: float = 0.0
    range_status: str = "short"
    range_label: str = ""
    support: float = float("nan")
    resistance: float = float("nan")
    stop: float | None = None
    target: float | None = None
    levels_src: str = ""
    stop_dist_pct: float | None = None     # how far the price is above the stop (negative = below)
    cap_pct: float = 25.0
    cap_src: str = "默认"
    flags: list[tuple[str, str]] = field(default_factory=list)    # (level, text); level: high|warn|info
    changes: list[str] = field(default_factory=list)
    has_data: bool = True


@dataclass
class Briefing:
    day: str = ""
    prev_day: str | None = None
    held: list[BriefItem] = field(default_factory=list)
    watch: list[BriefItem] = field(default_factory=list)
    changes: list[tuple[str, str]] = field(default_factory=list)   # (symbol, text)
    rebalance: list[dict] = field(default_factory=list)
    rebalance_method: str = ""
    month_trades: int = 0
    prev_month_trades: int = 0
    notes: list[str] = field(default_factory=list)


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _state(it: BriefItem, keys: set[str]) -> dict:
    return {"held": it.qty > 0, "stance": it.stance, "range": it.range_status, **{k: (k in keys) for k in
            ("below_stop", "at_target", "over_cap", "downtrend", "panic")}}


_CHANGE = {
    "below_stop": ("跌破止损参考位", "回到止损参考位上方"),
    "at_target": ("到达目标参考位", "回落到目标参考位下方"),
    "over_cap": ("仓位超过上限", "仓位回到上限以内"),
    "downtrend": ("进入“下行 50 日均线下方”状态（加仓暂停条件成立）", "离开“下行 50 日均线下方”状态"),
    "panic": ("出现急跌 + 超卖", "急跌 + 超卖状态结束"),
}


def diff(prev: dict | None, cur: dict) -> list[str]:
    if prev is None:
        return ["新出现在列表中"]
    out = []
    if prev.get("held") != cur["held"]:
        out.append("新建仓" if cur["held"] else "已清仓")
    if prev.get("stance") != cur["stance"]:
        out.append(f"综合信号 {prev.get('stance')} → {cur['stance']}")
    if prev.get("range") != cur["range"]:
        out.append(f"震荡区间 {RANGE_TEXT.get(prev.get('range'), '—')} → {RANGE_TEXT.get(cur['range'], '—')}")
    for k, (on, off) in _CHANGE.items():
        if bool(prev.get(k)) != cur[k]:
            out.append(on if cur[k] else off)
    return out


def _item(v, cfg: dict, default_cap: float, last_sell: dict[str, float], today: date) -> tuple[BriefItem, set[str]]:
    a = v.analysis
    it = BriefItem(symbol=v.symbol, currency=getattr(v, "currency", "USD") or "USD", price=v.price,
                   day_pct=v.day_pct, qty=v.qty, weight=v.weight, upl_pct=v.upl_pct,
                   stop=v.stop, target=v.target, levels_src=v.levels_src)
    keys: set[str] = set()
    user_cap = (v.plan or {}).get("max_pct")
    it.cap_pct, it.cap_src = (float(user_cap), "你设定的") if user_cap else (default_cap, "默认")
    if not a or not a.ok or _nan(v.price):
        it.has_data = False
        if v.qty:
            it.flags.append(("warn", "没有取到这只股票的行情 / 历史数据，信号和参考位都无法计算。"))
    else:
        it.stance, it.composite = a.stance, a.composite
        rg = a.range
        if rg is not None:
            it.range_status, it.range_label = rg.status, RANGE_TEXT.get(rg.status, rg.label)
            it.support, it.resistance = rg.support, rg.resistance
        i = a.ind
        down = (not _nan(i.get("sma50")) and not _nan(i.get("sma50_prev"))
                and v.price < i["sma50"] and i["sma50"] < i["sma50_prev"])
        if down:
            keys.add("downtrend")
            if v.qty:
                it.flags.append(("warn", f"股价在下行的 50 日均线（{i['sma50']:.2f}）下方："
                                         "“下跌趋势中不加仓”这条规则的条件成立。"))
        chg5, rsi = i.get("chg_5d"), i.get("rsi")
        if not _nan(chg5) and not _nan(rsi) and chg5 * 100 <= -cfg["panic_drop_pct"] and rsi <= cfg["panic_rsi"]:
            keys.add("panic")
            it.flags.append(("warn", f"5 日跌幅 {chg5*100:.1f}%、RSI {rsi:.0f}（超卖）："
                                     "“急跌时先等一天再决定”这条规则的条件成立。"))
        if rg is not None and rg.status in ("buy", "sell", "breakout_up", "breakdown"):
            lvl = "warn" if rg.status == "breakdown" else "info"
            it.flags.append((lvl, f"震荡区间：{it.range_label}（支撑 {rg.support:.2f} / 阻力 {rg.resistance:.2f}）。"))
    if v.qty and not _nan(v.price):
        src = "你设定的" if v.levels_src == "user" else "按 ATR 估算的"
        if v.stop:
            it.stop_dist_pct = (v.price / v.stop - 1) * 100
            if v.price < v.stop:
                keys.add("below_stop")
                it.flags.append(("high", f"现价 {v.price:.2f} 低于{src}止损参考位 {v.stop:.2f}。"))
        if v.target and v.price >= v.target:
            keys.add("at_target")
            it.flags.append(("info", f"现价 {v.price:.2f} 已到达{src}目标参考位 {v.target:.2f}。"))
        if v.weight * 100 > it.cap_pct:
            keys.add("over_cap")
            it.flags.append(("warn", f"占组合 {v.weight*100:.1f}%，超过{it.cap_src}单只上限 {it.cap_pct:g}%。"))
    ts = last_sell.get(v.symbol)
    if ts:
        sold = datetime.fromtimestamp(ts).date()
        n = int(np.busday_count(sold, today))
        if 0 <= n < cfg["cooloff_days"]:
            it.flags.append(("info", f"{sold:%m-%d} 卖出过，“卖出后 {cfg['cooloff_days']} 个交易日内不追高买回”"
                                     f"的冷静期还剩 {cfg['cooloff_days'] - n} 个交易日。"))
    return it, keys


def build(snap, store=None, cfg: dict | None = None, default_cap: float = 25.0,
          min_trade_pct: float = 1.0, now: float | None = None) -> Briefing:
    c = dict(DEFAULTS)
    c.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS and v is not None})
    now = now or time.time()
    today = datetime.fromtimestamp(now).date()
    b = Briefing(day=today.isoformat())

    last_sell: dict[str, float] = {}
    month = today.strftime("%Y-%m")
    pm = (today.replace(day=1).toordinal() - 1)
    prev_month = date.fromordinal(pm).strftime("%Y-%m")
    for t in snap.trades:
        if t.demo:
            continue
        m = datetime.fromtimestamp(t.ts).strftime("%Y-%m")
        b.month_trades += m == month
        b.prev_month_trades += m == prev_month
        if t.side == "SELL":
            last_sell[t.symbol] = max(last_sell.get(t.symbol, 0.0), t.ts)

    states: dict[str, dict] = {}
    for v in snap.tickers:
        it, keys = _item(v, c, default_cap, last_sell, today)
        states[v.symbol] = _state(it, keys)
        (b.held if v.qty else b.watch).append(it)
    b.held.sort(key=lambda i: -i.weight)

    # ---- what changed since the previous recorded day
    if store is not None:
        hist = store.get_meta("briefing:states", {}) or {}
        earlier = sorted(d for d in hist if d < b.day)
        if earlier:
            b.prev_day = earlier[-1]
            prev = hist[b.prev_day]
            for it in b.held + b.watch:
                it.changes = diff(prev.get(it.symbol), states[it.symbol])
                b.changes.extend((it.symbol, ch) for ch in it.changes)
            for sym, st in prev.items():
                if sym not in states and st.get("held"):
                    b.changes.append((sym, "已不在持仓 / 自选中"))
        hist[b.day] = states
        for d in sorted(hist)[: -int(c["keep_days"])]:
            hist.pop(d, None)
        store.set_meta("briefing:states", hist)

    # ---- distance between the current weights and a risk-based reference allocation
    opt = snap.opt
    method = c["rebalance_method"] if opt and c["rebalance_method"] in opt.weights else None
    if method and snap.total_value > 0:
        b.rebalance_method = optimizer.METHODS.get(method, method)
        fx = {v.symbol: (getattr(v, "fx", 1.0) or 1.0) for v in snap.tickers}
        prices = {v.symbol: v.price * fx[v.symbol] for v in snap.tickers if not _nan(v.price)}
        inside = {v.symbol: v.value for v in snap.tickers if v.symbol in opt.symbols}
        d = optimizer.rebalance_deltas(inside, opt.weights[method], prices, sum(inside.values()), min_trade_pct)
        for sym, r in d.sort_values("delta_value").iterrows():
            if r["delta_value"]:
                b.rebalance.append({"symbol": sym, "current": float(r["current_weight"]),
                                    "target": float(r["target_weight"]), "delta_value": float(r["delta_value"]),
                                    "delta_shares": float(r["delta_shares"])})
        missing = [v.symbol for v in snap.tickers if v.value > 0 and v.symbol not in opt.symbols]
        if missing:
            b.notes.append(f"{'、'.join(missing)} 的历史数据不足，没有参与参考配置的计算（差额只在其余股票之间算）。")

    if b.prev_month_trades and b.month_trades > b.prev_month_trades * 1.5 and b.month_trades >= 8:
        b.notes.append(f"本月已成交 {b.month_trades} 笔，明显多于上月的 {b.prev_month_trades} 笔。")
    return b
