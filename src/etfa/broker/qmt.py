"""QMT (迅投 xtquant) adapter — a declared, not-yet-implemented path.

Kept as a real file so the intent is visible and ``broker.type='qmt'`` fails
with a precise message rather than "unknown broker". 国元证券's QMT access is
reported at roughly ¥500k assets, which is why this is not the default path
for a ¥50k account.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..config import Config
from ..errors import UnsupportedFeature
from ..models import Balance, Order, Position, Quote
from .base import BrokerAdapter


class QmtBroker(BrokerAdapter):
    name = "qmt"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def connect(self) -> None:
        raise UnsupportedFeature(
            "the QMT/xtquant adapter is a stub. It needs (a) a broker that grants QMT "
            "programmatic access — 国元 reports ~¥500k assets — and (b) a Windows QMT "
            "terminal running. Use broker.type='easytrader_ths' otherwise."
        )

    def is_connected(self) -> bool:
        return False

    def balance(self) -> Balance:
        raise UnsupportedFeature("qmt adapter not implemented")

    def positions(self) -> List[Position]:
        raise UnsupportedFeature("qmt adapter not implemented")

    def quote(self, codes: List[str]) -> Dict[str, Quote]:
        raise UnsupportedFeature("qmt adapter not implemented")

    def buy(self, code: str, price: float, qty: int) -> Order:
        raise UnsupportedFeature("qmt adapter not implemented")

    def sell(self, code: str, price: float, qty: int) -> Order:
        raise UnsupportedFeature("qmt adapter not implemented")

    def orders(self, open_only: bool = False) -> List[Order]:
        raise UnsupportedFeature("qmt adapter not implemented")

    def cancel(self, order_id: Optional[str] = None) -> List[str]:
        raise UnsupportedFeature("qmt adapter not implemented")
