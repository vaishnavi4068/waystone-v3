"""JSON-serializable IBKR daily-report records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Book(StrEnum):
    FUTURES = "futures"
    OPTIONS = "options"
    EQUITIES = "equities"
    OTHER = "other"


class Execution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    exec_id: str
    perm_id: int | None = None
    order_id: int | None = None
    time: datetime
    account: str
    sec_type: str
    symbol: str
    local_symbol: str
    con_id: int | None = None
    exchange: str = ""
    currency: str = "USD"
    expiry: str | None = None
    strike: float | None = None
    right: str | None = None
    multiplier: str | None = None
    side: str
    qty: float
    price: float
    commission: float | None = None
    commission_currency: str | None = None
    realized_pnl: float | None = None
    cum_qty: float | None = None
    avg_price: float | None = None
    client_id: int | None = None
    book: Book = Book.OTHER


class PositionSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account: str = ""
    sec_type: str
    symbol: str
    local_symbol: str
    con_id: int | None = None
    exchange: str = ""
    currency: str = "USD"
    expiry: str | None = None
    strike: float | None = None
    right: str | None = None
    multiplier: str | None = None
    qty: float
    avg_cost: float
    market_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    book: Book = Book.OTHER


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_id: str = ""
    nlv: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    excess_liquidity: float = 0.0
    maint_margin: float = 0.0
    currency: str = "USD"


class BookStats(BaseModel):
    fills: int = 0
    qty: float = 0.0
    notional: float = 0.0
    commission: float = 0.0
    realized_pnl: float = 0.0


class DailySummary(BaseModel):
    date: str
    futures: BookStats = Field(default_factory=BookStats)
    options: BookStats = Field(default_factory=BookStats)
    other: BookStats = Field(default_factory=BookStats)
    totals: BookStats = Field(default_factory=BookStats)


class Manifest(BaseModel):
    schema_version: int = SCHEMA_VERSION
    generated_at: datetime
    date: str
    ib_host: str
    ib_port: int
    ib_client_id: int
    fill_count: int
    tws_connected: bool
    staged: bool = False


class DailyReport(BaseModel):
    date: str
    generated_at: datetime
    published: bool
    executions: list[Execution]
    positions: list[PositionSnapshot]
    account: AccountSnapshot
    summary: DailySummary
    manifest: Manifest
