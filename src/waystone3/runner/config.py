"""Runner configuration and sensible momentum defaults."""

from __future__ import annotations

from dataclasses import dataclass, field

from waystone3.core.types import Timeframe
from waystone3.decision.engine import DecisionConfig
from waystone3.signals.base import SignalContributor


def default_weights() -> dict[str, float]:
    """Technical-first blend. Sentiment, when added, is just another key here."""
    return {"ma_crossover": 0.4, "price_action": 0.4, "volume": 0.2}


def default_contributors() -> list[SignalContributor]:
    """Built from the registry so the live default tracks what's installed."""
    from waystone3.signals.registry import CONTRIBUTORS

    return [CONTRIBUTORS[name]() for name in default_weights() if name in CONTRIBUTORS]


@dataclass
class RunnerConfig:
    watchlist: list[str]
    timeframe: Timeframe = Timeframe.D1
    bars_lookback: int = 80
    weights: dict[str, float] = field(default_factory=default_weights)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
