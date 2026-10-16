"""Simulator tests.

These pin the three bugs the simulator itself exposed when first run, so they
cannot come back:

1. net P&L must be measured against starting equity (cash + holdings), not
   starting cash — otherwise an opening position looks like profit.
2. the 'down' scenario must actually go down (drift sign).
3. the grid must re-arm, so it keeps trading across a path instead of firing
   once and going dead.
"""

from __future__ import annotations

from etfa.models import Quote, Position
from etfa.simulate import CostModel, compare_cost_regimes, make_price_path, simulate_grid
from etfa.strategies.base import StrategyContext
from etfa.strategies.grid import GridStrategy


def ctx(prices, code="513050", sellable=100000, cash=1_000_000.0):
    """Feed a price series through a strategy, one quote at a time."""
    out = []
    for px in prices:
        q = Quote(code=code, last=px, bid1=round(px - 0.001, 3), ask1=round(px + 0.001, 3))
        pos = {code: Position(code=code, qty=sellable, available=sellable, cost=px)}
        c = StrategyContext(quotes={code: q}, positions=pos, cash_available=cash)
        out.append(c)
    return out


# ---- the three regressions ----------------------------------------------

def test_grid_rearms_and_keeps_trading():
    """A 3% grid must buy, sell the lot back, and buy again as price oscillates."""
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, qty_per_level=100)
    # walk down through L1, back up above it, then down again
    path = [1.00, 0.97, 1.00, 0.97]
    intents = []
    for c in ctx(path):
        intents.extend(g.plan(c))

    sides = [i.side for i in intents]
    assert "buy" in sides and "sell" in sides, f"grid never round-tripped: {sides}"
    # specifically: buy at 0.97, sell back, buy at 0.97 again
    assert sides.count("buy") >= 2, f"grid did not re-arm: {sides}"
    assert sides.count("sell") >= 1, f"grid never took profit: {sides}"


def test_grid_level_alternates_rather_than_latching_forever():
    g = GridStrategy(code="513050", base_price=1.0, spacing_pct=3.0, qty_per_level=100)
    cs = ctx([1.00, 0.96, 1.00, 0.96, 1.00])
    seq = []
    for c in cs:
        seq.extend(i.side for i in g.plan(c))
    assert seq == ["buy", "sell", "buy", "sell"], seq


def test_net_pnl_is_measured_against_starting_equity():
    """With a starting position, P&L must include it or it reads as free profit."""
    prices = [1.092] * 5 + [1.100] * 5      # small move, no grid trigger needed
    res = simulate_grid(base_price=1.092, prices=prices, qty_per_level=10_000,
                        spacing_pct=50.0,        # no triggers: isolate the baseline
                        starting_cash=50000.0, starting_position=18000)
    assert res.starting_equity == round(50000.0 + 18000 * 1.092, 2)
    # price ended ABOVE start with a long position, so equity rose; but net_pnl
    # must NOT equal the whole position value (the old bug's signature)
    assert res.net_pnl < res.starting_equity / 10, "net_pnl looks like it counted the position as profit"


def test_down_scenario_actually_declines():
    p0 = make_price_path(1.0, 480, scenario="down", seed=7)
    p1 = make_price_path(1.0, 480, scenario="up", seed=7)
    assert p0[-1] < p0[0], f"down scenario ended higher: {p0[0]} -> {p0[-1]}"
    assert p1[-1] > p1[0], f"up scenario ended lower: {p1[0]} -> {p1[-1]}"


def test_range_scenario_stays_near_the_anchor():
    p = make_price_path(1.0, 480, scenario="range", seed=7)
    # mean-reverting: does not run away like the trending paths
    assert 0.7 < p[-1] < 1.3, f"range drifted too far: {p[-1]}"


# ---- cost model ----------------------------------------------------------

def test_minimum_commission_dominates_small_tickets():
    cost = CostModel()
    # a 3000-yuan ticket at 万1.5 is 0.45 yuan, but the ¥5 floor applies
    assert cost.fee(3000) == round(5.0 + 3000 * 0.00004, 2)
    # a large ticket escapes the floor
    big = 100_000
    assert cost.fee(big) == round(big * 0.00015 + big * 0.00004, 2)
    # and the floor really is the binding constraint on small size
    assert cost.fee(3000) / 3000 > cost.fee(big) / big


def test_waiving_the_minimum_improves_a_grid():
    """The whole point of the comparison tool, asserted end-to-end."""
    common = dict(base_price=1.092, spacing_pct=3.0, qty_per_level=2800,
                  starting_cash=50000.0, starting_position=18000,
                  scenario="range", ticks=480, seed=7)
    out = compare_cost_regimes(commission_rate=0.00015, min_commission=5.0, **common)
    a = out["with_min_commission"]
    b = out["without_min_commission"]
    assert a["buys"] == b["buys"] and a["sells"] == b["sells"]   # same path, same trades
    assert b["net_pnl"] > a["net_pnl"], "免5 should not be worse on identical trades"
    assert a["fees_paid"] > b["fees_paid"]


def test_trending_regimes_are_where_a_grid_loses():
    """The honest part: a grid is not free money."""
    common = dict(base_price=1.092, spacing_pct=3.0, qty_per_level=2800,
                  starting_cash=50000.0, starting_position=18000, ticks=480, seed=7)
    down = simulate_grid(scenario="down", **common)
    assert down.net_pnl < 0, "a grid that buys into a decline must show a loss"


# ---- the T strategy in the simulator ------------------------------------

def test_simulate_t_runs_and_trades():
    from etfa.simulate import simulate_t

    res = simulate_t(base_price=1.092, qty=2800, trigger_pct=1.0, take_profit_pct=1.0,
                     scenario="range", ticks=480, starting_position=18000)
    assert res.buys > 0 or res.sells > 0, "the T strategy never traded"
    assert res.ticks == 480
    assert res.equity_curve


def test_simulate_t_fees_are_charged_per_leg():
    """Every leg pays the ¥5 floor, so fees must scale with the leg count."""
    from etfa.simulate import simulate_t

    res = simulate_t(base_price=1.092, qty=2800, trigger_pct=1.0, take_profit_pct=1.0,
                     scenario="range", ticks=480, starting_position=18000,
                     max_legs_per_day=50)
    legs = res.buys + res.sells
    assert legs >= 4, f"expected several legs, got {legs}"
    # at least ¥5 per leg (the floor), so fees >= 5 * legs * 0.9 (allow rounding)
    assert res.fees_paid >= 5.0 * legs * 0.9, (
        f"fees {res.fees_paid} look too low for {legs} legs"
    )


# ---- broker-App form conversion ----------------------------------------

def test_grid_app_form_reports_the_range_a_form_wants():
    from etfa.simulate import grid_app_form

    out = grid_app_form(base_price=1.011, spacing_pct=3.0,
                        levels_up=4, levels_down=4, qty_per_level=2800)
    f = out["app_form"]
    assert f["lower_bound"] < 1.011 < f["upper_bound"]
    assert f["grid_count"] == 8
    assert f["qty_per_grid"] == 2800
    # the range must match the geometric ladder it was derived from
    assert f["lower_bound"] == out["ladders"]["geometric_this_project"][0]
    assert f["upper_bound"] == out["ladders"]["geometric_this_project"][-1]


def test_grid_app_form_quantifies_the_geometric_vs_arithmetic_gap():
    """A broker grid form lays out EQUAL PRICE steps; our strategy uses EQUAL
    PERCENT steps. Over a wide range they diverge, and a user who hand-places
    our geometric levels alongside the App's arithmetic ones would double up."""
    from etfa.simulate import grid_app_form

    out = grid_app_form(base_price=1.011, spacing_pct=3.0, levels_up=4, levels_down=4)
    lad = out["ladders"]
    assert len(lad["geometric_this_project"]) == len(lad["arithmetic_app_likely"]) == 9
    assert lad["max_deviation"] > 0, "the two ladders should differ over an 8-grid range"
    assert any("arithmetic" in w and "geometric" in w for w in out["warnings"])


def test_grid_app_form_flags_cheap_spacing_and_fee_drag():
    from etfa.simulate import grid_app_form

    tight = grid_app_form(base_price=1.0, spacing_pct=1.5, qty_per_level=2800)
    assert any("below 2%" in w for w in tight["warnings"])

    # This figure is the fee share of ONE round trip (NOT cumulative fees vs
    # cumulative P&L, which is what makes the simulator show a net loss over a
    # session). The meaningful claim is that it rises as spacing tightens.
    wide = grid_app_form(base_price=1.0, spacing_pct=3.0, qty_per_level=2800)
    assert tight["fees"]["fee_share_of_spacing_pct"] > wide["fees"]["fee_share_of_spacing_pct"]
    # each individual leg must still pay at least the ¥5 floor
    for l in tight["fees"]["per_level"]:
        assert l["fee_one_side"] >= 5.0
    assert not any("below 2%" in w for w in wide["warnings"])


def test_grid_app_form_waiving_minimum_lowers_the_fee_share():
    from etfa.simulate import grid_app_form

    a = grid_app_form(base_price=1.0, spacing_pct=3.0, qty_per_level=2800, min_commission=5.0)
    b = grid_app_form(base_price=1.0, spacing_pct=3.0, qty_per_level=2800, min_commission=0.0)
    assert b["fees"]["est_round_trip_bp"] < a["fees"]["est_round_trip_bp"]
    assert b["fees"]["fee_share_of_spacing_pct"] < a["fees"]["fee_share_of_spacing_pct"]
