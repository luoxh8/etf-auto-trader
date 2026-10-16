"""Tests for the live adapter's pure parsing helpers.

These do not touch a trading client: they pin the mapping from whatever
easytrader returns onto our dataclasses, which is where a silent bug would
otherwise turn into a wrong order.
"""

from __future__ import annotations

from etfa.broker.easytrader_ths import _map_status, _order_from_result, _pick, _to_float, _to_int


def test_pick_is_case_tolerant_and_skips_none():
    row = {"证券代码": "513050", "Name": "x", "empty": None}
    assert _pick(row, "证券代码") == "513050"
    assert _pick(row, "name") == "x"          # case-insensitive fallback
    assert _pick(row, "empty", default="d") == "d"   # None is treated as absent
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
    assert o.id == "12345"
    assert o.status == "new"
    assert o.qty == 2800


def test_order_from_result_treats_missing_id_as_rejection():
    o = _order_from_result({"message": "资金不足"}, "513050", "buy", 1.093, 2800)
    assert o.id == ""
    assert o.status == "rejected"
    assert "资金不足" in o.message


def test_order_from_result_handles_chinese_key():
    o = _order_from_result({"委托编号": "888"}, "513050", "sell", 1.091, 1000)
    assert o.id == "888"


def test_order_from_result_rejects_unexpected_payload():
    o = _order_from_result("something odd", "513050", "buy", 1.0, 100)
    assert o.status == "rejected"
    assert "unexpected reply" in o.message
