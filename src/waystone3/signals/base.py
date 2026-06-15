"""The signal contributor seam.

A ``SignalContributor`` takes ``{symbol -> chronological list[Bar]}`` and returns a
per-symbol ``ContributorScore`` on a normalized ``-10..+10`` scale (bullish positive).
Contributors are pure and synchronous — no config object, no network, no async — so a
new momentum input is one file implementing this protocol plus one registry line.

Scores are signed by trend direction; this is a momentum platform, so a contributor
never expresses a contrarian (mean-reversion) view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from waystone3.core.types import Bar

SCORE_MIN = Decimal(-10)
SCORE_MAX = Decimal(10)


def clamp(x: Decimal, lo: Decimal = SCORE_MIN, hi: Decimal = SCORE_MAX) -> Decimal:
    """Clamp a score into the normalized range."""
    return max(lo, min(hi, x))


def clamp_unit(x: Decimal) -> Decimal:
    """Clamp into ``[-1, 1]`` — used to saturate a ratio before scaling to ±10."""
    return max(Decimal(-1), min(Decimal(1), x))


@dataclass(frozen=True)
class ContributorScore:
    score: Decimal  # normalized -10..+10, bullish positive
    confidence: Decimal = Decimal(1)  # 0..1
    drivers: list[str] = field(default_factory=list)


@runtime_checkable
class SignalContributor(Protocol):
    name: str

    def score(self, bars: dict[str, list[Bar]]) -> dict[str, ContributorScore]:
        """Score each symbol from its bars. Symbols lacking warmup are omitted."""
        ...
