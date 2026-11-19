"""Behavioural guardrails: spot emotionally driven patterns in the user's OWN trades.

Inputs are read-only: synced executions, current positions, watchlist changes, the user's
own plan (stop / target / max size) and daily price history. Alerts fire after a sync
(the app cannot see an order before it is placed); ``pretrade_check`` runs the same tests
on a trade the user is merely considering.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..models import Alert, Position, Trade
from ..quant import indicators as ind

_NY = ZoneInfo("America/New_York")

DEFAULTS = {
    "lookback_days": 45, "runup_days": 5, "runup_pct": 10.0, "drop_pct": 10.0,
    "rsi_overbought": 70, "rsi_oversold": 30, "above_ma_pct": 8.0, "near_low_pct": 3.0,
    "avg_down_count": 3, "avg_down_window_days": 30, "overtrade_count": 12,
    "revenge_hours": 24, "revenge_loss_pct": 5.0, "disposition_ratio": 1.5,
    "forward_days": 20, "max_position_pct": 25.0, "risk_per_trade_pct": 1.0, "atr_stop_mult": 2.0,
}


def thresholds(cfg: dict | None) -> dict:
    th = dict(DEFAULTS)
    th.update({k: v for k, v in (cfg or {}).items() if v is not None})
    return th


def trade_day(ts: float) -> pd.Timestamp:
    return pd.Timestamp(datetime.fromtimestamp(ts, _NY).date())


def features(bars: pd.DataFrame, th: dict) -> pd.DataFrame:
    """Daily context per bar. ``fwd`` (what happened afterwards) is only used for hindsight stats."""
    c = bars["close"]
    n, fd = int(th["runup_days"]), int(th["forward_days"])
    ma20 = ind.sma(c, 20)
    low20 = bars["low"].rolling(20, min_periods=5).min()
    sma50 = ind.sma(c, 50)
    return pd.DataFrame({
        "close": c, "chg": (c / c.shift(n) - 1.0) * 100.0, "rsi": ind.rsi(c),
        "dist_ma": (c / ma20 - 1.0) * 100.0, "dist_low": (c / low20 - 1.0) * 100.0,
        "sma50": sma50, "sma50_falling": sma50 < sma50.shift(10),
        "atr": ind.atr(bars["high"], bars["low"], c),
        "fwd": (c.shift(-fd) / c - 1.0) * 100.0,
    })


def _at(feat: pd.DataFrame | None, day: pd.Timestamp):
    if feat is None or feat.empty:
        return None
    pos = feat.index.searchsorted(day, side="right") - 1
    if pos < 0 or (day - feat.index[pos]).days > 7:
        return None
    return feat.iloc[pos]


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and not pd.isna(x)


@dataclass
class Verdict:
    hit: bool
    reasons: list[str]
    data: dict


def chase_verdict(row, th: dict) -> Verdict:
    """Buying into a sharp run-up?"""
    reasons, data = [], {}
    if row is None:
        return Verdict(False, reasons, data)
    n = int(th["runup_days"])
    if _ok(row["chg"]):
        data["chg"] = float(row["chg"])
        if row["chg"] >= th["runup_pct"]:
            reasons.append(f"{n} 日已上涨 {row['chg']:.1f}%（阈值 {th['runup_pct']:g}%）")
    if _ok(row["rsi"]):
        data["rsi"] = float(row["rsi"])
        if row["rsi"] >= th["rsi_overbought"]:
            reasons.append(f"RSI(14)={row['rsi']:.0f}，处于超买区（≥{th['rsi_overbought']:g}）")
    if _ok(row["dist_ma"]):
        data["dist_ma"] = float(row["dist_ma"])
        if row["dist_ma"] >= th["above_ma_pct"]:
            reasons.append(f"价格高于 20 日均线 {row['dist_ma']:.1f}%（阈值 {th['above_ma_pct']:g}%）")
    hit = len(reasons) >= 2 or (_ok(row["chg"]) and row["chg"] >= 1.5 * th["runup_pct"])
    return Verdict(bool(hit), reasons, data)


def panic_verdict(row, th: dict) -> Verdict:
    """Selling into a sharp drop / near the lows?"""
    reasons, data = [], {}
    if row is None:
        return Verdict(False, reasons, data)
    n = int(th["runup_days"])
    if _ok(row["chg"]):
        data["chg"] = float(row["chg"])
        if row["chg"] <= -th["drop_pct"]:
            reasons.append(f"{n} 日已下跌 {abs(row['chg']):.1f}%（阈值 {th['drop_pct']:g}%）")
    if _ok(row["rsi"]):
        data["rsi"] = float(row["rsi"])
        if row["rsi"] <= th["rsi_oversold"]:
            reasons.append(f"RSI(14)={row['rsi']:.0f}，处于超卖区（≤{th['rsi_oversold']:g}）")
    if _ok(row["dist_low"]):
        data["dist_low"] = float(row["dist_low"])
        if row["dist_low"] <= th["near_low_pct"]:
            reasons.append(f"价格距 20 日最低点仅 {row['dist_low']:.1f}%")
    hit = len(reasons) >= 2 or (_ok(row["chg"]) and row["chg"] <= -1.5 * th["drop_pct"])
    return Verdict(bool(hit), reasons, data)


def round_trips(trades: list[Trade]) -> list[dict]:
    """FIFO-matched closed lots: symbol, buy/sell time and price, return, holding days."""
    lots: dict[str, list[list]] = {}
    out: list[dict] = []
    for t in sorted(trades, key=lambda x: x.ts):
        if t.side == "BUY":
            lots.setdefault(t.symbol, []).append([t.qty, t.price, t.ts])
            continue
        remaining = t.qty
        queue = lots.get(t.symbol, [])
        while remaining > 1e-9 and queue:
            lot = queue[0]
            q = min(lot[0], remaining)
            out.append({"symbol": t.symbol, "qty": q, "buy_price": lot[1], "sell_price": t.price,
                        "buy_ts": lot[2], "sell_ts": t.ts, "ret": t.price / lot[1] - 1.0 if lot[1] else 0.0,
                        "days": max((t.ts - lot[2]) / 86400.0, 0.0), "sell_id": t.id})
            lot[0] -= q
            remaining -= q
            if lot[0] <= 1e-9:
                queue.pop(0)
    return out


def _history_note(kind: str, trades: list[Trade], feats: dict[str, pd.DataFrame], th: dict,
                  exclude_id: str | None = None) -> tuple[str, dict]:
    """How did the user's earlier trades of the same kind turn out? (hindsight statistics)"""
    side = "BUY" if kind == "chase" else "SELL"
    fwd: list[float] = []
    for t in trades:
        if t.side != side or t.id == exclude_id:
            continue
        row = _at(feats.get(t.symbol), trade_day(t.ts))
        if row is None or not _ok(row["fwd"]):
            continue
        v = chase_verdict(row, th) if kind == "chase" else panic_verdict(row, th)
        if v.hit:
            fwd.append(float(row["fwd"]))
    fd = int(th["forward_days"])
    if not fwd:
        return f"你的历史记录里还没有可以统计的同类操作（需要成交后满 {fd} 个交易日）。", {"n": 0}
    avg, n = float(np.mean(fwd)), len(fwd)
    if kind == "chase":
        bad = sum(1 for x in fwd if x < 0)
        note = (f"你过去 {n} 次类似的追涨买入，之后 {fd} 个交易日平均 {avg:+.1f}%，其中 {bad} 次是亏的。")
    else:
        rebound = sum(1 for x in fwd if x > 0)
        note = (f"你过去 {n} 次类似的急跌卖出，卖出后 {fd} 个交易日股价平均 {avg:+.1f}%，"
                f"其中 {rebound} 次卖出后反而上涨。")
    if n < 5:
        note += "（样本很少，仅供参考）"
    return note, {"n": n, "avg_fwd": avg}


def _fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, _NY).strftime("%Y-%m-%d")


def detect(trades: list[Trade], bars: dict[str, pd.DataFrame], positions: dict[str, Position],
           prices: dict[str, float], plans: dict[str, dict], wl_events: list[dict],
           cfg: dict | None = None, equity: float = 0.0, now: float | None = None,
           fx: dict[str, float] | None = None) -> list[Alert]:
    th = thresholds(cfg)
    fx = fx or {}
    now = now or time.time()
    since = now - th["lookback_days"] * 86400
    feats = {s: features(b, th) for s, b in bars.items() if b is not None and len(b) >= 30}
    alerts: list[Alert] = []
    trades = sorted(trades, key=lambda t: t.ts)
    recent = [t for t in trades if t.ts >= since]
    tag = lambda t: "【演示数据】" if t.demo else ""

    # 1+2: chasing rallies / panic selling, per recent trade ------------------------------
    for t in recent:
        row = _at(feats.get(t.symbol), trade_day(t.ts))
        if t.side == "BUY":
            v = chase_verdict(row, th)
            if v.hit:
                note, hist = _history_note("chase", trades, feats, th, t.id)
                alerts.append(Alert(
                    f"chase:{t.id}", "chase", t.symbol, "high" if len(v.reasons) >= 3 else "warn",
                    f"追涨提醒：{t.symbol}",
                    f"{tag(t)}你在 {_fmt_day(t.ts)} 以 {t.price:.2f} 买入 {t.qty:g} 股 {t.symbol}，"
                    f"当时：{'；'.join(v.reasons)}。急涨之后买入往往买在短期高点。\n{note}\n"
                    f"问问自己：这笔买入是计划内的，还是因为怕错过？",
                    {**v.data, "history": hist, "trade": t.id}, demo=t.demo))
        else:
            v = panic_verdict(row, th)
            if v.hit:
                note, hist = _history_note("panic", trades, feats, th, t.id)
                alerts.append(Alert(
                    f"panic:{t.id}", "panic", t.symbol, "high" if len(v.reasons) >= 3 else "warn",
                    f"杀跌提醒：{t.symbol}",
                    f"{tag(t)}你在 {_fmt_day(t.ts)} 以 {t.price:.2f} 卖出 {t.qty:g} 股 {t.symbol}，"
                    f"当时：{'；'.join(v.reasons)}。急跌之后卖出往往卖在短期低点。\n{note}\n"
                    f"问问自己：是触发了事先定好的止损，还是因为恐慌？",
                    {**v.data, "history": hist, "trade": t.id}, demo=t.demo))

    # 3: FOMO watchlist additions ---------------------------------------------------------
    for ev in wl_events:
        if ev.get("action") != "add" or ev["ts"] < since:
            continue
        row = _at(feats.get(ev["symbol"]), trade_day(ev["ts"]))
        if row is not None and _ok(row["chg"]) and row["chg"] >= th["runup_pct"]:
            alerts.append(Alert(
                f"fomo_watch:{ev['symbol']}:{_fmt_day(ev['ts'])}", "fomo_watch", ev["symbol"], "info",
                f"急涨后才加入自选：{ev['symbol']}",
                f"你在 {_fmt_day(ev['ts'])} 把 {ev['symbol']} 加入自选，此前 {int(th['runup_days'])} 日它已上涨 "
                f"{row['chg']:.1f}%。刚大涨就开始关注，容易在情绪高点买入。建议先设好计划买入价和止损，再决定是否参与。",
                {"chg": float(row["chg"])}))

    # 4: disposition effect ---------------------------------------------------------------
    trips = round_trips(trades)
    wins = [r for r in trips if r["ret"] > 0]
    losses = [r for r in trips if r["ret"] <= 0]
    if len(wins) >= 3 and len(losses) >= 3:
        dw, dl = float(np.mean([r["days"] for r in wins])), float(np.mean([r["days"] for r in losses]))
        aw, al = float(np.mean([r["ret"] for r in wins])), float(np.mean([r["ret"] for r in losses]))
        if dw > 0 and dl / dw >= th["disposition_ratio"]:
            demo = all(t.demo for t in trades)
            alerts.append(Alert(
                f"disposition:{datetime.fromtimestamp(now, _NY).strftime('%Y-%m')}", "disposition", "", "warn",
                "处置效应：盈利拿不住、亏损不肯走",
                f"{'【演示数据】' if demo else ''}你已平仓的 {len(trips)} 笔交易中，盈利单平均持有 {dw:.0f} 天"
                f"（平均 {aw*100:+.1f}%），亏损单平均持有 {dl:.0f} 天（平均 {al*100:+.1f}%）。"
                f"亏损单的持有时间是盈利单的 {dl/dw:.1f} 倍（阈值 {th['disposition_ratio']:g}）。"
                f"这是典型的“赚一点就跑、亏了死扛”。可以给每笔持仓预先写下止损和止盈条件。",
                {"win_days": dw, "loss_days": dl, "n_win": len(wins), "n_loss": len(losses)}, demo=demo))

    # 5: averaging down into a downtrend --------------------------------------------------
    win = th["avg_down_window_days"] * 86400
    by_sym: dict[str, list[Trade]] = {}
    for t in recent:
        if t.side == "BUY":
            by_sym.setdefault(t.symbol, []).append(t)
    for sym, buys in by_sym.items():
        streak = [buys[0]]
        for b in buys[1:]:
            if b.price < streak[-1].price and b.ts - streak[0].ts <= win:
                streak.append(b)
            else:
                streak = [b]
        row = _at(feats.get(sym), trade_day(streak[-1].ts))
        down = row is not None and _ok(row["sma50"]) and row["close"] < row["sma50"] and bool(row["sma50_falling"])
        if len(streak) >= th["avg_down_count"] and down:
            alerts.append(Alert(
                f"avg_down:{sym}:{streak[-1].id}", "avg_down", sym, "warn", f"下跌趋势中连续加仓：{sym}",
                f"{tag(streak[-1])}你在 {_fmt_day(streak[0].ts)} ~ {_fmt_day(streak[-1].ts)} 连续 {len(streak)} 次买入 "
                f"{sym}，价格一次比一次低（{' → '.join(f'{b.price:.2f}' for b in streak)}），而股价仍在下行的 "
                f"50 日均线（{row['sma50']:.2f}）下方。越跌越买会让一笔亏损变成重仓亏损。"
                f"建议先确认这是计划内的分批建仓，并设定总仓位上限。",
                {"buys": [b.price for b in streak]}, demo=streak[-1].demo))

    # 6: overtrading and revenge trading --------------------------------------------------
    week = [t for t in trades if t.ts >= now - 7 * 86400]
    if len(week) > th["overtrade_count"]:
        alerts.append(Alert(
            f"overtrade:{_fmt_day(now)}", "overtrade", "", "warn", "交易过于频繁",
            f"最近 7 天你成交了 {len(week)} 笔（阈值 {th['overtrade_count']:g}）。频繁交易会放大成本，"
            f"也常常是情绪化的信号。", {"count": len(week)}, demo=all(t.demo for t in week)))
    for r in trips:
        if r["sell_ts"] < since or r["ret"] * 100 > -th["revenge_loss_pct"]:
            continue
        after = [t for t in trades if t.side == "BUY" and 0 < t.ts - r["sell_ts"] <= th["revenge_hours"] * 3600]
        if after:
            b = after[0]
            alerts.append(Alert(
                f"revenge:{r['sell_id']}", "revenge", b.symbol, "warn", "亏损后立刻再次买入（报复性交易？）",
                f"{tag(b)}你在 {_fmt_day(r['sell_ts'])} 卖出 {r['symbol']} 亏损 {r['ret']*100:.1f}%，"
                f"随后 {th['revenge_hours']:g} 小时内又买入 {b.symbol}（{b.qty:g} 股 @ {b.price:.2f}）。"
                f"刚亏完就急着赚回来的交易，通常计划性最差。建议亏损后给自己设一个冷静期。",
                {"loss": r["ret"], "next_buy": b.id}, demo=b.demo))

    # 7+8: concentration and breaking one's own plan ---------------------------------------
    values = {s: p.qty * prices.get(s, p.market_price or p.avg_cost) * fx.get(s, 1.0)
              for s, p in positions.items() if p.qty > 0}
    total = equity if equity > 0 else sum(values.values())
    for sym, val in values.items():
        plan = plans.get(sym) or {}
        px = prices.get(sym)
        w = val / total * 100 if total else 0.0
        limit = plan.get("max_pct") or th["max_position_pct"]
        if w > limit:
            own = "你自己设定的" if plan.get("max_pct") else "默认的"
            alerts.append(Alert(
                f"oversize:{sym}", "oversize", sym, "warn", f"仓位过重：{sym}",
                f"{sym} 当前市值 {val:,.0f} 美元，占组合 {w:.1f}%，超过{own}单只上限 {limit:g}%。"
                f"单只股票仓位过重时，一次意外就会明显影响整个账户。", {"weight": w, "limit": limit}))
        if px and plan.get("stop") and px < plan["stop"]:
            alerts.append(Alert(
                f"stop_breach:{sym}:{plan['stop']}", "plan_breach", sym, "high", f"已跌破你设定的止损：{sym}",
                f"{sym} 现价 {px:.2f}，低于你自己设定的止损价 {plan['stop']:.2f}"
                f"（低 {(1 - px / plan['stop']) * 100:.1f}%），但你仍持有 {positions[sym].qty:g} 股。"
                f"计划是你冷静时定的；如果要改止损，请写下理由，而不是默默放宽。",
                {"price": px, "stop": plan["stop"]}))
        if px and plan.get("target") and px >= plan["target"]:
            alerts.append(Alert(
                f"target_hit:{sym}:{plan['target']}", "plan_breach", sym, "info", f"已到达你设定的目标价：{sym}",
                f"{sym} 现价 {px:.2f}，已达到你设定的目标价 {plan['target']:.2f}。"
                f"你原本的计划是到这里怎么做？如果要继续持有，建议同步上移止损。",
                {"price": px, "target": plan["target"]}))
    return alerts


# ---- pre-trade check ---------------------------------------------------------------------

@dataclass
class Check:
    level: str      # "ok" | "warn" | "high" | "info"
    title: str
    detail: str


def pretrade_check(symbol: str, side: str, qty: float, price: float, bars: pd.DataFrame | None,
                   trades: list[Trade], positions: dict[str, Position], prices: dict[str, float],
                   plan: dict | None, cfg: dict | None = None, equity: float = 0.0,
                   now: float | None = None) -> list[Check]:
    """Run the behavioural checks on a trade the user is only THINKING about. Places nothing."""
    th = thresholds(cfg)
    now = now or time.time()
    plan = plan or {}
    side = side.upper()
    out: list[Check] = []
    feat = features(bars, th) if bars is not None and len(bars) >= 30 else None
    row = feat.iloc[-1] if feat is not None else None
    feats = {symbol: feat} if feat is not None else {}
    values = {s: p.qty * prices.get(s, p.market_price or p.avg_cost) for s, p in positions.items() if p.qty > 0}
    total = equity if equity > 0 else sum(values.values())
    held = positions.get(symbol)
    if row is None:
        out.append(Check("info", "行情数据不足", "没有足够的历史数据来判断追涨 / 杀跌，只能检查仓位。"))

    if side == "BUY":
        v = chase_verdict(row, th)
        if v.hit:
            note, _ = _history_note("chase", trades, feats, th)
            out.append(Check("high", "这看起来像追涨", "；".join(v.reasons) + "。" + note))
        elif v.reasons:
            out.append(Check("warn", "短期偏热", "；".join(v.reasons) + "。只满足一项，尚未达到追涨标准。"))
        else:
            out.append(Check("ok", "没有追涨迹象", "近期涨幅、RSI、与均线的距离都在正常范围内。"))

        if held and held.avg_cost and price < held.avg_cost and row is not None and _ok(row["sma50"]) \
                and row["close"] < row["sma50"] and bool(row["sma50_falling"]):
            n_recent = sum(1 for t in trades if t.symbol == symbol and t.side == "BUY"
                           and t.ts >= now - th["avg_down_window_days"] * 86400)
            out.append(Check("warn", "下跌趋势中摊低成本",
                             f"你持有 {held.qty:g} 股、成本 {held.avg_cost:.2f}，现价低于成本，且股价在下行的 50 日均线"
                             f"（{row['sma50']:.2f}）下方；近 {th['avg_down_window_days']:g} 天已买入 {n_recent} 次。"))
        last_loss = [r for r in round_trips(trades)
                     if r["ret"] * 100 <= -th["revenge_loss_pct"] and 0 <= now - r["sell_ts"] <= th["revenge_hours"] * 3600]
        if last_loss:
            r = last_loss[-1]
            out.append(Check("warn", "刚刚亏损卖出过",
                             f"{th['revenge_hours']:g} 小时内你卖出 {r['symbol']} 亏损 {r['ret']*100:.1f}%。"
                             f"确认这笔买入不是为了“赚回来”。"))

        new_val = values.get(symbol, 0.0) + qty * price
        new_total = total if equity > 0 else total + qty * price
        if new_total > 0:
            w = new_val / new_total * 100
            limit = plan.get("max_pct") or th["max_position_pct"]
            lvl = "high" if w > limit else "ok"
            out.append(Check(lvl, "仓位占比", f"买入后 {symbol} 约占组合 {w:.1f}%"
                             f"（上限 {limit:g}%{'，你自己设定的' if plan.get('max_pct') else ''}）。"
                             + ("超过上限。" if w > limit else "")))
        if row is not None and _ok(row["atr"]) and total > 0:
            stop = plan.get("stop") or price - th["atr_stop_mult"] * float(row["atr"])
            if stop < price:
                loss = (price - stop) * qty
                pct = loss / (new_total or total) * 100
                lvl = "warn" if pct > th["risk_per_trade_pct"] else "ok"
                src = "你设定的止损" if plan.get("stop") else f"{th['atr_stop_mult']:g}×ATR 止损"
                max_sh = math.floor((new_total or total) * th["risk_per_trade_pct"] / 100 / (price - stop))
                out.append(Check(lvl, "这笔交易的风险",
                                 f"按{src} {stop:.2f} 计算，打到止损亏损约 {loss:,.0f} 美元，占账户 {pct:.2f}%"
                                 f"（单笔上限 {th['risk_per_trade_pct']:g}% → 最多约 {max_sh} 股）。"))
        if not plan.get("stop") and not plan.get("target"):
            out.append(Check("info", "还没有计划", "你没有为这只股票设定止损 / 目标价。先写下退出条件，再决定买不买。"))
    else:
        v = panic_verdict(row, th)
        if v.hit:
            note, _ = _history_note("panic", trades, feats, th)
            out.append(Check("high", "这看起来像杀跌", "；".join(v.reasons) + "。" + note))
        elif v.reasons:
            out.append(Check("warn", "短期偏冷", "；".join(v.reasons) + "。只满足一项，尚未达到杀跌标准。"))
        else:
            out.append(Check("ok", "没有杀跌迹象", "近期跌幅、RSI、与低点的距离都在正常范围内。"))
        if not held or held.qty <= 0:
            out.append(Check("info", "未持有", f"记录里你没有 {symbol} 的持仓。"))
        else:
            if qty > held.qty + 1e-9:
                out.append(Check("warn", "数量超过持仓", f"你只持有 {held.qty:g} 股。"))
            pnl = (price / held.avg_cost - 1) * 100 if held.avg_cost else 0.0
            stop, target = plan.get("stop"), plan.get("target")
            if stop and price <= stop:
                out.append(Check("ok", "符合你的止损计划", f"现价 {price:.2f} 已到 / 低于你设定的止损 {stop:.2f}。"))
            elif target and price >= target:
                out.append(Check("ok", "符合你的止盈计划", f"现价 {price:.2f} 已到 / 高于你设定的目标 {target:.2f}。"))
            elif stop or target:
                out.append(Check("warn", "计划并未触发",
                                 f"现价 {price:.2f} 在你的止损 {stop or '-'} 和目标 {target or '-'} 之间，"
                                 f"当前浮动盈亏 {pnl:+.1f}%。是什么让你想现在卖？"))
            losers = [s for s, p in positions.items() if s != symbol and p.avg_cost
                      and prices.get(s) and prices[s] / p.avg_cost - 1 < -0.10]
            if pnl > 0 and losers and not (target and price >= target):
                out.append(Check("warn", "卖盈利、留亏损？",
                                 f"这笔是盈利的（{pnl:+.1f}%），而你同时持有亏损超过 10% 的 {'、'.join(losers[:4])}。"
                                 f"留意“处置效应”：倾向于过早卖出赚钱的、死扛亏钱的。"))
    week = sum(1 for t in trades if t.ts >= now - 7 * 86400)
    if week >= th["overtrade_count"]:
        out.append(Check("warn", "最近交易频繁", f"过去 7 天已成交 {week} 笔（阈值 {th['overtrade_count']:g}）。"))
    return out
