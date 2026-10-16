"""Geometric grid strategy.

A geometric grid places levels at a fixed *percentage* spacing, which suits
high-volatility cross-border ETFs (513050 span >50% over a year — an
arithmetic grid would be too dense low and too sparse high).

State model
-----------
The grid keeps a ``base`` price and a set of filled levels per side. On each
call it looks at the current price and emits at most one order per crossed
level, then records the fill in ``state`` so the next call does not repeat it.
State is a plain dict, persisted by the CLI to ``var/grid-<code>.json``.

Cost awareness
--------------
A grid only makes money if the spacing clears round-trip friction. This class
does not enforce that (the risk guard and docs do); but ``min_spacing_pct``
default of 2.0% exists because with a ¥5 minimum commission, spacing below
~2% nets almost nothing on small tickets — see docs/COSTS.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..models import OrderRequest, SIDE_BUY, SIDE_SELL
from .base import Strategy, StrategyContext


@dataclass
class GridParams:
    code: str = "513050"
    base_price: float = 1.092      # anchor price; levels are set around it
    spacing_pct: float = 3.0       # percent between adjacent levels
    levels_up: int = 4             # number of sell levels above base
    levels_down: int = 4           # number of buy levels below base
    qty_per_level: int = 2800      # shares per level (2800 @ ~1.09 ≈ ¥3050)
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
        # filled[side] = set of level indices already acted on, as strings
        self.state: Dict[str, List[int]] = {"buy": [], "sell": []}

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
        p = self.params
        q = ctx.quote(p.code)
        if q is None or q.last <= 0:
            return []

        price = q.last
        if p.lower_bound and price < p.lower_bound:
            return []
        if p.upper_bound and price > p.upper_bound:
            return []

        done_buy = set(self.state.get("buy", []))
        done_sell = set(self.state.get("sell", []))
        orders: List[OrderRequest] = []

        sellable = ctx.sellable(p.code)
        cash = ctx.cash_available

        # price dropped through a buy level -> want to BUY (if we can afford it)
        for idx, lvl in enumerate(self.buy_levels()):
            if price <= lvl and idx not in done_buy:
                cost = p.qty_per_level * lvl
                if cost <= cash:
                    orders.append(OrderRequest(code=p.code, side=SIDE_BUY, qty=p.qty_per_level,
                                               price=lvl, reason=f"grid buy L{idx + 1} @ {lvl}"))
                    self.state.setdefault("buy", []).append(idx)
                    cash -= cost
                break  # one level per call keeps behaviour predictable
        else:
            # price rose through a sell level -> want to SELL settled shares
            for idx, lvl in enumerate(self.sell_levels()):
                if price >= lvl and idx not in done_sell:
                    if sellable >= p.qty_per_level:
                        orders.append(OrderRequest(code=p.code, side=SIDE_SELL, qty=p.qty_per_level,
                                                   price=lvl, reason=f"grid sell L{idx + 1} @ {lvl}"))
                        self.state.setdefault("sell", []).append(idx)
                    break

        return orders

    # ---- persistence -----------------------------------------------------

    def dump_state(self) -> dict:
        return {"state": self.state, "params": self.params.__dict__}

    def load_state(self, state: dict) -> None:
        if not state:
            return
        self.state = {"buy": list(state.get("state", {}).get("buy", [])),
                      "sell": list(state.get("state", {}).get("sell", []))}

    def reset(self) -> None:
        self.state = {"buy": [], "sell": []}
