"""Strategy and risk tests. No broker client or network needed."""

from __future__ import annotations

import pytest

from etfa.models import OrderRequest, Position, Quote, SIDE_BUY, SIDE_SELL
from etfa.strategies.base import StrategyContext
from etfa.strategies.grid import GridStrategy
from etfa.strategies.t_intraday import IntradayTStrategy


def ctx(price: float, sellable: int = 0, cash: float = 100000.0, code: str = "513050") -> StrategyContext:
    q = Quote(code=code, last=price, bid1=round(price - 0.001, 3), ask1=round(price + 0.001, 3))
    pos = {code: Position(code=code, qty=sellable, available=sellable, cost=price)}
    return StrategyContext(quotes={code: q}, positions=pos, cash_available=cash)


# ---- grid ---------------------------------------------------------------

def test_grid_levels_are_geometric():
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, levels_up=2, levels_down=2)
    assert g.sell_levels() == [1.03, 1.061]
    assert g.buy_levels() == [0.97, 0.941]


def test_grid_buys_when_price_drops_through_a_level():
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, qty_per_level=1000)
    reqs = g.plan(ctx(price=0.965, cash=10000))
    assert len(reqs) == 1
    assert reqs[0].side == SIDE_BUY
    assert reqs[0].qty == 1000
    # the same level must not fire twice
    assert g.plan(ctx(price=0.965, cash=10000)) == []


def test_grid_sells_only_settled_shares():
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, qty_per_level=1000)
    # price above a sell level but nothing sellable -> no order
    assert g.plan(ctx(price=1.04, sellable=0)) == []
    # with settled shares -> sells
    reqs = g.plan(ctx(price=1.04, sellable=1000))
    assert len(reqs) == 1 and reqs[0].side == SIDE_SELL


def test_grid_respects_upper_and_lower_bounds():
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0,
                     qty_per_level=1000, lower_bound=0.95, upper_bound=1.10)
    assert g.plan(ctx(price=0.90)) == []   # below the floor: sit out
    assert g.plan(ctx(price=1.20, sellable=5000)) == []  # above the ceiling


def test_grid_state_roundtrip():
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, qty_per_level=1000)
    g.plan(ctx(price=0.965, cash=10000))
    saved = g.dump_state()
    g2 = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, qty_per_level=1000)
    g2.load_state(saved)
    assert g2.plan(ctx(price=0.965, cash=10000)) == []


def test_grid_ladder_reports_bounds():
    lad = GridStrategy(code="513050", base_price=1.092, spacing_pct=3.0,
                       levels_up=4, levels_down=4).ladder()
    assert lad["buy_levels"][0] < lad["base_price"] < lad["sell_levels"][0]
    assert len(lad["sell_levels"]) == 4


# ---- intraday T ---------------------------------------------------------

def test_t_needs_a_reference_price_first():
    s = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.5)
    assert s.plan(ctx(price=1.00)) == []   # just records the reference
    assert s.ref_price == 1.00


def test_t_reverse_leg_sells_then_buys_back():
    s = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.0, take_profit_pct=1.0)
    s.plan(ctx(price=1.00, sellable=5000))               # set reference
    sell = s.plan(ctx(price=1.02, sellable=5000))        # +2% -> 反T sell
    assert len(sell) == 1 and sell[0].side == SIDE_SELL
    # selling filled; now sellable is lower but buy-back does not need it
    buy = s.plan(ctx(price=1.00, sellable=4000))         # -2% from leg -> buy back
    assert len(buy) == 1 and buy[0].side == SIDE_BUY
    assert s.legs_done == 1


def test_t_forward_leg_requires_t0():
    t1 = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.0, t0=False)
    t1.plan(ctx(price=1.00, sellable=0))
    assert t1.plan(ctx(price=0.98, sellable=0)) == []    # T+1 fund: cannot 正T

    t0 = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.0, t0=True)
    t0.plan(ctx(price=1.00))
    buy = t0.plan(ctx(price=0.98))
    assert len(buy) == 1 and buy[0].side == SIDE_BUY


def test_t_caps_legs_per_day():
    s = IntradayTStrategy(code="513050", qty=1000, trigger_pct=0.5,
                          take_profit_pct=0.5, max_legs_per_day=1)
    s.plan(ctx(price=1.00, sellable=9000))
    s.plan(ctx(price=1.02, sellable=9000))   # leg 1 open
    s.plan(ctx(price=1.00, sellable=8000))   # close leg 1 -> legs_done=1
    assert s.legs_done == 1
    assert s.plan(ctx(price=1.02, sellable=8000)) == []  # cap reached


# ---- the "stuck leg" regression -----------------------------------------

def test_t_leg_times_out_when_the_target_never_comes():
    """A 反T sell into a rally never sees its buy-back price. Without a time stop
    the leg stays open forever and the strategy does nothing all day — the 'T飞'
    risk. This is the regression that `simulate --strategy t` exposed."""
    s = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.0,
                          take_profit_pct=1.0, max_leg_ticks=3, stop_loss_pct=99.0)
    s.plan(ctx(price=1.00, sellable=9000))            # reference
    sell = s.plan(ctx(price=1.02, sellable=9000))     # open 反T sell
    assert [r.side for r in sell] == [SIDE_SELL]

    # price keeps rising: take-profit (a 1% pullback) never arrives
    for px in (1.03, 1.04, 1.05):
        intents = s.plan(ctx(price=px, sellable=8000))
    # after max_leg_ticks observations the leg must be force-closed
    closes = [r for r in intents if r.side == SIDE_BUY]
    assert closes, "the stuck leg was never force-closed"
    assert s.open_leg is None
    assert s.legs_done == 1


def test_t_is_not_stuck_forever_across_a_monotonic_rise():
    """End-to-end: with a rising path the strategy must keep operating, not
    freeze on the first leg (it did, before the time stop existed)."""
    from etfa.simulate import simulate_t

    res = simulate_t(base_price=1.092, qty=2800, trigger_pct=1.0, take_profit_pct=1.0,
                     scenario="up", ticks=480, starting_position=18000, max_legs_per_day=50)
    assert res.sells >= 2, f"t strategy froze after {res.sells} sells"
    assert res.buys >= 2, "the t strategy never completed a leg in a rally"
    assert res.round_trips >= 2


def test_t_stop_loss_closes_a_losing_leg():
    s = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.0,
                          take_profit_pct=1.0, max_leg_ticks=999, stop_loss_pct=2.0)
    s.plan(ctx(price=1.00, sellable=9000))           # reference
    buy = s.plan(ctx(price=0.98, sellable=9000))     # open 正T buy (down 2%)
    assert [r.side for r in buy] == [SIDE_BUY]
    # on a T+0 fund the shares just bought are sellable, so a stop loss can exit
    intents = s.plan(ctx(price=0.960, sellable=10000))
    assert any(r.side == SIDE_SELL for r in intents), "stop loss never fired"
    assert s.open_leg is None


def test_t_cannot_close_a_leg_it_cannot_fill():
    """A 正T leg on a T+1 fund cannot be sold the same day, so the leg must stay
    open rather than emitting an order the broker would reject."""
    s = IntradayTStrategy(code="513050", qty=1000, trigger_pct=1.0,
                          take_profit_pct=1.0, stop_loss_pct=2.0)
    s.plan(ctx(price=1.00, sellable=9000))
    s.plan(ctx(price=0.98, sellable=9000))           # 正T buy
    # nothing sellable (today's buy has not settled)
    assert s.plan(ctx(price=1.00, sellable=0)) == []
    assert s.open_leg is not None                     # leg correctly still open
