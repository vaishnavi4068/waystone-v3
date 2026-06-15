"""Weighted fusion of contributor scores.

Generalized from the proven weighted-average pattern, with no special-casing of any one
signal: every contributor is just a key with a weight. A symbol's composite is the
weight-weighted mean of the contributors that scored it (a contributor that omits a
symbol simply doesn't vote on it). Optionally each contributor's weight is scaled by its
per-symbol confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from waystone3.signals.base import ContributorScore, clamp


@dataclass(frozen=True)
class CompositeScore:
    symbol: str
    score: Decimal  # -10..+10
    drivers: list[str]
    per_contributor: dict[str, Decimal]


def fuse(
    per_contributor: dict[str, dict[str, ContributorScore]],
    weights: dict[str, float],
    *,
    use_confidence: bool = False,
) -> list[CompositeScore]:
    """Blend ``{contributor_name -> {symbol -> ContributorScore}}`` into composites.

    Contributors are visited in a deterministic (alphabetical) order. Zero or missing
    weights are ignored. Results are sorted by absolute composite score, descending, so
    the strongest convictions come first.
    """
    symbols: set[str] = set()
    for scores in per_contributor.values():
        symbols.update(scores)

    composites: list[CompositeScore] = []
    for symbol in symbols:
        num = Decimal(0)
        den = Decimal(0)
        drivers: list[str] = []
        per: dict[str, Decimal] = {}
        for name in sorted(per_contributor):
            weight = Decimal(str(weights.get(name, 0.0)))
            if weight <= 0:
                continue
            cs = per_contributor[name].get(symbol)
            if cs is None:
                continue
            eff = weight * cs.confidence if use_confidence else weight
            if eff <= 0:
                continue
            num += eff * cs.score
            den += eff
            per[name] = cs.score
            drivers.extend(f"{name}:{d}" for d in cs.drivers)
        if den == 0:
            continue
        composites.append(
            CompositeScore(
                symbol=symbol,
                score=clamp(num / den),
                drivers=drivers,
                per_contributor=per,
            )
        )

    composites.sort(key=lambda c: abs(c.score), reverse=True)
    return composites
