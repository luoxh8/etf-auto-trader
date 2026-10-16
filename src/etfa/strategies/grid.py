"""Geometric grid strategy.

A geometric grid places levels at a fixed *percentage* spacing, which suits
high-volatility cross-border ETFs (513050 span >50% over a year — an
arithmetic grid would be too dense low and too sparse high).

State model
-----------
Each level has a **two-state latch** and re-arms automatically, which is what
makes the grid repeat instead of firing once:

* a *buy* level (below the anchor) is ``idle`` until price crosses **down**
  through it, then it buys and latches ``long``; when price crosses back **up**
  through it, it sells that lot and returns to ``idle``.
* a *sell* level (above the anchor) is ``idle`` until price crosses **up**
  through it, then it sells and latches ``short``; when price crosses back
  **down** through it, it buys that lot back and returns to ``idle``.

The previous implementation recorded a level as done forever, so the grid
traded once and then went dead — found by ``etfa simulate``, which is exactly
what that tool is for.

Cost awareness
--------------
A grid only makes money if the spacing clears round-trip friction. With a ¥5
minimum commission, spacing below ~2% nets almost nothing on small tickets —
see docs/COSTS.md and run ``etfa simulate`` to see it on your own numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..models import OrderRequest, SIDE_BUY, SIDE_SELL
from .base import Strategy, StrategyContext

IDLE = 0
LONG = 1    # a buy level that bought and is waiting to sell that lot back
SHORT = -1  # a sell level that sold and is waiting to buy that lot back


@dataclass
class GridParams:
    code: str = "513050"
    base_price: float = 1.092      # anchor price; levels are set around it
    spacing_pct: float = 3.0       # percent between adjacent levels
    levels_up: int = 4             # number of sell levels above base
    levels_down: int = 4           # number of buy levels below base
    qty_per_level: int = 2800      # shares per level
    lower_bound: float = 0.0       # optional hard floor (0 = derived from levels)
    upper_bound: float = 0.0       # optional hard ceiling


class GridStrategy(Strategy):
    name = "grid"

    def __init__(
        self,
        code: str = "513050",
        base_price: float = 1.092,
        spacing_pct: float = 3.0,
        levels_up: int = 4,
        levels_down: int = 4,
        qty_per_level: int = 2800,
        lower_bound: float = 0.0,
        upper_bound: float = 0.0,
        state_path: Optional[str] = None,
    ):
        self.params = GridParams(
            code=code, base_price=base_price, spacing_pct=spacing_pct,
            levels_up=levels_up, levels_down=levels_down, qty_per_level=qty_per_level,
            lower_bound=lower_bound, upper_bound=upper_bound,
        )
        if spacing_pct <= 0:
            raise ValueError("spacing_pct must be > 0")
        self.state_path = Path(state_path) if state_path else None
        # latch per level: 0 idle, LONG for a buy level holding a lot,
        # SHORT for a sell level having sold a lot it must buy back.
        self.buy_state: List[int] = [IDLE] * self.params.levels_down
        self.sell_state: List[int] = [IDLE] * self.params.levels_up

    # ---- level maths (pure, exposed for tests) ---------------------------

    def sell_levels(self) -> List[float]:
        return [round(self.params.base_price * (1 + self.params.spacing_pct / 100) ** k, 3)
                for k in range(1, self.params.levels_up + 1)]

    def buy_levels(self) -> List[float]:
        return [round(self.params.base_price * (1 - self.params.spacing_pct / 100) ** k, 3)
                for k in range(1, self.params.levels_down + 1)]

    def ladder(self) -> dict:
        return {
            "code": self.params.code,
            "base_price": self.params.base_price,
            "spacing_pct": self.params.spacing_pct,
            "qty_per_level": self.params.qty_per_level,
            "buy_levels": self.buy_levels(),
            "sell_levels": self.sell_levels(),
            "lower_bound": self.params.lower_bound or (self.buy_levels()[-1] if self.params.levels_down else self.params.base_price),
            "upper_bound": self.params.upper_bound or (self.sell_levels()[-1] if self.params.levels_up else self.params.base_price),
        }

    # ---- planning --------------------------------------------------------

    def plan(self, ctx: StrategyContext) -> List[OrderRequest]:
        """Return at most one intent per call.

        Semantics are **level-based with an auto-unlatching latch**, which is
        what makes the grid repeat:

        * a buy level below the anchor: while ``state == IDLE`` and price is at
          or below the level, buy and latch ``LONG``; while ``LONG`` and price
          rises back above the level, sell that lot and unlatch to ``IDLE``.
        * a sell level above the anchor: while ``IDLE`` and price is at or above
          the level, sell and latch ``SHORT``; while ``SHORT`` and price falls
          back below, buy the lot back and unlatch.

        The earlier implementation recorded a level as "done" forever, so the
        grid traded once and then went dead. `etfa simulate` caught that. A
        first attempt to fix it with price-crossing detection needed the
        previous tick and mis-handled an exact touch of a level; the latch
        above needs neither and re-arms naturally.
        """
        p = self.params
        q = ctx.quote(p.code)
        if q is None or q.last <= 0:
            return []

        price = q.last
        if p.lower_bound and price < p.lower_bound:
            return []
        if p.upper_bound and price > p.upper_bound:
            return []

        sellable = ctx.sellable(p.code)
        cash = ctx.cash_available
        orders: List[OrderRequest] = []

        # BUY levels (below anchor): buy the dip, sell the lot back on recovery.
        for idx, lvl in enumerate(self.buy_levels()):
            if self.buy_state[idx] == IDLE:
                if price <= lvl and p.qty_per_level * lvl <= cash:
                    self.buy_state[idx] = LONG
                    orders.append(OrderRequest(code=p.code, side=SIDE_BUY, qty=p.qty_per_level,
                                               price=lvl, reason=f"grid buy L{idx + 1} @ {lvl}"))
                    return orders
            elif self.buy_state[idx] == LONG:
                if price > lvl and sellable >= p.qty_per_level:
                    self.buy_state[idx] = IDLE
                    orders.append(OrderRequest(code=p.code, side=SIDE_SELL, qty=p.qty_per_level,
                                               price=lvl, reason=f"grid take-profit L{idx + 1} @ {lvl}"))
                    return orders

        # SELL levels (above anchor): sell the rip, buy the lot back on pullback.
        for idx, lvl in enumerate(self.sell_levels()):
            if self.sell_state[idx] == IDLE:
                if price >= lvl and sellable >= p.qty_per_level:
                    self.sell_state[idx] = SHORT
                    orders.append(OrderRequest(code=p.code, side=SIDE_SELL, qty=p.qty_per_level,
                                               price=lvl, reason=f"grid sell H{idx + 1} @ {lvl}"))
                    return orders
            elif self.sell_state[idx] == SHORT:
                if price < lvl and p.qty_per_level * lvl <= cash:
                    self.sell_state[idx] = IDLE
                    orders.append(OrderRequest(code=p.code, side=SIDE_BUY, qty=p.qty_per_level,
                                               price=lvl, reason=f"grid buy-back H{idx + 1} @ {lvl}"))
                    return orders

        return orders

    # ---- persistence -----------------------------------------------------

    def dump_state(self) -> dict:
        return {
            "buy_state": list(self.buy_state),
            "sell_state": list(self.sell_state),
            "params": self.params.__dict__,
        }

    def load_state(self, state: dict) -> None:
        if not state:
            return
        bs = state.get("buy_state")
        ss = state.get("sell_state")
        if isinstance(bs, list) and len(bs) == len(self.buy_state):
            self.buy_state = [int(x) for x in bs]
        if isinstance(ss, list) and len(ss) == len(self.sell_state):
            self.sell_state = [int(x) for x in ss]

    def reset(self) -> None:
        self.buy_state = [IDLE] * self.params.levels_down
        self.sell_state = [IDLE] * self.params.levels_up

