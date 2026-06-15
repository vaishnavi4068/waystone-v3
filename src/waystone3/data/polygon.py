"""Polygon.io market data source — paid subscription for backtesting-grade bars.

Implements the same ``MarketDataSource`` protocol as yfinance, so it drops into ``run`` and
``backtest`` unchanged. The HTTP layer is injectable (``fetch_fn``) so tests run with canned
responses — no key, no network. The key is read from ``POLYGON_API_KEY`` unless passed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from waystone3.core.types import Bar, Timeframe

# Timeframe -> (multiplier, timespan) for Polygon's aggregates endpoint.
_AGG = {
    Timeframe.M1: (1, "minute"),
    Timeframe.M5: (5, "minute"),
    Timeframe.M15: (15, "minute"),
    Timeframe.H1: (1, "hour"),
    Timeframe.D1: (1, "day"),
}

# Rough calendar span needed to cover `lookback` bars (markets aren't open every hour/day).
_SPAN_DAYS = {
    Timeframe.M1: 5,
    Timeframe.M5: 10,
    Timeframe.M15: 20,
    Timeframe.H1: 120,
    Timeframe.D1: 800,
}

FetchFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class PolygonDataSource:
    name = "polygon"
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str | None = None, fetch_fn: FetchFn | None = None) -> None:
        import os

        self.api_key = api_key or os.getenv("POLYGON_API_KEY", "")
        self._fetch_fn = fetch_fn

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._fetch_fn is not None:
            return await self._fetch_fn(path, params)
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY is not set")
        import httpx

        async with httpx.AsyncClient(base_url=self.BASE, timeout=30.0) as client:
            resp = await client.get(path, params={**params, "apiKey": self.api_key})
            resp.raise_for_status()
            return cast("dict[str, Any]", resp.json())

    async def _aggregates(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        mult, span = _AGG[timeframe]
        path = (
            f"/v2/aggs/ticker/{symbol.upper()}/range/{mult}/{span}/"
            f"{start.date().isoformat()}/{end.date().isoformat()}"
        )
        data = await self._request(
            path, {"adjusted": "true", "sort": "asc", "limit": 50000}
        )
        return [_bar_from_agg(symbol, timeframe, row) for row in data.get("results", [])]

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, lookback: int
    ) -> list[Bar]:
        end = datetime.now(UTC)
        start = end - timedelta(days=_SPAN_DAYS[timeframe])
        bars = await self._aggregates(symbol, timeframe, start, end)
        return bars[-lookback:]

    async def get_history(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        return await self._aggregates(symbol, timeframe, start, end)


def _bar_from_agg(symbol: str, timeframe: Timeframe, row: dict[str, Any]) -> Bar:
    def d(x: Any) -> Decimal:
        return Decimal(str(x))

    return Bar(
        symbol=symbol.upper(),
        timeframe=timeframe,
        open=d(row["o"]),
        high=d(row["h"]),
        low=d(row["l"]),
        close=d(row["c"]),
        volume=d(row.get("v", 0)),
        timestamp=datetime.fromtimestamp(row["t"] / 1000, tz=UTC),
    )
