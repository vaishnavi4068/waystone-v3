"""Momentum-only decision engine.

Turns a composite score plus the current position into a concrete intent. The logic is
deliberately trend-following:

* flat and the composite is strongly bullish  -> open a long
* long and the composite has turned bearish    -> exit (momentum lost)
* otherwise                                     -> hold / no action

There is no mean-reversion: a weak/negative score never *opens* a position, and we never
buy into weakness. Shorting is gated off for the stocks MVP.

Sizing starts as fixed notional. The single seam for signal-strength sizing is marked in
:meth:`DecisionEngine.decide` — scaling ``qty`` by the score is a one-line change there.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from waystone3.core.types import OrderSide
from waystone3.fusion.fuse import CompositeScore


@dataclass(frozen=True)
class DecisionConfig:
    bullish_threshold: Decimal = Decimal("3")
    bearish_threshold: Decimal = Decimal("-3")
    notional_per_trade: Decimal = Decimal("2000")
    allow_short: bool = False


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: OrderSide
    qty: Decimal
    reason: str


class DecisionEngine:
    def __init__(self, config: DecisionConfig | None = None) -> None:
        self.config = config or DecisionConfig()

    def _entry_qty(self, ref_price: Decimal) -> Decimal:
        # Sizing seam: today fixed-notional. Signal-strength sizing would multiply this
        # by a scale(comp.score) factor here, isolated to one place.
        if ref_price <= 0:
            return Decimal(0)
        qty = (self.config.notional_per_trade // ref_price)
        return max(qty, Decimal(1))

    def decide(
        self,
        comp: CompositeScore,
        position_qty: Decimal,
        ref_price: Decimal,
    ) -> Intent | None:
        cfg = self.config
        long = position_qty > 0
        short = position_qty < 0

        if not long and not short:
            if comp.score >= cfg.bullish_threshold:
                qty = self._entry_qty(ref_price)
                return Intent(comp.symbol, OrderSide.BUY, qty, f"bullish {comp.score}")
            if cfg.allow_short and comp.score <= cfg.bearish_threshold:
                qty = self._entry_qty(ref_price)
                return Intent(comp.symbol, OrderSide.SELL, qty, f"bearish {comp.score}")
            return None

        if long:
            if comp.score <= cfg.bearish_threshold:
                return Intent(
                    comp.symbol, OrderSide.SELL, position_qty, f"exit long {comp.score}"
                )
            return None  # ride the trend

        # short position (only reachable when allow_short was enabled when opened)
        if comp.score >= cfg.bullish_threshold:
            return Intent(
                comp.symbol, OrderSide.BUY, abs(position_qty), f"cover short {comp.score}"
            )
        return None
