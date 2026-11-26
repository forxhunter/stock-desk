"""Analysis centre: portfolio optimisation, behaviour alerts, pre-trade check, research, data sources."""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .. import credentials, paths
from .. import notify
from ..config import Config
from ..engine import Snapshot
from ..i18n import L
from ..quant import optimizer
from ..watchlist import normalize_symbol
from . import render
from . import theme as T
from . import win11

TABS = ["briefing", "portfolio", "alerts", "pretrade", "research", "flow", "congress", "playbook", "sources"]


class NoWheel(QObject):
    """Ignore the mouse wheel on spin boxes / combo boxes unless they were clicked first.

    Otherwise scrolling a settings page with the pointer over a number silently changes it."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel and not obj.hasFocus():
            ev.ignore()
            return True
        return False


def guard_wheel(root: QWidget) -> None:
    f = NoWheel(root)
    for w in root.findChildren(QAbstractSpinBox) + root.findChildren(QComboBox):
        w.setFocusPolicy(Qt.StrongFocus)
        w.installEventFilter(f)


def _browser() -> QTextBrowser:
    b = QTextBrowser()
    b.setOpenLinks(False)
    b.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return b


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(True)
    return lab


class HubWindow(QWidget):
    snooze_requested = Signal(str, float)
    dismiss_requested = Signal(str)
    pretrade_requested = Signal(str, str, float, object)
    settings_saved = Signal()
    plan_edit_requested = Signal(str)
    research_refresh_requested = Signal()
    flow_scan_requested = Signal()

    def __init__(self, pal: T.Palette):
        super().__init__()
        self.pal = pal
        self.snap: Snapshot | None = None
        self.cfg = Config.load()
        self.setWindowTitle(L("分析中心 · Stock Desk", "Analysis hub · Stock Desk"))
        self.setStyleSheet(T.WINDOW_QSS)
        self.resize(1120, 800)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 10)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # -- daily briefing
        self.briefing = _browser()
        self.briefing.anchorClicked.connect(
            lambda u: self.plan_edit_requested.emit(u.toString()[5:]) if u.toString().startswith("plan:") else None)
        self.tabs.addTab(self.briefing, L("每日简报", "Daily briefing"))

        # -- portfolio
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(L("再平衡参考方法", "Rebalancing reference method")))
        self.method = QComboBox()
        for key, label in optimizer.METHODS.items():
            self.method.addItem(label, key)
        self.method.currentIndexChanged.connect(self._render_portfolio)
        bar.addWidget(self.method)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.portfolio = _browser()
        lay.addWidget(self.portfolio)
        self.tabs.addTab(w, L("组合优化", "Portfolio"))

        # -- alerts
        self.alerts = _browser()
        self.alerts.anchorClicked.connect(self._alert_link)
        self.tabs.addTab(self.alerts, L("行为提醒", "Behaviour alerts"))

        # -- pre-trade check
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(_hint(L("下单前先在这里过一遍：输入你<b>打算</b>做的交易，程序用和“行为提醒”相同的规则检查它。"
                              "这里不会下单，也没有任何下单功能。",
                              "Run a trade through here before you place it: enter the trade you <b>plan</b> to make and the program "
                              "checks it with the same rules as \"Behaviour alerts\". It places no orders and has no order function.")))
        form = QHBoxLayout()
        self.pt_symbol = QLineEdit()
        self.pt_symbol.setPlaceholderText(L("代码，如 NVDA", "Symbol, e.g. NVDA"))
        self.pt_symbol.setMaximumWidth(140)
        self.pt_side = QComboBox()
        self.pt_side.addItem(L("买入", "Buy"), "BUY")
        self.pt_side.addItem(L("卖出", "Sell"), "SELL")
        self.pt_qty = QDoubleSpinBox()
        self.pt_qty.setRange(0.0001, 1e7)
        self.pt_qty.setDecimals(4)
        self.pt_qty.setValue(10)
        self.pt_price = QDoubleSpinBox()
        self.pt_price.setRange(0, 1e7)
        self.pt_price.setDecimals(2)
        self.pt_price.setSpecialValueText(L("现价", "Market"))
        btn = QPushButton(L("检查", "Check"))
        btn.setObjectName("primary")
        btn.clicked.connect(self._run_pretrade)
        self.pt_symbol.returnPressed.connect(self._run_pretrade)
        for lab, wid in ((L("股票", "Stock"), self.pt_symbol), (L("方向", "Side"), self.pt_side), (L("股数", "Shares"), self.pt_qty), (L("价格", "Price"), self.pt_price)):
            form.addWidget(QLabel(lab))
            form.addWidget(wid)
            form.addSpacing(8)
        form.addWidget(btn)
        form.addStretch(1)
        lay.addLayout(form)
        self.pretrade = _browser()
        lay.addWidget(self.pretrade)
        self.tabs.addTab(w, L("交易前检查", "Pre-trade check"))

        # -- research
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addStretch(1)
        rb = QPushButton(L("刷新资讯", "Refresh news"))
        rb.clicked.connect(self.research_refresh_requested)
        bar.addWidget(rb)
        lay.addLayout(bar)
        self.research = _browser()
        self.research.anchorClicked.connect(self._open_link)
        lay.addWidget(self.research)
        self.tabs.addTab(w, L("资讯 · 预测市场", "News · odds"))

        # -- unusual-activity scan
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addStretch(1)
        fb = QPushButton(L("立即扫描", "Scan now"))
        fb.clicked.connect(self.flow_scan_requested)
        bar.addWidget(fb)
        lay.addLayout(bar)
        self.flow = _browser()
        self.flow.anchorClicked.connect(self._open_link)
        lay.addWidget(self.flow)
        self.tabs.addTab(w, L("异动扫描", "Unusual-activity scan"))

        # -- trades disclosed by members of Congress
        self.congress = _browser()
        self.congress.anchorClicked.connect(self._open_link)
        self.tabs.addTab(self.congress, L("议员交易", "Congress trades"))

        # -- drawdown playbook
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addStretch(1)
        pe = QPushButton(L("编辑我的预案…", "Edit my playbook…"))
        pe.clicked.connect(self._edit_playbook)
        bar.addWidget(pe)
        lay.addLayout(bar)
        self.playbook = _browser()
        lay.addWidget(self.playbook)
        self.tabs.addTab(w, L("预案", "Playbook"))

        # -- sources / settings
        self.tabs.addTab(self._build_sources(), L("数据源与设置", "Sources && settings"))

    def showEvent(self, e):
        win11.dark_titlebar(int(self.winId()))
        super().showEvent(e)

    def show_tab(self, name: str) -> None:
        if name in TABS:
            self.tabs.setCurrentIndex(TABS.index(name))
        self.show()
        self.raise_()
        self.activateWindow()

    # ---- rendering
    def set_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap
        self._keep_scroll(self.briefing, render.private_html(L("每日简报", "daily briefing")) if T.PRIVATE else
                          render.briefing_html(snap.briefing, self.pal, snap.refs, snap.plans, snap.bottoms))
        self._render_portfolio()
        self._keep_scroll(self.alerts, render.private_html(L("行为提醒", "behaviour alerts")) if T.PRIVATE else
                          render.alerts_html(snap.alerts, snap, float(self.cfg.get("behavior.snooze_days", 7))))
        self._keep_scroll(self.research, render.research_html(snap.news, snap.odds))
        self._keep_scroll(self.flow, render.flow_html(snap.flow, self.pal, snap.rates))
        hot = sum(1 for i in snap.flow.items if i.severity != "info") if snap.flow else 0
        self.tabs.setTabText(TABS.index("flow"), L(f"异动扫描（{hot}）", f"Unusual-activity scan ({hot})") if hot else L("异动扫描", "Unusual-activity scan"))
        self._keep_scroll(self.congress, render.congress_html(snap.congress, self.pal, {v.symbol for v in snap.tickers}))
        self._keep_scroll(self.playbook, render.private_html(L("预案", "playbook")) if T.PRIVATE else render.playbook_html(snap.playbook, self.pal))
        self.status.setHtml(render.sources_html(snap))
        self.tabs.setTabText(TABS.index("alerts"), L(f"行为提醒（{len(snap.alerts)}）", f"Behaviour alerts ({len(snap.alerts)})") if snap.alerts else L("行为提醒", "Behaviour alerts"))

    @staticmethod
    def _keep_scroll(browser: QTextBrowser, html: str) -> None:
        pos = browser.verticalScrollBar().value()
        browser.setHtml(html)
        browser.verticalScrollBar().setValue(pos)

    def _render_portfolio(self) -> None:
        if self.snap is not None and T.PRIVATE:
            self._keep_scroll(self.portfolio, render.private_html(L("组合数据", "portfolio data")))
        elif self.snap is not None:
            self._keep_scroll(self.portfolio, render.portfolio_html(
                self.snap, self.method.currentData(), self.pal, float(self.cfg.get("quant.rebalance_min_trade_pct", 1.0))))

    def _alert_link(self, url: QUrl) -> None:
        s = url.toString()
        if s.startswith("snooze:"):
            self.snooze_requested.emit(s[7:], float(self.cfg.get("behavior.snooze_days", 7)))
        elif s.startswith("dismiss:"):
            self.dismiss_requested.emit(s[8:])

    def _edit_playbook(self) -> None:
        """Open the user's playbook file in their own editor (the program never writes actions into it)."""
        from .. import playbook
        playbook.ensure_template(["^GSPC", "^IXIC"])
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(playbook.path())))

    @staticmethod
    def _open_link(url: QUrl) -> None:
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    # ---- pre-trade
    def prefill_pretrade(self, symbol: str) -> None:
        self.pt_symbol.setText(symbol)
        self.pretrade.setHtml("")
        self.show_tab("pretrade")

    def _run_pretrade(self) -> None:
        sym = normalize_symbol(self.pt_symbol.text())
        if not sym:
            self.pretrade.setHtml(render._note(L("请输入有效的股票代码。", "Enter a valid ticker symbol."), T.AMBER))
            return
        self.pretrade.setHtml(render._note(L("正在检查…", "Checking…")))
        price = self.pt_price.value() or None
        self.pretrade_requested.emit(sym, self.pt_side.currentData(), float(self.pt_qty.value()), price)

    def show_pretrade(self, result) -> None:
        symbol, side, qty, px, checks = result
        self.pretrade.setHtml(render.private_html(L("交易前检查的结果", "pre-trade check result")) if T.PRIVATE else render.pretrade_html(checks, symbol, side, qty, px))

    # ---- sources & settings
    def _build_sources(self) -> QWidget:
        c = self.cfg
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setSpacing(6)
        self.status = QTextBrowser()
        self.status.setMaximumHeight(120)
        lay.addWidget(self.status)
        lay.addWidget(_hint(L("本程序对所有数据源都是<b>只读</b>的：代码里没有任何下单 / 改单 / 撤单调用。"
                              "密码和 token 只保存在 Windows 凭据管理器里，不会写入任何文件或日志。",
                              "This program is <b>read-only</b> for every data source: the code has no order / amend / cancel calls. "
                              "Passwords and tokens are stored only in Windows Credential Manager and are never written to any file or log.")))

        g = QGroupBox(L("IBKR · TWS API（官方，免费）— 推荐", "IBKR · TWS API (official, free) — recommended"))
        f = QFormLayout(g)
        self.tws_on = QCheckBox(L("启用（需要 TWS 或 IB Gateway 正在运行并已登录）", "Enable (needs TWS or IB Gateway running and logged in)"))
        self.tws_on.setChecked(bool(c.get("sources.ibkr_tws.enabled")))
        self.tws_host = QLineEdit(str(c.get("sources.ibkr_tws.host", "127.0.0.1")))
        self.tws_port = QSpinBox()
        self.tws_port.setRange(1, 65535)
        self.tws_port.setValue(int(c.get("sources.ibkr_tws.port", 7497)))
        self.tws_cid = QSpinBox()
        self.tws_cid.setRange(0, 9999)
        self.tws_cid.setValue(int(c.get("sources.ibkr_tws.client_id", 47)))
        f.addRow(self.tws_on)
        f.addRow(L("主机", "Host"), self.tws_host)
        f.addRow(L("端口", "Port"), self.tws_port)
        f.addRow("Client ID", self.tws_cid)
        f.addRow(_hint(L("TWS：File → Global Configuration → API → Settings，勾选 “Enable ActiveX and Socket Clients” "
                         "和 <b>“Read-Only API”</b>。端口：TWS 实盘 7496 / 模拟 7497；IB Gateway 实盘 4001 / 模拟 4002。"
                         "程序始终以 readonly=True 连接。",
                         "TWS: File → Global Configuration → API → Settings, tick \"Enable ActiveX and Socket Clients\" "
                         "and <b>\"Read-Only API\"</b>. Ports: TWS live 7496 / paper 7497; IB Gateway live 4001 / paper 4002. "
                         "The program always connects with readonly=True.")))
        lay.addWidget(g)

        g = QGroupBox(L("IBKR · Flex Web Service（官方，免费，天然只读）— 用于历史成交", "IBKR · Flex Web Service (official, free, read-only by nature) — for trade history"))
        f = QFormLayout(g)
        self.flex_on = QCheckBox(L("启用", "Enable"))
        self.flex_on.setChecked(bool(c.get("sources.ibkr_flex.enabled")))
        self.flex_qid = QLineEdit(str(c.get("sources.ibkr_flex.query_id", "")))
        self.flex_token = QLineEdit()
        self.flex_token.setEchoMode(QLineEdit.Password)
        self.flex_token.setPlaceholderText(L("已保存（留空则不修改）", "Saved (leave blank to keep)") if credentials.has_secret(credentials.KEY_FLEX_TOKEN) else L("粘贴 Flex token", "Paste Flex token"))
        f.addRow(self.flex_on)
        f.addRow("Query ID", self.flex_qid)
        f.addRow("Token", self.flex_token)
        f.addRow(_hint(L("Client Portal 网页：Performance &amp; Reports → Flex Queries。新建一个 Activity Flex Query，"
                         "勾选 <b>Open Positions</b> 和 <b>Trades</b>（XML，周期建议 Last 365 Calendar Days），记下 Query ID；"
                         "再在 Flex Web Service Configuration 里生成 token。token 只能下载报告，不能交易。",
                         "Client Portal website: Performance &amp; Reports → Flex Queries. Create an Activity Flex Query, "
                         "tick <b>Open Positions</b> and <b>Trades</b> (XML, period Last 365 Calendar Days suggested) and note the Query ID; "
                         "then generate a token under Flex Web Service Configuration. The token can only download reports, not trade.")))
        lay.addWidget(g)

        g = QGroupBox(L("IBKR · Client Portal Web API（官方，免费，需本机运行 Client Portal Gateway）", "IBKR · Client Portal Web API (official, free, needs Client Portal Gateway running locally)"))
        f = QFormLayout(g)
        self.cp_on = QCheckBox(L("启用", "Enable"))
        self.cp_on.setChecked(bool(c.get("sources.ibkr_cp.enabled")))
        self.cp_url = QLineEdit(str(c.get("sources.ibkr_cp.base_url", "https://localhost:5000/v1/api")))
        f.addRow(self.cp_on)
        f.addRow(L("地址", "URL"), self.cp_url)
        f.addRow(_hint(L("这个接口本身没有只读权限档位，所以程序在客户端强制：只发 GET，且只访问持仓 / 成交查询这几个路径。"
                         "你在自己的浏览器里登录网关，程序接触不到密码。一般用 TWS API 或 Flex 就够了。",
                         "This API has no read-only permission level of its own, so the program enforces it on the client: GET only, "
                         "and only the positions / trades query paths. You log in to the gateway in your own browser, so the program "
                         "never sees your password. TWS API or Flex is usually enough.")))
        lay.addWidget(g)

        g = QGroupBox("Robinhood")
        f = QFormLayout(g)
        self.rh_csv_on = QCheckBox(L("使用官方导出的账户活动报告 CSV / XLSX（推荐，无需密码）", "Use the official account-activity report CSV / XLSX (recommended, no password)"))
        self.rh_csv_on.setChecked(bool(c.get("sources.robinhood_csv.enabled", True)))
        row = QHBoxLayout()
        imp = QPushButton(L("导入报告（CSV / XLSX）…", "Import report (CSV / XLSX)…"))
        imp.clicked.connect(self._import_csv)
        self.rh_csv_count = QLabel("")
        row.addWidget(imp)
        row.addWidget(self.rh_csv_count)
        row.addStretch(1)
        f.addRow(self.rh_csv_on)
        f.addRow(row)
        f.addRow(_hint(L("Robinhood 网页 / App：Account → Reports and statements → Reports，生成 Account activity 报告（CSV 或 XLSX 都可以）后导入。日期范围请从开户日选起，否则持仓和成本算不准。"
                         "持仓由成交记录按平均成本法重建，所以报告需要覆盖开户以来的全部历史才准确。",
                         "Robinhood website / app: Account → Reports and statements → Reports, generate an Account activity report (CSV or XLSX) and import it. "
                         "Pick the date range from the day you opened the account, otherwise positions and cost will be wrong. "
                         "Positions are rebuilt from trades using average cost, so the report must cover the full history since opening.")))
        self.rh_on = QCheckBox(L("启用非官方接口 robin_stocks（可能违反 Robinhood 服务条款，自行决定）", "Enable the unofficial robin_stocks interface (may violate Robinhood's terms of service; your decision)"))
        self.rh_on.setChecked(bool(c.get("sources.robinhood_unofficial.enabled")))
        self.rh_on.clicked.connect(self._confirm_unofficial)
        self.rh_user = QLineEdit(str(c.get("sources.robinhood_unofficial.username", "")))
        self.rh_pass = QLineEdit()
        self.rh_pass.setEchoMode(QLineEdit.Password)
        self.rh_pass.setPlaceholderText(L("已保存（留空则不修改）", "Saved (leave blank to keep)") if credentials.has_secret(credentials.KEY_RH_PASSWORD) else L("密码（存入 Windows 凭据管理器）", "Password (stored in Windows Credential Manager)"))
        f.addRow(self.rh_on)
        f.addRow(L("用户名", "Username"), self.rh_user)
        f.addRow(L("密码", "Password"), self.rh_pass)
        f.addRow(_hint(L("Robinhood 没有面向普通股票账户的官方只读 API（官方 API 只有加密货币交易 API，"
                         "以及 2026 年推出、带交易权限的 Agentic Trading / MCP）。robin_stocks 调用的是 App 私有接口；"
                         "本程序只调用其中的只读函数（持仓、历史订单），登录时可能需要你在手机上批准设备或输入验证码。",
                         "Robinhood has no official read-only API for regular stock accounts (its official APIs are only the crypto trading API "
                         "and the Agentic Trading / MCP launched in 2026, which has trading permissions). robin_stocks calls the app's private interface; "
                         "this program only calls its read-only functions (positions, order history), and logging in may need you to approve the device on your phone or enter a code.")))
        lay.addWidget(g)

        g = QGroupBox(L("邮件提醒（可选）", "Email alerts (optional)"))
        f = QFormLayout(g)
        self.mail_on = QCheckBox(L("出现筑底信号、到达我在计划里写的价位、标普 / 纳指跌破关键位或出现高级别异动时，给我发邮件",
                                    "Email me on a bottoming signal, a price level from my plan, an S&&P / Nasdaq break of a key level, or high-level unusual activity"))
        self.mail_on.setChecked(bool(c.get("notify.email.enabled")))
        self.mail_to = QLineEdit(str(c.get("notify.email.to", "") or ""))
        self.mail_to.setPlaceholderText(L("收件地址", "Recipient address"))
        self.mail_host = QLineEdit(str(c.get("notify.email.smtp_host", "") or ""))
        self.mail_host.setPlaceholderText(L("例如 smtp.gmail.com / smtp.qq.com / smtp.163.com", "e.g. smtp.gmail.com / smtp.qq.com / smtp.163.com"))
        self.mail_port = QSpinBox()
        self.mail_port.setRange(1, 65535)
        self.mail_port.setValue(int(c.get("notify.email.smtp_port", 587) or 587))
        self.mail_user = QLineEdit(str(c.get("notify.email.username", "") or ""))
        self.mail_user.setPlaceholderText(L("发件邮箱（也是 SMTP 登录名）", "Sender address (also the SMTP login)"))
        self.mail_secret = QLineEdit()
        self.mail_secret.setEchoMode(QLineEdit.Password)
        self.mail_secret.setPlaceholderText(L("已保存（留空则不修改）", "Saved (leave blank to keep)") if credentials.has_secret(notify.KEY_SMTP_SECRET)
                                            else L("邮箱的授权码 / 应用专用密码（存入 Windows 凭据管理器）", "Mailbox authorisation code / app password (stored in Windows Credential Manager)"))
        test = QPushButton(L("保存并发一封测试邮件", "Save and send a test email"))
        test.clicked.connect(self._test_mail)
        f.addRow(self.mail_on)
        f.addRow(L("发到", "Send to"), self.mail_to)
        f.addRow(L("SMTP 服务器", "SMTP server"), self.mail_host)
        f.addRow(L("端口", "Port"), self.mail_port)
        f.addRow(L("发件邮箱", "Sender address"), self.mail_user)
        f.addRow(L("授权码", "Authorisation code"), self.mail_secret)
        f.addRow(test)
        f.addRow(_hint(L("邮件由你自己的邮箱通过 SMTP 发出，只发到上面填的地址；内容只有行情信号，不含持仓和金额。"
                         "Gmail / QQ / 163 需要先在邮箱设置里开启 SMTP 并生成“授权码 / 应用专用密码”，这里填的是它，不是登录密码。"
                         "Outlook / Hotmail 个人邮箱已停用这种登录方式，通常不能用作发件邮箱（可以作收件地址）。",
                         "Email is sent from your own mailbox over SMTP, only to the address above; it contains market signals only, no positions or amounts. "
                         "Gmail / QQ / 163 need SMTP enabled in the mailbox settings and an \"authorisation code / app password\" generated; enter that here, not your login password. "
                         "Personal Outlook / Hotmail mailboxes no longer allow this kind of login and usually cannot be used as the sender (they can be the recipient).")))
        lay.addWidget(g)

        g = QGroupBox(L("显示", "Display"))
        f = QFormLayout(g)
        self.up_color = QComboBox()
        self.up_color.addItem(L("绿涨红跌（美股习惯）", "Green up, red down (US style)"), "green")
        self.up_color.addItem(L("红涨绿跌（A 股习惯）", "Red up, green down (China style)"), "red")
        self.up_color.setCurrentIndex(1 if str(c.get("app.up_color", "green")) == "red" else 0)
        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.3, 1.0)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(float(c.get("app.opacity", 0.86)))
        self.refresh_s = QSpinBox()
        self.refresh_s.setRange(30, 3600)
        self.refresh_s.setValue(int(c.get("app.refresh_seconds", 120)))
        self.equity = QDoubleSpinBox()
        self.equity.setRange(0, 1e9)
        self.equity.setDecimals(0)
        self.equity.setSingleStep(1000)
        self.equity.setSpecialValueText(L("自动（持仓市值合计）", "Auto (total holdings value)"))
        self.equity.setValue(float(c.get("app.account_equity", 0) or 0))
        self.language = QComboBox()
        self.language.addItem(L("跟随系统", "Follow system"), "auto")
        self.language.addItem("中文", "zh")
        self.language.addItem("English", "en")
        cur_lang = str(c.get("app.language", "auto") or "auto").strip().lower()
        self.language.setCurrentIndex(max(0, self.language.findData(cur_lang)))
        f.addRow(L("涨跌颜色", "Up / down colours"), self.up_color)
        f.addRow(L("小组件不透明度", "Widget opacity"), self.opacity)
        f.addRow(L("刷新间隔（秒）", "Refresh interval (s)"), self.refresh_s)
        f.addRow(L("账户总资金（用于仓位计算）", "Account equity (for position sizing)"), self.equity)
        f.addRow(L("界面语言", "Language"), self.language)
        f.addRow(_hint(L("改后需重启程序生效", "Restart the app to apply")))
        lay.addWidget(g)

        row = QHBoxLayout()
        save = QPushButton(L("保存并同步", "Save and sync"))
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        o1 = QPushButton(L("打开 watchlist.yaml", "Open watchlist.yaml"))
        o1.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.WATCHLIST))))
        o2 = QPushButton(L("打开数据文件夹", "Open data folder"))
        o2.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.DATA_DIR))))
        row.addWidget(save)
        row.addWidget(o1)
        row.addWidget(o2)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_hint(L("阈值（追涨 / 杀跌标准、仓位上限、回测成本等）在 data/config.yaml 里修改，"
                              "所有可调项及说明见 config.default.yaml。",
                              "Thresholds (chasing / panic-selling criteria, position cap, backtest costs, etc.) are edited in data/config.yaml; "
                              "all adjustable items are described in config.default.yaml.")))
        lay.addStretch(1)
        self._update_csv_count()

        guard_wheel(host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(host)
        return scroll

    def _update_csv_count(self) -> None:
        n = len([f for f in paths.RH_IMPORT_DIR.iterdir() if f.suffix.lower() in (".csv", ".xlsx")]) \
            if paths.RH_IMPORT_DIR.exists() else 0
        self.rh_csv_count.setText(L(f"已导入 {n} 个文件（{paths.RH_IMPORT_DIR}）", f"{n} file(s) imported ({paths.RH_IMPORT_DIR})") if n else L("尚未导入", "Nothing imported yet"))

    def _import_csv(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, L("选择 Robinhood 账户活动报告", "Select Robinhood account activity report"), str(Path.home() / "Downloads"),
                                                L("Robinhood 报告 (*.csv *.xlsx)", "Robinhood report (*.csv *.xlsx)"))
        if not files:
            return
        paths.RH_IMPORT_DIR.mkdir(parents=True, exist_ok=True)
        for f in files:
            try:
                shutil.copyfile(f, paths.RH_IMPORT_DIR / Path(f).name)
            except OSError as e:
                QMessageBox.warning(self, L("导入失败", "Import failed"), f"{Path(f).name}: {type(e).__name__}")
        self._update_csv_count()
        self._import_summary()

    def _import_summary(self) -> None:
        """Tell the user what the imported reports contain, then sync so every page updates."""
        from ..adapters.robinhood_csv import RobinhoodCsvAdapter
        ad = RobinhoodCsvAdapter(paths.RH_IMPORT_DIR)
        try:
            data = ad.fetch()
        except Exception as e:
            QMessageBox.warning(self, L("导入失败", "Import failed"), L(f"没能读取报告：{e}", f"Could not read the report: {e}"))
            return
        if not data.trades:
            QMessageBox.warning(self, L("没有找到成交", "No trades found"),
                                L("文件已复制，但里面没有识别到买入 / 卖出记录。"
                                  "请确认它是 Robinhood 的 Account activity 报告（CSV 或 XLSX）。",
                                  "The file was copied, but no buy / sell records were recognised in it. "
                                  "Please check that it is a Robinhood Account activity report (CSV or XLSX)."))
            return
        lines = [L(f"读取到 {len(data.trades)} 笔买卖成交，重建出 {len(data.positions)} 个持仓。",
                   f"Read {len(data.trades)} buy / sell trades and rebuilt {len(data.positions)} position(s).")] + \
                [n + L("。", ".") for n in ad.notes] + \
                ["", L("正在同步。稍后可以在这些地方看到：", "Syncing now. You will soon see the result in these places:"),
                 L("· 小组件：Robinhood 的持仓会出现在列表里，角标显示 RH", "· Widget: Robinhood positions appear in the list, with an RH badge"),
                 L("· 本页最上面：Robinhood 那一行的状态", "· Top of this page: the status line for Robinhood"),
                 L("· 行为提醒 / 每日简报：会把这些成交一起算进去", "· Behaviour alerts / daily briefing: these trades are included"),
                 L("· 个股详情图：你的买卖点", "· Stock detail chart: your buy / sell points")]
        QMessageBox.information(self, L("已导入", "Imported"), "\n".join(lines))
        if not self.rh_csv_on.isChecked():
            self.rh_csv_on.setChecked(True)
        self.settings_saved.emit()

    def _confirm_unofficial(self, checked: bool) -> None:
        if not checked:
            return
        r = QMessageBox.warning(
            self, L("启用非官方 Robinhood 接口？", "Enable the unofficial Robinhood interface?"),
            L("robin_stocks 不是 Robinhood 官方提供的接口，它模拟官方 App 调用私有接口：\n\n"
              "· 可能违反 Robinhood 的服务条款，极端情况下账户可能被限制；\n"
              "· 需要你的 Robinhood 用户名和密码（密码只存入 Windows 凭据管理器）；\n"
              "· 接口随时可能失效。\n\n"
              "本程序只调用只读函数，不会下单。更稳妥的做法是使用上面的官方 CSV 导出。\n\n仍然启用吗？",
              "robin_stocks is not an interface provided by Robinhood; it imitates the official app and calls private interfaces:\n\n"
              "· It may violate Robinhood's terms of service, and in extreme cases the account may be restricted;\n"
              "· It needs your Robinhood username and password (the password is stored only in Windows Credential Manager);\n"
              "· The interface may stop working at any time.\n\n"
              "This program only calls read-only functions and places no orders. The safer option is the official CSV export above.\n\nEnable it anyway?"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            self.rh_on.setChecked(False)

    def _save(self, quiet: bool = False) -> None:
        # re-read the file first so settings added to data/config.yaml by hand since this window
        # opened (symbol map, reference stocks, ...) are kept instead of being overwritten
        self.cfg = Config.load()
        c = self.cfg
        c.set("sources.ibkr_tws.enabled", self.tws_on.isChecked())
        c.set("sources.ibkr_tws.host", self.tws_host.text().strip() or "127.0.0.1")
        c.set("sources.ibkr_tws.port", self.tws_port.value())
        c.set("sources.ibkr_tws.client_id", self.tws_cid.value())
        c.set("sources.ibkr_flex.enabled", self.flex_on.isChecked())
        c.set("sources.ibkr_flex.query_id", self.flex_qid.text().strip())
        c.set("sources.ibkr_cp.enabled", self.cp_on.isChecked())
        c.set("sources.ibkr_cp.base_url", self.cp_url.text().strip())
        c.set("sources.robinhood_csv.enabled", self.rh_csv_on.isChecked())
        c.set("sources.robinhood_unofficial.enabled", self.rh_on.isChecked())
        c.set("sources.robinhood_unofficial.username", self.rh_user.text().strip())
        c.set("notify.email.enabled", self.mail_on.isChecked())
        c.set("notify.email.to", self.mail_to.text().strip())
        c.set("notify.email.smtp_host", self.mail_host.text().strip())
        c.set("notify.email.smtp_port", self.mail_port.value())
        c.set("notify.email.username", self.mail_user.text().strip())
        c.set("app.up_color", self.up_color.currentData())
        c.set("app.opacity", round(self.opacity.value(), 2))
        c.set("app.refresh_seconds", self.refresh_s.value())
        c.set("app.account_equity", float(self.equity.value()))
        c.set("app.language", self.language.currentData())
        c.save()
        ok = True
        if self.flex_token.text():
            ok &= credentials.set_secret(credentials.KEY_FLEX_TOKEN, self.flex_token.text().strip())
            self.flex_token.clear()
            self.flex_token.setPlaceholderText(L("已保存（留空则不修改）", "Saved (leave blank to keep)"))
        if self.mail_secret.text():
            ok &= credentials.set_secret(notify.KEY_SMTP_SECRET, self.mail_secret.text().strip())
            self.mail_secret.clear()
            self.mail_secret.setPlaceholderText(L("已保存（留空则不修改）", "Saved (leave blank to keep)"))
        if self.rh_pass.text():
            ok &= credentials.set_secret(credentials.KEY_RH_PASSWORD, self.rh_pass.text())
            self.rh_pass.clear()
            self.rh_pass.setPlaceholderText(L("已保存（留空则不修改）", "Saved (leave blank to keep)"))
        if not ok:
            QMessageBox.warning(self, L("凭据未保存", "Credentials not saved"), L("无法写入 Windows 凭据管理器，token / 密码没有保存。", "Could not write to Windows Credential Manager; the token / password was not saved."))
        self.settings_saved.emit()
        if ok and not quiet:
            QMessageBox.information(self, L("已保存", "Saved"),
                                    L("设置已保存，正在后台同步券商数据。\n"
                                      "IBKR 生成 Flex 报告可能要几十秒，结果见本页最上面的状态行。",
                                      "Settings saved; broker data is syncing in the background.\n"
                                      "IBKR may take tens of seconds to generate the Flex report; see the status line at the top of this page."))

    def _test_mail(self) -> None:
        """Save the mail settings, then send one test message to the address the user typed."""
        self._save(quiet=True)
        s = notify.settings(self.cfg.get("notify.email", {}))
        problem = notify.ready(s)
        if problem:
            QMessageBox.warning(self, L("还不能发送", "Cannot send yet"), problem + L("。", "."))
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            notify.send(s, L("Stock Desk 测试邮件", "Stock Desk test email"),
                        L("这是一封测试邮件。收到它说明邮件提醒已经设置好：\n"
                          "以后盯着的股票出现筑底信号时，会发一封类似的邮件到这个地址。",
                          "This is a test email. If you received it, email alerts are set up:\n"
                          "when a stock you are watching shows a bottoming signal, a similar email will be sent to this address."))
            QApplication.restoreOverrideCursor()
            QMessageBox.information(self, L("已发送", "Sent"), L(f"测试邮件已发到 {s['to']}，请查收（也看一下垃圾邮件箱）。", f"Test email sent to {s['to']}; please check your inbox (and the spam folder)."))
        except notify.NotifyError as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, L("发送失败", "Send failed"), str(e))
