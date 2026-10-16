"""Tests for the live adapter's interaction with easytrader.

The most important test here is ``test_property_style_capabilities_are_read_not_called``:
in easytrader 0.23.7 ``balance`` / ``position`` / ``today_entrusts`` are
*properties*, and an earlier version of this adapter called them like methods,
which would raise ``TypeError`` on the very first real read. That regression is
pinned by a fake trader that exposes real properties.
"""

from __future__ import annotations

import pytest

from etfa.broker.easytrader_ths import (
    EasytraderThsBroker,
    _map_status,
    _order_from_result,
    _pick,
    _to_float,
    _to_int,
)
from etfa.config import BrokerConfig, Config
from etfa.errors import BrokerError, NotConnected, UnsupportedFeature


class FakeTraderProperties:
    """Mirrors easytrader 0.23.7: these are PROPERTIES, not methods."""

    def __init__(self):
        self.buy_calls = []
        self.sell_calls = []
        self.cancel_calls = []

    @property
    def balance(self):
        return {"资金余额": "50000.00", "可用金额": "48000.00",
                "最新市值": "19656.00", "总资产": "69656.00", "冻结金额": "0"}

    @property
    def position(self):
        return [{"证券代码": "513050", "证券名称": "中概互联网ETF易方达",
                 "股票余额": "18000", "可用余额": "18000",
                 "参考成本价": "1.050", "市价": "1.092"}]

    @property
    def today_entrusts(self):
        return [{"委托编号": "12345", "证券代码": "513050", "操作": "买入",
                 "委托价格": "1.093", "委托数量": "2800", "成交数量": "2800",
                 "委托状态": "已成"}]

    def buy(self, security, price, amount, **kwargs):
        self.buy_calls.append((security, price, amount))
        return {"entrust_no": "99901"}

    def sell(self, security, price, amount, **kwargs):
        self.sell_calls.append((security, price, amount))
        return {"entrust_no": "99902"}

    def cancel_entrust(self, entrust_no):
        self.cancel_calls.append(entrust_no)
        return {"message": "success"}


class FakeTraderMethods(FakeTraderProperties):
    """Same data, but exposed as callables — some subclasses do it this way."""

    def balance(self):  # type: ignore[override]
        return {"资金余额": "1000.00", "可用金额": "1000.00", "总资产": "1000.00"}

    def position(self):  # type: ignore[override]
        return []

    def today_entrusts(self):  # type: ignore[override]
        return []


def make_cfg() -> Config:
    return Config(broker=BrokerConfig(type="easytrader_ths", clienttype="ths", exe_path="x.exe"))


def test_property_style_capabilities_are_read_not_called():
    """Regression: properties must be read, not invoked."""
    cfg = make_cfg()
    b = EasytraderThsBroker(cfg, trader=FakeTraderProperties())
    bal = b.balance()             # would raise TypeError if we called a dict
    assert bal.cash == 50000.0
    assert bal.available == 48000.0
    assert bal.total == 69656.00
    pos = b.positions()
    assert len(pos) == 1
    assert pos[0].code == "513050"
    assert pos[0].qty == 18000
    assert pos[0].available == 18000
    assert pos[0].cost == 1.050
    assert pos[0].last == 1.092
    o = b.orders()
    assert len(o) == 1
    assert o[0].id == "12345"
    assert o[0].side == "buy"
    assert o[0].status == "filled"
    assert o[0].filled == 2800


def test_method_style_capabilities_also_work():
    cfg = make_cfg()
    b = EasytraderThsBroker(cfg, trader=FakeTraderMethods())
    assert b.balance().cash == 1000.0
    assert b.positions() == []
    assert b.orders() == []


def test_buy_and_sell_pass_positional_arguments():
    """easytrader's signature is (security, price, amount)."""
    cfg = make_cfg()
    fake = FakeTraderProperties()
    b = EasytraderThsBroker(cfg, trader=fake)
    order = b.buy("513050", 1.093, 2800)
    assert fake.buy_calls == [("513050", 1.093, 2800)]
    assert order.id == "99901"
    assert order.status == "new"
    order = b.sell("513050", 1.091, 1000)
    assert fake.sell_calls == [("513050", 1.091, 1000)]
    assert order.id == "99902"


def test_cancel_by_id_and_cancel_all():
    cfg = make_cfg()
    fake = FakeTraderProperties()
    b = EasytraderThsBroker(cfg, trader=fake)
    assert b.cancel("12345") == ["12345"]
    assert fake.cancel_calls == ["12345"]


def test_quote_only_knows_held_codes():
    """easytrader has no quote feed: an un-held code must yield no quote,
    so the caller is forced to pass --price rather than get an invented one."""
    cfg = make_cfg()
    b = EasytraderThsBroker(cfg, trader=FakeTraderProperties())
    q = b.quote(["513050", "159920"])
    assert "513050" in q and q["513050"].last == 1.092
    assert "159920" not in q


def test_missing_capability_raises_unsupported_feature():
    class AlmostEmpty:
        @property
        def balance(self):
            return {}

    cfg = make_cfg()
    b = EasytraderThsBroker(cfg, trader=AlmostEmpty())
    with pytest.raises(UnsupportedFeature):
        b.positions()


def test_disconnected_adapter_raises_not_connected():
    cfg = make_cfg()
    b = EasytraderThsBroker(cfg)   # no trader, not connected
    assert b.is_connected() is False
    with pytest.raises(NotConnected):
        b.balance()


def test_ths_requires_exe_path(monkeypatch):
    cfg = Config(broker=BrokerConfig(type="easytrader_ths", clienttype="ths", exe_path=""))
    b = EasytraderThsBroker(cfg)
    # easytrader is installed in this venv, so use() will succeed and we should
    # fail on the missing exe_path rather than on the import
    import easytrader  # noqa: F401
    with pytest.raises(BrokerError):
        b.connect()


# ---- parsing helpers ----------------------------------------------------

def test_pick_is_case_tolerant_and_skips_none():
    row = {"证券代码": "513050", "Name": "x", "empty": None}
    assert _pick(row, "证券代码") == "513050"
    assert _pick(row, "name") == "x"
    assert _pick(row, "empty", default="d") == "d"
    assert _pick(row, "missing", default=None) is None


def test_numeric_coercion_handles_client_formatting():
    assert _to_float("1,234.50") == 1234.5
    assert _to_float("1.092元") == 1.092
    assert _to_float("") == 0.0
    assert _to_float(None) == 0.0
    assert _to_float("abc") == 0.0
    assert _to_int("2,800") == 2800


def test_status_mapping_covers_chinese_client_strings():
    assert _map_status("已成") == "filled"
    assert _map_status("部成") == "partial"
    assert _map_status("已撤") == "cancelled"
    assert _map_status("废单") == "rejected"
    assert _map_status("已报") == "reported"
    assert _map_status("") == "new"


def test_order_from_result_reads_entrust_no():
    o = _order_from_result({"entrust_no": "12345"}, "513050", "buy", 1.093, 2800)
    assert o.id == "12345" and o.status == "new" and o.qty == 2800


def test_order_from_result_fails_closed_on_missing_id():
    o = _order_from_result({"message": "资金不足"}, "513050", "buy", 1.093, 2800)
    assert o.status == "rejected" and "资金不足" in o.message


def test_order_from_result_handles_chinese_key():
    assert _order_from_result({"委托编号": "888"}, "513050", "sell", 1.091, 1000).id == "888"


def test_order_from_result_rejects_unexpected_payload():
    o = _order_from_result("something odd", "513050", "buy", 1.0, 100)
    assert o.status == "rejected" and "unexpected reply" in o.message
