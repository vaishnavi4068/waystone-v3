from __future__ import annotations

from decimal import Decimal

import pytest

from waystone3.brokers.paper import PaperBroker
from waystone3.competition.models import StrategyConfig
from waystone3.data.stub import StubDataSource
from waystone3.workspace.runtime import DEFAULT_DASHBOARD_USERS
from waystone3.workspace.store import WorkspaceStore
from waystone3.workspace.workspace import TradingWorkspace, WorkspaceError


def _ws(store: WorkspaceStore | None = None) -> TradingWorkspace:
    return TradingWorkspace(
        StubDataSource(start_price=100.0, default_slope=1.0), PaperBroker(), store=store
    )


def _strategy() -> StrategyConfig:
    return StrategyConfig(
        weights={"ma_crossover": 0.6, "price_action": 0.4}, watchlist=["AAPL", "MSFT"]
    )


def test_register_and_authenticate() -> None:
    ws = _ws()
    m = ws.register_member("Manoj")
    assert ws.authenticate(m.token) == "Manoj"
    assert ws.authenticate("bogus") is None
    assert "Manoj" in ws.members()
    assert m.password  # unique dashboard password issued at registration
    assert ws.authenticate_password("Manoj", m.password) == m.token
    assert ws.authenticate_password("manoj", m.password) == m.token  # case-insensitive name
    assert ws.authenticate_password("Manoj", "wrong-password") is None
    assert ws.authenticate_password("Unknown", m.password) is None


def test_explicit_password_and_duplicate_name() -> None:
    ws = _ws()
    m = ws.register_member("Manoj", password="unique-pass-1")
    assert ws.authenticate_password("Manoj", "unique-pass-1") == m.token
    with pytest.raises(WorkspaceError, match="already exists"):
        ws.register_member("manoj", password="unique-pass-2")
    with pytest.raises(WorkspaceError, match="at least 8"):
        ws.register_member("Mark", password="short")


def test_five_users_max_with_unique_passwords() -> None:
    ws = TradingWorkspace(StubDataSource(), PaperBroker(), max_members=5)
    issued: set[str] = set()
    for name in DEFAULT_DASHBOARD_USERS:
        member = ws.register_member(name)
        assert member.password not in issued
        issued.add(member.password)
        assert ws.authenticate_password(name, member.password) == member.token
    with pytest.raises(WorkspaceError, match="team is full"):
        ws.register_member("Extra")


def test_team_is_capped() -> None:
    ws = TradingWorkspace(StubDataSource(), PaperBroker(), max_members=2)
    ws.register_member("A")
    ws.register_member("B")
    with pytest.raises(WorkspaceError):
        ws.register_member("C")


async def test_run_cycle_requires_strategy() -> None:
    ws = _ws()
    with pytest.raises(WorkspaceError):
        await ws.run_cycle("Manoj")


async def test_run_cycle_places_orders_on_shared_account() -> None:
    ws = _ws()
    ws.set_strategy(_strategy(), "Manoj")
    report = await ws.run_cycle("Manoj")
    assert len(report.orders) >= 1  # uptrend -> buys
    positions = await ws.broker.get_positions()
    assert positions  # the shared account now holds something
    # everyone sees the same shared account
    acct = await ws.broker.get_account()
    assert acct.cash < Decimal(100_000)


async def test_kill_switch_blocks_trading() -> None:
    ws = _ws()
    ws.set_strategy(_strategy(), "Manoj")
    ws.set_trading(False, "Mark")
    report = await ws.run_cycle("Manoj")
    assert report.orders == []  # halted -> no orders
    assert await ws.broker.get_positions() == []


async def test_audit_records_actions() -> None:
    ws = _ws()
    ws.set_strategy(_strategy(), "Manoj")
    await ws.run_cycle("Mark")
    actions = [(e.actor, e.action) for e in ws.audit]
    assert ("Manoj", "set_strategy") in actions
    assert ("Mark", "run_cycle") in actions


async def test_persistence_across_restart(tmp_path) -> None:
    db = str(tmp_path / "ws.db")
    ws = _ws(WorkspaceStore(db))
    m = ws.register_member("Manoj")
    ws.set_strategy(_strategy(), "Manoj")
    ws.set_trading(False, "Manoj")
    ws.store.close()

    ws2 = _ws(WorkspaceStore(db))
    assert ws2.authenticate(m.token) == "Manoj"     # member restored
    assert ws2.authenticate_password("Manoj", m.password) == m.token
    assert ws2.strategy is not None                 # shared strategy restored
    assert ws2.strategy.watchlist == ["AAPL", "MSFT"]
    assert ws2.trading_enabled is False             # kill-switch restored
