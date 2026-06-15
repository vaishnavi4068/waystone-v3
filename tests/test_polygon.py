from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from waystone3.core.types import Timeframe
from waystone3.data.polygon import PolygonDataSource

# One daily bar: 2024-01-02 (t in ms), OHLCV.
_RESULTS = {
    "results": [
        {"t": 1704153600000, "o": 100.0, "h": 105.0, "l": 99.0, "c": 104.0, "v": 1_000_000},
        {"t": 1704240000000, "o": 104.0, "h": 106.0, "l": 103.0, "c": 105.5, "v": 900_000},
    ]
}


def make_fetch(captured: dict[str, Any]):
    async def _fetch(path: str, params: dict[str, Any]) -> dict[str, Any]:
        captured["path"] = path
        captured["params"] = params
        return _RESULTS

    return _fetch


async def test_get_history_maps_aggregates_to_bars() -> None:
    captured: dict[str, Any] = {}
    src = PolygonDataSource(fetch_fn=make_fetch(captured))
    bars = await src.get_history(
        "aapl",
        Timeframe.D1,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )
    assert len(bars) == 2
    assert bars[0].symbol == "AAPL"
    assert bars[0].open == Decimal("100.0")
    assert bars[0].close == Decimal("104.0")
    assert bars[0].volume == Decimal("1000000")
    assert bars[1].close == Decimal("105.5")
    # request used the day aggregate endpoint with the date range
    assert captured["path"] == "/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-03"


async def test_get_bars_returns_last_n() -> None:
    src = PolygonDataSource(fetch_fn=make_fetch({}))
    bars = await src.get_bars("AAPL", Timeframe.D1, lookback=1)
    assert len(bars) == 1
    assert bars[0].close == Decimal("105.5")  # most recent


async def test_intraday_uses_minute_timespan() -> None:
    captured: dict[str, Any] = {}
    src = PolygonDataSource(fetch_fn=make_fetch(captured))
    await src.get_bars("MSFT", Timeframe.M5, lookback=10)
    assert "/range/5/minute/" in captured["path"]


async def test_missing_key_raises_only_on_use() -> None:
    src = PolygonDataSource(api_key="")  # no fetch_fn, no key
    with pytest.raises(RuntimeError):
        await src.get_bars("AAPL", Timeframe.D1, 10)
