"""Offline mock broker.

Purpose: let the entire CLI, strategy engine and risk guard be exercised with
**no brokerage client installed and no network**. It keeps an in-memory book
that behaves like a real A-share account (cash, positions, available vs total,
T+1 sellable), and it can be seeded from JSON so tests are deterministic.

This is also the adapter used by ``etfa --broker mock`` for dry runs, which is
how the tool should be evaluated before it is ever pointed at a live account.
"""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..errors import NotConnected
from ..models import (
    Balance,
    Order,
    Position,
    Quote,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_FILLED,
    STATUS_REPORTED,
    now_iso,
)
from .base import BrokerAdapter

_counter = itertools.count(1)


class MockBroker(BrokerAdapter):
    name = "mock"

    def __init__(self, seed: Optional[dict] = None, fill_immediately: bool = True,
                 persist_path: Optional[str] = None):
        seed = seed or {}
        self.cash: float = float(seed.get("cash", 50000.0))
        self._connected = False
        self.fill_immediately = fill_immediately
        self._prices: Dict[str, float] = dict(seed.get("prices", {"513050": 1.092}))
        self._names: Dict[str, str] = dict(seed.get("names", {"513050": "中概互联网ETF易方达"}))
        self._positions: Dict[str, Position] = {}
        for p in seed.get("positions", []):
            self._positions[p["code"]] = Position(
                code=p["code"],
                name=p.get("name", ""),
                qty=int(p.get("qty", 0)),
                available=int(p.get("available", p.get("qty", 0))),
                cost=float(p.get("cost", self._prices.get(p["code"], 1.0))),
            )
        self._orders: Dict[str, Order] = {}
        self.calls: List[tuple] = []  # audit of what the adapter was asked to do
        # orders must not collide across separate CLI processes, so the id
        # carries a per-process time prefix instead of a bare counter
        self._id_prefix = f"M{int(time.time() % 100000):05d}"
        self.persist_path = Path(persist_path) if persist_path else None
        if self.persist_path and self.persist_path.exists():
            self._load_persisted()

    @classmethod
    def from_file(cls, path: str) -> "MockBroker":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- persistence (so a mock "account" survives across CLI calls) -------

    def _load_persisted(self) -> None:
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.cash = float(data.get("cash", self.cash))
        self._prices.update({k: float(v) for k, v in (data.get("prices") or {}).items()})
        self._names.update(data.get("names") or {})
        for p in data.get("positions", []):
            self._positions[p["code"]] = Position(
                code=p["code"], name=p.get("name", ""), qty=int(p.get("qty", 0)),
                available=int(p.get("available", 0)), cost=float(p.get("cost", 0.0)),
            )

    def _persist(self) -> None:
        if not self.persist_path:
            return
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cash": round(self.cash, 2),
            "prices": self._prices,
            "names": self._names,
            "positions": [
                {"code": p.code, "name": p.name, "qty": p.qty,
                 "available": p.available, "cost": round(p.cost, 4)}
                for p in self._positions.values()
            ],
        }
        self.persist_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


    # ---- connection -------------------------------------------------------

    def connect(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def _require(self) -> None:
        if not self._connected:
            raise NotConnected("mock broker not connected (call connect first)")

    # ---- reads ------------------------------------------------------------

    def balance(self) -> Balance:
        self._require()
        mv = sum(p.qty * self._prices.get(p.code, p.cost) for p in self._positions.values())
        return Balance(
            cash=round(self.cash, 2),
            available=round(self.cash, 2),
            market_value=round(mv, 2),
            total=round(self.cash + mv, 2),
            frozen=0.0,
        )

    def positions(self) -> List[Position]:
        self._require()
        out = []
        for p in self._positions.values():
            p.last = self._prices.get(p.code, p.cost)
            out.append(p)
        return out

    def quote(self, codes: List[str]) -> Dict[str, Quote]:
        self._require()
        out = {}
        for c in codes:
            px = self._prices.get(c, 1.0)
            out[c] = Quote(
                code=c,
                name=self._names.get(c, ""),
                last=px,
                bid1=round(px - 0.001, 3),
                ask1=round(px + 0.001, 3),
                bid1_vol=100000,
                ask1_vol=100000,
                prev_close=px,
                ts=now_iso(),
            )
        return out

    def orders(self, open_only: bool = False) -> List[Order]:
        self._require()
        vals = list(self._orders.values())
        if open_only:
            vals = [o for o in vals if o.is_open]
        return vals

    # ---- writes -----------------------------------------------------------

    def _place(self, side: str, code: str, price: float, qty: int) -> Order:
        self._require()
        self.calls.append((side, code, price, qty))
        oid = f"{self._id_prefix}{next(_counter):03d}"
        order = Order(id=oid, code=code, side=side, price=price, qty=qty, ts=now_iso())

        if self.fill_immediately:
            cost = round(price * qty, 2)
            if side == SIDE_BUY:
                if cost > self.cash:
                    order.status = "rejected"
                    order.message = "insufficient cash"
                else:
                    self.cash = round(self.cash - cost, 2)
                    pos = self._positions.get(code)
                    if pos is None:
                        pos = Position(code=code, name=self._names.get(code, ""), qty=0, available=0, cost=price)
                        self._positions[code] = pos
                    total_cost = pos.cost * pos.qty + cost
                    pos.qty += qty
                    pos.cost = round(total_cost / pos.qty, 4) if pos.qty else 0.0
                    # T+1: today's buy is NOT sellable today
                    order.filled = qty
                    order.status = STATUS_FILLED
            else:  # sell
                pos = self._positions.get(code)
                if pos is None or pos.available < qty:
                    order.status = "rejected"
                    order.message = "insufficient sellable position"
                else:
                    proceeds = round(price * qty, 2)
                    self.cash = round(self.cash + proceeds, 2)
                    pos.qty -= qty
                    pos.available -= qty
                    if pos.qty == 0:
                        self._positions.pop(code, None)
                    order.filled = qty
                    order.status = STATUS_FILLED
        else:
            order.status = STATUS_REPORTED

        self._orders[oid] = order
        self._persist()
        return order

    def buy(self, code: str, price: float, qty: int) -> Order:
        return self._place(SIDE_BUY, code, price, qty)

    def sell(self, code: str, price: float, qty: int) -> Order:
        return self._place(SIDE_SELL, code, price, qty)

    def cancel(self, order_id: Optional[str] = None) -> List[str]:
        self._require()
        cancelled = []
        targets = [order_id] if order_id else [o.id for o in self._orders.values() if o.is_open]
        for oid in targets:
            o = self._orders.get(oid)
            if o and o.is_open:
                o.status = "cancelled"
                cancelled.append(oid)
        return cancelled

    # ---- test helpers -----------------------------------------------------

    def set_price(self, code: str, price: float) -> None:
        self._prices[code] = round(price, 3)
        self._persist()

    def next_day(self) -> None:
        """Make everything sellable (simulate an overnight settlement)."""
        for p in self._positions.values():
            p.available = p.qty
        self._persist()
