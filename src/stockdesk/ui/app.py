"""Application controller: worker thread, tray icon, timers, windows."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QPoint, QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QIcon, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QScrollArea,
                               QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget)

from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .. import paths, remote
from ..config import Config
from ..engine import Engine, Snapshot
from ..i18n import L
from . import theme as T
from . import win11
from .detail import DetailWindow
from .hub import HubWindow, guard_wheel
from .widget import DeskWidget

log = logging.getLogger("stockdesk")


def setup_logging() -> None:
    paths.ensure_dirs()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    handler = RotatingFileHandler(paths.LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for noisy in ("yfinance", "peewee", "urllib3", "ib_async"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 64, 64)
    g.setColorAt(0, QColor("#2b62b8"))
    g.setColorAt(1, QColor("#16233f"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    path = QPainterPath(QPointF(12, 44))
    for x, y in ((24, 34), (33, 40), (50, 20)):
        path.lineTo(x, y)
    p.setPen(QPen(QColor("white"), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(T.GREEN))
    p.drawEllipse(QPointF(50, 20), 6, 6)
    p.end()
    return QIcon(pm)


class Worker(QObject):
    """Owns the Engine; everything here runs in the worker thread."""
    snapshot = Signal(object)
    busy = Signal(bool)
    cycle_done = Signal()
    pretrade_done = Signal(object)
    code_request = Signal(str)

    def __init__(self):
        super().__init__()
        self.engine: Engine | None = None
        self._code = ""
        self._code_event = threading.Event()

    def _prompt_code(self, prompt: str) -> str:
        self._code, _ = "", self._code_event.clear()
        self.code_request.emit(prompt)
        self._code_event.wait(180)
        return self._code

    def provide_code(self, code: str) -> None:       # called from the GUI thread
        self._code = code
        self._code_event.set()

    @Slot()
    def start(self) -> None:
        try:
            self.engine = Engine(code_prompt=self._prompt_code)
        except Exception:
            log.exception("engine start failed")
            return
        self.refresh(False)

    @Slot(bool)
    def refresh(self, force: bool) -> None:
        if self.engine is None:
            return
        self.busy.emit(True)
        try:
            self.snapshot.emit(self.engine.refresh(force))
            if self.engine.research_due():
                self.engine.refresh_research()
                self.snapshot.emit(self.engine.build_snapshot())
            if self.engine.flow_due():
                self.engine.scan_flow()
                self.snapshot.emit(self.engine.build_snapshot())
            if self.engine.rates_due():
                self.engine.scan_rates()
                self.snapshot.emit(self.engine.build_snapshot())
            if self.engine.congress_due():
                self.engine.scan_congress()
                self.snapshot.emit(self.engine.build_snapshot())
            before = (getattr(self, "_remote", None) or {}).get("status")
            self._remote = remote.keep_in_sync(self.engine.cfg.get("remote", {}), getattr(self, "_remote", None))
            status = (self._remote or {}).get("status")
            if status:
                self.engine._status["remote"] = tuple(status)
            else:
                self.engine._status.pop("remote", None)
            if status != before:
                self.snapshot.emit(self.engine.build_snapshot())
        except Exception:
            log.exception("refresh failed")
        finally:
            self.busy.emit(False)
            self.cycle_done.emit()

    @Slot()
    def research(self) -> None:
        if self.engine is None:
            return
        self.busy.emit(True)
        try:
            self.engine.refresh_research(force=True)
            self.snapshot.emit(self.engine.build_snapshot())
        except Exception:
            log.exception("research refresh failed")
        finally:
            self.busy.emit(False)

    @Slot()
    def scan_flow(self) -> None:
        if self.engine is None:
            return
        self.busy.emit(True)
        try:
            self.engine.scan_flow(force=True)
            self.snapshot.emit(self.engine.build_snapshot())
        except Exception:
            log.exception("flow scan failed")
        finally:
            self.busy.emit(False)

    @Slot(str)
    def add_symbol(self, raw: str) -> None:
        if self.engine and self.engine.add_symbol(raw):
            self.refresh(False)

    @Slot(str)
    def remove_symbol(self, symbol: str) -> None:
        if self.engine:
            self.engine.remove_symbol(symbol)
            self._rebuild()                       # its markets disappear at once
            try:
                if self.engine.research_due():    # then re-search for the new set of stocks
                    self.engine.refresh_research()
                    self._rebuild()
            except Exception:
                log.exception("research refresh after remove failed")

    @Slot(str, object, object, object)
    def set_plan(self, symbol: str, stop, target, max_pct) -> None:
        if self.engine:
            self.engine.set_plan(symbol, stop, target, max_pct)
            self._rebuild()

    @Slot(str, float)
    def snooze(self, alert_id: str, days: float) -> None:
        if self.engine:
            self.engine.store.snooze_alert(alert_id, days)
            self._rebuild()

    @Slot(str)
    def dismiss(self, alert_id: str) -> None:
        if self.engine:
            self.engine.store.dismiss_alert(alert_id)
            self._rebuild()

    @Slot(str, str, float, object)
    def pretrade(self, symbol: str, side: str, qty: float, price) -> None:
        if not self.engine:
            return
        try:
            checks, px = self.engine.pretrade(symbol, side, qty, price)
            self.pretrade_done.emit((symbol, side, qty, px, checks))
        except Exception:
            log.exception("pretrade failed")

    @Slot()
    def reload_config(self) -> None:
        if self.engine:
            self.engine.cfg = Config.load()
            self.refresh(True)

    def _rebuild(self) -> None:
        try:
            self.snapshot.emit(self.engine.build_snapshot())
        except Exception:
            log.exception("rebuild failed")


class PlanDialog(QDialog):
    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.setWindowTitle(L(f"{view.symbol} · 我的计划", f"{view.symbol} · My plan"))
        self.setStyleSheet(T.WINDOW_QSS)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        tip = QLabel(L("写下你自己的退出条件。留 0 表示不设；不设时小组件用 ATR 自动估算参考位。\n"
                     "一旦设定，跌破止损仍持有、或仓位超过上限时，“行为提醒”会提示你。",
                     "Write down your own exit conditions. Leave 0 for none; without them the widget estimates reference levels from ATR.\n"
                     "Once set, \"Behaviour alerts\" will flag it if you still hold below your stop or the position exceeds the cap."))
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        form = QFormLayout()
        self.stop, self.target, self.max_pct = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
        for w, val in ((self.stop, view.plan.get("stop")), (self.target, view.plan.get("target"))):
            w.setRange(0, 1e7)
            w.setDecimals(2)
            w.setSpecialValueText(L("不设", "None"))
            w.setValue(float(val or 0))
        self.max_pct.setRange(0, 100)
        self.max_pct.setDecimals(1)
        self.max_pct.setSuffix(" %")
        self.max_pct.setSpecialValueText(L("不设（用默认上限）", "None (use default cap)"))
        self.max_pct.setValue(float(view.plan.get("max_pct") or 0))
        form.addRow(L("止损价", "Stop price"), self.stop)
        form.addRow(L("目标价", "Target price"), self.target)
        form.addRow(L("最大仓位占比", "Max position share"), self.max_pct)
        lay.addLayout(form)
        a = view.analysis
        if a and a.ok and a.levels:
            ref = QLabel(L(f"参考：现价 {T.price(view.price)}，ATR(14) {a.levels.atr:.2f}；"
                           f"ATR 止损 {T.price(a.levels.stop)} / 止盈 {T.price(a.levels.target)}"
                           f"（以{'成本' if a.levels.basis == 'cost' else '现价'}为锚）",
                           f"Reference: price {T.price(view.price)}, ATR(14) {a.levels.atr:.2f}; "
                           f"ATR stop {T.price(a.levels.stop)} / take-profit {T.price(a.levels.target)} "
                           f"(anchored on {'cost' if a.levels.basis == 'cost' else 'price'})"))
            ref.setObjectName("hint")
            ref.setWordWrap(True)
            lay.addWidget(ref)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def values(self):
        return (self.stop.value() or None, self.target.value() or None, self.max_pct.value() or None)


class Toast(QLabel):
    """A small pop-up in the corner of the screen for new alerts; click to open them, closes by itself."""
    clicked = Signal()

    def __init__(self, title: str, body: str):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(True)
        self.setFixedWidth(380)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"QLabel {{ background:{T.PANEL}; color:{T.TEXT}; border:1px solid {T.AMBER};"
                           f" border-radius:8px; padding:12px 14px; font-size:10pt; }}")
        import html as _html
        self.setText(f"<b>{_html.escape(title)}</b><br><span style='color:{T.SUB};'>{_html.escape(body)}</span>"
                     f"<br><span style='color:{T.DIM}; font-size:8.5pt;'>" + L("Stock Desk · 点击查看", "Stock Desk · click to view") + "</span>")

    def popup(self, screen, seconds: int = 20) -> None:
        self.adjustSize()
        g = screen.availableGeometry()
        self.move(g.right() - self.width() - 18, g.bottom() - self.height() - 18)
        self.show()
        QTimer.singleShot(seconds * 1000, self.close)

    def mousePressEvent(self, e):
        self.clicked.emit()
        self.close()


class HalfYearPlanDialog(QDialog):
    """The user writes their own plan for one position; the program only records and tracks it."""

    def __init__(self, view, plan: dict, refs: list[tuple[str, str]], parent=None, follow_opt: dict | None = None):
        """``follow_opt``: {symbol, name, ccy, to_local} when the levels can be written in another stock's prices."""
        super().__init__(parent)
        self.follow_opt = follow_opt
        from datetime import date, timedelta
        from PySide6.QtCore import QDate
        self.setWindowTitle(L(f"{view.symbol} · 我的计划（建仓 / 减仓）", f"{view.symbol} · My plan (build / trim)"))
        self.setStyleSheet(T.WINDOW_QSS)
        self.setMinimumWidth(560)
        # the form can be taller than a small screen: scroll the content, keep the buttons visible
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(content)
        tip = QLabel(L("这里的每个数字都是<b>你自己的决定</b>：程序不会替你填，也不会下单，只会每天对照并在简报和图上显示。"
                     "留 0 / 留空表示暂时不定。",
                     "Every number here is <b>your own decision</b>: the program does not fill it in and places no orders; "
                     "it only compares against it daily and shows it in the briefing and on the chart. "
                     "Leave 0 / blank if undecided."))
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.cur = getattr(view, "currency", "USD")
        form = QFormLayout()
        self.follow = QCheckBox()
        self.follow_hint = QLabel()
        self.follow_hint.setObjectName("hint")
        self.follow_hint.setWordWrap(True)
        if follow_opt:
            self.follow.setText(L(f"价位按 {follow_opt['name']}（{follow_opt['symbol']}，{follow_opt['ccy']}）的股价填写和判断",
                                 f"Enter and judge price levels on the {follow_opt['name']} share price ({follow_opt['symbol']}, {follow_opt['ccy']})"))
            self.follow.setChecked(str(plan.get("follow") or "").upper() == follow_opt["symbol"].upper())
            self.follow.toggled.connect(self._relabel)
            form.addRow(self.follow)
            form.addRow(self.follow_hint)
        self.target = QDoubleSpinBox()
        self.target.setRange(0, 100)
        self.target.setDecimals(1)
        self.target.setSuffix(" %")
        self.target.setSpecialValueText(L("未定", "Undecided"))
        self.target.setValue(float(plan.get("target_weight_pct") or 0))
        self.exit_below = QDoubleSpinBox()
        self.exit_below.setRange(0, 1e9)
        self.exit_below.setDecimals(2)
        self.exit_below.setSpecialValueText(L("未定", "Undecided"))
        self.exit_below.setValue(float(plan.get("exit_below") or 0))
        self.levels = QLineEdit(", ".join(f"{float(x):.10g}" for x in (plan.get("reduce_above") or [])))
        self.levels.setPlaceholderText(L("用逗号分隔，例如 900, 1000, 1100（数字里不要写千分位逗号）", "Comma-separated, e.g. 900, 1000, 1100 (no thousands separators in numbers)"))
        self.buys = QLineEdit(", ".join(f"{float(x):.10g}" for x in (plan.get("buy_below") or [])))
        self.buys.setPlaceholderText(L("想分批买入的价位，用逗号分隔；到价提醒（数字里不要写千分位逗号）", "Price levels to buy in stages, comma-separated; alerts when reached (no thousands separators in numbers)"))
        self.target_price = QDoubleSpinBox()
        self.target_price.setRange(0, 1e9)
        self.target_price.setDecimals(2)
        self.target_price.setSpecialValueText(L("未定", "Undecided"))
        self.target_price.setValue(float(plan.get("target_price") or 0))
        self.max_weight = QDoubleSpinBox()
        self.max_weight.setRange(0, 100)
        self.max_weight.setDecimals(1)
        self.max_weight.setSuffix(" %")
        self.max_weight.setSpecialValueText(L("未定", "Undecided"))
        self.max_weight.setValue(float(plan.get("max_weight_pct") or 0))
        end = plan.get("end")
        try:
            end = date.fromisoformat(str(end))
        except (TypeError, ValueError):
            end = date.today() + timedelta(days=183)
        self.end = QDateEdit(QDate(end.year, end.month, end.day))
        self.end.setCalendarPopup(True)
        self.end.setDisplayFormat("yyyy-MM-dd")
        self.review = QSpinBox()
        self.review.setRange(7, 180)
        self.review.setSuffix(L(" 天", " days"))
        self.review.setValue(int(plan.get("review_days") or 30))
        self.no_add = QCheckBox(L("计划期间不再加仓（有买入时简报会标出）", "No adding during the plan (the briefing flags any buy)"))
        self.no_add.setChecked(bool(plan.get("no_add", not (plan.get("buy_below") or not view.qty))))
        self.notes = QLineEdit(str(plan.get("notes") or ""))
        self.notes.setPlaceholderText(L("为什么持有；什么情况下认错", "Why you hold; when you would admit you were wrong"))
        form.addRow(QLabel(L("<b>建仓</b>（想买入时填）", "<b>Build a position</b> (fill in if you want to buy)")))
        self._labs = {k: QLabel() for k in ("buy", "target", "reduce", "exit")}
        form.addRow(self._labs["buy"], self.buys)
        form.addRow(L("这只股票的仓位上限", "Position cap for this stock"), self.max_weight)
        form.addRow(self._labs["target"], self.target_price)
        form.addRow(QLabel(L("<b>减仓</b>（想降低仓位时填）", "<b>Trim</b> (fill in if you want to reduce)")))
        form.addRow(L("结束日前想降到占组合", "Reduce to this share of portfolio by end date"), self.target)
        form.addRow(self._labs["reduce"], self.levels)
        form.addRow(QLabel(L("<b>通用</b>", "<b>General</b>")))
        form.addRow(self._labs["exit"], self.exit_below)
        self._relabel()
        form.addRow(L("计划结束日", "Plan end date"), self.end)
        form.addRow(L("复查间隔", "Review interval"), self.review)
        form.addRow("", self.no_add)
        form.addRow(L("写给自己的话", "Note to self"), self.notes)
        lay.addLayout(form)
        if refs:
            rows = "".join(f"<tr><td style='padding:2px 14px 2px 0; color:{T.SUB};'>{k}</td><td>{v}</td></tr>" for k, v in refs)
            ref = QLabel(L("<b>可以对照的现有数据</b>（按固定规则算出，不是建议填的值）", "<b>Existing data to compare against</b> (computed by fixed rules; not suggested values)") + "<table>" + rows + "</table>")
            ref.setTextFormat(Qt.RichText)
            ref.setObjectName("hint")
            ref.setWordWrap(True)
            lay.addWidget(ref)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        outer.addWidget(box)
        guard_wheel(content)
        screen = (parent.screen() if parent is not None else QApplication.primaryScreen()).availableGeometry()
        want = content.sizeHint().height() + box.sizeHint().height() + 60
        self.resize(max(self.minimumWidth(), content.sizeHint().width() + 40), min(want, int(screen.height() * 0.88)))

    def _relabel(self) -> None:
        """Show which currency the price fields are in (the followed stock's when the box is ticked)."""
        on = bool(self.follow_opt) and self.follow.isChecked()
        cur = self.follow_opt["ccy"] if on else self.cur
        for key, text in (("buy", L("分批买入价位", "Buy levels")), ("target", L("目标价", "Target price")), ("reduce", L("分批减仓价位", "Trim levels")), ("exit", L("底线价", "Floor price"))):
            self._labs[key].setText(L(f"{text}（{cur}）", f"{text} ({cur})"))
        if self.follow_opt:
            o = self.follow_opt
            conv = L(f"按现在的汇率，1,000,000 {o['ccy']} ≈ {1e6 * o['to_local']:,.0f} {self.cur}。",
                     f"At the current rate, 1,000,000 {o['ccy']} ≈ {1e6 * o['to_local']:,.0f} {self.cur}. ") if o.get("to_local") else ""
            self.follow_hint.setText(
                (L(f"已勾选：下面四个价位都是 {o['name']} 的 {o['ccy']} 股价，提醒也按它的行情触发；"
                   f"图上换算成 {self.cur} 画在 {self.windowTitle().split(' ')[0]} 的 K 线上。",
                   f"Ticked: the four price levels below are {o['name']} share prices in {o['ccy']}, and alerts trigger on its quote; "
                   f"on the chart they are converted to {self.cur} and drawn on the {self.windowTitle().split(' ')[0]} candles. ") if on else
                 L(f"未勾选：价位按这只股票自己的 {self.cur} 价格。", f"Not ticked: levels are in this stock's own {self.cur} price. ")) + conv
                + L("切换时已填的数字不会自动换算，请自己改。", "Numbers already entered are not converted when you switch; edit them yourself."))

    @staticmethod
    def _levels(text: str) -> list[float]:
        out = []
        for part in text.replace("，", ",").replace("/", ",").replace("、", ",").split(","):
            try:
                x = float(part.strip())
            except ValueError:
                continue
            if x > 0:
                out.append(x)
        return sorted(set(out))

    def values(self) -> dict:
        return {"target_weight_pct": self.target.value() or None, "exit_below": self.exit_below.value() or None,
                "reduce_above": self._levels(self.levels.text()),
                "buy_below": sorted(self._levels(self.buys.text()), reverse=True),
                "target_price": self.target_price.value() or None,
                "max_weight_pct": self.max_weight.value() or None, "end": self.end.date().toString("yyyy-MM-dd"),
                "review_days": self.review.value(), "no_add": self.no_add.isChecked(),
                "notes": self.notes.text().strip(),
                "follow": self.follow_opt["symbol"] if self.follow_opt and self.follow.isChecked() else None}


class App(QObject):
    sig_refresh = Signal(bool)
    sig_research = Signal()
    sig_flow = Signal()
    sig_add = Signal(str)
    sig_remove = Signal(str)
    sig_plan = Signal(str, object, object, object)
    sig_snooze = Signal(str, float)
    sig_dismiss = Signal(str)
    sig_pretrade = Signal(str, str, float, object)
    sig_reload = Signal()
    sig_start = Signal()

    def __init__(self, qapp: QApplication, shots_dir: Path | None = None):
        super().__init__()
        self.qapp = qapp
        self.shots_dir = shots_dir
        self.cfg = Config.load()
        self.pal = T.Palette(self.cfg.get("app.up_color", "green"))
        self.state = self._load_state()
        T.PRIVATE = bool(self.state.get("private", False))
        self.snap: Snapshot | None = None
        self.details: dict[str, DetailWindow] = {}
        self.hub: HubWindow | None = None
        self._cycles = 0

        self.widget = DeskWidget(self.cfg.get("app.opacity", 0.86),
                                 bool(self.state.get("on_top", self.cfg.get("app.always_on_top", True))), self.pal)
        self.widget.set_collapsed(bool(self.state.get("collapsed", False)))
        self.widget.restore_position(self.state.get("x"), self.state.get("y"))
        self.widget.set_dock(self.state.get("dock"))
        self.widget.list_h = self.state.get("list_h") or None
        self.widget.refresh_requested.connect(lambda: self.sig_refresh.emit(True))
        self.widget.add_requested.connect(self.add_dialog)
        self.widget.detail_requested.connect(self.open_detail)
        self.widget.hub_requested.connect(self.open_hub)
        self.widget.row_menu_requested.connect(self.row_menu)
        self.widget.main_menu_requested.connect(lambda pos: self.menu.popup(pos))
        self.widget.state_changed.connect(self._save_state)
        self.widget.privacy_toggled.connect(lambda: self.set_private(not T.PRIVATE))

        self.thread = QThread()
        self.worker = Worker()
        self.worker.moveToThread(self.thread)
        self.worker.snapshot.connect(self.on_snapshot)
        self.worker.busy.connect(self.widget.set_busy)
        self.worker.cycle_done.connect(self._on_cycle)
        self.worker.pretrade_done.connect(self._on_pretrade)
        self.worker.code_request.connect(self._ask_code)
        self.sig_start.connect(self.worker.start)
        self.sig_refresh.connect(self.worker.refresh)
        self.sig_research.connect(self.worker.research)
        self.sig_flow.connect(self.worker.scan_flow)
        self.sig_add.connect(self.worker.add_symbol)
        self.sig_remove.connect(self.worker.remove_symbol)
        self.sig_plan.connect(self.worker.set_plan)
        self.sig_snooze.connect(self.worker.snooze)
        self.sig_dismiss.connect(self.worker.dismiss)
        self.sig_pretrade.connect(self.worker.pretrade)
        self.sig_reload.connect(self.worker.reload_config)
        self.thread.start()

        self._build_tray()
        self._listen()
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.sig_refresh.emit(False))
        self.timer.start(max(30, int(self.cfg.get("app.refresh_seconds", 120))) * 1000)
        self.widget.show()
        self.sig_start.emit()

    # ---- state
    def _load_state(self) -> dict:
        try:
            return json.loads(paths.STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        self.state.update(x=self.widget.home_pos().x(), y=self.widget.home_pos().y(), collapsed=self.widget.collapsed,
                          on_top=self.widget.on_top, private=T.PRIVATE, dock=self.widget.dock,
                          list_h=self.widget.list_h)
        try:
            paths.STATE_PATH.write_text(json.dumps(self.state), encoding="utf-8")
        except OSError:
            pass

    # ---- tray
    def _build_tray(self) -> None:
        self.menu = QMenu()
        self.menu.setStyleSheet(T.MENU_QSS)
        self.act_show = self.menu.addAction(L("显示 / 隐藏小组件", "Show / hide widget"), self.toggle_widget)
        self.menu.addAction(L("立即刷新", "Refresh now"), lambda: self.sig_refresh.emit(True))
        self.menu.addAction(L("添加自选股…", "Add to watchlist…"), self.add_dialog)
        self.menu.addSeparator()
        self.menu.addAction(L("每日简报…", "Daily briefing…"), lambda: self.open_hub("briefing"))
        self.menu.addAction(L("组合优化…", "Portfolio optimisation…"), lambda: self.open_hub("portfolio"))
        self.menu.addAction(L("行为提醒…", "Behaviour alerts…"), lambda: self.open_hub("alerts"))
        self.menu.addAction(L("交易前检查…", "Pre-trade check…"), lambda: self.open_hub("pretrade"))
        self.menu.addAction(L("资讯 · 预测市场…", "News · prediction markets…"), lambda: self.open_hub("research"))
        self.menu.addAction(L("数据源与设置…", "Sources && settings…"), lambda: self.open_hub("sources"))
        self.menu.addSeparator()
        self.act_private = QAction(L("隐藏关键数据（持仓 / 金额 / 盈亏）", "Hide key data (positions / amounts / P&L)"), self.menu, checkable=True, checked=T.PRIVATE)
        self.act_private.toggled.connect(self.set_private)
        self.menu.addAction(self.act_private)
        self.act_top = QAction(L("窗口置顶", "Always on top"), self.menu, checkable=True, checked=self.widget.on_top)
        self.act_top.toggled.connect(self._set_on_top)
        self.menu.addAction(self.act_top)
        self.menu.addAction(L("编辑半年计划…", "Edit half-year plans…"), self.edit_plans)
        self.menu.addAction(L("编辑 watchlist.yaml", "Edit watchlist.yaml"), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.WATCHLIST))))
        self.menu.addSeparator()
        self.menu.addAction(L("退出", "Quit"), self.quit)
        # The tray icon is optional (app.tray_icon). Without it Stock Desk lives inside Pipeline Hub:
        # launching it again shows the widget, and right-clicking the widget opens this same menu.
        self.tray = None
        if not self.cfg.get("app.tray_icon", False):
            return
        self.tray = QSystemTrayIcon(app_icon())
        self.tray.setToolTip(L("Stock Desk（只读）", "Stock Desk (read-only)"))
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(lambda r: self.toggle_widget() if r == QSystemTrayIcon.Trigger else None)
        self.tray.messageClicked.connect(lambda: self.open_hub("alerts"))
        self.tray.show()

    # ---- single instance: a second launch (e.g. from Pipeline Hub) asks this one to show itself
    def _listen(self) -> None:
        self.server = None
        if self.shots_dir is not None:
            return
        name = instance_key()
        QLocalServer.removeServer(name)        # clean up a server left behind by a crash
        srv = QLocalServer(self)
        if srv.listen(name):
            srv.newConnection.connect(self._on_activate)
            self.server = srv

    def _on_activate(self) -> None:
        while self.server is not None and self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            conn.disconnected.connect(conn.deleteLater)
        w = self.widget
        w.set_collapsed(False)
        w.show()
        w.raise_()
        w.activateWindow()
        w.reveal()
        QTimer.singleShot(50, self._fit_on_screen)

    def _fit_on_screen(self) -> None:
        """After expanding, pull the widget up / in so it is fully visible."""
        w = self.widget
        if w.dock:                 # a docked widget manages its own position
            return
        g, avail = w.frameGeometry(), w.screen().availableGeometry()
        x = min(max(g.x(), avail.left()), avail.right() - g.width())
        y = min(max(g.y(), avail.top()), avail.bottom() - g.height())
        if (x, y) != (g.x(), g.y()):
            w.move(max(x, avail.left()), max(y, avail.top()))
        self._save_state()

    def set_private(self, on: bool) -> None:
        """Privacy mode: hide holdings, amounts and P&L everywhere (for when someone can see the screen)."""
        T.PRIVATE = bool(on)
        if self.act_private.isChecked() != T.PRIVATE:
            self.act_private.blockSignals(True)
            self.act_private.setChecked(T.PRIVATE)
            self.act_private.blockSignals(False)
        self.widget.apply_privacy()
        if self.snap is not None:
            if self.hub is not None:
                self.hub.set_snapshot(self.snap)
                self.hub.pretrade.setHtml("")
            for sym, win in list(self.details.items()):
                v = self.snap.get(sym)
                if v is not None:
                    win.set_view(v, self.snap.trades, self.snap.refs.get(sym), self._plan_of(sym))
        self._save_state()

    def edit_plans(self) -> None:
        """Edit the plan of the largest holding (other symbols: right-click their row)."""
        held = sorted((v for v in (self.snap.tickers if self.snap else []) if v.qty), key=lambda v: -v.weight)
        if held:
            self.half_year_plan_dialog(held[0].symbol)

    def half_year_plan_dialog(self, symbol: str) -> None:
        from .. import plans
        v = self.snap.get(symbol) if self.snap else None
        if v is None:
            return
        if T.PRIVATE:
            QMessageBox.information(self.widget, L("隐私模式", "Privacy mode"), L("隐私模式开启时不显示计划。先点眼睛图标关闭隐私模式。", "Plans are not shown while privacy mode is on. Click the eye icon to turn it off first."))
            return
        plan = plans.load().get(symbol) or {}
        cap = float(self.cfg.get("quant.max_position_pct", 25.0))
        ref = self.snap.refs.get(symbol)
        refs = plans.reference_levels(v, cap, ref)
        follow_opt = None
        if ref is not None:                    # this stock has a home-market share its plan can follow
            to_local = ref.implied / ref.ref_price if ref.ref_price and ref.implied == ref.implied else None
            follow_opt = {"symbol": ref.ref_symbol, "name": ref.name, "ccy": ref.ref_currency, "to_local": to_local}
            fv = self.snap.get(ref.ref_symbol)
            if fv is not None:
                refs = refs + [(L(f"<b>{ref.name}（{ref.ref_symbol}，{ref.ref_currency}）</b>", f"<b>{ref.name} ({ref.ref_symbol}, {ref.ref_currency})</b>"), "")] + \
                    plans.reference_levels(fv, cap, market_only=True)
        dlg = HalfYearPlanDialog(v, plan, refs, self.widget, follow_opt)
        if dlg.exec() == QDialog.Accepted:
            fields = dlg.values()
            if not plan:
                fields["start_weight_pct"] = round(v.weight * 100, 1)
            plans.save(symbol, fields)
            self.sig_refresh.emit(False)

    def _plan_of(self, symbol: str):
        all_plans = self.snap.plans if self.snap else []
        return next((p for p in all_plans if p.symbol == symbol), None) or \
            next((p for p in all_plans if p.follow == symbol), None)     # a plan written in this stock's prices

    def toggle_widget(self) -> None:
        if self.widget.isVisible():
            self.widget.hide()
        else:
            self.widget.show()
            self.widget.raise_()

    def _set_on_top(self, on: bool) -> None:
        self.widget.set_on_top(on)
        self._save_state()

    # ---- snapshots
    def on_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        self.widget.set_snapshot(snap)
        if getattr(self, "_await_research", False) and self._cycles >= 1:
            self._await_research = False
            QTimer.singleShot(900, self._shots)
        if self.hub is not None:
            self.hub.set_snapshot(snap)
        for sym, win in list(self.details.items()):
            v = snap.get(sym)
            if v is not None and win.isVisible():
                win.set_view(v, snap.trades, snap.refs.get(sym), self._plan_of(sym))
        if self.tray is None:          # no tray icon: show our own small pop-up for new alerts
            fresh = [a for a in snap.new_alerts if a.severity in ("warn", "high")]
            if fresh and self.shots_dir is None:
                title = fresh[0].title if len(fresh) == 1 else L(f"{len(fresh)} 条新提醒", f"{len(fresh)} new alerts")
                body = fresh[0].body.split("\n")[0][:160] if len(fresh) == 1 else L("；", "; ").join(a.title for a in fresh[:4])
                private = T.PRIVATE and any(a.kind not in ("bottoming", "flow", "index_level", "congress", "range", "rates") for a in fresh)
                self._toast = Toast(title, L("隐私模式：内容已隐藏，点击查看。", "Privacy mode: content hidden, click to view.") if private else body)
                self._toast.clicked.connect(lambda k=fresh[0].kind: self.open_hub("flow" if k == "flow" and len(fresh) == 1 else "alerts"))
                self._toast.popup(self.widget.screen())
            return
        self.tray.setToolTip(L(f"Stock Desk（只读）\n市值 {T.money(snap.total_value)}　今日 {T.money(snap.day_pnl, True)}",
                              f"Stock Desk (read-only)\nValue {T.money(snap.total_value)}  Today {T.money(snap.day_pnl, True)}"))
        fresh = [a for a in snap.new_alerts if a.severity in ("warn", "high")]
        if fresh and self.shots_dir is None:
            title = fresh[0].title if len(fresh) == 1 else L(f"{len(fresh)} 条新的行为提醒", f"{len(fresh)} new behaviour alerts")
            body = fresh[0].body.split("\n")[0][:180] if len(fresh) == 1 else L("；", "; ").join(a.title for a in fresh[:4])
            self.tray.showMessage(title, body, QSystemTrayIcon.Warning, 12000)

    def _on_cycle(self) -> None:
        self._cycles += 1
        if self.shots_dir is not None and self._cycles == 1:
            self._await_research = True          # self-test: force a fresh research pull first
            self.sig_research.emit()

    # ---- windows
    def open_detail(self, symbol: str) -> None:
        if self.snap is None or self.snap.get(symbol) is None:
            return
        win = self.details.get(symbol)
        if win is None:
            win = self.details[symbol] = DetailWindow(symbol, self.pal)
            win.setWindowIcon(app_icon())
        win.set_view(self.snap.get(symbol), self.snap.trades, self.snap.refs.get(symbol), self._plan_of(symbol))
        win.show()
        win.raise_()
        win.activateWindow()

    def _ensure_hub(self) -> HubWindow:
        if self.hub is None:
            self.hub = HubWindow(self.pal)
            self.hub.setWindowIcon(app_icon())
            self.hub.snooze_requested.connect(self.sig_snooze)
            self.hub.dismiss_requested.connect(self.sig_dismiss)
            self.hub.pretrade_requested.connect(self.sig_pretrade)
            self.hub.research_refresh_requested.connect(self.sig_research)
            self.hub.flow_scan_requested.connect(self.sig_flow)
            self.hub.settings_saved.connect(self._settings_saved)
            self.hub.plan_edit_requested.connect(self.half_year_plan_dialog)
            if self.snap is not None:
                self.hub.set_snapshot(self.snap)
        return self.hub

    def open_hub(self, tab: str = "briefing") -> None:
        self._ensure_hub().show_tab(tab)

    def _on_pretrade(self, result) -> None:
        if self.hub is not None:
            self.hub.show_pretrade(result)

    def _settings_saved(self) -> None:
        self.cfg = Config.load()
        self.pal.__init__(self.cfg.get("app.up_color", "green"))
        self.widget.set_opacity(self.cfg.get("app.opacity", 0.86))
        self.timer.start(max(30, int(self.cfg.get("app.refresh_seconds", 120))) * 1000)
        self.sig_reload.emit()

    def _ask_code(self, prompt: str) -> None:
        text, ok = QInputDialog.getText(self.widget, L("Robinhood 验证", "Robinhood verification"), prompt or L("请输入验证码：", "Enter the verification code:"))
        self.worker.provide_code(text if ok else "")

    def add_dialog(self) -> None:
        dlg = QInputDialog(self.widget)
        dlg.setStyleSheet(T.WINDOW_QSS)
        dlg.setWindowTitle(L("添加自选股", "Add to watchlist"))
        dlg.setLabelText(L("股票代码（美股，如 NVDA、BRK-B）：", "Ticker (US stocks, e.g. NVDA, BRK-B):"))
        if dlg.exec() == QDialog.Accepted and dlg.textValue().strip():
            self.sig_add.emit(dlg.textValue())

    def row_menu(self, symbol: str, pos: QPoint) -> None:
        v = self.snap.get(symbol) if self.snap else None
        if v is None:
            return
        m = QMenu()
        m.setStyleSheet(T.MENU_QSS)
        m.addAction(L("查看图表与依据", "View chart and rationale"), lambda: self.open_detail(symbol))
        m.addAction(L("设置我的止损 / 目标 / 仓位上限…", "Set my stop / target / position cap…"), lambda: self.plan_dialog(symbol))
        m.addAction(L("编辑我的计划（建仓 / 减仓）…", "Edit my plan (build / trim)…"), lambda: self.half_year_plan_dialog(symbol))
        m.addAction(L("交易前检查…", "Pre-trade check…"), lambda: self._ensure_hub().prefill_pretrade(symbol))
        m.addSeparator()
        rm = m.addAction(L("移出自选", "Remove from watchlist"), lambda: self.sig_remove.emit(symbol))
        if v.auto:
            rm.setEnabled(False)
            rm.setText(L("移出自选（券商持仓，自动显示）", "Remove from watchlist (broker position, shown automatically)"))
        m.exec(pos)

    def plan_dialog(self, symbol: str) -> None:
        v = self.snap.get(symbol) if self.snap else None
        if v is None:
            return
        dlg = PlanDialog(v, self.widget)
        if dlg.exec() == QDialog.Accepted:
            self.sig_plan.emit(symbol, *dlg.values())

    def quit(self) -> None:
        self._save_state()
        if self.tray is not None:
            self.tray.hide()
        if self.server is not None:
            self.server.close()
        self.thread.quit()
        self.thread.wait(4000)
        self.qapp.quit()

    # ---- self-test screenshots (python -m stockdesk --screenshots DIR)
    def _shots(self) -> None:
        out = self.shots_dir
        out.mkdir(parents=True, exist_ok=True)
        steps = []

        def grab_widget(name):
            pm = self.widget.grab()               # the widget only: never whatever else is on the screen
            dpr = pm.devicePixelRatio()
            w, h = int(pm.width() / dpr), int(pm.height() / dpr)
            canvas = QPixmap(int((w + 60) * dpr), int((h + 40) * dpr))
            canvas.setDevicePixelRatio(dpr)
            p = QPainter(canvas)
            grad = QLinearGradient(0, 0, w + 60, h + 40)
            grad.setColorAt(0.0, QColor("#1b2233"))
            grad.setColorAt(1.0, QColor("#0d1017"))
            p.fillRect(0, 0, w + 60, h + 40, QBrush(grad))
            p.drawPixmap(30, 20, pm)
            p.end()
            canvas.save(str(out / name))

        steps.append(lambda: grab_widget("widget.png"))
        sym = self.snap.tickers[0].symbol if self.snap and self.snap.tickers else None
        if sym:
            steps.append(lambda: self.open_detail(sym))
            steps.append(lambda: self.details[sym].grab().save(str(out / "detail.png")))
            steps.append(lambda: self.details[sym].close())
        for tab in ("briefing", "portfolio", "alerts", "research", "flow", "congress", "playbook", "sources"):
            steps.append(lambda tab=tab: self.open_hub(tab))
            steps.append(lambda tab=tab: self.hub.grab().save(str(out / f"hub_{tab}.png")))
        if sym:
            def pre():
                self.hub.prefill_pretrade(sym)
                self.hub.pt_qty.setValue(100)
                self.hub._run_pretrade()
            steps.append(pre)
            steps.append(lambda: self.hub.grab().save(str(out / "hub_pretrade.png")))
        steps.append(lambda: self.hub.close())
        steps.append(lambda: self.widget.set_collapsed(True))
        steps.append(lambda: grab_widget("widget_collapsed.png"))
        steps.append(lambda: self.widget.set_collapsed(False))
        steps.append(self.quit)

        def run(i=0):
            if i >= len(steps):
                return
            try:
                steps[i]()
            except Exception:
                log.exception("screenshot step %d failed", i)
            QTimer.singleShot(900, lambda: run(i + 1))
        run()


def instance_key() -> str:
    """Local-socket name unique per data folder (an isolated STOCKDESK_HOME gets its own)."""
    import hashlib
    return "StockDesk-" + hashlib.sha1(str(paths.DATA_DIR.resolve()).lower().encode()).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stockdesk", description="Stock Desk - read-only desktop stock analysis widget")
    ap.add_argument("--screenshots", metavar="DIR", help="start, save screenshots of every window into DIR, then exit")
    args = ap.parse_args(argv)
    setup_logging()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName("Stock Desk")
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(app_icon())
    lock = QLockFile(str(paths.DATA_DIR / "stockdesk.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        sock = QLocalSocket()
        sock.connectToServer(instance_key())
        if sock.waitForConnected(1500):        # the running copy brings its widget to the front
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()
            return 0
        QMessageBox.information(None, "Stock Desk", L("Stock Desk 已经在运行。", "Stock Desk is already running."))
        return 0
    app = App(qapp, Path(args.screenshots) if args.screenshots else None)
    code = qapp.exec()
    lock.unlock()
    del app
    return code
