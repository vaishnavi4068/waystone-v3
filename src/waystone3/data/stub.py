"""Synthetic data source — deterministic trends, no network.

Lets ``waystone3 run`` produce a paper trade with zero configuration and powers the
end-to-end smoke test. Each symbol gets a steady uptrend (default) so the momentum
contributors fire; pass per-symbol slopes to shape other regimes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from waystone3.core.types import Bar, Timeframe

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class StubDataSource:
    name = "stub"

    def __init__(
        self,
        start_price: float = 100.0,
        slopes: dict[str, float] | None = None,
        default_slope: float = 1.0,
        volume: float = 1000.0,
    ) -> None:
        self.start_price = start_price
        self.slopes = slopes or {}
        self.default_slope = default_slope
        self.volume = volume

    def _series(self, symbol: str, count: int) -> list[Bar]:
        slope = self.slopes.get(symbol, self.default_slope)
        bars: list[Bar] = []
        prev = Decimal(str(self.start_price))
        for i in range(count):
            close = Decimal(str(self.start_price + slope * i))
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=Timeframe.D1,
                    open=prev,
                    high=max(prev, close),
                    low=min(prev, close),
                    close=close,
                    volume=Decimal(str(self.volume)),
                    timestamp=_EPOCH + timedelta(days=i),
                )
            )
            prev = close
        return bars

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, lookback: int
    ) -> list[Bar]:
        # Generate a fixed-length canonical window and return its tail, so the *latest* bar
        # is stable regardless of `lookback` — as a real feed behaves. (A naive
        # `_series(symbol, lookback)` would move the endpoint with the request size, which
        # breaks mark-to-market when one call asks for 80 bars and another for 1.)
        canon = max(lookback, 120)
        return self._series(symbol, canon)[-lookback:]

    async def get_history(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        days = max(1, (end - start).days)
        return self._series(symbol, days)
