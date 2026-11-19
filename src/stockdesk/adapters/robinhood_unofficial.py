"""Robinhood via ``robin_stocks`` - UNOFFICIAL, OPT-IN, off by default.

Robinhood publishes no official read API for equity positions (see README). robin_stocks
talks to the private endpoints used by Robinhood's own apps; using it may conflict with
Robinhood's Terms of Service and can break at any time. The user must enable it explicitly.

Only these read functions are ever called:
    login, get_open_stock_positions, get_symbol_by_url, get_all_stock_orders,
    load_account_profile, logout
No order function of robin_stocks is referenced anywhere in this project (enforced by a test).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..models import BrokerData, Position, Trade
from .base import AdapterError, AdapterUnavailable, BrokerAdapter

_READ_FUNCS = ("login", "get_open_stock_positions", "get_symbol_by_url", "get_all_stock_orders",
               "load_account_profile", "logout")


class _ReadOnly:
    """Proxy that exposes only the allow-listed read functions of robin_stocks."""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name: str):
        if name not in _READ_FUNCS:
            raise AdapterError(f"robin_stocks.{name} 不在只读白名单内")
        return getattr(self._module, name)


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class RobinhoodUnofficialAdapter(BrokerAdapter):
    name = "robinhood_unofficial"
    label = "Robinhood (非官方 robin_stocks)"
    official = False

    def __init__(self, username: str, password_provider: Callable[[], str | None],
                 code_prompt: Callable[[str], str] | None = None, store_session: bool = False,
                 api=None, max_orders: int = 500):
        self.username = username
        self._password_provider = password_provider
        self._code_prompt = code_prompt
        self.store_session = store_session
        self._api = _ReadOnly(api) if api is not None else None
        self._logged_in = False
        self._symbols: dict[str, str] = {}
        self.max_orders = max_orders

    def _rh(self):
        if self._api is not None:
            return self._api
        try:
            import robin_stocks.robinhood as rh
            import robin_stocks.robinhood.authentication as auth
        except ImportError:
            raise AdapterUnavailable("未安装 robin_stocks（pip install robin_stocks）") from None
        if self._code_prompt is not None:
            # robin_stocks asks for SMS/e-mail codes with input(); route that to the GUI instead
            auth.input = lambda prompt="": self._code_prompt(str(prompt))
        self._api = _ReadOnly(rh)
        return self._api

    def _login(self, rh) -> None:
        if self._logged_in:
            return
        if not self.username:
            raise AdapterUnavailable("Robinhood 未配置用户名")
        password = self._password_provider()
        if not password:
            raise AdapterUnavailable("Robinhood 密码未保存（在 数据源 设置里输入，存入 Windows 凭据管理器）")
        try:
            res = rh.login(username=self.username, password=password, store_session=self.store_session)
        except Exception as e:
            raise AdapterError(f"Robinhood 登录失败：{type(e).__name__}") from None
        finally:
            password = None
        if not res:
            raise AdapterUnavailable("Robinhood 登录未成功（密码错误或未在手机上批准设备）")
        self._logged_in = True

    def _symbol(self, rh, url: str) -> str:
        if url not in self._symbols:
            self._symbols[url] = str(rh.get_symbol_by_url(url) or "").upper()
        return self._symbols[url]

    def fetch(self) -> BrokerData:
        rh = self._rh()
        self._login(rh)
        data = BrokerData()
        try:
            for item in rh.get_open_stock_positions() or []:
                if not item:
                    continue
                qty = _num(item.get("quantity"))
                sym = self._symbol(rh, item.get("instrument", ""))
                if qty and sym:
                    data.positions.append(Position(sym, qty, _num(item.get("average_buy_price")), "Robinhood"))
            orders = rh.get_all_stock_orders() or []
            for o in orders[: self.max_orders]:
                if not o or o.get("state") != "filled":
                    continue
                qty = _num(o.get("cumulative_quantity") or o.get("quantity"))
                price = _num(o.get("average_price") or o.get("price"))
                sym = self._symbol(rh, o.get("instrument", ""))
                if not (qty and price and sym):
                    continue
                data.trades.append(Trade(
                    f"rh:{o.get('id')}", sym, "BUY" if o.get("side") == "buy" else "SELL", qty, price,
                    _ts(o.get("last_transaction_at") or o.get("updated_at")), "Robinhood", _num(o.get("fees"))))
            try:
                prof = rh.load_account_profile() or {}
                if prof.get("cash") is not None:
                    data.cash = _num(prof.get("cash"))
            except Exception:
                pass
        except AdapterError:
            raise
        except Exception as e:
            self._logged_in = False
            raise AdapterError(f"Robinhood 读取失败：{type(e).__name__}") from None
        return data
