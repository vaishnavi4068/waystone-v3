from __future__ import annotations

import asyncio

import pytest

from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.workspace.runtime import seed_members
from waystone3.workspace.service import AuthError, WorkspaceService
from waystone3.workspace.workspace import TradingWorkspace


def _service() -> WorkspaceService:
    ws = TradingWorkspace(
        StubDataSource(start_price=100.0, default_slope=1.0), PaperBroker()
    )
    return WorkspaceService(ws, admin_token="ADMIN")


def test_add_member_requires_admin() -> None:
    svc = _service()
    with pytest.raises(AuthError):
        svc.register("wrong", "Manoj")
    out = svc.register("ADMIN", "Manoj")
    assert out["name"] == "Manoj" and len(out["token"]) > 10
    assert len(out["password"]) >= 8
    logged_in = svc.login("Manoj", out["password"])
    assert logged_in["token"] == out["token"]
    with pytest.raises(AuthError):
        svc.login("Manoj", "not-the-password")


def test_seed_five_unique_passwords() -> None:
    svc = _service()
    created = seed_members(
        svc,
        ["Manoj", "Mark", "Brent", "Akash", "Cole"],
        ["pass-manoj1", "pass-mark12", "pass-brent1", "pass-akash1", "pass-cole12"],
    )
    assert len(created) == 5
    assert len({row["password"] for row in created}) == 5
    assert svc.login("Cole", "pass-cole12")["name"] == "Cole"
    with pytest.raises(ValueError, match="at most"):
        seed_members(_service(), ["A", "B", "C", "D", "E", "F"])
    with pytest.raises(ValueError, match="unique"):
        seed_members(
            _service(),
            ["A", "B"],
            ["same-password", "same-password"],
        )


def test_invalid_token_rejected() -> None:
    svc = _service()
    with pytest.raises(AuthError):
        svc.set_strategy("nope", weights={"ma_crossover": 1.0}, watchlist=["AAPL"])


def test_shared_flow() -> None:
    svc = _service()
    tok = svc.register("ADMIN", "Manoj")["token"]
    svc.set_strategy(tok, weights={"ma_crossover": 0.6, "price_action": 0.4}, watchlist=["AAPL"])

    async def flow() -> None:
        cycle = await svc.run_cycle(tok)
        assert cycle["orders_submitted"] >= 1
        assert cycle["by"] == "Manoj"
        acct = await svc.account(tok)
        assert acct["cash"] < 100_000
        assert acct["is_paper"] is True
        pos = await svc.positions(tok)
        assert pos and pos[0]["symbol"] == "AAPL"
        orders = await svc.orders(tok)
        assert orders

    asyncio.run(flow())


def test_halt_and_resume() -> None:
    svc = _service()
    tok = svc.register("ADMIN", "Manoj")["token"]
    svc.set_strategy(tok, weights={"ma_crossover": 1.0}, watchlist=["AAPL"])
    assert svc.halt(tok, "pause")["trading_enabled"] is False

    async def run() -> dict:
        return await svc.run_cycle(tok)

    assert asyncio.run(run())["orders_submitted"] == 0  # halted
    assert svc.resume(tok)["trading_enabled"] is True


def test_backtest_uses_shared_strategy() -> None:
    svc = _service()
    tok = svc.register("ADMIN", "Manoj")["token"]
    svc.set_strategy(tok, weights={"ma_crossover": 1.0}, watchlist=["AAPL"])
    out = asyncio.run(svc.backtest(tok, "2022-01-01", "2023-01-01"))
    assert out["trades"] >= 1


def test_two_members_share_one_account() -> None:
    svc = _service()
    a = svc.register("ADMIN", "Manoj")["token"]
    b = svc.register("ADMIN", "Mark")["token"]
    svc.set_strategy(a, weights={"ma_crossover": 1.0}, watchlist=["AAPL"])
    asyncio.run(svc.run_cycle(a))  # Manoj trades
    # Mark sees the same account state
    acct_seen_by_mark = asyncio.run(svc.account(b))
    assert acct_seen_by_mark["cash"] < 100_000
