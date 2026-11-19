"""Run Stock Desk without a window, for an always-on machine (cron every 10 minutes).

    python -m stockdesk.headless tick          one cycle: quotes, signals, scan, alert mails, daily digest
    python -m stockdesk.headless digest        print today's digest (add --send to mail it now)
    python -m stockdesk.headless status        one line per data source

Same engine and rules as the desktop app; read-only, signals only. Set ``STOCKDESK_ROLE=remote`` on
the server so that it (and not the desktop, which may be asleep) is the one that sends mail when
``notify.email.send_from`` is ``remote``.
"""
from __future__ import annotations

import logging
import math
import sys
import time
from datetime import datetime

from . import flow, notify, paths
from .briefing import RANGE_TEXT
from .engine import Engine, Snapshot

log = logging.getLogger("stockdesk.headless")
MAIL_KINDS = ("bottoming", "plan_level", "flow")


def _px(x: float) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.2f}"


def digest(snap: Snapshot, now: float | None = None) -> tuple[str, str]:
    """Plain-text daily summary: signals and levels only, no holdings, amounts or P&L."""
    day = datetime.fromtimestamp(now or time.time())
    out = [f"Stock Desk 每日信号汇总 · {day:%Y-%m-%d %H:%M}", ""]

    out.append("【量化信号】")
    for v in snap.tickers:
        a = v.analysis
        if a is None or not a.ok or math.isnan(v.price):
            out.append(f"  {v.symbol}：没有取到行情")
            continue
        line = f"  {v.symbol}　{_px(v.price)} {v.currency}　今日 {v.day_pct:+.2f}%　综合信号：{a.stance}（{a.composite:+.2f}）"
        rg = a.range
        if rg is not None and rg.status not in ("none", "short"):
            line += f"　震荡区间：{RANGE_TEXT.get(rg.status, rg.label)}（支撑 {_px(rg.support)} / 阻力 {_px(rg.resistance)}）"
        out.append(line)

    if snap.bottoms:
        out += ["", "【筑底检查】（价格 6 项 + 成交量 3 项，达到门槛才算信号）"]
        for sym, st in snap.bottoms.items():
            mark = "★ 已达到门槛" if st.triggered else "未达到门槛" if st.eligible else "跌幅不够深，不适用"
            out.append(f"  {sym}：{st.summary}，距 52 周高点 {st.drawdown_pct:.1f}%，{mark}")
            if st.eligible:
                out.append("    成立：" + ("、".join(st.passed) or "无"))

    if snap.plans:
        out += ["", "【你的计划】（价位是你自己写的）"]
        for p in snap.plans:
            who = f"{p.symbol}（按 {p.follow} 的价格）" if p.follow else p.symbol
            unit = f" {p.level_ccy}" if p.level_ccy else ""
            bits = [f"现价 {_px(p.check_price)}{unit}"]
            if p.next_buy is not None:
                bits.append(f"下一档买入价 {_px(p.next_buy)}")
            if p.next_level is not None:
                bits.append(f"下一档减仓价 {_px(p.next_level)}")
            if p.target_price is not None:
                bits.append(f"目标价 {_px(p.target_price)}")
            if p.exit_below is not None:
                bits.append(f"底线 {_px(p.exit_below)}")
            if p.days_left:
                bits.append(f"还剩 {p.days_left} 天")
            out.append(f"  {who}：" + "，".join(bits))
            for _, _, title, _ in p.events:
                out.append(f"    ● {title}")
            if p.review_due:
                out.append("    ● 到了你定的复盘日")

    rep = snap.flow
    if rep is not None and rep.ts:
        hot = [i for i in rep.items if i.severity != "info"]
        out += ["", f"【异动扫描】（{datetime.fromtimestamp(rep.ts):%H:%M} 的一轮，共 {len(hot)} 条达到中级以上）"]
        for i in hot[:8]:
            tag = f"[{flow.TIER_LABEL[i.tier]}{' ' + ','.join(i.tickers) if i.tickers else ''}]"
            out.append(f"  {tag} {i.title}：{i.headline}")
        if not hot:
            out.append("  没有达到门槛的异动。")
        for n in rep.notes:
            k, _, text = n.partition("|")
            if k == "futures":
                out.append(f"  · {text}")

    today = [a for a in snap.alerts if a.kind in MAIL_KINDS and not a.demo and a.created >= (now or time.time()) - 86400]
    if today:
        out += ["", "【近 24 小时的提醒】"] + [f"  · {a.title}" for a in today[:12]]

    bad = [msg for state, msg in snap.sources.values() if state == "error"]
    if bad:
        out += ["", "【数据源问题】"] + [f"  · {m}" for m in bad]
    return f"Stock Desk 每日信号 {day:%m-%d}", "\n".join(out)


def digest_due(cfg, store, now: float | None = None) -> bool:
    c = dict(cfg.get("notify.email", {}) or {})
    if not c.get("enabled") or not c.get("daily_enabled", True):
        return False
    t = datetime.fromtimestamp(now or time.time())
    if t.weekday() >= 5 and not c.get("daily_weekends", False):
        return False
    try:
        hh, mm = (int(x) for x in str(c.get("daily_at", "15:30")).split(":")[:2])
    except ValueError:
        hh, mm = 15, 30
    return (t.hour, t.minute) >= (hh, mm) and store.get_meta("email:digest_day") != f"{t:%Y-%m-%d}"


def tick(engine: Engine | None = None, now: float | None = None) -> Snapshot:
    e = engine or Engine()
    e.refresh(False)
    if e.research_due():
        e.refresh_research()
    if e.flow_due():
        e.scan_flow()
    snap = e.build_snapshot()                 # raises alerts and mails the new ones
    if e.mail_role_ok() and digest_due(e.cfg, e.store, now):
        try:
            notify.send(notify.settings(e.cfg.get("notify.email", {})), *digest(snap, now))
            e.store.set_meta("email:digest_day", f"{datetime.fromtimestamp(now or time.time()):%Y-%m-%d}")
            log.info("daily digest sent")
        except notify.NotifyError as err:
            log.warning("daily digest failed: %s", err)
    e.store.set_meta("headless:tick", {"at": now or time.time(), "sources": {k: list(v) for k, v in snap.sources.items()}})
    return snap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "tick"
    paths.ensure_dirs()
    logging.basicConfig(filename=str(paths.LOG_PATH), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if cmd == "tick":
            snap = tick()
            print(f"{datetime.now():%Y-%m-%d %H:%M} ok tickers={len(snap.tickers)} alerts={len(snap.alerts)} "
                  f"flow={len(snap.flow.items) if snap.flow else 0} mail={snap.sources.get('email', ('', '-'))[1]}")
        elif cmd == "digest":
            e = Engine()
            e.refresh(False)
            subject, body = digest(e.build_snapshot())
            print(subject + "\n\n" + body)
            if "--send" in argv:
                notify.send(notify.settings(e.cfg.get("notify.email", {})), subject, body)
                print("\n已发送。")
        elif cmd == "status":
            e = Engine()
            snap = e.refresh(False)
            for name, (state, msg) in sorted(snap.sources.items()):
                print(f"[{state}] {msg}")
        else:
            print(__doc__)
            return 2
    except notify.NotifyError as err:
        print(f"邮件：{err}")
        return 1
    except Exception as err:                      # cron: one line in the log, details in stockdesk.log
        log.exception("headless %s failed", cmd)
        print(f"{datetime.now():%Y-%m-%d %H:%M} failed: {type(err).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
