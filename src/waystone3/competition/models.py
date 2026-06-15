"""Competition domain types."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from waystone3.brokers.paper import PaperBroker
from waystone3.core.types import Timeframe
from waystone3.risk.guard import RiskGuard


@dataclass(frozen=True)
class StrategyConfig:
    """A competitor's submitted strategy — the tunable knobs of the engine."""

    weights: dict[str, float]
    watchlist: list[str]
    bullish_threshold: Decimal = Decimal(3)
    bearish_threshold: Decimal = Decimal(-3)
    notional_per_trade: Decimal = Decimal(2000)
    timeframe: Timeframe = Timeframe.D1
    lookback: int = 80

    def validate(self) -> None:
        from waystone3.signals.registry import CONTRIBUTORS

        unknown = [name for name in self.weights if name not in CONTRIBUTORS]
        if unknown:
            raise ValueError(f"unknown contributors in weights: {unknown}")
        if not any(w > 0 for w in self.weights.values()):
            raise ValueError("at least one contributor must have a positive weight")
        if not self.watchlist:
            raise ValueError("watchlist must not be empty")


@dataclass
class Entry:
    user_id: str
    display_name: str
    token: str
    broker: PaperBroker
    guard: RiskGuard
    config: StrategyConfig | None = None
    cycles_run: int = 0


@dataclass(frozen=True)
class Standing:
    rank: int
    display_name: str
    equity: Decimal
    return_pct: Decimal
    cycles_run: int
    has_strategy: bool = field(default=True)
