"""Broker adapter interface.

An adapter is a thin, dumb transport: it normalises whatever the underlying
client returns into the dataclasses in ``models`` and it does **not** apply
policy. All policy (whitelists, size caps, hours) lives in ``risk`` so there
is exactly one place to audit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..models import Balance, Order, Position, Quote


class BrokerAdapter(ABC):
    #: stable identifier, also recorded in the audit log
    name: str = "base"

    @abstractmethod
    def connect(self) -> None:
        """Establish the link, or raise NotConnected/BrokerError."""

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def balance(self) -> Balance: ...

    @abstractmethod
    def positions(self) -> List[Position]: ...

    def position_map(self) -> Dict[str, Position]:
        return {p.code: p for p in self.positions()}

    @abstractmethod
    def quote(self, codes: List[str]) -> Dict[str, Quote]: ...

    @abstractmethod
    def buy(self, code: str, price: float, qty: int) -> Order: ...

    @abstractmethod
    def sell(self, code: str, price: float, qty: int) -> Order: ...

    @abstractmethod
    def orders(self, open_only: bool = False) -> List[Order]: ...

    @abstractmethod
    def cancel(self, order_id: Optional[str] = None) -> List[str]:
        """Cancel one order by id, or all open orders when id is None.

        Returns the list of order ids that were cancelled.
        """

    def close(self) -> None:  # optional
        return None
