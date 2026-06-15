from __future__ import annotations

from decimal import Decimal

from waystone3.brokers.paper import PaperBroker
from waystone3.core.types import OrderSide, OrderStatus


async def test_buy_fills_and_updates_cash_and_position() -> None:
    b = PaperBroker(initial_cash=Decimal(100_000))
    order = await b.submit_order("AAPL", OrderSide.BUY, Decimal(10), ref_price=Decimal(150))
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == Decimal(10)

    acct = await b.get_account()
    assert acct.cash == Decimal(100_000) - Decimal(1500)

    positions = await b.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == Decimal(10)
    assert positions[0].avg_entry_price == Decimal(150)


async def test_sell_closes_position() -> None:
    b = PaperBroker()
    await b.submit_order("AAPL", OrderSide.BUY, Decimal(10), ref_price=Decimal(150))
    await b.submit_order("AAPL", OrderSide.SELL, Decimal(10), ref_price=Decimal(160))
    positions = await b.get_positions()
    assert positions == []
    # realized: bought 1500, sold 1600 -> cash back to 100_000 + 100
    acct = await b.get_account()
    assert acct.cash == Decimal(100_100)


async def test_average_entry_on_add() -> None:
    b = PaperBroker()
    await b.submit_order("X", OrderSide.BUY, Decimal(10), ref_price=Decimal(100))
    await b.submit_order("X", OrderSide.BUY, Decimal(10), ref_price=Decimal(120))
    positions = await b.get_positions()
    assert positions[0].qty == Decimal(20)
    assert positions[0].avg_entry_price == Decimal(110)


async def test_marked_pnl() -> None:
    b = PaperBroker()
    await b.submit_order("X", OrderSide.BUY, Decimal(10), ref_price=Decimal(100))
    b.set_mark("X", Decimal(105))
    positions = await b.get_positions()
    assert positions[0].unrealized_pnl == Decimal(50)


async def test_missing_price_raises() -> None:
    import pytest

    b = PaperBroker()
    with pytest.raises(ValueError):
        await b.submit_order("X", OrderSide.BUY, Decimal(1))
