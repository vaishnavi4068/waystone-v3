"""Shared test helpers — synthetic bar builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from waystone3.core.types import Bar, Timeframe

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def dec(x: object) -> Decimal:
    return Decimal(str(x))


def make_bars(
    closes: list[float],
    symbol: str = "TEST",
    volumes: list[float] | None = None,
) -> list[Bar]:
    """Build a chronological bar series from close prices.

    OHLC are derived simply from closes (open = prior close). Volume defaults to a flat
    1000 unless an explicit per-bar list is given.
    """
    vols = volumes if volumes is not None else [1000.0] * len(closes)
    if len(vols) != len(closes):
        raise ValueError("closes and volumes must be the same length")
    bars: list[Bar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                open=dec(prev),
                high=dec(max(prev, c)),
                low=dec(min(prev, c)),
                close=dec(c),
                volume=dec(vols[i]),
                timestamp=_EPOCH + timedelta(days=i),
            )
        )
        prev = c
    return bars


def rising(n: int = 60, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + step * i for i in range(n)]


def falling(n: int = 60, start: float = 160.0, step: float = 1.0) -> list[float]:
    return [start - step * i for i in range(n)]
