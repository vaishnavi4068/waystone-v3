"""Market data source protocol.

Two access patterns:

* :meth:`get_bars` — the most recent ``lookback`` bars, used by the live cycle.
* :meth:`get_history` — a full date range, used by the backtester.

Both return chronologically-ordered (oldest first) bars.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from waystone3.core.types import Bar, Timeframe


@runtime_checkable
class MarketDataSource(Protocol):
    name: str

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, lookback: int
    ) -> list[Bar]: ...

    async def get_history(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]: ...
