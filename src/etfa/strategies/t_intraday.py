"""Base-position intraday T strategy.

Two forms, and which one is legal depends on the fund's settlement rule:

* 反T (reverse T) — **sell first, buy back later**. Legal on any fund as long as
  you hold settled (yesterday) shares, because you are selling what is already
  sellable. This is the only form available on a T+1 fund.
* 正T (forward T) — buy first, sell the same day. Requires a **T+0** fund, since
  today's purchase is otherwise not sellable.

The strategy is parameterised by ``t0`` so the caller states the fund's rule
explicitly rather than the code silently assuming one.

State is a small journal: ``open_leg`` (side/price/qty of a leg already sent
today) and ``legs_done_today``. It emits at most one action per call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

from ..models import OrderRequest, SIDE_BUY, SIDE_SELL
from .base import Strategy, StrategyContext


@dataclass
class TParams:
    code: str = "513050"
    qty: int = 2800              # shares per leg
    trigger_pct: float = 1.5     # move from the day's reference that triggers a leg
    take_profit_pct: float = 1.5 # pullback/rebound from a leg's fill to close it
    t0: bool = True              # True: 正T allowed (cross-border ETF). False: 反T only.
    max_legs_per_day: int = 2
    max_leg_ticks: int = 12      # TIME STOP: force-close a leg after this many
    #   observations. Without it a leg whose profit target is never reached stays
    #   open forever and the strategy does nothing for the rest of the session —
    #   the "T飞" risk this project warns about, but actually implemented. A stock
    #   that only rises after you sold never gives a buy-back price, so a price
    #   target alone is not enough.
    stop_loss_pct: float = 3.0   # force-close a leg if it moves this far against us


class IntradayTStrategy(Strategy):
    name = "t"

    def __init__(
        self,
        code: str = "513050",
        qty: int = 2800,
        trigger_pct: float = 1.5,
        take_profit_pct: float = 1.5,
        t0: bool = True,
        max_legs_per_day: int = 2,
        max_leg_ticks: int = 12,
        stop_loss_pct: float = 3.0,
    ):
        self.params = TParams(code=code, qty=qty, trigger_pct=trigger_pct,
                              take_profit_pct=take_profit_pct, t0=t0,
                              max_legs_per_day=max_legs_per_day,
                              max_leg_ticks=max_leg_ticks,
                              stop_loss_pct=stop_loss_pct)
        self.day: str = date.today().isoformat()
        self.ref_price: Optional[float] = None   # first seen price of the day
        self.open_leg: Optional[dict] = None      # {"side","price","qty","ticks"}
        self.legs_done: int = 0

    def _roll_day(self, price: float) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day = today
            self.ref_price = None
            self.open_leg = None
            self.legs_done = 0

    def plan(self, ctx: StrategyContext) -> List[OrderRequest]:
        p = self.params
        q = ctx.quote(p.code)
        if q is None or q.last <= 0:
            return []
        price = q.last
        self._roll_day(price)
        if self.ref_price is None:
            self.ref_price = price
            return []

        # 1) manage an open leg
        if self.open_leg is not None:
            leg = self.open_leg
            leg["ticks"] += 1
            target_hit = False
            if leg["side"] == SIDE_BUY:
                target = round(leg["price"] * (1 + p.take_profit_pct / 100), 3)
                target_hit = price >= target
            else:
                target = round(leg["price"] * (1 - p.take_profit_pct / 100), 3)
                target_hit = price <= target

            # price-based exit first
            if target_hit:
                return self._close_leg(ctx, leg, price, "止盈")

            # TIME STOP: the target never came, so cut the leg and move on
            timed_out = leg["ticks"] >= p.max_leg_ticks
            # STOP LOSS: it moved against us too far
            stopped = (
                leg["side"] == SIDE_BUY
                and price <= leg["price"] * (1 - p.stop_loss_pct / 100)
            ) or (
                leg["side"] == SIDE_SELL
                and price >= leg["price"] * (1 + p.stop_loss_pct / 100)
            )
            if timed_out or stopped:
                why = "超时止损" if timed_out else "价格止损"
                return self._close_leg(ctx, leg, price, why)
            return []

        # 2) open a new leg
        if self.legs_done >= p.max_legs_per_day:
            return []
        ref = self.ref_price
        drop = (ref - price) / ref * 100
        rise = (price - ref) / ref * 100

        if rise >= p.trigger_pct and ctx.sellable(p.code) >= p.qty:
            self.open_leg = {"side": SIDE_SELL, "price": price, "qty": p.qty, "ticks": 0}
            return [OrderRequest(code=p.code, side=SIDE_SELL, qty=p.qty, price=price,
                                 reason=f"反T卖出（相对基准 +{rise:.2f}%）")]
        if drop >= p.trigger_pct and self.params.t0 and ctx.cash_available >= p.qty * price:
            self.open_leg = {"side": SIDE_BUY, "price": price, "qty": p.qty, "ticks": 0}
            return [OrderRequest(code=p.code, side=SIDE_BUY, qty=p.qty, price=price,
                                 reason=f"正T买入（相对基准 -{drop:.2f}%）")]
        return []

    def _close_leg(self, ctx: StrategyContext, leg: dict, price: float, why: str) -> List[OrderRequest]:
        """Unwind an open leg in the opposite direction and count it as done."""
        if leg["side"] == SIDE_BUY:
            if ctx.sellable(self.params.code) < leg["qty"]:
                return []          # cannot sell yet; leave the leg open
            side = SIDE_SELL
        else:
            if ctx.cash_available < leg["qty"] * price:
                return []
            side = SIDE_BUY
        self.open_leg = None
        self.legs_done += 1
        self.ref_price = price     # re-anchor so the next leg measures from here
        return [OrderRequest(code=self.params.code, side=side, qty=leg["qty"],
                             price=price, reason=f"{why}平仓 {leg['price']}→{price}")]

    def dump_state(self) -> dict:
        return {"day": self.day, "ref_price": self.ref_price,
                "open_leg": self.open_leg, "legs_done": self.legs_done, "params": self.params.__dict__}

    def load_state(self, state: dict) -> None:
        if not state:
            return
        self.day = state.get("day", self.day)
        self.ref_price = state.get("ref_price")
        self.open_leg = state.get("open_leg")
        self.legs_done = int(state.get("legs_done", 0))
