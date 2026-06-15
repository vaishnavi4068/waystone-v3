"""Core domain types — market data and broker primitives.

Frozen pydantic models with Decimal money fields. Mirrors the clean shape proven in
the previous platform, trimmed to what the momentum MVP needs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    FILLED = "filled"
    REJECTED = "rejected"
    PENDING = "pending"


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)


class Bar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timestamp: datetime


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    symbol: str
    side: OrderSide
    type: OrderType
    qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    status: OrderStatus
    submitted_at: datetime
    filled_at: datetime | None


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_price: Decimal | None = None

    @property
    def unrealized_pnl(self) -> Decimal | None:
        if self.market_price is None:
            return None
        return (self.market_price - self.avg_entry_price) * self.qty


class AccountInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    currency: str = "USD"
