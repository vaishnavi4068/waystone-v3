"""Contributor registry — the single place new signals are wired in.

Adding a momentum input: implement ``SignalContributor`` in a new module, then add one
line here. Nothing else in the pipeline changes. A future ``sentiment`` contributor
slots in exactly the same way.
"""

from __future__ import annotations

from typing import Any

from waystone3.signals.base import SignalContributor
from waystone3.signals.ma_crossover import MaCrossoverContributor
from waystone3.signals.price_action import PriceActionContributor
from waystone3.signals.sentiment import SentimentContributor
from waystone3.signals.volume import VolumeContributor

CONTRIBUTORS: dict[str, type[SignalContributor]] = {
    "ma_crossover": MaCrossoverContributor,
    "price_action": PriceActionContributor,
    "volume": VolumeContributor,
    # Opt-in: with no injected scores it contributes nothing, so it's safe to register
    # but absent from default_weights() until you wire news + a scorer.
    "sentiment": SentimentContributor,
}


def build_contributor(name: str, **params: Any) -> SignalContributor:
    try:
        cls = CONTRIBUTORS[name]
    except KeyError:
        raise KeyError(
            f"unknown contributor {name!r}; known: {sorted(CONTRIBUTORS)}"
        ) from None
    return cls(**params)
