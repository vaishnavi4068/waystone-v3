from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from waystone3.core.types import Timeframe
from waystone3.data.stub import StubDataSource
from waystone3.runner.backtest import _max_drawdown, run_backtest

START = datetime(2022, 1, 1, tzinfo=UTC)
END = datetime(2023, 6, 1, tzinfo=UTC)  # ~500 calendar days of stub bars


async def test_uptrend_backtest_is_profitable() -> None:
    data = StubDataSource(start_price=100.0, default_slope=1.0)
    result = await run_backtest(
        data=data,
        symbols=["AAPL"],
        timeframe=Timeframe.D1,
        start=START,
        end=END,
        lookback=80,
    )
    m = result.metrics
    assert m.num_trades >= 1
    assert m.total_return > 0  # buying and holding a steady uptrend makes money
    assert len(result.equity_curve) > 0


async def test_downtrend_backtest_stays_flat() -> None:
    # Pure downtrend: momentum-only never opens a long, so equity should not fall.
    data = StubDataSource(start_price=600.0, default_slope=-1.0)
    result = await run_backtest(
        data=data,
        symbols=["AAPL"],
        timeframe=Timeframe.D1,
        start=START,
        end=END,
        lookback=80,
    )
    assert result.metrics.num_trades == 0
    assert result.metrics.total_return == Decimal(0)


async def test_no_history_raises() -> None:
    import pytest

    class _Empty(StubDataSource):
        async def get_history(self, symbol, timeframe, start, end):  # type: ignore[override]
            return []

    with pytest.raises(ValueError):
        await run_backtest(
            data=_Empty(),
            symbols=["X"],
            timeframe=Timeframe.D1,
            start=START,
            end=END,
        )


def test_max_drawdown_basic() -> None:
    curve = [Decimal(100), Decimal(120), Decimal(90), Decimal(110)]
    # peak 120 -> trough 90 => 25% drawdown
    assert _max_drawdown(curve) == Decimal("0.25")
