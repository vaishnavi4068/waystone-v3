from __future__ import annotations

from decimal import Decimal

from conftest import falling, make_bars, rising

from waystone3.signals.ma_crossover import MaCrossoverContributor


def test_uptrend_is_strongly_positive() -> None:
    c = MaCrossoverContributor()
    out = c.score({"UP": make_bars(rising(80), symbol="UP")})
    assert out["UP"].score > Decimal(5)


def test_downtrend_is_strongly_negative() -> None:
    c = MaCrossoverContributor()
    out = c.score({"DN": make_bars(falling(80), symbol="DN")})
    assert out["DN"].score < Decimal(-5)


def test_steady_uptrend_saturates_near_max() -> None:
    # A steep, persistent uptrend should saturate the score toward +10.
    c = MaCrossoverContributor()
    out = c.score({"UP": make_bars(rising(80, step=3.0), symbol="UP")})
    assert out["UP"].score >= Decimal(8)


def test_warmup_skips_short_series() -> None:
    c = MaCrossoverContributor(fast=10, slow=30)
    out = c.score({"SHORT": make_bars(rising(20), symbol="SHORT")})
    assert "SHORT" not in out


def test_sma_mode_matches_sign() -> None:
    c = MaCrossoverContributor(use_ema=False)
    out = c.score({"UP": make_bars(rising(80), symbol="UP")})
    assert out["UP"].score > Decimal(0)


def test_invalid_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        MaCrossoverContributor(fast=30, slow=10)
