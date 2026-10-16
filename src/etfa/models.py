"""Plain data carriers shared by adapters, strategies and the CLI.

All money/quantity fields are ``float`` for simplicity; A-share ETF amounts
are small enough that binary-float error never reaches a lot boundary, and
every value that goes to the broker is rounded to a whole number of shares
before submission (see ``risk.normalise_qty``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Order lifecycle. Adapters map their raw status strings onto these.
STATUS_NEW = "new"          # accepted, not yet acknowledged by the exchange
STATUS_REPORTED = "reported"  # acknowledged
STATUS_PARTIAL = "partial"  # partially filled
STATUS_FILLED = "filled"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"


@dataclass
class Quote:
    code: str
    name: str = ""
    last: float = 0.0
    bid1: float = 0.0
    ask1: float = 0.0
    bid1_vol: int = 0
    ask1_vol: int = 0
    prev_close: float = 0.0
    ts: str = ""

    @property
    def mid(self) -> float:
        if self.bid1 and self.ask1:
            return round((self.bid1 + self.ask1) / 2, 3)
        return self.last

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mid"] = self.mid
        return d


@dataclass
class Position:
    code: str
    name: str = ""
    qty: int = 0            # total holding (shares)
    available: int = 0      # sellable today
    cost: float = 0.0       # average cost price
    last: float = 0.0       # latest price

    @property
    def market_value(self) -> float:
        return round(self.qty * (self.last or self.cost), 2)


@dataclass
class Balance:
    cash: float = 0.0         # total cash
    available: float = 0.0    # usable for trading today
    withdrawable: float = 0.0  # cash that can actually be withdrawn (T+1 settled)
    market_value: float = 0.0
    total: float = 0.0
    frozen: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRequest:
    """Intent to trade, before any guardrail or normalisation."""

    code: str
    side: str
    qty: Optional[int] = None
    amount: Optional[float] = None   # alternative to qty: notional in CNY
    price: float = 0.0               # 0 => use a reference price (bid1/ask1)
    reason: str = ""


@dataclass
class Order:
    id: str
    code: str
    side: str
    price: float
    qty: int
    filled: int = 0
    status: str = STATUS_NEW
    ts: str = ""
    message: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in (STATUS_NEW, STATUS_REPORTED, STATUS_PARTIAL)

    @property
    def remaining(self) -> int:
        return max(0, self.qty - self.filled)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_open"] = self.is_open
        d["remaining"] = self.remaining
        return d


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def orders_to_dicts(orders: List[Order]) -> List[Dict[str, Any]]:
    return [o.to_dict() for o in orders]
