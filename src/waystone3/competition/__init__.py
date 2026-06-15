"""Multi-user paper-trading strategy competition."""

from waystone3.competition.competition import Competition
from waystone3.competition.models import Entry, Standing, StrategyConfig
from waystone3.competition.service import AuthError, CompetitionService

__all__ = [
    "AuthError",
    "Competition",
    "CompetitionService",
    "Entry",
    "Standing",
    "StrategyConfig",
]
