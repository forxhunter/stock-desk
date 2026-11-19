"""Colours, fonts and number formatting shared by all windows."""
from __future__ import annotations

import math

from PySide6.QtGui import QColor, QFont

GREEN = "#2fd08a"
RED = "#ff5c6c"
AMBER = "#f5b547"
BLUE = "#5aa9ff"
TEXT = "#f2f4f8"
SUB = "#9aa3b2"
DIM = "#6b7482"
BG = "#12141a"
PANEL = "#1a1d26"
LINE = "#2a2f3b"

UI_FONT = "Microsoft YaHei UI"
NUM_FONT = "Segoe UI"
ICON_FONT = "Segoe Fluent Icons"

ICON = {"refresh": "", "add": "", "up": "", "down": "", "close": "",
        "bell": "", "hub": "", "more": ""}


ICON["eye"], ICON["eye_off"] = chr(0xE890), chr(0xED1A)

PRIVATE = False        # privacy mode: hide holdings, amounts and P&L from the screen
MASK = "••••"


def qty(x: float) -> str:
    return MASK if PRIVATE else f"{x:g}"


def secret(text: str) -> str:
    """``text`` unless privacy mode is on."""
    return MASK if PRIVATE else text


class Palette:
    """Up / down colours depend on the user's convention (US: green up, CN: red up)."""

    def __init__(self, up_color: str = "green"):
        self.up, self.down = (RED, GREEN) if str(up_color).lower() == "red" else (GREEN, RED)

    def sign(self, x: float) -> str:
        if x is None or (isinstance(x, float) and math.isnan(x)) or abs(x) < 1e-12:
            return SUB
        return self.up if x > 0 else self.down

    def stance(self, stance: str) -> str:
        return {"偏多": self.up, "偏空": self.down, "中性": AMBER}.get(stance, DIM)


def font(size: float, bold: bool = False, family: str = UI_FONT) -> QFont:
    f = QFont(family)
    f.setPointSizeF(size)
    f.setBold(bold)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


def qc(hex_color: str, alpha: int = 255) -> QColor:
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


def money(x: float, signed: bool = False, decimals: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if PRIVATE:
        return "$" + MASK
    s = f"{abs(x):,.{decimals}f}"
    if signed:
        return ("+" if x >= 0 else "−") + "$" + s
    return ("−" if x < 0 else "") + "$" + s


def price(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4f}"


def pct(x: float, decimals: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{'+' if x >= 0 else '−'}{abs(x):.{decimals}f}%"


def compact(x: float) -> str:
    if x is None:
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.1f}{unit}"
    return f"${x:.0f}"


WINDOW_QSS = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-family: "{UI_FONT}"; font-size: 10pt; }}
QTextBrowser {{ background: {BG}; border: none; padding: 6px; selection-background-color: #2f4f7f; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {LINE}; }}
QTabBar::tab {{ background: transparent; color: {SUB}; padding: 9px 18px; border: none; font-size: 10.5pt; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {BLUE}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QPushButton {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ background: #232836; border-color: #3a4152; }}
QPushButton:checked {{ background: #24364f; border-color: {BLUE}; }}
QPushButton#primary {{ background: #2b62b8; border-color: #3a78d6; }}
QPushButton#primary:hover {{ background: #3470cf; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {PANEL}; border: 1px solid {LINE};
  border-radius: 6px; padding: 5px 8px; selection-background-color: #2f4f7f; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {BLUE}; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {LINE}; selection-background-color: #2f4f7f; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 8px; margin-top: 14px; padding: 12px 10px 8px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {SUB}; }}
QCheckBox {{ spacing: 8px; }}
QLabel#hint {{ color: {SUB}; font-size: 9pt; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3a4152; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #3a4152; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSplitter::handle {{ background: {LINE}; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: #2a3550; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px; }}
"""

MENU_QSS = f"""
QMenu {{ background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px;
        font-family: "{UI_FONT}"; font-size: 10pt; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: #2a3550; }}
QMenu::item:disabled {{ color: {DIM}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
"""
