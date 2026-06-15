"""Signal contributors — pure functions over bars producing normalized scores."""

from waystone3.signals.base import (
    SCORE_MAX,
    SCORE_MIN,
    ContributorScore,
    SignalContributor,
    clamp,
    clamp_unit,
)

__all__ = [
    "SCORE_MAX",
    "SCORE_MIN",
    "ContributorScore",
    "SignalContributor",
    "clamp",
    "clamp_unit",
]
