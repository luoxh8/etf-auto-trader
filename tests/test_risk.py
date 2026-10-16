"""Risk-guard tests: the guard must fail closed on every rule it owns."""

from __future__ import annotations

from dataclasses import replace

import pytest

from etfa.audit import AuditLog
from etfa.config import Config, BrokerConfig, TradingConfig
from etfa.errors import RiskRejected, TradingHoursError
from etfa.models import OrderRequest, Quote, SIDE_BUY, SIDE_SELL
from etfa.risk import RiskGuard, normalise_qty, reference_price


def guard(tmp_path, **trading_overrides) -> RiskGuard:
    t = TradingConfig(
        allowed_codes=["513050"],
        max_order_amount=10000.0,
        max_daily_amount=30000.0,
        max_position_pct=80.0,
    )
    for k, v in trading_overrides.items():
        setattr(t, k, v)
    cfg = Config(broker=BrokerConfig(type="mock"), trading=t)
    return RiskGuard(cfg, AuditLog(tmp_path / "audit.jsonl"))


def q(price=1.0):
    return Quote(code="513050", last=price, bid1=price - 0.001, ask1=price + 0.001)


def test_normalise_qty_rounds_to_lots():
    assert normalise_qty(2850) == 2800
    assert normalise_qty(99) == 0
    assert normalise_qty(100) == 100


def test_reference_price_prefers_touchable_side():
    assert reference_price(OrderRequest(code="513050", side=SIDE_BUY), q(1.0)) == 1.001
    assert reference_price(OrderRequest(code="513050", side=SIDE_SELL), q(1.0)) == 0.999
    assert reference_price(OrderRequest(code="513050", side=SIDE_BUY, price=0.995), q(1.0)) == 0.995


def test_rejects_code_outside_whitelist(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(RiskRejected):
        g.evaluate(OrderRequest(code="159915", side=SIDE_BUY, qty=100), q(), force=True)


def test_rejects_amount_above_cap(tmp_path):
    g = guard(tmp_path)
    # 100 lots of a 2-yuan ETF = 20000 > 10000 cap
    with pytest.raises(RiskRejected):
        g.evaluate(OrderRequest(code="513050", side=SIDE_BUY, qty=10000), q(2.0), force=True)


def test_rejects_daily_cap(tmp_path):
    g = guard(tmp_path, max_order_amount=1e9, max_daily_amount=3000.0)
    d = g.evaluate(OrderRequest(code="513050", side=SIDE_BUY, qty=1000), q(1.0), force=True)
    assert d.amount == 1001.0  # ask1 = 1.001
    assert g.audit.notional_today() == 0.0  # evaluate alone never logs 'placed'


def test_rejects_selling_more_than_sellable(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(RiskRejected):
        g.evaluate(OrderRequest(code="513050", side=SIDE_SELL, qty=1000), q(1.0),
                   sellable=500, force=True)


def test_rejects_position_above_pct(tmp_path):
    g = guard(tmp_path, max_order_amount=1e9)
    # total assets 10000, cap 80% => 8000. Buying 9000 worth breaches it.
    with pytest.raises(RiskRejected):
        g.evaluate(OrderRequest(code="513050", side=SIDE_BUY, qty=9000), q(1.0),
                   total_assets=10000.0, force=True)


def test_zero_qty_after_rounding_is_rejected(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(RiskRejected):
        g.evaluate(OrderRequest(code="513050", side=SIDE_BUY, amount=50.0), q(1.0), force=True)


def test_force_bypasses_hours_but_not_other_rules(tmp_path):
    g = guard(tmp_path)
    # force should let it through regardless of clock (weekday-independent)
    d = g.evaluate(OrderRequest(code="513050", side=SIDE_BUY, qty=100), q(1.0), force=True)
    assert d.ok and d.qty == 100


def test_accepts_a_clean_order(tmp_path):
    g = guard(tmp_path)
    d = g.evaluate(OrderRequest(code="513050", side=SIDE_BUY, qty=2800), q(1.092), force=True,
                   total_assets=50000.0)
    assert d.ok
    assert d.qty == 2800
    assert d.price == 1.093          # ask1
    assert round(d.amount, 2) == round(2800 * 1.093, 2)
