"""Strategy contract and the snapshot it reasons over."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..models import OrderRequest, Position, Quote


@dataclass
class StrategyContext:
    """Everything a strategy may look at. Read-only by convention."""

    quotes: Dict[str, Quote] = field(default_factory=dict)
    positions: Dict[str, Position] = field(default_factory=dict)
    cash_available: float = 0.0
    session_label: str = ""

    def quote(self, code: str) -> Optional[Quote]:
        return self.quotes.get(code)

    def position(self, code: str) -> Optional[Position]:
        return self.positions.get(code)

    def sellable(self, code: str) -> int:
        p = self.positions.get(code)
        return p.available if p else 0

    def held(self, code: str) -> int:
        p = self.positions.get(code)
        return p.qty if p else 0


class Strategy(ABC):
    name = "base"

    @abstractmethod
    def plan(self, ctx: StrategyContext) -> List[OrderRequest]:
        """Return the intents that *should* exist right now.

        Implementations are expected to be idempotent-ish: called twice on the
        same state they should not propose doubling an order, which is why the
        persistent strategies track their own levels in ``state``.
        """

    # Optional state persistence hook so a grid remembers its levels across
    # process invocations (the CLI calls these).
    def dump_state(self) -> dict:
        return {}

    def load_state(self, state: dict) -> None:
        return None
