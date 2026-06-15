"""Single-gate risk guard.

Every order passes through :meth:`RiskGuard.check` before it reaches a broker. The guard
refuses to arm against a non-paper broker (paper-only platform for now) and enforces a
small set of hard caps. It keeps an in-cycle view of positions via :meth:`apply_fill` so
successive intents in one cycle respect the caps cumulatively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from waystone3.core.types import OrderSide


class RiskViolationError(Exception):
    """Raised when an order would breach a configured limit."""


@dataclass(frozen=True)
class RiskLimits:
    max_order_qty: Decimal = Decimal(10_000)
    max_position_qty: Decimal = Decimal(10_000)
    max_open_positions: int = 20
    max_notional_per_order: Decimal = Decimal(50_000)
    require_paper: bool = True


@dataclass
class RiskGuard:
    limits: RiskLimits = field(default_factory=RiskLimits)
    is_paper: bool = True
    _positions: dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.limits.require_paper and not self.is_paper:
            raise RiskViolationError("guard refuses to arm against a non-paper broker")

    def sync_positions(self, positions: dict[str, Decimal]) -> None:
        """Reset the in-cycle position view (typically from the broker at cycle start)."""
        self._positions = {s: q for s, q in positions.items() if q != 0}

    def check(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        ref_price: Decimal,
    ) -> None:
        if qty <= 0:
            raise RiskViolationError(f"{symbol}: order qty must be positive, got {qty}")
        if qty > self.limits.max_order_qty:
            raise RiskViolationError(
                f"{symbol}: order qty {qty} exceeds max {self.limits.max_order_qty}"
            )
        notional = qty * ref_price
        if notional > self.limits.max_notional_per_order:
            raise RiskViolationError(
                f"{symbol}: notional {notional} exceeds max "
                f"{self.limits.max_notional_per_order}"
            )

        current = self._positions.get(symbol, Decimal(0))
        delta = qty if side == OrderSide.BUY else -qty
        projected = current + delta
        if abs(projected) > self.limits.max_position_qty:
            raise RiskViolationError(
                f"{symbol}: projected position {projected} exceeds max "
                f"{self.limits.max_position_qty}"
            )

        opening_new = current == 0 and projected != 0
        if opening_new:
            open_count = sum(1 for q in self._positions.values() if q != 0)
            if open_count >= self.limits.max_open_positions:
                raise RiskViolationError(
                    f"{symbol}: already at max open positions "
                    f"{self.limits.max_open_positions}"
                )

    def apply_fill(self, symbol: str, side: OrderSide, qty: Decimal) -> None:
        """Update the in-cycle position view after an order fills."""
        delta = qty if side == OrderSide.BUY else -qty
        new = self._positions.get(symbol, Decimal(0)) + delta
        if new == 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = new
