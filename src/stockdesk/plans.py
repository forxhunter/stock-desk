"""The user's own written plans for a position (data/plans.yaml) and their daily status.

The numbers in a plan are the user's decisions. The program only tracks them: how far along the
plan is, whether today's price is at one of the levels the user wrote down, and whether the user's
own rules (for example "no adding") were kept. It never fills in or changes a level by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import paths
from .i18n import L

PLAN_PATH_NAME = "plans.yaml"

TEMPLATE = L("""# 我的计划（本文件不进 git）。数字由你自己填，程序只负责每天对照、提醒，不会替你做决定，也不会下单。
# 留空（null / []）的项目，简报里会显示“未填写”。改完保存，下次刷新生效。
plans:
  {symbol}:
    start: {start}            # 计划开始日
    end: {end}              # 计划结束日（半年）
    start_weight_pct: {weight}    # 开始时占组合的比例（程序记录的，可改）
    target_weight_pct: null     # 你想在结束日前把它降到占组合的多少 %
    exit_below: null            # 你的底线价：收盘跌破它，就按你事先想好的方式处理（{currency}）
    reduce_above: []            # 你自己定的分批减仓价位，例如 [900, 1000, 1100]（{currency}）
    no_add: true                # 计划期间不再加仓
    review_days: 30             # 每隔多少天复查一次计划
    notes: ""                   # 写给自己的话：为什么持有、什么情况下认错
""", """# My plans (this file is not tracked by git). You fill in the numbers; the program only compares and reminds you each day. It makes no decisions for you and places no orders.
# Items left empty (null / []) show as "not filled in" in the briefing. Save after editing; it takes effect on the next refresh.
plans:
  {symbol}:
    start: {start}            # plan start date
    end: {end}              # plan end date (six months)
    start_weight_pct: {weight}    # share of the portfolio at the start (recorded by the program, you can change it)
    target_weight_pct: null     # what % of the portfolio you want it down to by the end date
    exit_below: null            # your bottom-line price: if the close falls below it, handle it the way you decided in advance ({currency})
    reduce_above: []            # your own staged trim levels, e.g. [900, 1000, 1100] ({currency})
    no_add: true                # no adding during the plan period
    review_days: 30             # review the plan every this many days
    notes: ""                   # a note to yourself: why you hold it, when you would admit a mistake
""")


def plan_path() -> Path:
    return paths.DATA_DIR / PLAN_PATH_NAME


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def _num(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def load(path: Path | None = None) -> dict[str, dict]:
    path = path or plan_path()
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return {}
    plans = raw.get("plans") or {}
    return {str(k).upper(): v for k, v in plans.items() if isinstance(v, dict)}


def ensure_template(symbol: str, weight_pct: float, currency: str = "USD", months: int = 6,
                    path: Path | None = None, today: date | None = None) -> bool:
    """Create plans.yaml with an empty plan for ``symbol`` if the file does not exist yet."""
    path = path or plan_path()
    if path.exists():
        return False
    today = today or date.today()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.format(symbol=symbol, start=today.isoformat(),
                                    end=(today + timedelta(days=round(30.44 * months))).isoformat(),
                                    weight=f"{weight_pct:.1f}", currency=currency), encoding="utf-8")
    return True


def fmt(x: float | None) -> str:
    """A price level for display: thousands separators for large quotes (e.g. KRW), plain otherwise."""
    if x is None or x != x:
        return "—"
    if abs(x) >= 10000:
        return f"{x:,.0f}"
    if abs(x) >= 1000:
        return f"{x:,.2f}".rstrip("0").rstrip(".")
    return f"{x:g}"


def fmt_px(x: float) -> str:
    if x != x:
        return "—"
    return f"{x:,.0f}" if abs(x) >= 10000 else f"{x:,.2f}"


@dataclass
class PlanStatus:
    symbol: str
    start: date | None = None
    end: date | None = None
    days_total: int = 0
    days_done: int = 0
    days_left: int = 0
    next_review: date | None = None
    review_due: bool = False
    weight_pct: float = 0.0
    start_weight_pct: float | None = None
    target_weight_pct: float | None = None
    path_weight_pct: float | None = None      # where a straight line from start to target is today
    exit_below: float | None = None
    reduce_levels: list[float] = field(default_factory=list)
    levels_reached: list[float] = field(default_factory=list)
    next_level: float | None = None
    buy_levels: list[float] = field(default_factory=list)       # the user's own staged-entry prices
    buy_reached: list[float] = field(default_factory=list)
    next_buy: float | None = None
    target_price: float | None = None
    max_weight_pct: float | None = None
    events: list[tuple[str, float, str, str]] = field(default_factory=list)   # (key, level, title, text)
    price: float = float("nan")               # price of the planned symbol itself
    held: bool = True
    notes: str = ""
    missing: list[str] = field(default_factory=list)
    steps: list[tuple[float, float]] = field(default_factory=list)          # the user's staged trims on the way down:
    #                                                                         (price, weight to cut to), highest price first
    steps_reached: list[tuple[float, float]] = field(default_factory=list)
    flags: list[tuple[str, str]] = field(default_factory=list)      # (level, text)
    building: bool = False
    # a plan can be written in the prices of another stock (e.g. HY9H planned on 000660.KS):
    follow: str = ""                          # the symbol whose price the levels refer to ("" = the symbol itself)
    follow_name: str = ""
    level_ccy: str = ""                       # currency the levels are written in
    check_price: float = float("nan")         # the price the levels are compared with
    to_local: float | None = None             # multiply a level by this for the planned symbol's own currency
    local_ccy: str = ""

    def chart_levels(self, for_symbol: str) -> dict:
        """The plan's price levels in the price units of ``for_symbol``'s chart."""
        k, approx = 1.0, False
        if self.follow and for_symbol != self.follow:
            if not self.to_local:
                return {}
            k, approx = self.to_local, True
        return {"exit": self.exit_below * k if self.exit_below else None,
                "reduce": [x * k for x in self.reduce_levels], "buy": [x * k for x in self.buy_levels],
                "target": self.target_price * k if self.target_price else None, "approx": approx}


def status(symbol: str, plan: dict, *, price: float, weight_pct: float, qty: float, trades: list,
           today: date | None = None, ccy: str = "", follow_price: float | None = None,
           follow_name: str = "", follow_ccy: str = "", to_local: float | None = None) -> PlanStatus:
    today = today or date.today()
    st = PlanStatus(symbol, _date(plan.get("start")), _date(plan.get("end")), weight_pct=weight_pct,
                    price=price, held=qty > 0, notes=str(plan.get("notes") or ""), local_ccy=ccy)
    st.start_weight_pct = _num(plan.get("start_weight_pct"))
    st.target_weight_pct = _num(plan.get("target_weight_pct"))
    st.exit_below = _num(plan.get("exit_below"))
    st.reduce_levels = sorted(x for x in (_num(v) for v in (plan.get("reduce_above") or [])) if x)
    st.buy_levels = sorted((x for x in (_num(v) for v in (plan.get("buy_below") or [])) if x), reverse=True)
    st.target_price = _num(plan.get("target_price"))
    for row in plan.get("steps_below") or []:
        try:
            lvl, w = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if lvl > 0:
            st.steps.append((lvl, w))
    st.steps.sort(reverse=True)
    st.max_weight_pct = _num(plan.get("max_weight_pct"))
    kind = str(plan.get("kind") or "")
    filled = bool(st.buy_levels or st.target_price or st.max_weight_pct)
    building = st.building = kind == "build" or (filled and kind != "reduce")
    if building and not filled:
        st.missing += [L("分批买入价位", "staged buy levels"), L("目标价", "target price"),
                       L("仓位上限", "position cap")]
    for key, label in (("target_weight_pct", L("目标占比", "target weight")),
                       ("exit_below", L("底线价", "bottom-line price")),
                       ("reduce_above", L("分批减仓价位", "staged trim levels"))):
        if not plan.get(key) and not building:
            st.missing.append(label)

    # which price the levels are compared with
    st.follow = str(plan.get("follow") or "").upper()
    if st.follow:
        st.follow_name, st.level_ccy, st.to_local = follow_name or st.follow, follow_ccy, to_local
        px = follow_price if follow_price is not None else float("nan")
        who = (L(f"{st.follow_name}（{st.follow}）", f"{st.follow_name} ({st.follow})")
               if st.follow_name != st.follow else st.follow)
        mine = L(f"你在 {symbol} 计划里", f"your {symbol} plan")
        if px != px:
            st.flags.append(("warn", L(f"没有取到 {who} 的行情，计划里的价位暂时无法检查。",
                                       f"No price data for {who}; the levels in the plan cannot be checked for now.")))
    else:
        st.level_ccy = ccy
        px, who, mine = price, symbol, L("你在计划里", "your plan")
    st.check_price = px
    unit = f" {st.level_ccy}" if st.follow and st.level_ccy else ""
    tag = L(f"{symbol}（按 {st.follow}）", f"{symbol} (by {st.follow})") if st.follow else symbol

    if st.start and st.end and st.end > st.start:
        st.days_total = (st.end - st.start).days
        st.days_done = min(max((today - st.start).days, 0), st.days_total)
        st.days_left = max((st.end - today).days, 0)
        every = int(_num(plan.get("review_days")) or 30)
        k = max((today - st.start).days, 0) // every
        st.review_due = k > 0 and (today - st.start).days % every == 0
        st.next_review = min(st.start + timedelta(days=(k + 1) * every), st.end)
        if st.target_weight_pct is not None and st.start_weight_pct is not None:
            f = st.days_done / st.days_total
            st.path_weight_pct = st.start_weight_pct + (st.target_weight_pct - st.start_weight_pct) * f
            if st.held and weight_pct > st.path_weight_pct + 2.0 and st.target_weight_pct < st.start_weight_pct:
                st.flags.append(("warn", L(f"当前占比 {weight_pct:.1f}%，高于按时间均匀推进到今天的 {st.path_weight_pct:.1f}%"
                                           f"（从 {st.start_weight_pct:g}% 到 {st.target_weight_pct:g}% 的直线）。",
                                           f"Current weight {weight_pct:.1f}% is above {st.path_weight_pct:.1f}%, "
                                           f"where an even schedule would put it today "
                                           f"(a straight line from {st.start_weight_pct:g}% to {st.target_weight_pct:g}%).")))
        if today > st.end:
            st.flags.append(("warn", L("计划已到结束日，请复盘并决定是否写新的计划。",
                                       "The plan has reached its end date; review it and decide whether to write a new one.")))
        elif st.review_due:
            st.flags.append(("info", L("今天是你设定的复查日。", "Today is a review day you set.")))
    else:
        st.missing.append(L("起止日期", "start and end dates"))
    if px == px:      # not NaN
        now = L(f"{who} 现价 {fmt_px(px)}{unit}", f"{who} price {fmt_px(px)}{unit}")
        for k, (lvl, w) in enumerate(st.steps, 1):
            if px < lvl:
                st.steps_reached.append((lvl, w))
                text = L(f"{now}，低于{mine}写下的第 {k} 步减仓价 {fmt(lvl)}：你写的是把占比降到 {w:g}%。",
                         f"{now}, below step {k} of the staged trim written in {mine}, {fmt(lvl)}: you wrote to cut the weight to {w:g}%.")
                st.flags.append(("high", text))
                st.events.append(("step", lvl, L(f"到达你的第 {k} 步减仓价：{tag}", f"Reached step {k} of your staged trim: {tag}"), text))
        if st.exit_below and px < st.exit_below and not st.steps:
            text = L(f"{now}，低于{mine}写下的底线价 {fmt(st.exit_below)}。",
                     f"{now}, below the bottom-line price of {fmt(st.exit_below)} written in {mine}.")
            st.flags.append(("high", text))
            st.events.append(("exit", st.exit_below,
                              L(f"跌破你的底线价：{tag}", f"Fell below your bottom-line price: {tag}"), text))
        st.levels_reached = [x for x in st.reduce_levels if px >= x]
        st.next_level = next((x for x in st.reduce_levels if px < x), None)
        if st.levels_reached and st.held:
            st.flags.append(("info", L(f"{now}，已到达{mine}写下的减仓价位 "
                                       f"{' / '.join(fmt(x) for x in st.levels_reached)}。",
                                       f"{now}, has reached the trim level written in {mine}: "
                                       f"{' / '.join(fmt(x) for x in st.levels_reached)}.")))
            for x in st.levels_reached:
                st.events.append(("reduce", x, L(f"到达你的减仓价位：{tag}", f"Reached your trim level: {tag}"),
                                  L(f"{now}，已到达{mine}写下的减仓价位 {fmt(x)}。",
                                    f"{now}, has reached the trim level of {fmt(x)} written in {mine}.")))
        st.buy_reached = [x for x in st.buy_levels if px <= x]
        st.next_buy = next((x for x in st.buy_levels if px > x), None)
        if st.buy_reached:
            st.flags.append(("info", L(f"{now}，已到达{mine}写下的买入价位 "
                                       f"{' / '.join(fmt(x) for x in st.buy_reached)}。",
                                       f"{now}, has reached the buy level written in {mine}: "
                                       f"{' / '.join(fmt(x) for x in st.buy_reached)}.")))
            for x in st.buy_reached:
                st.events.append(("buy", x, L(f"到达你的买入价位：{tag}", f"Reached your buy level: {tag}"),
                                  L(f"{now}，已到达{mine}写下的买入价位 {fmt(x)}。"
                                    "这只是提醒你的计划到了这一步，是否买、买多少由你决定。",
                                    f"{now}, has reached the buy level of {fmt(x)} written in {mine}. "
                                    "This only reminds you that your plan has reached this step; whether to buy "
                                    "and how much is up to you.")))
        if st.target_price and px >= st.target_price:
            text = L(f"{now}，已到达{mine}写下的目标价 {fmt(st.target_price)}。",
                     f"{now}, has reached the target price of {fmt(st.target_price)} written in {mine}.")
            st.flags.append(("info", text))
            st.events.append(("target", st.target_price,
                              L(f"到达你的目标价：{tag}", f"Reached your target price: {tag}"), text))
    if st.max_weight_pct and weight_pct > st.max_weight_pct:
        st.flags.append(("warn", L(f"当前占组合 {weight_pct:.1f}%，超过你给这只股票定的仓位上限 {st.max_weight_pct:g}%。",
                                   f"Currently {weight_pct:.1f}% of the portfolio, above the position cap of "
                                   f"{st.max_weight_pct:g}% you set for this stock.")))
    if plan.get("no_add", not building) and st.start:
        t0 = datetime.combine(st.start, datetime.min.time()).timestamp()
        buys = [t for t in trades if t.symbol == symbol and t.side == "BUY" and t.ts >= t0 and not t.demo]
        if buys:
            last = max(buys, key=lambda t: t.ts)
            st.flags.append(("high", L(f"计划写的是“期间不加仓”，但开始后有 {len(buys)} 笔买入"
                                       f"（最近一笔 {datetime.fromtimestamp(last.ts):%m-%d}，{last.qty:g} 股 @ {last.price:.2f}）。",
                                       f"The plan says 'no adding during the period', but there have been {len(buys)} "
                                       f"buys since it started (latest on {datetime.fromtimestamp(last.ts):%m-%d}, "
                                       f"{last.qty:g} shares @ {last.price:.2f}).")))
    return st


HEADER = L("# 我的计划（本文件不进 git）。数字由你自己决定，程序只负责每天对照、提醒，不会替你做决定，也不会下单。\n"
           "# 可以在这里改，也可以右键股票 → “编辑我的计划…” 在界面里改。\n",
           "# My plans (this file is not tracked by git). The numbers are your own decisions; the program only compares "
           "and reminds you each day. It makes no decisions for you and places no orders.\n"
           "# Edit here, or right-click a stock -> 'Edit my plan...' to edit in the interface.\n")


def save(symbol: str, fields: dict, path: Path | None = None, today: date | None = None) -> dict:
    """Create or update one plan with the values the user entered; other plans are left alone."""
    path = path or plan_path()
    all_plans = load(path)
    today = today or date.today()
    plan = all_plans.get(symbol) or {"start": today.isoformat(),
                                     "end": (today + timedelta(days=183)).isoformat(), "no_add": True,
                                     "review_days": 30}
    plan.update(fields)
    for k in ("start", "end"):
        d = _date(plan.get(k))
        plan[k] = d.isoformat() if d else None
    all_plans[symbol] = plan
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + yaml.safe_dump({"plans": all_plans}, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return plan


def _zone_text(z) -> str:
    why = L("一年低点", "1-year low") if z.year_low and z.touches == 1 else L(f"{z.touches} 个转折点", f"{z.touches} turning points")
    return L(f"{fmt_px(z.level)}（{fmt_px(z.low)}–{fmt_px(z.high)}，{why}）",
             f"{fmt_px(z.level)} ({fmt_px(z.low)}–{fmt_px(z.high)}, {why})")


def reference_levels(v, default_cap: float = 25.0, ref=None, market_only: bool = False) -> list[tuple[str, str]]:
    """Levels the program already computes by fixed, generic rules, shown next to the plan editor.

    They are context for the user's own decision, not suggested plan values."""
    out: list[tuple[str, str]] = []
    px = v.price
    if px == px:
        out.append((L("现价", "Current price"), fmt_px(px)))
    if v.qty and v.avg_cost and not market_only:
        out.append((L("你的平均成本", "Your average cost"), fmt_px(v.avg_cost)))
    if not market_only:
        out.append((L("当前占组合", "Current weight in portfolio"),
                    L(f"{v.weight * 100:.1f}%（程序默认的单只上限是 {default_cap:g}%）",
                      f"{v.weight * 100:.1f}% (the program's default per-stock cap is {default_cap:g}%)")))
    a = v.analysis
    if a is not None and a.ok:
        i = a.ind
        for key, label in (("sma50", L("50 日均线", "50-day average")), ("sma200", L("200 日均线", "200-day average"))):
            if i.get(key) == i.get(key) and i.get(key) is not None:
                out.append((label, fmt_px(i[key])))
        out.append((L("52 周最低 / 最高", "52-week low / high"), f"{fmt_px(i['low_252'])} / {fmt_px(i['high_252'])}"))
        out.append((L("ATR(14)，日均波幅", "ATR(14), average daily range"),
                    L(f"{fmt_px(i['atr'])}（{i['atr_pct'] * 100:.1f}%）",
                      f"{fmt_px(i['atr'])} ({i['atr_pct'] * 100:.1f}%)")))
        rg = a.range
        if rg is not None and rg.status not in ("short", "none"):
            out.append((L(f"最近的震荡区间（{rg.label}）", f"Latest trading range ({rg.label})"),
                        L(f"支撑 {fmt_px(rg.support)} / 阻力 {fmt_px(rg.resistance)}",
                          f"support {fmt_px(rg.support)} / resistance {fmt_px(rg.resistance)}")))
    try:
        from .quant.levels import support_zones
        zones = support_zones(v.bars, None if px != px else px) if getattr(v, "bars", None) is not None else []
    except Exception:
        zones = []
    if zones:
        out.append((L("下方支撑区（一年内波段高低点聚类）", "Support zones below (clusters of swing highs / lows over the past year)"),
                    L("；", "; ").join(_zone_text(z) for z in zones[:4])))
    if ref is not None and ref.implied == ref.implied:
        out.append((L(f"{ref.name} 折算价", f"{ref.name} converted price"),
                    L(f"{ref.implied:,.2f}（{ref.ref_asof:%m-%d} 收盘）",
                      f"{ref.implied:,.2f} (close of {ref.ref_asof:%m-%d})")))
    return out
