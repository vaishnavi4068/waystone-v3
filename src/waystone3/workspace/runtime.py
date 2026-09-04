"""Env-driven assembly of the shared workspace.

  POLYGON_API_KEY                         -> live Polygon bars/news (else offline stub)
  WAYSTONE_BROKER (alpaca|paper)          -> force the broker; unset = auto-detect
  ALPACA_API_KEY / ALPACA_API_SECRET      -> Alpaca PAPER execution (else in-process sim)
  WAYSTONE_DB                             -> SQLite (members + shared strategy + flags)
  WAYSTONE_ADMIN_TOKEN                    -> token to add team members

Paper-only platform: the Alpaca broker always runs against the paper account
(ALPACA_PAPER is pinned true); there is no live-trading path here.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from waystone3.brokers.base import Broker
from waystone3.data.base import MarketDataSource
from waystone3.data.stub import StubDataSource
from waystone3.workspace.service import WorkspaceService
from waystone3.workspace.store import WorkspaceStore
from waystone3.workspace.workspace import TradingWorkspace

log = structlog.get_logger()


def build_data_from_env() -> MarketDataSource:
    if os.getenv("POLYGON_API_KEY"):
        from waystone3.data.polygon import PolygonDataSource

        return PolygonDataSource()
    return StubDataSource()


def build_broker_from_env() -> Broker:
    """Select the broker for the shared **paper** account.

    ``WAYSTONE_BROKER`` makes the choice explicit (``alpaca`` or ``paper``); unset =
    auto-detect — Alpaca paper if both ALPACA creds are present, else the in-process sim.
    The explicit form lets a deploy pin the sim even with creds present, and surfaces a clear
    error instead of silently falling back to the sim when ``alpaca`` is requested but
    misconfigured. Either way it's paper — there is no live path. Logged at startup so
    operators can confirm the selection.
    """
    choice = os.getenv("WAYSTONE_BROKER", "").strip().lower()
    if choice not in ("", "alpaca", "paper"):
        raise ValueError(f"WAYSTONE_BROKER must be 'alpaca' or 'paper', got {choice!r}")
    has_creds = bool(os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_API_SECRET"))
    use_alpaca = choice == "alpaca" or (choice == "" and has_creds)

    if use_alpaca:
        from waystone3.brokers.alpaca import AlpacaBroker

        broker: Broker = AlpacaBroker()  # reads ALPACA_* from env; paper account
    else:
        from waystone3.brokers.paper import PaperBroker

        broker = PaperBroker()
    log.info(
        "broker_selected", broker=broker.name, is_paper=broker.is_paper, choice=choice or "auto"
    )
    return broker


def build_workspace_from_env() -> TradingWorkspace:
    db = os.getenv("WAYSTONE_DB")
    store = WorkspaceStore(db) if db else None
    return TradingWorkspace(build_data_from_env(), build_broker_from_env(), store=store)


def build_service_from_env() -> WorkspaceService:
    return WorkspaceService(
        build_workspace_from_env(), admin_token=os.getenv("WAYSTONE_ADMIN_TOKEN")
    )


def seed_members(
    service: WorkspaceService,
    names: list[str],
    passwords: list[str | None] | None = None,
) -> list[dict[str, Any]]:
    """Register up to ``max_members`` dashboard users with unique passwords."""
    if not names:
        raise ValueError("at least one player name is required")
    if len(names) > service.ws.max_members:
        raise ValueError(f"at most {service.ws.max_members} dashboard users")
    lowered = [n.strip().lower() for n in names]
    if len(lowered) != len(set(lowered)):
        raise ValueError("player names must be unique")
    pws = list(passwords) if passwords is not None else [None] * len(names)
    if len(pws) != len(names):
        raise ValueError("passwords count must match players")
    provided = [p for p in pws if p]
    if len(provided) != len(set(provided)):
        raise ValueError("passwords must be unique")
    return [
        service.register(service.admin_token, name, password=pw)
        for name, pw in zip(names, pws, strict=True)
    ]
