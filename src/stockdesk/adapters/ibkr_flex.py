"""IBKR Flex Web Service (official, free, inherently read-only: it only returns reports).

Flow (v3), as documented by IBKR:
  1. GET .../FlexWebService/SendRequest?t=<token>&q=<queryId>&v=3   -> ReferenceCode + Url
  2. GET <Url>?q=<ReferenceCode>&t=<token>&v=3                      -> the statement XML
The Flex query should include the "Open Positions" and "Trades" sections.
The token is read from the OS keyring and is never logged or put into error messages.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

from ..i18n import L
from ..credentials import redact
from ..models import BrokerData, Position, Trade
from .base import AdapterError, AdapterUnavailable, BrokerAdapter, HttpGet, http_get

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
_NY = ZoneInfo("America/New_York")


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(s: str) -> float:
    s = (s or "").strip()
    for fmt in ("%Y%m%d;%H%M%S", "%Y%m%d %H%M%S", "%Y-%m-%d;%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y%m%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if "H" not in fmt:
                dt = dt.replace(hour=12)
            return dt.replace(tzinfo=_NY).timestamp()
        except ValueError:
            continue
    return 0.0


def parse_statement(xml_bytes: bytes) -> BrokerData:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise AdapterError(L("Flex 报告不是合法的 XML", "The Flex report is not valid XML")) from None
    data = BrokerData()
    for node in root.iter("OpenPosition"):
        a = node.attrib
        if a.get("assetCategory", "STK") != "STK" or a.get("levelOfDetail", "SUMMARY") not in ("SUMMARY", ""):
            continue
        qty = _num(a.get("position"))
        if not qty:
            continue
        data.positions.append(Position(
            symbol=a.get("symbol", "").upper().replace(" ", "-"), qty=qty,
            avg_cost=_num(a.get("costBasisPrice")), source="IBKR", currency=a.get("currency", "USD"),
            market_price=_num(a.get("markPrice")) or None))
    for node in root.iter("Trade"):
        a = node.attrib
        if a.get("assetCategory", "STK") != "STK":
            continue
        qty = _num(a.get("quantity"))
        side = (a.get("buySell") or ("BUY" if qty > 0 else "SELL")).upper()
        tid = a.get("tradeID") or a.get("transactionID") or f"{a.get('symbol')}-{a.get('dateTime')}-{qty}"
        data.trades.append(Trade(
            id=f"ibkr:{tid}", symbol=a.get("symbol", "").upper().replace(" ", "-"),
            side="BUY" if side.startswith("B") else "SELL", qty=abs(qty), price=_num(a.get("tradePrice")),
            ts=_parse_dt(a.get("dateTime") or a.get("tradeDate", "")), source="IBKR",
            fees=abs(_num(a.get("ibCommission")))))
    for node in root.iter("EquitySummaryByReportDateInBase"):
        data.net_liquidation = _num(node.attrib.get("total")) or data.net_liquidation
        data.cash = _num(node.attrib.get("cash")) if "cash" in node.attrib else data.cash
    return data


class IbkrFlexAdapter(BrokerAdapter):
    name = "ibkr_flex"
    label = L("IBKR (Flex 报告)", "IBKR (Flex report)")
    official = True

    def __init__(self, token: str | None, query_id: str, getter: HttpGet = http_get,
                 poll_seconds: float = 2.0, max_polls: int = 15, sleep=time.sleep):
        self._token, self.query_id = token, str(query_id or "").strip()
        self._get, self._poll, self._max_polls, self._sleep = getter, poll_seconds, max_polls, sleep

    def fetch(self) -> BrokerData:
        if not self._token or not self.query_id:
            raise AdapterUnavailable(L("Flex 未配置：需要 Query ID 和 token（token 保存在 Windows 凭据管理器）", "Flex is not set up: a Query ID and a token are needed (the token is kept in Windows Credential Manager)"))
        try:
            return parse_statement(self._download())
        except AdapterError as e:
            raise type(e)(redact(str(e), self._token)) from None

    def _download(self) -> bytes:
        _, _, body = self._get(f"{SEND_URL}?t={self._token}&q={self.query_id}&v=3")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise AdapterError(L("Flex 返回内容无法解析", "The Flex response could not be parsed")) from None
        if (root.findtext("Status") or "") != "Success":
            raise AdapterError(L(f"Flex 请求被拒绝：{root.findtext('ErrorCode') or ''} "
                               f"{root.findtext('ErrorMessage') or ''}".strip(),
                               f"Flex request rejected: {root.findtext('ErrorCode') or ''} "
                               f"{root.findtext('ErrorMessage') or ''}".strip()))
        code, base = root.findtext("ReferenceCode"), root.findtext("Url")
        if not code or not base or not base.startswith("https://"):
            raise AdapterError(L("Flex 返回缺少 ReferenceCode / Url", "The Flex response is missing ReferenceCode / Url"))
        for _ in range(self._max_polls):
            self._sleep(self._poll)
            _, _, data = self._get(f"{base}?q={code}&t={self._token}&v=3")
            head = data[:600].decode("utf-8", "ignore")
            if "<FlexStatementResponse" in head:
                if "in progress" in head or "<ErrorCode>1019" in head:
                    continue
                try:
                    r = ET.fromstring(data)
                    msg = f"{r.findtext('ErrorCode') or ''} {r.findtext('ErrorMessage') or ''}".strip()
                except ET.ParseError:
                    msg = L("未知错误", "unknown error")
                raise AdapterError(L(f"Flex 报告生成失败：{msg}", f"Flex report failed: {msg}"))
            return data
        raise AdapterUnavailable(L("Flex 报告仍在生成，稍后会自动重试", "The Flex report is still being generated; will retry automatically"))
