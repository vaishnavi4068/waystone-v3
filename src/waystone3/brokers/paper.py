"""In-process paper broker — the zero-credential default.

Fills MARKET orders immediately at a reference price (the latest known mark, or a price
passed on the order), tracks cash and positions, and computes simple average-cost P&L.
No network, no keys — ``waystone3 run`` works out of the box against this.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from waystone3.core.types import (
    AccountInfo,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)


class PaperBroker:
    name = "paper"
    is_paper = True

    def __init__(self, initial_cash: Decimal = Decimal(100_000)) -> None:
        self._cash = initial_cash
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._marks: dict[str, Decimal] = {}
        self._ids = itertools.count(1)

    def set_mark(self, symbol: str, price: Decimal) -> None:
        """Update the last known price used for fills and mark-to-market."""
        self._marks[symbol] = price

    def export_state(self) -> dict[str, Any]:
        """Serialize cash + positions + marks for durable persistence."""
        return {
            "cash": str(self._cash),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": str(p.qty),
                    "avg_entry_price": str(p.avg_entry_price),
                }
                for p in self._positions.values()
            ],
            "marks": {s: str(v) for s, v in self._marks.items()},
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> PaperBroker:
        broker = cls(initial_cash=Decimal(str(state["cash"])))
        for p in state.get("positions", []):
            broker._positions[p["symbol"]] = Position(
                symbol=p["symbol"],
                qty=Decimal(str(p["qty"])),
                avg_entry_price=Decimal(str(p["avg_entry_price"])),
            )
        for symbol, value in state.get("marks", {}).items():
            broker._marks[symbol] = Decimal(str(value))
        return broker

    async def get_account(self) -> AccountInfo:
        market_value = sum(
            (
                p.qty * self._marks.get(p.symbol, p.avg_entry_price)
                for p in self._positions.values()
            ),
            Decimal(0),
        )
        equity = self._cash + market_value
        return AccountInfo(
            account_id="paper",
            cash=self._cash,
            equity=equity,
            buying_power=self._cash,
        )

    async def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for p in self._positions.values():
            mark = self._marks.get(p.symbol)
            out.append(p.model_copy(update={"market_price": mark}) if mark else p)
        return out

    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        ref_price: Decimal | None = None,
    ) -> Order:
        price = ref_price if ref_price is not None else self._marks.get(symbol)
        if price is None:
            raise ValueError(f"no reference price for {symbol}; pass ref_price or set_mark")
        self.set_mark(symbol, price)
        self._apply_fill(symbol, side, qty, price)

        now = datetime.now(UTC)
        order = Order(
            id=f"paper-{next(self._ids)}",
            symbol=symbol,
            side=side,
            type=order_type,
            qty=qty,
            filled_qty=qty,
            avg_fill_price=price,
            status=OrderStatus.FILLED,
            submitted_at=now,
            filled_at=now,
        )
        self._orders[order.id] = order
        return order

    async def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    def _apply_fill(
        self, symbol: str, side: OrderSide, qty: Decimal, price: Decimal
    ) -> None:
        signed = qty if side == OrderSide.BUY else -qty
        self._cash -= signed * price
        existing = self._positions.get(symbol)
        prev_qty = existing.qty if existing else Decimal(0)
        new_qty = prev_qty + signed

        if new_qty == 0:
            self._positions.pop(symbol, None)
            return

        if existing is None or prev_qty == 0 or (prev_qty > 0) == (signed > 0):
            # opening or adding in the same direction -> weighted-average entry
            prev_cost = prev_qty * (existing.avg_entry_price if existing else Decimal(0))
            avg = (prev_cost + signed * price) / new_qty
        else:
            # reducing an existing position -> entry price unchanged
            avg = existing.avg_entry_price

        self._positions[symbol] = Position(
            symbol=symbol,
            qty=new_qty,
            avg_entry_price=avg,
            market_price=price,
        )
