"""Pre-trade guardrails.

This module is the *only* thing standing between an AI-issued command and a
real order, so it fails closed: anything it cannot positively verify is a
rejection. It is pure logic (no I/O beyond the audit log passed in), which
makes every rule unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from .audit import AuditEvent, AuditLog
from .config import Config
from .errors import RiskRejected, TradingHoursError
from .models import OrderRequest, Quote, SIDE_BUY, SIDE_SELL, now_iso
from .timeutil import classify

LOT = 100


@dataclass
class Decision:
    ok: bool
    req: OrderRequest
    qty: int
    price: float
    amount: float
    reasons: List[str]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.req.code,
            "side": self.req.side,
            "qty": self.qty,
            "price": self.price,
            "amount": self.amount,
            "reasons": self.reasons,
        }


def normalise_qty(qty: float) -> int:
    """Round down to a whole 100-share lot (A-share ETF board lot)."""
    if qty <= 0:
        return 0
    return int(qty // LOT) * LOT


def reference_price(req: OrderRequest, quote: Optional[Quote]) -> float:
    """Price to use when the caller did not pin one.

    Buy lifts the offer, sell hits the bid — the cheapest that gets filled.
    Falls back to last, then to the caller's own value.
    """
    if req.price and req.price > 0:
        return round(req.price, 3)
    if quote is not None:
        if req.side == SIDE_BUY and quote.ask1:
            return round(quote.ask1, 3)
        if req.side == SIDE_SELL and quote.bid1:
            return round(quote.bid1, 3)
        if quote.last:
            return round(quote.last, 3)
        if quote.mid:
            return round(quote.mid, 3)
    return 0.0


class RiskGuard:
    def __init__(self, cfg: Config, audit: Optional[AuditLog] = None):
        self.cfg = cfg
        self.audit = audit

    # ---- individual rules -------------------------------------------------

    def _check_code(self, code: str) -> None:
        allowed = self.cfg.trading.allowed_codes
        if allowed and code not in allowed:
            raise RiskRejected(f"code {code} is not in trading.allowed_codes {allowed}")

    def _check_hours(self, force: bool, enforce: bool = True) -> None:
        # `enforce=False` is for read-only planning: describing an intent that
        # *would* fire is useful outside hours, submitting one is not.
        if force or not enforce:
            return
        s = classify(holidays_file=self.cfg.trading.holidays_file or None)
        if not s.tradable:
            raise TradingHoursError(
                f"market is '{s.label}' ({s.label} => not tradable); use --force to override"
            )

    def _check_amounts(self, amount: float) -> None:
        t = self.cfg.trading
        if t.min_order_amount and amount < t.min_order_amount:
            raise RiskRejected(
                f"notional {amount:.2f} below trading.min_order_amount {t.min_order_amount:.2f}"
            )
        if t.max_order_amount and amount > t.max_order_amount:
            raise RiskRejected(
                f"notional {amount:.2f} above trading.max_order_amount {t.max_order_amount:.2f}"
            )

    def _check_daily(self, amount: float, broker: Optional[str]) -> None:
        t = self.cfg.trading
        if not t.max_daily_amount:
            return
        used = self.audit.notional_today(broker=broker) if self.audit else 0.0
        if used + amount > t.max_daily_amount:
            raise RiskRejected(
                f"daily notional cap {t.max_daily_amount:.2f} would be exceeded "
                f"(used {used:.2f} + {amount:.2f})"
            )

    def _check_position(
        self, req: OrderRequest, qty: int, price: float, position_qty: int, sellable: int, total_assets: float
    ) -> None:
        t = self.cfg.trading
        if req.side == SIDE_SELL:
            if qty > sellable:
                raise RiskRejected(f"want to sell {qty} but only {sellable} shares are sellable today")
            return
        # buy: cap resulting position value as a share of total assets
        if t.max_position_pct >= 100 or total_assets <= 0:
            return
        new_value = (position_qty + qty) * price
        cap = total_assets * t.max_position_pct / 100.0
        if new_value > cap + 1e-6:
            raise RiskRejected(
                f"position value {new_value:.2f} would exceed {t.max_position_pct:.0f}% of "
                f"total assets ({cap:.2f})"
            )

    # ---- the gate ---------------------------------------------------------

    def evaluate(
        self,
        req: OrderRequest,
        quote: Optional[Quote] = None,
        *,
        position_qty: int = 0,
        sellable: int = 0,
        total_assets: float = 0.0,
        force: bool = False,
        broker: Optional[str] = None,
        enforce_hours: bool = True,
    ) -> Decision:
        """Return a Decision, or raise one of the EtfaError subclasses."""
        if req.side not in (SIDE_BUY, SIDE_SELL):
            raise RiskRejected(f"side must be buy/sell, got {req.side!r}")
        self._check_code(req.code)
        self._check_hours(force, enforce_hours)

        price = reference_price(req, quote)
        if price <= 0:
            raise RiskRejected("no usable price (pass --price or provide a quote)")

        if req.qty is not None and req.qty > 0:
            qty = normalise_qty(req.qty)
        elif req.amount is not None and req.amount > 0:
            qty = normalise_qty(req.amount / price)
        else:
            raise RiskRejected("either qty or amount is required")

        if qty <= 0:
            raise RiskRejected(f"computed qty rounds to 0 at price {price} (min one lot = {LOT})")

        amount = round(qty * price, 2)
        self._check_amounts(amount)
        self._check_position(req, qty, price, position_qty, sellable, total_assets)
        self._check_daily(amount, broker)

        return Decision(ok=True, req=req, qty=qty, price=price, amount=amount, reasons=[])

    def reject(self, req: OrderRequest, exc: Exception, code_label: str = "risk_rejected") -> AuditEvent:
        ev = AuditEvent(
            ts=now_iso(), kind="rejected", ok=False, code=req.code, side=req.side,
            qty=req.qty or 0, amount=req.amount or 0.0, detail=f"{code_label}: {exc}",
        )
        if self.audit:
            self.audit.append(ev)
        return ev
