"""Per-ticker detail window: price chart with indicators + the reasoning behind the suggestion."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPicture
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QPushButton, QSplitter, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..behavior.detectors import trade_day
from ..engine import TickerView
from ..models import Trade
from ..quant import indicators as ind
from . import render
from . import theme as T
from . import win11

pg.setConfigOptions(antialias=True, background=T.BG, foreground=T.SUB)


class DateIndexAxis(pg.AxisItem):
    """X axis over bar indices (no weekend gaps) labelled with dates."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.dates: list[pd.Timestamp] = []

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            out.append(self.dates[i].strftime("%y-%m-%d") if 0 <= i < len(self.dates) else "")
        return out


class CandleItem(pg.GraphicsObject):
    def __init__(self, o, h, l, c, up: str, down: str):
        super().__init__()
        self.picture = QPicture()
        p = QPainter(self.picture)
        for i in range(len(c)):
            col = QColor(up if c[i] >= o[i] else down)
            p.setPen(pg.mkPen(col, width=1))
            p.setBrush(col)
            p.drawLine(QPointF(i, l[i]), QPointF(i, h[i]))
            top, bot = max(o[i], c[i]), min(o[i], c[i])
            p.drawRect(QRectF(i - 0.32, bot, 0.64, max(top - bot, 1e-9)))
        p.end()
        self._rect = QRectF(-1, float(np.nanmin(l)), len(c) + 1, float(np.nanmax(h) - np.nanmin(l))) if len(c) else QRectF()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self._rect


class DetailWindow(QWidget):
    RANGES = (("3月", 63), ("6月", 126), ("1年", 252), ("2年", 504), ("全部", 0))
    show_range = True          # 震荡区间 overlay; shared by all detail windows in this session

    def __init__(self, symbol: str, pal: T.Palette):
        super().__init__()
        self.symbol, self.pal = symbol, pal
        self.view: TickerView | None = None
        self.plan = None
        self.trades: list[Trade] = []
        self._range = 252
        self._sig = None
        self.setWindowTitle(f"{symbol} · Stock Desk")
        self.setStyleSheet(T.WINDOW_QSS)
        self.resize(1320, 840)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        top = QHBoxLayout()
        self.title = QLabel(symbol)
        self.title.setFont(T.font(17, True, T.NUM_FONT))
        self.quote = QLabel("")
        self.quote.setFont(T.font(12, True, T.NUM_FONT))
        self.pos = QLabel("")
        self.pos.setStyleSheet(f"color:{T.SUB};")
        top.addWidget(self.title)
        top.addSpacing(10)
        top.addWidget(self.quote)
        top.addSpacing(16)
        top.addWidget(self.pos)
        top.addStretch(1)
        self.btn_range = QPushButton("震荡区间")
        self.btn_range.setCheckable(True)
        self.btn_range.setChecked(DetailWindow.show_range)
        self.btn_range.setToolTip("在图上显示 / 隐藏高抛低吸规则的支撑、阻力、低吸区 / 高抛区和历史信号")
        self.btn_range.toggled.connect(self._toggle_range)
        top.addWidget(self.btn_range)
        top.addSpacing(12)
        self._group = QButtonGroup(self)
        for label, n in self.RANGES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(n == self._range)
            b.clicked.connect(lambda _=False, n=n: self._set_range(n))
            self._group.addButton(b)
            top.addWidget(b)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.layout.setSpacing(4)
        self.ax = [DateIndexAxis(orientation="bottom") for _ in range(3)]
        self.p_price = self.glw.addPlot(row=0, col=0, axisItems={"bottom": self.ax[0]})
        self.p_rsi = self.glw.addPlot(row=1, col=0, axisItems={"bottom": self.ax[1]})
        self.p_macd = self.glw.addPlot(row=2, col=0, axisItems={"bottom": self.ax[2]})
        self.glw.ci.layout.setRowStretchFactor(0, 5)
        self.glw.ci.layout.setRowStretchFactor(1, 2)
        self.glw.ci.layout.setRowStretchFactor(2, 2)
        for pl, name in ((self.p_price, "价格 · SMA20/50/200 · 布林带"), (self.p_rsi, "RSI(14)"), (self.p_macd, "MACD(12,26,9)")):
            pl.showGrid(x=True, y=True, alpha=0.12)
            pl.setMenuEnabled(False)
            pl.hideButtons()
            pl.getAxis("left").setWidth(54)
            pl.setTitle(name, color=T.SUB, size="9pt")
            pl.setMouseEnabled(x=True, y=False)
        self.p_rsi.setXLink(self.p_price)
        self.p_macd.setXLink(self.p_price)
        self.p_price.getAxis("bottom").setStyle(showValues=False)
        self.p_rsi.getAxis("bottom").setStyle(showValues=False)
        split.addWidget(self.glw)

        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text.anchorClicked.connect(lambda u: QDesktopServices.openUrl(u) if u.scheme() in ("http", "https") else None)
        split.addWidget(self.text)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([720, 580])
        root.addWidget(split, 1)

    def showEvent(self, e):
        win11.dark_titlebar(int(self.winId()))
        super().showEvent(e)

    def _set_range(self, n: int) -> None:
        self._range = n
        self._sig = None
        if self.view is not None:
            self._draw_chart(self.view)

    def _toggle_range(self, on: bool) -> None:
        DetailWindow.show_range = on
        self._sig = None
        if self.view is not None:
            self._draw_chart(self.view)

    def set_view(self, v: TickerView, trades: list[Trade], ref=None, plan=None) -> None:
        self.view = v
        self.plan = plan
        self.trades = [t for t in trades if t.symbol == self.symbol]
        col = self.pal.sign(v.day_chg)
        self.quote.setText(f"{T.price(v.price)}　<span style='color:{col};'>{v.day_chg:+.2f}（{T.pct(v.day_pct)}）</span>")
        self.quote.setTextFormat(Qt.RichText)
        if v.qty and T.PRIVATE:
            self.pos.setText("隐私模式：持仓数据已隐藏")
        elif v.qty:
            c = self.pal.sign(v.upl)
            self.pos.setText(f"持有 {v.qty:g} 股 · 成本 {T.price(v.avg_cost)} · 市值 {T.money(v.value, decimals=0)} · "
                             f"<span style='color:{c};'>浮动盈亏 {T.money(v.upl, True, 0)}（{T.pct(v.upl_pct, 1)}）</span>"
                             f" · 来源 {'手动填写' if v.source in ('', 'manual') else v.source}")
        else:
            self.pos.setText("未持仓" + (f" · {v.note}" if v.note else ""))
        self.pos.setTextFormat(Qt.RichText)
        pos = self.text.verticalScrollBar().value()
        self.text.setHtml(render.analysis_html(v, self.pal, ref))
        self.text.verticalScrollBar().setValue(pos)
        self._draw_chart(v)

    def _draw_chart(self, v: TickerView) -> None:
        bars = v.bars
        if bars is None or bars.empty:
            return
        sig = (len(bars), float(bars["close"].iloc[-1]), self._range, v.stop, v.target, len(self.trades),
               DetailWindow.show_range, T.PRIVATE,
               None if self.plan is None else str(self.plan.chart_levels(self.symbol)))
        if sig == self._sig:          # nothing changed: keep the user's zoom
            return
        self._sig = sig
        c, h, l, o = bars["close"], bars["high"], bars["low"], bars["open"]
        s20, s50, s200 = ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
        bb, rsi, macd = ind.bollinger(c), ind.rsi(c), ind.macd(c)
        n = len(bars)
        start = 0 if not self._range else max(0, n - self._range)
        x = np.arange(n)
        dates = list(bars.index)
        for ax in self.ax:
            ax.dates = dates
        up, down = self.pal.up, self.pal.down

        pp = self.p_price
        pp.clear()
        band_hi = pg.PlotDataItem(x[start:], bb["upper"].to_numpy()[start:], pen=pg.mkPen(T.qc(T.BLUE, 90), width=1))
        band_lo = pg.PlotDataItem(x[start:], bb["lower"].to_numpy()[start:], pen=pg.mkPen(T.qc(T.BLUE, 90), width=1))
        pp.addItem(band_hi)
        pp.addItem(band_lo)
        pp.addItem(pg.FillBetweenItem(band_hi, band_lo, brush=T.qc(T.BLUE, 18)))
        candles = CandleItem(o.to_numpy()[start:], h.to_numpy()[start:], l.to_numpy()[start:],
                             c.to_numpy()[start:], up, down)
        candles.setPos(start, 0)
        pp.addItem(candles)
        pp.addLegend(offset=(8, 8), labelTextSize="8pt", brush=T.qc(T.BG, 160), pen=T.qc(T.LINE))
        for series, name, color in ((s20, "SMA20", "#f5b547"), (s50, "SMA50", "#5aa9ff"), (s200, "SMA200", "#c08bff")):
            pp.plot(x[start:], series.to_numpy()[start:], pen=pg.mkPen(color, width=1.3), name=name)

        def hline(val, color, text, style=Qt.DashLine):
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return
            line = pg.InfiniteLine(pos=val, angle=0, pen=pg.mkPen(color, width=1, style=style),
                                   label=f"{text} {val:,.2f}", labelOpts={"position": 0.985, "color": color, "fill": T.qc(T.BG, 210),
                                              "movable": False, "anchors": [(1, 1), (1, 1)]})
            pp.addItem(line)
        src = "" if v.levels_src == "user" else "≈"
        secret = bool(v.qty) and T.PRIVATE      # cost-anchored lines and own trades would reveal the position
        if not secret:
            hline(v.stop, down, f"止损{src}")
            hline(v.target, up, f"目标{src}")
        if v.qty and not secret:
            hline(v.avg_cost, T.SUB, "成本", Qt.DotLine)
        if self.plan is not None and not T.PRIVATE:        # levels the user wrote into their own plan
            lv = self.plan.chart_levels(self.symbol)       # converted when the plan follows another stock
            if lv:
                ap = "≈" if lv["approx"] else ""
                hline(lv["exit"], T.AMBER, f"计划·底线{ap}", Qt.DashDotLine)
                for x in lv["reduce"]:
                    hline(x, T.BLUE, f"计划·减仓{ap}", Qt.DashDotLine)
                for x in lv["buy"]:
                    hline(x, T.BLUE, f"计划·买入{ap}", Qt.DashDotLine)
                hline(lv["target"], T.AMBER, f"计划·目标{ap}", Qt.DashDotLine)

        # the user's own executions
        day_pos = {d: i for i, d in enumerate(dates)}
        for side, sym, color in (("BUY", "t1", up), ("SELL", "t", down)):
            pts = [(day_pos[trade_day(t.ts)], t.price) for t in ([] if T.PRIVATE else self.trades)
                   if t.side == side and trade_day(t.ts) in day_pos and day_pos[trade_day(t.ts)] >= start]
            if pts:
                pp.addItem(pg.ScatterPlotItem([p[0] for p in pts], [p[1] for p in pts], symbol=sym, size=13,
                                              brush=pg.mkBrush(color), pen=pg.mkPen("#ffffff", width=1),
                                              name="我的买入" if side == "BUY" else "我的卖出"))

        rg = v.analysis.range if v.analysis is not None else None
        if DetailWindow.show_range and rg is not None:
            self._draw_range(rg, start, n, up, down)

        pr = self.p_rsi
        pr.clear()
        pr.plot(x[start:], rsi.to_numpy()[start:], pen=pg.mkPen("#f5b547", width=1.3))
        for lvl, color in ((70, down), (30, up)):
            pr.addItem(pg.InfiniteLine(pos=lvl, angle=0, pen=pg.mkPen(T.qc(color, 150), width=1, style=Qt.DashLine)))
        pr.setYRange(0, 100, padding=0.02)

        pm = self.p_macd
        pm.clear()
        hist = macd["hist"].to_numpy()[start:]
        xs = x[start:]
        ok = ~np.isnan(hist)
        pm.addItem(pg.BarGraphItem(x=xs[ok], height=hist[ok], width=0.7,
                                   brushes=[pg.mkBrush(T.qc(up if v_ >= 0 else down, 170)) for v_ in hist[ok]], pen=None))
        pm.plot(xs, macd["macd"].to_numpy()[start:], pen=pg.mkPen("#5aa9ff", width=1.2))
        pm.plot(xs, macd["signal"].to_numpy()[start:], pen=pg.mkPen("#f5b547", width=1.2))

        lo = float(np.nanmin(l.to_numpy()[start:]))
        hi = float(np.nanmax(h.to_numpy()[start:]))
        for val in (() if secret else (v.stop, v.target, v.avg_cost if v.qty else None)):
            if val and lo * 0.6 < val < hi * 1.6:
                lo, hi = min(lo, val), max(hi, val)
        pad = (hi - lo) * 0.06
        pp.setXRange(start - 1, n + 2, padding=0)
        pp.setYRange(lo - pad, hi + pad, padding=0)
        pm.enableAutoRange(axis="y")

    def _draw_range(self, rg, start: int, n: int, up: str, down: str) -> None:
        """Support / resistance, buy-low & sell-high zones and the rule's past signals."""
        pp = self.p_price
        if rg.buy_band:
            x0 = max(rg.start_i, start)
            xs = np.array([x0, n + 1], dtype=float)

            def band(lo_v, hi_v, color):
                a = pg.PlotDataItem(xs, [lo_v, lo_v], pen=pg.mkPen(None))
                b = pg.PlotDataItem(xs, [hi_v, hi_v], pen=pg.mkPen(None))
                pp.addItem(a)
                pp.addItem(b)
                pp.addItem(pg.FillBetweenItem(a, b, brush=T.qc(color, 34)))
            band(*rg.buy_band, up)
            band(*rg.sell_band, down)
            for val, color, text, style, width in (
                    (rg.support, up, "支撑", Qt.SolidLine, 1.6), (rg.resistance, down, "阻力", Qt.SolidLine, 1.6),
                    (rg.stop, T.SUB, "区间失效", Qt.DotLine, 1.0), (rg.breakout, T.SUB, "突破", Qt.DotLine, 1.0)):
                pp.plot(xs, [val, val], pen=pg.mkPen(color, width=width, style=style))
                if style == Qt.SolidLine:      # dotted invalidation lines stay unlabelled (values are in the table)
                    lbl = pg.TextItem(f"{text} {val:,.2f}", color=color, anchor=(0, 1 if text == "阻力" else 0))
                    lbl.setPos(x0, val)
                    pp.addItem(lbl)
        groups = {"buy": ("t1", up, "区间低吸"), "exit": ("t", down, "区间高抛 / 离场")}
        for key, (sym, color, name) in groups.items():
            kinds = ("buy",) if key == "buy" else ("sell", "stop", "time")
            pts = [(e.i, e.price) for e in rg.events if e.kind in kinds and e.i >= start]
            if pts:
                pp.addItem(pg.ScatterPlotItem([p[0] for p in pts], [p[1] for p in pts], symbol=sym, size=10,
                                              brush=pg.mkBrush(T.qc(color, 60)), pen=pg.mkPen(color, width=1.4),
                                              name=name))
