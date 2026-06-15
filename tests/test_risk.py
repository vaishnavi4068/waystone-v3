from __future__ import annotations

from decimal import Decimal

import pytest

from waystone3.core.types import OrderSide
from waystone3.risk.guard import RiskGuard, RiskLimits, RiskViolationError


def test_refuses_non_paper_broker() -> None:
    with pytest.raises(RiskViolationError):
        RiskGuard(is_paper=False)


def test_allows_paper() -> None:
    g = RiskGuard(is_paper=True)
    g.check("X", OrderSide.BUY, Decimal(1), Decimal(100))  # no raise


def test_order_qty_cap() -> None:
    g = RiskGuard(limits=RiskLimits(max_order_qty=Decimal(5)))
    with pytest.raises(RiskViolationError):
        g.check("X", OrderSide.BUY, Decimal(6), Decimal(1))


def test_notional_cap() -> None:
    g = RiskGuard(limits=RiskLimits(max_notional_per_order=Decimal(1000)))
    with pytest.raises(RiskViolationError):
        g.check("X", OrderSide.BUY, Decimal(20), Decimal(100))  # 2000 > 1000


def test_position_cap_cumulative() -> None:
    g = RiskGuard(limits=RiskLimits(max_position_qty=Decimal(10)))
    g.check("X", OrderSide.BUY, Decimal(10), Decimal(1))
    g.apply_fill("X", OrderSide.BUY, Decimal(10))
    with pytest.raises(RiskViolationError):
        g.check("X", OrderSide.BUY, Decimal(1), Decimal(1))  # would be 11


def test_max_open_positions() -> None:
    g = RiskGuard(limits=RiskLimits(max_open_positions=1))
    g.check("A", OrderSide.BUY, Decimal(1), Decimal(1))
    g.apply_fill("A", OrderSide.BUY, Decimal(1))
    with pytest.raises(RiskViolationError):
        g.check("B", OrderSide.BUY, Decimal(1), Decimal(1))


def test_apply_fill_closes_position() -> None:
    g = RiskGuard()
    g.apply_fill("X", OrderSide.BUY, Decimal(5))
    g.apply_fill("X", OrderSide.SELL, Decimal(5))
    # position cleared -> a new symbol can open even at max_open_positions=1
    g2 = RiskGuard(limits=RiskLimits(max_open_positions=1))
    g2.apply_fill("X", OrderSide.BUY, Decimal(5))
    g2.apply_fill("X", OrderSide.SELL, Decimal(5))
    g2.check("Y", OrderSide.BUY, Decimal(1), Decimal(1))  # no raise
