"""Volume contributor.

A confirmation signal: it asks whether the latest move is backed by a volume expansion,
and signs that by the bar's own direction. High volume on an up-bar is bullish
confirmation; high volume on a down-bar is bearish; quiet bars score near zero.
"""

from __future__ import annotations

from decimal import Decimal

from waystone3.core.types import Bar
from waystone3.signals.base import ContributorScore, clamp, clamp_unit


class VolumeContributor:
    name = "volume"

    def __init__(self, lookback: int = 20, cap: Decimal = Decimal("3")) -> None:
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if cap <= 1:
            raise ValueError("cap must be > 1")
        self.lookback = lookback
        self.cap = cap

    def score(self, bars: dict[str, list[Bar]]) -> dict[str, ContributorScore]:
        out: dict[str, ContributorScore] = {}
        for symbol, series in bars.items():
            if len(series) < self.lookback + 1:
                continue
            closes = [b.close for b in series]
            volumes = [b.volume for b in series]
            trailing = volumes[-self.lookback - 1 : -1]
            avg_vol = sum(trailing, Decimal(0)) / Decimal(len(trailing))
            if avg_vol <= 0:
                continue
            rel_vol = volumes[-1] / avg_vol
            move = closes[-1] - closes[-2]
            direction = Decimal(1) if move > 0 else Decimal(-1) if move < 0 else Decimal(0)
            magnitude = clamp_unit((rel_vol - 1) / (self.cap - 1))
            score = clamp(direction * magnitude * 10)
            out[symbol] = ContributorScore(
                score=score,
                confidence=clamp_unit(rel_vol / self.cap),
                drivers=[f"rel_vol={rel_vol:.2f}"],
            )
        return out
