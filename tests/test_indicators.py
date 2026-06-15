from __future__ import annotations

from decimal import Decimal

import pytest

from waystone3.indicators.core import ema, roc, rolling_mean, sma


def dec(x: object) -> Decimal:
    return Decimal(str(x))


def test_sma_basic() -> None:
    vals = [dec(1), dec(2), dec(3), dec(4)]
    assert sma(vals, 2) == dec("3.5")
    assert sma(vals, 4) == dec("2.5")


def test_sma_insufficient() -> None:
    assert sma([dec(1)], 2) is None


def test_rolling_mean_is_sma() -> None:
    vals = [dec(2), dec(4), dec(6)]
    assert rolling_mean(vals, 3) == sma(vals, 3)


def test_sma_invalid_period() -> None:
    with pytest.raises(ValueError):
        sma([dec(1)], 0)


def test_ema_constant_series() -> None:
    # EMA of a constant series is that constant.
    vals = [dec(5)] * 10
    assert ema(vals, 4)[-1] == dec(5)


def test_ema_length_and_seed() -> None:
    vals = [dec(1), dec(2), dec(3)]
    out = ema(vals, 2)
    assert len(out) == 3
    assert out[0] == dec(1)  # seeded with first value
    # alpha = 2/3; second = 1 + 2/3*(2-1) = 1.6667
    assert out[1] == pytest.approx(dec("1.6666666"), abs=1e-4)


def test_ema_empty() -> None:
    assert ema([], 5) == []


def test_roc_positive() -> None:
    vals = [dec(10), dec(11), dec(12)]
    # over lookback 2: (12 - 10)/10 = 0.2
    assert roc(vals, 2) == dec("0.2")


def test_roc_insufficient() -> None:
    assert roc([dec(10), dec(11)], 5) is None


def test_roc_zero_prior() -> None:
    assert roc([dec(0), dec(5)], 1) is None
