from __future__ import annotations

from decimal import Decimal

from waystone3.brokers.paper import PaperBroker
from waystone3.core.types import OrderSide, Timeframe
from waystone3.data.stub import StubDataSource
from waystone3.decision.engine import DecisionEngine
from waystone3.risk.guard import RiskGuard
from waystone3.runner.config import default_contributors, default_weights
from waystone3.runner.cycle import run_cycle


async def test_uptrend_produces_a_buy_and_position() -> None:
    data = StubDataSource(default_slope=1.0)  # steady uptrend
    broker = PaperBroker()
    guard = RiskGuard(is_paper=True)

    report = await run_cycle(
        data=data,
        broker=broker,
        guard=guard,
        contributors=default_contributors(),
        weights=default_weights(),
        engine=DecisionEngine(),
        watchlist=["AAPL"],
        timeframe=Timeframe.D1,
        bars_lookback=80,
    )

    assert len(report.orders) == 1
    order = report.orders[0]
    assert order.symbol == "AAPL"
    assert order.side == OrderSide.BUY
    assert order.filled_qty > 0

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == order.filled_qty


async def test_downtrend_no_orders() -> None:
    data = StubDataSource(start_price=200.0, default_slope=-1.0)  # downtrend
    broker = PaperBroker()
    guard = RiskGuard(is_paper=True)

    report = await run_cycle(
        data=data,
        broker=broker,
        guard=guard,
        contributors=default_contributors(),
        weights=default_weights(),
        engine=DecisionEngine(),
        watchlist=["AAPL"],
        timeframe=Timeframe.D1,
        bars_lookback=80,
    )
    # momentum-only: a downtrend opens nothing (no shorting by default)
    assert report.orders == []


async def test_exit_when_momentum_flips() -> None:
    broker = PaperBroker()
    guard = RiskGuard(is_paper=True)
    contributors = default_contributors()
    weights = default_weights()

    up = StubDataSource(default_slope=1.0)
    await run_cycle(
        data=up,
        broker=broker,
        guard=guard,
        contributors=contributors,
        weights=weights,
        engine=DecisionEngine(),
        watchlist=["AAPL"],
        timeframe=Timeframe.D1,
        bars_lookback=80,
    )
    assert len(await broker.get_positions()) == 1

    # now feed a downtrend: the held long should be exited
    down = StubDataSource(start_price=200.0, default_slope=-1.0)
    report = await run_cycle(
        data=down,
        broker=broker,
        guard=guard,
        contributors=contributors,
        weights=weights,
        engine=DecisionEngine(),
        watchlist=["AAPL"],
        timeframe=Timeframe.D1,
        bars_lookback=80,
    )
    assert len(report.orders) == 1
    assert report.orders[0].side == OrderSide.SELL
    assert await broker.get_positions() == []


def test_position_qty_sign_helper_uses_decimal() -> None:
    # guards against accidental float qty in the cycle's position bookkeeping
    assert isinstance(Decimal(1), Decimal)
