"""Session simulator: does the strategy survive a whole trading day?

Every other piece of verification in this project checks *plumbing* — that the
adapter calls easytrader correctly, that the guard refuses bad orders, that the
CLI emits JSON. None of it answers the question the user actually cares about:

    if I run a 3% grid on 5万 for a week, do I make money or bleed out?

This module answers it offline, with no client and no network:

* a synthetic price path (mean-reverting / trending up / trending down) whose
  volatility is calibrated to a cross-border ETF like 513050 (~28% annualised);
* the REAL `GridStrategy` and the REAL `RiskGuard`, tick by tick, so the thing
  under test is the shipped code, not a re-implementation;
* a REAL cost model including the **¥5 minimum commission** that dominates a
  small account, charged on every fill;
* round-trip accounting, gross vs net P&L, fees paid, max drawdown.

The honest framing it enforces: a grid's gross P&L is not the point. What matters
is net after the minimum commission, and what happens when the price trends out
of the band instead of oscillating.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .broker.mock import MockBroker
from .models import SIDE_BUY, SIDE_SELL
from .risk import RiskGuard
from .strategies.base import StrategyContext
from .strategies.grid import GridStrategy


# --------------------------------------------------------------------------
# cost model
# --------------------------------------------------------------------------

@dataclass
class CostModel:
    """Chinese A-share ETF costs. No stamp duty and no transfer fee on ETFs;
    what actually bites a small account is the per-order minimum commission."""

    commission_rate: float = 0.00015   # 万1.5, one side
    min_commission: float = 5.0        # 单笔最低 5 元 (most brokers)
    exchange_fee_rate: float = 0.00004  # 上交所竞价经手费 0.004%, two-way

    def fee(self, amount: float) -> float:
        commission = max(amount * self.commission_rate, self.min_commission)
        return round(commission + amount * self.exchange_fee_rate, 2)

    def describe(self) -> Dict[str, float]:
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "exchange_fee_rate": self.exchange_fee_rate,
        }


# --------------------------------------------------------------------------
# price paths
# --------------------------------------------------------------------------

def make_price_path(
    start: float,
    ticks: int,
    scenario: str = "range",
    annual_vol: float = 0.28,
    ticks_per_day: int = 48,          # ~5-minute bars over a 4-hour session
    drift_annual: float = 2.0,        # for the trending scenarios
    seed: Optional[int] = 7,
) -> List[float]:
    """Synthetic intraday+multiday price path.

    * ``range``  — mean-reverting around the start (the regime a grid is built for)
    * ``up``     — persistent uptrend (grid sells out and "飞仓")
    * ``down``   — persistent downtrend (grid buys all the way down)

    ``drift_annual`` applies to the two trending scenarios. It defaults to
    200%/yr because the simulated window is short (480 ticks ≈ 10 sessions): at
    a realistic-looking 60%/yr the drift over ten sessions (~2%) is drowned by
    the noise, so "down" was indistinguishable from "up" — a bug this simulator
    exposed on its first run. Calibrate it with ``ticks`` if you lengthen the run.
    """
    rng = random.Random(seed)
    steps_per_year = ticks_per_day * 244
    dt = 1.0 / steps_per_year
    sigma = annual_vol * math.sqrt(dt)

    prices: List[float] = []
    px = float(start)
    anchor = float(start)
    # Mean-reversion strength for the 'range' scenario. Half-life ≈ 230 ticks
    # (~5 sessions at 48 ticks/day), so the price is free to wander across the
    # grid levels over days rather than being snapped back every bar. A much
    # stronger pull (0.02, half-life under a day) kept the price glued to the
    # anchor and the grid almost never traded — which made 'range' a misleading
    # name for what was really a flat line.
    kappa = 0.003

    trend = {"range": 0.0, "up": abs(drift_annual), "down": -abs(drift_annual)}.get(scenario, 0.0)
    mu = trend * dt

    for _ in range(ticks):
        shock = rng.gauss(0.0, sigma)
        px *= math.exp(mu + shock)
        if scenario == "range":
            px += kappa * (anchor - px)
        px = max(px, 0.01)
        prices.append(round(px, 3))
    return prices


# --------------------------------------------------------------------------
# the simulation
# --------------------------------------------------------------------------

@dataclass
class SimResult:
    scenario: str
    ticks: int
    start_price: float
    end_price: float
    starting_equity: float          # cash + starting position marked at start price
    fees_paid: float
    buys: int
    sells: int
    round_trips: int
    filled_notional: float
    equity_curve: List[float] = field(default_factory=list)
    max_drawdown_pct: float = 0.0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.starting_equity

    @property
    def net_pnl(self) -> float:
        """Equity change against the *full* starting equity (cash + holdings).

        Against starting cash alone this number is meaningless whenever a
        starting position exists — the first version made it because the
        opening position was counted as profit. `simulate` exposed that.
        """
        return round(self.final_equity - self.starting_equity, 2)

    @property
    def gross_pnl(self) -> float:
        return round(self.net_pnl + self.fees_paid, 2)

    @property
    def fee_drag_pct(self) -> float:
        if not self.gross_pnl:
            return 0.0
        return round(self.fees_paid / abs(self.gross_pnl) * 100, 1)

    def to_dict(self) -> Dict[str, object]:
        return {
            "scenario": self.scenario,
            "ticks": self.ticks,
            "start_price": self.start_price,
            "end_price": self.end_price,
            "starting_equity": round(self.starting_equity, 2),
            "final_equity": round(self.final_equity, 2),
            "gross_pnl": self.gross_pnl,
            "fees_paid": round(self.fees_paid, 2),
            "net_pnl": self.net_pnl,
            "net_pnl_pct": round(self.net_pnl / self.starting_equity * 100, 2) if self.starting_equity else 0.0,
            "fee_drag_pct_of_gross": self.fee_drag_pct,
            "buys": self.buys,
            "sells": self.sells,
            "round_trips": self.round_trips,
            "filled_notional": round(self.filled_notional, 2),
            "max_drawdown_pct": self.max_drawdown_pct,
        }


def simulate_strategy(
    strategy,
    code: str = "513050",
    base_price: float = 1.092,
    starting_cash: float = 50000.0,
    starting_position: int = 18000,
    prices: Optional[List[float]] = None,
    scenario: str = "range",
    ticks: int = 480,
    cost: Optional[CostModel] = None,
    max_position_pct: float = 90.0,
    drift_annual: float = 2.0,
    seed: int = 7,
) -> SimResult:
    """Run ANY strategy object over a price path with real costs and the real guard.

    Shared by the grid and the intraday-T simulator so both are measured the same
    way. ``strategy`` is the shipped class — not a re-implementation.
    """
    cost = cost or CostModel()
    prices = prices or make_price_path(
        base_price, ticks, scenario=scenario, drift_annual=drift_annual, seed=seed
    )

    broker = MockBroker(
        {
            "cash": starting_cash,
            "prices": {code: base_price},
            "names": {code: code},
            "positions": [
                {"code": code, "qty": starting_position,
                 "available": starting_position, "cost": base_price}
            ],
        },
        t0=True,   # 513050-class funds are T+0
    )
    broker.connect()

    guard = RiskGuard(_GuardConfig([code], max_position_pct))
    starting_equity = round(starting_cash + starting_position * base_price, 2)
    result = SimResult(
        scenario=scenario, ticks=len(prices), start_price=base_price,
        end_price=prices[-1], starting_equity=starting_equity,
        fees_paid=0.0, buys=0, sells=0, round_trips=0, filled_notional=0.0,
    )
    open_buys = 0

    for px in prices:
        broker.set_price(code, px)
        pos = broker.position_map().get(code)
        ctx = StrategyContext(
            quotes=broker.quote([code]),
            positions=broker.position_map(),
            cash_available=broker.cash,
            session_label="sim",
        )
        for req in strategy.plan(ctx):
            pos = broker.position_map().get(code)
            try:
                decision = guard.evaluate(
                    req, ctx.quote(code), force=True,
                    position_qty=pos.qty if pos else 0,
                    sellable=pos.available if pos else 0,
                    total_assets=broker.cash + (pos.qty * px if pos else 0),
                )
            except Exception:  # noqa: BLE001 - a rejected intent is a normal outcome
                continue

            filled = broker.buy(decision.req.code, decision.price, decision.qty) \
                if decision.req.side == SIDE_BUY \
                else broker.sell(decision.req.code, decision.price, decision.qty)
            if filled.status != "filled":
                continue

            fee = cost.fee(decision.amount)
            broker.cash = round(broker.cash - fee, 2)
            result.fees_paid = round(result.fees_paid + fee, 2)
            result.filled_notional = round(result.filled_notional + decision.amount, 2)
            if decision.req.side == SIDE_BUY:
                result.buys += 1
                open_buys += 1
            else:
                result.sells += 1
                if open_buys > 0:
                    result.round_trips += 1
                    open_buys -= 1

        p = broker.position_map().get(code)
        equity = broker.cash + (p.qty * px if p else 0.0)
        result.equity_curve.append(round(equity, 2))

    peak = result.equity_curve[0] if result.equity_curve else starting_equity
    worst = 0.0
    for e in result.equity_curve:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    result.max_drawdown_pct = round(worst, 2)
    return result


def simulate_t(
    code: str = "513050",
    base_price: float = 1.092,
    qty: int = 2800,
    trigger_pct: float = 1.5,
    take_profit_pct: float = 1.5,
    t0: bool = True,
    max_legs_per_day: int = 20,
    scenario: str = "range",
    **kwargs,
) -> SimResult:
    """Simulate the intraday-T strategy over ONE session.

    `max_legs_per_day` is raised from its live default of 2 because a simulation
    stands in for a whole path; the live cap exists to stop a runaway day.
    """
    from .strategies.t_intraday import IntradayTStrategy

    strategy = IntradayTStrategy(
        code=code, qty=qty, trigger_pct=trigger_pct,
        take_profit_pct=take_profit_pct, t0=t0, max_legs_per_day=max_legs_per_day,
    )
    return simulate_strategy(
        strategy, code=code, base_price=base_price, scenario=scenario, **kwargs
    )


def simulate_grid(
    code: str = "513050",
    base_price: float = 1.092,
    spacing_pct: float = 3.0,
    qty_per_level: int = 2800,
    levels_up: int = 4,
    levels_down: int = 4,
    starting_cash: float = 50000.0,
    starting_position: int = 18000,
    prices: Optional[List[float]] = None,
    scenario: str = "range",
    ticks: int = 480,
    cost: Optional[CostModel] = None,
    allowed_codes: Optional[List[str]] = None,
    max_position_pct: float = 90.0,
    drift_annual: float = 2.0,
    seed: int = 7,
) -> SimResult:
    """Run the real GridStrategy + RiskGuard against a price path.

    Fees are charged per fill and deducted from cash, so `net_pnl` is already
    net of the ¥5 minimum commission — the number that decides whether a
    small-account grid is viable at all.
    """
    strategy = GridStrategy(
        code=code, base_price=base_price, spacing_pct=spacing_pct,
        levels_up=levels_up, levels_down=levels_down, qty_per_level=qty_per_level,
    )
    return simulate_strategy(
        strategy, code=code, base_price=base_price,
        starting_cash=starting_cash, starting_position=starting_position,
        prices=prices, scenario=scenario, ticks=ticks, cost=cost,
        max_position_pct=max_position_pct, drift_annual=drift_annual, seed=seed,
    )


class _GuardConfig:
    """Minimal duck-typed config for RiskGuard inside the simulator."""

    def __init__(self, allowed_codes: List[str], max_position_pct: float):
        from .config import BrokerConfig, TradingConfig

        self.broker = BrokerConfig(type="mock")
        self.trading = TradingConfig(
            allowed_codes=list(allowed_codes),
            max_order_amount=0.0,       # 0 disables the per-order cap
            max_daily_amount=0.0,       # 0 disables the daily cap
            max_position_pct=max_position_pct,
            min_order_amount=0.0,
        )

    def var_dir(self):
        from pathlib import Path
        return Path(".")

    def state_path(self, name: str):
        from pathlib import Path
        return Path(name)


def compare_cost_regimes(
    commission_rate: float = 0.00015,
    min_commission: float = 5.0,
    **kwargs,
) -> Dict[str, object]:
    """The same run with and without the ¥5 minimum commission.

    This is the decision tool: it shows how much of a small account's grid edge
    the minimum fee eats, and therefore whether renegotiating the fee is worth
    more than tuning the spacing. Any ``cost`` in ``kwargs`` is ignored — this
    function owns the two cost models it compares.
    """
    kwargs.pop("cost", None)
    with_min = simulate_grid(
        cost=CostModel(commission_rate=commission_rate, min_commission=min_commission), **kwargs
    )
    without_min = simulate_grid(
        cost=CostModel(commission_rate=commission_rate, min_commission=0.0), **kwargs
    )
    return {
        "with_min_commission": with_min.to_dict(),
        "without_min_commission": without_min.to_dict(),
        "note": (
            "Same price path, same trades; only the per-order minimum differs. "
            "The gap is what negotiating '免5' is worth on this account size."
        ),
    }
