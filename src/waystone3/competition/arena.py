"""Environment-driven assembly of the hosted Arena.

Reads config from env so the same image works locally and on GKE:
  POLYGON_API_KEY      -> live Polygon bars/news (else the offline stub)
  WAYSTONE_DB          -> SQLite path for durable players/leaderboard (else in-memory)
  WAYSTONE_ADMIN_TOKEN -> organizer token used to register players
"""

from __future__ import annotations

import os
from typing import Any

from waystone3.competition.competition import Competition
from waystone3.competition.service import CompetitionService
from waystone3.competition.store import CompetitionStore
from waystone3.data.base import MarketDataSource
from waystone3.data.stub import StubDataSource


def build_competition_from_env() -> Competition:
    data: MarketDataSource
    if os.getenv("POLYGON_API_KEY"):
        from waystone3.data.polygon import PolygonDataSource

        data = PolygonDataSource()
    else:
        data = StubDataSource()
    db = os.getenv("WAYSTONE_DB")
    store = CompetitionStore(db) if db else None
    return Competition(data, store=store)


def build_service_from_env() -> CompetitionService:
    return CompetitionService(
        build_competition_from_env(), admin_token=os.getenv("WAYSTONE_ADMIN_TOKEN")
    )


def seed_players(service: CompetitionService, names: list[str]) -> list[dict[str, Any]]:
    """Register players using the service's admin token; returns their access tokens."""
    return [service.register(service.admin_token, name) for name in names]
