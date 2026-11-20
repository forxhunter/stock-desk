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

from . import congress, flow, insight, notify, paths
from .i18n import L
from .briefing import RANGE_TEXT
from .engine import Engine, Snapshot

log = logging.getLogger("stockdesk.headless")
MAIL_KINDS = ("bottoming", "plan_level", "flow", "index_level", "congress")


def _px(x: float) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.2f}"


def digest(snap: Snapshot, now: float | None = None) -> tuple[str, str]:
    """Plain-text daily summary: signals and levels only, no holdings, amounts or P&L."""
    day = datetime.fromtimestamp(now or time.time())
    out = [L(f"Stock Desk 每日信号汇总 · {day:%Y-%m-%d %H:%M}", f"Stock Desk daily signal digest · {day:%Y-%m-%d %H:%M}"), ""]

    market = insight.digest_lines(snap.indices, snap.vix)
    if market:
        out += [L("【大盘关键位】（指数相对均线、支撑区等固定规则价位的位置）",
                  "[Market key levels] (where the indices stand against averages, support zones and other rule-based levels)")]
        out += market + [""]

    out.append(L("【量化信号】", "[Quant signals]"))
    for v in snap.tickers:
        a = v.analysis
        if a is None or not a.ok or math.isnan(v.price):
            out.append(L(f"  {v.symbol}：没有取到行情", f"  {v.symbol}: no quote available"))
            continue
        line = L(f"  {v.symbol}　{_px(v.price)} {v.currency}　今日 {v.day_pct:+.2f}%　综合信号：{a.stance}（{a.composite:+.2f}）",
                 f"  {v.symbol}  {_px(v.price)} {v.currency}  today {v.day_pct:+.2f}%  composite signal: {a.stance} ({a.composite:+.2f})")
        rg = a.range
        if rg is not None and rg.status not in ("none", "short"):
            line += L(f"　震荡区间：{RANGE_TEXT.get(rg.status, rg.label)}（支撑 {_px(rg.support)} / 阻力 {_px(rg.resistance)}）",
                      f"  trading range: {RANGE_TEXT.get(rg.status, rg.label)} (support {_px(rg.support)} / resistance {_px(rg.resistance)})")
        out.append(line)

    if snap.bottoms:
        out += ["", L("【筑底检查】（价格 6 项 + 成交量 3 项，达到门槛才算信号）", "[Bottoming check] (6 price conditions + 3 volume conditions; a signal needs the threshold met)")]
        for sym, st in snap.bottoms.items():
            mark = (L("★ 已达到门槛", "★ threshold met") if st.triggered else L("未达到门槛", "threshold not met") if st.eligible
                    else L("跌幅不够深，不适用", "decline not deep enough, not applicable"))
            out.append(L(f"  {sym}：{st.summary}，距 52 周高点 {st.drawdown_pct:.1f}%，{mark}",
                         f"  {sym}: {st.summary}, {st.drawdown_pct:.1f}% below the 52-week high, {mark}"))
            if st.eligible:
                out.append(L("    成立：", "    Met: ") + (L("、", ", ").join(st.passed) or L("无", "none")))

    if snap.plans:
        out += ["", L("【你的计划】（价位是你自己写的）", "[Your plans] (the levels are ones you wrote yourself)")]
        for p in snap.plans:
            who = L(f"{p.symbol}（按 {p.follow} 的价格）", f"{p.symbol} (priced off {p.follow})") if p.follow else p.symbol
            unit = f" {p.level_ccy}" if p.level_ccy else ""
            bits = [L(f"现价 {_px(p.check_price)}{unit}", f"price {_px(p.check_price)}{unit}")]
            if p.next_buy is not None:
                bits.append(L(f"下一档买入价 {_px(p.next_buy)}", f"next buy level {_px(p.next_buy)}"))
            if p.next_level is not None:
                bits.append(L(f"下一档减仓价 {_px(p.next_level)}", f"next trim level {_px(p.next_level)}"))
            if p.target_price is not None:
                bits.append(L(f"目标价 {_px(p.target_price)}", f"target {_px(p.target_price)}"))
            if p.exit_below is not None:
                bits.append(L(f"底线 {_px(p.exit_below)}", f"floor {_px(p.exit_below)}"))
            if p.days_left:
                bits.append(L(f"还剩 {p.days_left} 天", f"{p.days_left} days left"))
            out.append(f"  {who}" + L("：", ": ") + L("，", ", ").join(bits))
            for _, _, title, _ in p.events:
                out.append(f"    ● {title}")
            if p.review_due:
                out.append(L("    ● 到了你定的复盘日", "    ● Your review date has arrived"))

    rep = snap.flow
    if rep is not None and rep.ts:
        hot = [i for i in rep.items if i.severity != "info"]
        out += ["", L(f"【异动扫描】（{datetime.fromtimestamp(rep.ts):%H:%M} 的一轮，共 {len(hot)} 条达到中级以上）",
                      f"[Unusual-activity scan] (the {datetime.fromtimestamp(rep.ts):%H:%M} run, {len(hot)} at medium level or above)")]
        for i in hot[:8]:
            tag = f"[{flow.TIER_LABEL[i.tier]}{' ' + ','.join(i.tickers) if i.tickers else ''}]"
            out.append(L(f"  {tag} {i.title}：{i.headline}", f"  {tag} {i.title}: {i.headline}"))
        if not hot:
            out.append(L("  没有达到门槛的异动。", "  No unusual activity reached the threshold."))
        for n in rep.notes:
            k, _, text = n.partition("|")
            if k == "futures":
                out.append(f"  · {text}")

    cg = congress.digest_lines(snap.congress)
    if cg:
        out += ["", L("【议员交易】（近两天披露；只列评级偏准以上的议员，或涉及你自选的）",
                      "[Congress trades] (disclosed in the last two days; only members rated fairly accurate or better, or trades in your watchlist)")] + cg

    today = [a for a in snap.alerts if a.kind in MAIL_KINDS and not a.demo and a.created >= (now or time.time()) - 86400]
    if today:
        out += ["", L("【近 24 小时的提醒】", "[Alerts in the last 24 hours]")] + [f"  · {a.title}" for a in today[:12]]

    bad = [msg for state, msg in snap.sources.values() if state == "error"]
    if bad:
        out += ["", L("【数据源问题】", "[Data source problems]")] + [f"  · {m}" for m in bad]
    return L(f"Stock Desk 每日信号 {day:%m-%d}", f"Stock Desk daily signals {day:%m-%d}"), "\n".join(out)


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
    if e.congress_due():
        rep = e.scan_congress()
        for _ in range(3):                    # no window to keep responsive here: read a backlog faster
            if not rep.backlog:
                break
            rep = e.scan_congress(force=True)
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
                print(L("\n已发送。", "\nSent."))
        elif cmd == "status":
            e = Engine()
            snap = e.refresh(False)
            for name, (state, msg) in sorted(snap.sources.items()):
                print(f"[{state}] {msg}")
        else:
            print(__doc__)
            return 2
    except notify.NotifyError as err:
        print(L(f"邮件：{err}", f"E-mail: {err}"))
        return 1
    except Exception as err:                      # cron: one line in the log, details in stockdesk.log
        log.exception("headless %s failed", cmd)
        print(f"{datetime.now():%Y-%m-%d %H:%M} failed: {type(err).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
