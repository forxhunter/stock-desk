"""The translucent, frameless desktop widget."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QHBoxLayout, QLabel, QMenu, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..engine import Snapshot, TickerView
from . import theme as T
from . import win11

WIDTH = 384
ROW_H = 72
MAX_ROWS = 8
SNAP_PX = 28          # dropping the widget this close to a screen edge docks it there
PEEK_PX = 10          # how much stays visible while it is tucked away
GRIP_PX = 10          # bottom strip that resizes the list when dragged


class IconButton(QAbstractButton):
    """Flat glyph button (Segoe Fluent Icons) with an optional count badge."""

    def __init__(self, glyph: str, tip: str, parent=None):
        super().__init__(parent)
        self.glyph, self.badge, self.spin = glyph, 0, False
        self.setToolTip(tip)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)

    def set_glyph(self, glyph: str) -> None:
        self.glyph = glyph
        self.update()

    def set_badge(self, n: int) -> None:
        self.badge = n
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(T.qc("#ffffff", 28))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        p.setFont(T.font(10.5, family=T.ICON_FONT))
        p.setPen(T.qc(T.BLUE if self.spin else T.TEXT, 235))
        p.drawText(self.rect(), Qt.AlignCenter, self.glyph)
        if self.badge:
            r = QRectF(self.width() - 15, 1, 14, 14)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.RED))
            p.drawEllipse(r)
            p.setPen(QColor("white"))
            p.setFont(T.font(6.5, True, T.NUM_FONT))
            p.drawText(r, Qt.AlignCenter, str(self.badge) if self.badge < 10 else "9+")

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()


def _draw_spark(p: QPainter, rect: QRectF, pts: list[float], base: float, color: str) -> None:
    if len(pts) < 2:
        return
    lo, hi = min(pts + ([base] if not math.isnan(base) else [])), max(pts + ([base] if not math.isnan(base) else []))
    span = (hi - lo) or 1.0
    n = len(pts)
    xy = [QPointF(rect.left() + rect.width() * i / (n - 1), rect.bottom() - rect.height() * (v - lo) / span)
          for i, v in enumerate(pts)]
    if not math.isnan(base):
        y = rect.bottom() - rect.height() * (base - lo) / span
        p.setPen(QPen(T.qc("#ffffff", 50), 1, Qt.DotLine))
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
    path = QPainterPath(xy[0])
    for q in xy[1:]:
        path.lineTo(q)
    fill = QPainterPath(path)
    fill.lineTo(rect.right(), rect.bottom())
    fill.lineTo(rect.left(), rect.bottom())
    fill.closeSubpath()
    g = QLinearGradient(0, rect.top(), 0, rect.bottom())
    g.setColorAt(0, T.qc(color, 70))
    g.setColorAt(1, T.qc(color, 0))
    p.fillPath(fill, QBrush(g))
    p.setPen(QPen(QColor(color), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(xy[-1], 2.2, 2.2)


class TickerRow(QWidget):
    clicked = Signal(str)
    menu_requested = Signal(str, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.v: TickerView | None = None
        self.pal = T.Palette()
        self.setFixedHeight(ROW_H)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._press: QPoint | None = None

    def set_view(self, v: TickerView, pal: T.Palette) -> None:
        self.v, self.pal = v, pal
        a = v.analysis
        tip = [f"{v.symbol}　{T.price(v.price)}　{T.pct(v.day_pct)}"]
        if a and a.ok:
            tip.append(a.headline)
        if v.qty and T.PRIVATE:
            tip.append("隐私模式：持仓数据已隐藏")
        elif v.qty:
            src = "你设定的" if v.levels_src == "user" else "按 ATR 自动估算的"
            tip.append(f"进度条：{src}止损 {T.price(v.stop)} → 目标 {T.price(v.target)}；来源 {'手动填写' if v.source in ('', 'manual') else v.source}")
        else:
            tip.append("进度条：现价在 52 周最低 / 最高之间的位置")
        rg = a.range if a and a.ok else None
        if rg is not None and rg.status in ("buy", "sell", "breakout_up", "breakdown"):
            tip.append(f"震荡区间：{rg.label}（支撑 {rg.support:.2f} / 阻力 {rg.resistance:.2f}）")
        if v.news:
            tip.append(f"SemiAnalysis：{v.news[0].title}")
        tip.append("点击查看图表与依据；右键更多操作")
        self.setToolTip("\n".join(tip))
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press is not None and self.v is not None:
            if (e.position().toPoint() - self._press).manhattanLength() < 6:
                self.clicked.emit(self.v.symbol)
        self._press = None

    def contextMenuEvent(self, e):
        if self.v is not None:
            self.menu_requested.emit(self.v.symbol, e.globalPos())

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def paintEvent(self, _):
        v = self.v
        if v is None:
            return
        held = 0 if T.PRIVATE else v.qty      # privacy mode draws the row as a plain quote
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, pad = self.width(), 12
        if self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(T.qc("#ffffff", 18))
            p.drawRoundedRect(QRectF(2, 1, w - 4, ROW_H - 2), 8, 8)
        day_col = self.pal.sign(v.day_chg)

        # line 1: symbol, stance dot, research tag
        f_sym = T.font(11, True, T.NUM_FONT)
        p.setFont(f_sym)
        p.setPen(QColor(T.TEXT))
        p.drawText(QPointF(pad, 23), v.symbol)
        x = pad + QFontMetricsF(f_sym).horizontalAdvance(v.symbol) + 8
        a = v.analysis
        if a and a.ok:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self.pal.stance(a.stance)))
            p.drawEllipse(QPointF(x + 3, 18), 3.5, 3.5)
            x += 14
        if v.news:
            p.setFont(T.font(7))
            p.setPen(QColor(T.BLUE))
            p.drawText(QPointF(x, 22), "研")

        # line 2: position
        p.setFont(T.font(8))
        p.setPen(QColor(T.SUB))
        sub = f"{held:g} 股 · 成本 {T.price(v.avg_cost)}" if held else (
            "持仓已隐藏" if getattr(v, "qty") else "未持仓" + (f" · {v.note}" if v.note else ""))
        p.drawText(QRectF(pad, 28, w * 0.42 - pad, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(p.font()).elidedText(sub, Qt.ElideRight, w * 0.42 - pad))

        # sparkline
        _draw_spark(p, QRectF(w * 0.44, 9, 84, 30), v.spark, v.prev_close, day_col)

        # price + day change pill
        p.setFont(T.font(11, True, T.NUM_FONT))
        p.setPen(QColor(T.TEXT))
        p.drawText(QRectF(w - pad - 110, 7, 110, 20), Qt.AlignRight | Qt.AlignVCenter, T.price(v.price))
        f_pill = T.font(8, True, T.NUM_FONT)
        txt = T.pct(v.day_pct)
        tw = QFontMetricsF(f_pill).horizontalAdvance(txt) + 12
        pill = QRectF(w - pad - tw, 28, tw, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(T.qc(day_col, 46))
        p.drawRoundedRect(pill, 8, 8)
        p.setFont(f_pill)
        p.setPen(QColor(day_col))
        p.drawText(pill, Qt.AlignCenter, txt)

        # line 3: progress bar (stop -> target if held, else 52-week range) + P&L / stance
        y = 57.0
        right_w = 122
        f_s = T.font(7.5, family=T.NUM_FONT)
        p.setFont(f_s)
        if held and v.stop and v.target and v.progress is not None:
            lo_txt, hi_txt, frac = f"止 {T.price(v.stop)}", f"目 {T.price(v.target)}", v.progress
            lo_col, hi_col, fill_col = self.pal.down, self.pal.up, self.pal.sign(v.upl)
            tick = (v.avg_cost - v.stop) / (v.target - v.stop) if v.target > v.stop else None
        elif not math.isnan(v.low52) and v.high52 > v.low52 and not math.isnan(v.price):
            lo_txt, hi_txt = T.price(v.low52), T.price(v.high52)
            frac, lo_col, hi_col, fill_col, tick = (v.price - v.low52) / (v.high52 - v.low52), T.DIM, T.DIM, T.BLUE, None
        else:
            lo_txt = hi_txt = ""
            frac, lo_col, hi_col, fill_col, tick = None, T.DIM, T.DIM, T.DIM, None
        fm = QFontMetricsF(f_s)
        lw, hw = fm.horizontalAdvance(lo_txt), fm.horizontalAdvance(hi_txt)
        x0, x1 = pad + lw + 6, w - pad - right_w - hw - 6
        if frac is not None and x1 - x0 > 30:
            p.setPen(T.qc(lo_col, 220))
            p.drawText(QRectF(pad, y - 8, lw, 16), Qt.AlignLeft | Qt.AlignVCenter, lo_txt)
            p.setPen(T.qc(hi_col, 220))
            p.drawText(QRectF(x1 + 6, y - 8, hw, 16), Qt.AlignLeft | Qt.AlignVCenter, hi_txt)
            p.setPen(Qt.NoPen)
            p.setBrush(T.qc("#ffffff", 34))
            p.drawRoundedRect(QRectF(x0, y - 2, x1 - x0, 4), 2, 2)
            fc = max(0.0, min(1.0, frac))
            mx = x0 + (x1 - x0) * fc
            p.setBrush(T.qc(fill_col, 190))
            p.drawRoundedRect(QRectF(x0, y - 2, max(mx - x0, 2), 4), 2, 2)
            if tick is not None and 0 < tick < 1:
                tx = x0 + (x1 - x0) * tick
                p.setPen(QPen(T.qc("#ffffff", 170), 1))
                p.drawLine(QPointF(tx, y - 5), QPointF(tx, y + 5))
            hollow = held and v.levels_src == "auto"
            p.setPen(QPen(QColor(fill_col), 1.6))
            p.setBrush(T.qc(T.BG) if hollow else QColor("#ffffff"))
            p.drawEllipse(QPointF(mx, y), 4, 4)
        p.setFont(T.font(8, True, T.NUM_FONT) if held else T.font(8))
        if held:
            p.setPen(QColor(self.pal.sign(v.upl)))
            txt = f"{T.money(v.upl, True, 0)}  {T.pct(v.upl_pct, 1)}"
        elif a and a.ok:
            p.setPen(QColor(self.pal.stance(a.stance)))
            txt = f"{a.stance} {a.composite:+.2f}"
        else:
            p.setPen(QColor(T.DIM))
            txt = "等待数据"
        p.drawText(QRectF(w - pad - right_w, y - 9, right_w, 18), Qt.AlignRight | Qt.AlignVCenter, txt)

        p.setPen(QPen(T.qc("#ffffff", 16), 1))
        p.drawLine(QPointF(pad, ROW_H - 0.5), QPointF(w - pad, ROW_H - 0.5))


class SummaryCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.snap: Snapshot | None = None
        self.pal = T.Palette()
        self.setFixedHeight(84)

    def set_snapshot(self, snap: Snapshot, pal: T.Palette) -> None:
        self.snap, self.pal = snap, pal
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(8, 2, -8, -6)
        p.setPen(QPen(T.qc("#ffffff", 22), 1))
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, T.qc("#ffffff", 26))
        g.setColorAt(1, T.qc("#ffffff", 10))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, 10, 10)
        s = self.snap
        x, right = r.left() + 14, r.right() - 14
        p.setFont(T.font(8))
        p.setPen(QColor(T.SUB))
        p.drawText(QPointF(x, r.top() + 21), "持仓市值")
        p.setFont(T.font(17, True, T.NUM_FONT))
        p.setPen(QColor(T.TEXT))
        p.drawText(QPointF(x, r.top() + 50), T.money(s.total_value) if s and s.total_value else "—")
        if not s:
            return
        held = sum(1 for t in s.tickers if t.qty)
        p.setFont(T.font(7.5))
        warn = s.conc.warnings[0] if s.conc and s.conc.warnings and not T.PRIVATE else ""
        p.setPen(QColor(T.AMBER if warn else T.DIM))
        txt = ("⚠ " + warn) if warn else ("隐私模式：金额与持仓已隐藏" if T.PRIVATE else
                                              f"持仓 {held} 只 · 自选 {len(s.tickers)} 只")
        p.drawText(QRectF(x, r.top() + 55, r.width() - 28, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetricsF(p.font()).elidedText(txt, Qt.ElideRight, r.width() - 28))
        for i, (label, val, extra) in enumerate((
                ("今日", s.day_pnl, T.secret(T.pct(s.day_pct))),
                ("浮盈", s.total_upl, T.secret(T.pct(s.total_upl / s.total_cost * 100 if s.total_cost else 0.0, 1))))):
            y = r.top() + 12 + i * 20
            col = T.SUB if T.PRIVATE else self.pal.sign(val)
            f = T.font(9, True, T.NUM_FONT)
            p.setFont(f)
            p.setPen(QColor(col if s.total_value else T.DIM))
            txt = f"{T.money(val, True)}  {extra}" if s.total_value else "—"
            p.drawText(QRectF(right - 190, y, 190, 18), Qt.AlignRight | Qt.AlignVCenter, txt)
            tw = QFontMetricsF(f).horizontalAdvance(txt)
            p.setFont(T.font(8))
            p.setPen(QColor(T.SUB))
            p.drawText(QRectF(right - tw - 40, y, 34, 18), Qt.AlignRight | Qt.AlignVCenter, label)


class DeskWidget(QWidget):
    refresh_requested = Signal()
    add_requested = Signal()
    detail_requested = Signal(str)
    hub_requested = Signal(str)
    privacy_toggled = Signal()
    row_menu_requested = Signal(str, QPoint)
    main_menu_requested = Signal(QPoint)
    state_changed = Signal()

    def __init__(self, opacity: float = 0.86, on_top: bool = True, pal: T.Palette | None = None):
        super().__init__()
        self.pal = pal or T.Palette()
        self.opacity = max(0.3, min(1.0, float(opacity)))
        self.collapsed = False
        self.snap: Snapshot | None = None
        self._rows: list[TickerRow] = []
        self._ctx: list[tuple[str, str]] = []
        self._ctx_i = 0
        self._drag: QPoint | None = None
        self.list_h: int | None = None        # height of the ticker list chosen by the user (None = automatic)
        self._resize: tuple[int, int] | None = None     # (mouse y, list height) when a resize drag started
        self.setMouseTracking(True)
        self.dock: str | None = None          # "left" | "right" | "top" when docked to a screen edge
        self.tucked = False
        self._home: QPoint | None = None      # position while fully shown (what gets saved)
        self._away_since = 0.0
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._dock_timer = QTimer(self)
        self._dock_timer.setInterval(200)
        self._dock_timer.timeout.connect(self._dock_tick)
        self._dock_timer.start()
        self.setWindowTitle("Stock Desk")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._apply_flags(on_top)
        self.setFixedWidth(WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 8)
        root.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(10, 2, 2, 2)
        head.setSpacing(2)
        title = QLabel("Stock Desk")
        title.setFont(T.font(10.5, True, T.NUM_FONT))
        title.setStyleSheet(f"color:{T.TEXT}; background:transparent;")
        self.badge = QLabel("演示")
        self.badge.setFont(T.font(7.5))
        self._style_badge(T.AMBER)
        self.btn_refresh = IconButton(T.ICON["refresh"], "立即刷新")
        self.btn_add = IconButton(T.ICON["add"], "添加自选股")
        self.btn_eye = IconButton(T.ICON["eye_off" if T.PRIVATE else "eye"], "隐藏 / 显示关键数据（持仓、金额、盈亏）")
        self.btn_bell = IconButton(T.ICON["bell"], "行为提醒")
        self.btn_hub = IconButton(T.ICON["hub"], "分析中心：组合优化 / 行为提醒 / 交易前检查 / 资讯 / 数据源")
        self.btn_fold = IconButton(T.ICON["up"], "折叠 / 展开")
        self.btn_close = IconButton(T.ICON["close"], "隐藏（在 Pipeline Hub 里再点一次 Stock Desk 即可显示；右键小组件可退出）")
        head.addWidget(title)
        head.addSpacing(6)
        head.addWidget(self.badge)
        head.addStretch(1)
        for b in (self.btn_eye, self.btn_refresh, self.btn_add, self.btn_bell, self.btn_hub, self.btn_fold, self.btn_close):
            head.addWidget(b)
        root.addLayout(head)

        self.summary = SummaryCard()
        root.addWidget(self.summary)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.rows_host = QWidget()
        self.rows_host.setAttribute(Qt.WA_TranslucentBackground)
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.rows_host)
        root.addWidget(self.scroll)

        self.ctx = QLabel("")
        self.ctx.setFont(T.font(8))
        self.ctx.setStyleSheet(f"color:{T.TEXT}; background:rgba(255,255,255,14); border-radius:6px; padding:5px 9px;")
        self.ctx.setCursor(Qt.PointingHandCursor)
        self.ctx.setToolTip("预测市场概率与研究文章（背景信息）。点击查看全部。")
        self.ctx.mousePressEvent = lambda e: self.hub_requested.emit("research")
        self.ctx.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.ctx)
        self.status = QLabel("正在加载行情…")
        self.status.setFont(T.font(7.5))
        self.status.setStyleSheet(f"color:{T.DIM}; background:transparent; padding:2px 10px 0 10px;")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        root.addWidget(self.status)

        self.btn_refresh.clicked.connect(self.refresh_requested)
        self.btn_add.clicked.connect(self.add_requested)
        self.btn_bell.clicked.connect(lambda: self.hub_requested.emit("alerts"))
        self.btn_hub.clicked.connect(lambda: self.hub_requested.emit("briefing"))
        self.btn_eye.clicked.connect(self.privacy_toggled)
        self.btn_fold.clicked.connect(self.toggle_collapsed)
        self.btn_close.clicked.connect(self.hide)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.state_changed)
        self._ctx_timer = QTimer(self)
        self._ctx_timer.setInterval(6000)
        self._ctx_timer.timeout.connect(self._next_ctx)
        self._ctx_timer.start()
        self._relayout()

    # ---- window behaviour
    def _apply_flags(self, on_top: bool) -> None:
        flags = Qt.FramelessWindowHint | Qt.Tool
        if on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.on_top = on_top
        self.setWindowFlags(flags)

    def set_on_top(self, on_top: bool) -> None:
        visible = self.isVisible()
        self._apply_flags(on_top)
        if visible:
            self.show()

    def set_opacity(self, opacity: float) -> None:
        self.opacity = max(0.3, min(1.0, float(opacity)))
        self.update()

    def _style_badge(self, color: str) -> None:
        c = QColor(color)
        self.badge.setStyleSheet(f"color:{color}; background:rgba({c.red()},{c.green()},{c.blue()},45);"
                                 f"border-radius:7px; padding:1px 7px;")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        a = int(255 * self.opacity)
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0, QColor(30, 34, 46, a))
        g.setColorAt(1, QColor(14, 16, 22, a))
        p.setBrush(QBrush(g))
        p.setPen(QPen(T.qc("#ffffff", 46), 1))
        p.drawRoundedRect(r, 14, 14)

    def _on_grip(self, e) -> bool:
        return not self.collapsed and bool(self._rows) and e.position().y() >= self.height() - GRIP_PX

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._on_grip(e):            # drag the bottom edge to change the height of the list
            self._resize = (e.globalPosition().toPoint().y(), self.scroll.height())
        else:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._resize is not None and e.buttons() & Qt.LeftButton:
            y0, h0 = self._resize
            self.set_list_height(h0 + e.globalPosition().toPoint().y() - y0)
        elif self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
        else:
            self.setCursor(Qt.SizeVerCursor if self._on_grip(e) else Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        if self._on_grip(e):            # double-click the bottom edge: back to automatic height
            self.list_h = None
            self._relayout()
            self.state_changed.emit()

    def leaveEvent(self, e):
        self.setCursor(Qt.ArrowCursor)

    def set_list_height(self, h: int | None) -> None:
        """User-chosen height of the ticker list; the list scrolls when it is shorter than its rows."""
        if h is not None:
            room = self.screen().availableGeometry().height() - (self.height() - self.scroll.height()) - 16
            full = max(len(self._rows), 1) * ROW_H
            h = int(max(ROW_H, min(h, full, room)))
        self.list_h = h
        self._relayout()

    def mouseReleaseEvent(self, e):
        if self._resize is not None:
            self._resize = None
            self.state_changed.emit()
            return
        was_drag, self._drag = self._drag is not None, None
        if was_drag and e.button() == Qt.LeftButton:
            self._snap_to_edge()

    # ---- edge docking: drop it at the left / right / top screen edge and it tucks itself away
    def _edge_free(self, side: str) -> bool:
        """False when another monitor continues past that edge (tucking would spill onto it)."""
        g, c = self.screen().geometry(), self.frameGeometry().center()
        probe = {"left": QPoint(g.left() - 40, c.y()), "right": QPoint(g.right() + 40, c.y()),
                 "top": QPoint(c.x(), g.top() - 40)}[side]
        return QGuiApplication.screenAt(probe) is None

    def _snap_to_edge(self) -> None:
        avail, g = self.screen().availableGeometry(), self.frameGeometry()
        side = "left" if g.left() - avail.left() <= SNAP_PX else             "right" if avail.right() - g.right() <= SNAP_PX else             "top" if g.top() - avail.top() <= SNAP_PX else None
        self.dock = side if side and self._edge_free(side) else None
        self.tucked = False
        if self.dock == "left":
            self.move(avail.left(), self.y())
        elif self.dock == "right":
            self.move(avail.right() - self.width() + 1, self.y())
        elif self.dock == "top":
            self.move(self.x(), avail.top())
        self._away_since = time.time()
        self._home = self.pos()
        self._float(bool(self.dock))
        self.state_changed.emit()

    def home_pos(self) -> QPoint:
        return self._home if (self.dock and self._home is not None) else self.pos()

    def set_dock(self, side: str | None) -> None:
        self.dock = side if side in ("left", "right", "top") and self._edge_free(side) else None
        self._home = self.pos()
        self._away_since = time.time()
        if self.dock:
            QTimer.singleShot(600, lambda: self._float(True))

    def _float(self, on: bool) -> None:
        """While docked and shown, stay above other windows even if "always on top" is off, so the
        widget is not hidden behind whatever is covering that screen edge."""
        if not self.on_top:
            win11.set_topmost(int(self.winId()), on)

    def _slide_to(self, pt: QPoint) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(pt)
        self._anim.start()

    def reveal(self) -> None:
        if self.dock and self.tucked and self._home is not None:
            self.tucked = False
            self._float(True)
            self._slide_to(self._home)
        self._away_since = time.time()

    def _tuck(self) -> None:
        g, home = self.screen().geometry(), self._home or self.pos()
        self.tucked = True
        self._slide_to({"left": QPoint(g.left() - self.width() + PEEK_PX, home.y()),
                        "right": QPoint(g.right() + 1 - PEEK_PX, home.y()),
                        "top": QPoint(home.x(), g.top() - self.height() + PEEK_PX)}[self.dock])

    def _dock_tick(self) -> None:
        if not self.dock or not self.isVisible() or self._drag is not None:
            return
        inside = self.frameGeometry().adjusted(-4, -4, 4, 4).contains(QCursor.pos())
        busy = QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None
        if inside or busy:
            self._away_since = time.time()
            if inside and self.tucked:
                self.reveal()
        elif not self.tucked and time.time() - self._away_since > 0.8:
            self._tuck()

    def moveEvent(self, e):
        self._save_timer.start()

    def contextMenuEvent(self, e):
        self.main_menu_requested.emit(e.globalPos())

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)
        self.state_changed.emit()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.btn_fold.set_glyph(T.ICON["down" if collapsed else "up"])
        self._relayout()

    def _relayout(self) -> None:
        show = not self.collapsed
        self.scroll.setVisible(show and bool(self._rows))
        self.ctx.setVisible(show and bool(self._ctx))
        n = min(len(self._rows), MAX_ROWS)
        list_h = max(n, 1) * ROW_H
        if self.list_h:                 # the user dragged the bottom edge
            list_h = max(ROW_H, min(self.list_h, max(len(self._rows), 1) * ROW_H))
        self.scroll.setFixedHeight(list_h)
        h = 6 + 32 + 2 + self.summary.height() + 2 + 20 + 8
        if show:
            h += (list_h + 2 if self._rows else 0) + (30 if self._ctx else 0)
        self.setFixedHeight(h)

    def place_default(self) -> None:
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.right() - self.width() - 24, g.top() + 24)

    def restore_position(self, x, y) -> None:
        if x is None or y is None:
            return self.place_default()
        pt = QPoint(int(x), int(y))
        for scr in QGuiApplication.screens():
            if scr.availableGeometry().adjusted(-200, 0, 200, 0).contains(pt + QPoint(self.width() // 2, 20)):
                return self.move(pt)
        self.place_default()

    def apply_privacy(self) -> None:
        self.btn_eye.set_glyph(T.ICON["eye_off" if T.PRIVATE else "eye"])
        if self.snap is not None:
            self.set_snapshot(self.snap)
        self.update()

    # ---- data
    def set_busy(self, busy: bool) -> None:
        self.btn_refresh.spin = busy
        self.btn_refresh.update()

    def set_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        self.summary.set_snapshot(snap, self.pal)
        while len(self._rows) < len(snap.tickers):
            row = TickerRow()
            row.clicked.connect(self.detail_requested)
            row.menu_requested.connect(self.row_menu_requested)
            self.rows_layout.insertWidget(len(self._rows), row)
            self._rows.append(row)
        while len(self._rows) > len(snap.tickers):
            row = self._rows.pop()
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        for row, v in zip(self._rows, snap.tickers):
            row.set_view(v, self.pal)

        if snap.mode == "演示":
            badge = "演示模式" if snap.sample_holdings else "手动持仓"
            tip = ("行情是 Yahoo 的真实数据；持仓仍是 watchlist.yaml 里自带的示例数字（不是你的）。\n"
                   "改成你自己的持仓，或在 分析中心 → 数据源 里连接 IBKR / 导入 Robinhood CSV。"
                   if snap.sample_holdings else
                   "行情是 Yahoo 的真实数据；持仓来自你在 watchlist.yaml 里手动填写的数量（未连接券商）。")
        else:
            badge, tip = snap.mode, ""
        self.badge.setText(badge)
        self._style_badge(T.AMBER if snap.mode in ("演示", "券商离线") else T.GREEN)
        self.badge.setToolTip("\n".join([tip] + [m for _, m in snap.sources.values()]).strip())
        self.btn_bell.set_badge(len(snap.alerts))

        ctx: list[tuple[str, str]] = []
        for o in snap.odds[:8]:
            d = "" if o.change_1w is None or abs(o.change_1w) < 0.0005 else \
                f" {'▲' if o.change_1w > 0 else '▼'}{abs(o.change_1w)*100:.0f}"
            ctx.append((f"预测市场 {o.prob*100:.0f}%{d}　{o.question}", T.TEXT))
        for n in snap.news[:4]:
            tag = f"[{','.join(n.tickers[:3])}] " if n.tickers else ""
            ctx.append((f"SemiAnalysis　{tag}{n.title}", T.TEXT))
        for a in snap.alerts[:3]:
            ctx.insert(0, (f"行为提醒　{a.title}", T.AMBER))
        self._ctx = ctx
        self._ctx_i = 0
        self._show_ctx()

        t = time.strftime("%H:%M", time.localtime(snap.ts))
        if snap.market_msg:
            self.status.setText(f"⚠ {snap.market_msg}")
            self.status.setStyleSheet(f"color:{T.AMBER}; background:transparent; padding:2px 10px 0 10px;")
        else:
            self.status.setText(f"更新于 {t} · 只读 · 仅供参考，非投资建议")
            self.status.setStyleSheet(f"color:{T.DIM}; background:transparent; padding:2px 10px 0 10px;")
        self._relayout()
        self.update()

    def _show_ctx(self) -> None:
        if not self._ctx:
            self.ctx.setText("")
            return
        text, color = self._ctx[self._ctx_i % len(self._ctx)]
        fm = QFontMetricsF(self.ctx.font())
        self.ctx.setText(fm.elidedText(text, Qt.ElideRight, WIDTH - 12 - 22))
        self.ctx.setStyleSheet(f"color:{color}; background:rgba(255,255,255,14); border-radius:6px; padding:5px 9px;")

    def _next_ctx(self) -> None:
        if self._ctx and not self.ctx.underMouse():
            self._ctx_i += 1
            self._show_ctx()
