"""Strategy layer: pure functions from (state, config) to order intents.

Strategies never talk to a broker. They receive a snapshot and return a list
of ``OrderRequest``; the CLI is what applies the risk guard and submits. This
separation is what makes the strategies testable with no client installed, and
it means the same logic powers ``plan`` (dry-run) and ``run`` (live).

Two strategies ship:

* ``grid``  — geometric grid over a price band. Because 513050-class ETFs are
  T+0 *cross-border* funds, a grid on them can both buy and sell intraday; on a
  T+1 fund the sell leg only ever touches settled (yesterday's) shares.
* ``t``     — base-position intraday T. On a T+1 fund you can only 反T (sell
  settled shares, buy back later); on a T+0 fund both directions are legal.
"""

from .base import Strategy, StrategyContext
from .grid import GridStrategy, GridParams
from .t_intraday import IntradayTStrategy, TParams

__all__ = [
    "Strategy",
    "StrategyContext",
    "GridStrategy",
    "GridParams",
    "IntradayTStrategy",
    "TParams",
    "build",
]


def build(name: str, **kwargs) -> Strategy:
    name = (name or "").lower()
    if name == "grid":
        return GridStrategy(**kwargs)
    if name in ("t", "intraday_t"):
        return IntradayTStrategy(**kwargs)
    raise ValueError(f"unknown strategy '{name}' (have: grid, t)")
