"""Env-driven assembly of the shared workspace.

  POLYGON_API_KEY                         -> live Polygon bars/news (else offline stub)
  ALPACA_API_KEY / ALPACA_API_SECRET      -> real Alpaca paper execution (else in-process sim)
  ALPACA_PAPER (default true)             -> paper vs live (keep true)
  WAYSTONE_DB                             -> SQLite (members + shared strategy + flags)
  WAYSTONE_ADMIN_TOKEN                    -> token to add team members
"""

from __future__ import annotations

import os
from typing import Any

from waystone3.brokers.base import Broker
from waystone3.data.base import MarketDataSource
from waystone3.data.stub import StubDataSource
from waystone3.workspace.service import WorkspaceService
from waystone3.workspace.store import WorkspaceStore
from waystone3.workspace.workspace import TradingWorkspace


def build_data_from_env() -> MarketDataSource:
    if os.getenv("POLYGON_API_KEY"):
        from waystone3.data.polygon import PolygonDataSource

        return PolygonDataSource()
    return StubDataSource()


def build_broker_from_env() -> Broker:
    if os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_API_SECRET"):
        from waystone3.brokers.alpaca import AlpacaBroker

        return AlpacaBroker()  # reads ALPACA_* + ALPACA_PAPER from env
    from waystone3.brokers.paper import PaperBroker

    return PaperBroker()


def build_workspace_from_env() -> TradingWorkspace:
    db = os.getenv("WAYSTONE_DB")
    store = WorkspaceStore(db) if db else None
    return TradingWorkspace(build_data_from_env(), build_broker_from_env(), store=store)


def build_service_from_env() -> WorkspaceService:
    return WorkspaceService(
        build_workspace_from_env(), admin_token=os.getenv("WAYSTONE_ADMIN_TOKEN")
    )


def seed_members(service: WorkspaceService, names: list[str]) -> list[dict[str, Any]]:
    return [service.register(service.admin_token, name) for name in names]
