from __future__ import annotations

from decimal import Decimal

from waystone3.core.types import OrderSide
from waystone3.decision.engine import DecisionConfig, DecisionEngine
from waystone3.fusion.fuse import CompositeScore


def comp(score: float, symbol: str = "X") -> CompositeScore:
    return CompositeScore(symbol=symbol, score=Decimal(str(score)), drivers=[], per_contributor={})


def test_flat_bullish_opens_long_with_notional_qty() -> None:
    eng = DecisionEngine(DecisionConfig(notional_per_trade=Decimal(2000)))
    intent = eng.decide(comp(5), position_qty=Decimal(0), ref_price=Decimal(100))
    assert intent is not None
    assert intent.side == OrderSide.BUY
    assert intent.qty == Decimal(20)  # 2000 // 100


def test_flat_neutral_does_nothing() -> None:
    eng = DecisionEngine()
    assert eng.decide(comp(1), Decimal(0), Decimal(100)) is None


def test_flat_bearish_no_short_by_default() -> None:
    eng = DecisionEngine()
    assert eng.decide(comp(-9), Decimal(0), Decimal(100)) is None


def test_long_bearish_exits_full_position() -> None:
    eng = DecisionEngine()
    intent = eng.decide(comp(-5), position_qty=Decimal(20), ref_price=Decimal(100))
    assert intent is not None
    assert intent.side == OrderSide.SELL
    assert intent.qty == Decimal(20)


def test_long_still_bullish_holds_no_double_buy() -> None:
    eng = DecisionEngine()
    assert eng.decide(comp(8), position_qty=Decimal(20), ref_price=Decimal(100)) is None


def test_entry_qty_minimum_one_when_price_exceeds_notional() -> None:
    eng = DecisionEngine(DecisionConfig(notional_per_trade=Decimal(50)))
    intent = eng.decide(comp(5), Decimal(0), ref_price=Decimal(100))
    assert intent is not None
    assert intent.qty == Decimal(1)


def test_short_enabled_opens_and_covers() -> None:
    eng = DecisionEngine(DecisionConfig(allow_short=True))
    open_short = eng.decide(comp(-9), Decimal(0), Decimal(100))
    assert open_short is not None and open_short.side == OrderSide.SELL
    cover = eng.decide(comp(9), position_qty=Decimal(-5), ref_price=Decimal(100))
    assert cover is not None and cover.side == OrderSide.BUY and cover.qty == Decimal(5)
