"""Pure indicator functions.

All operate on chronologically-ordered ``list[Decimal]`` (oldest first) and return a
plain value or a same-length series. No I/O, no state — trivially unit-testable.
"""

from __future__ import annotations

from decimal import Decimal


def sma(values: list[Decimal], period: int) -> Decimal | None:
    """Simple moving average of the last ``period`` values. None if too few."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


def rolling_mean(values: list[Decimal], period: int) -> Decimal | None:
    """Alias for :func:`sma`, named for readability where it's a trailing average."""
    return sma(values, period)


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    """Exponential moving average series, same length as ``values``.

    Seeded with the first value; ``alpha = 2 / (period + 1)``. Returns an empty list
    for empty input. Index ``-1`` is the most recent EMA.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    alpha = Decimal(2) / Decimal(period + 1)
    out: list[Decimal] = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + alpha * (v - out[-1]))
    return out


def roc(values: list[Decimal], lookback: int) -> Decimal | None:
    """Rate of change over ``lookback`` bars: (last - prior) / prior.

    None if too few values or the prior value is zero.
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if len(values) < lookback + 1:
        return None
    prior = values[-1 - lookback]
    if prior == 0:
        return None
    return (values[-1] - prior) / prior
