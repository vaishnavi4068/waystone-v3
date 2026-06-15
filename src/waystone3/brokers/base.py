"""Broker protocol — async, asset-agnostic.

Any implementation (paper, Alpaca, …) satisfies the same interface so the runner is
broker-agnostic. ``is_paper`` lets the risk guard refuse to arm against live trading.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from waystone3.core.types import (
    AccountInfo,
    Order,
    OrderSide,
    OrderType,
    Position,
)


@runtime_checkable
class Broker(Protocol):
    name: str
    is_paper: bool

    async def get_account(self) -> AccountInfo: ...

    async def get_positions(self) -> list[Position]: ...

    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        ref_price: Decimal | None = None,
    ) -> Order: ...

    async def get_order(self, order_id: str) -> Order: ...

    async def list_orders(self, limit: int = 50) -> list[Order]: ...
