from __future__ import annotations

from decimal import Decimal

from conftest import falling, make_bars, rising

from waystone3.signals.price_action import PriceActionContributor


def test_persistent_uptrend_is_strongly_positive() -> None:
    c = PriceActionContributor()
    out = c.score({"UP": make_bars(rising(40, step=2.0), symbol="UP")})
    assert out["UP"].score > Decimal(5)


def test_persistent_downtrend_is_strongly_negative() -> None:
    c = PriceActionContributor()
    out = c.score({"DN": make_bars(falling(40, step=2.0), symbol="DN")})
    assert out["DN"].score < Decimal(-5)


def test_flat_series_scores_near_zero() -> None:
    c = PriceActionContributor()
    flat = [100.0] * 40
    out = c.score({"X": make_bars(flat, symbol="X")})
    # roc 0 and no directional alignment -> near zero
    assert abs(out["X"].score) <= Decimal(4)


def test_strong_roc_saturates() -> None:
    # a steep persistent ramp should push the score toward the max
    c = PriceActionContributor(roc_cap=Decimal("0.05"))
    out = c.score({"UP": make_bars(rising(40, step=3.0), symbol="UP")})
    assert out["UP"].score >= Decimal(9)


def test_warmup_skips_short_series() -> None:
    c = PriceActionContributor(trend_lookback=20)
    out = c.score({"X": make_bars(rising(10), symbol="X")})
    assert "X" not in out
