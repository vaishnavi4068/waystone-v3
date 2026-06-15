from __future__ import annotations

import pytest

from waystone3.competition.competition import Competition
from waystone3.competition.service import AuthError, CompetitionService
from waystone3.data.stub import StubDataSource


def _service() -> CompetitionService:
    return CompetitionService(
        Competition(StubDataSource(start_price=100.0, default_slope=1.0)),
        admin_token="ADMIN",
    )


def test_register_requires_admin_token() -> None:
    svc = _service()
    with pytest.raises(AuthError):
        svc.register("wrong", "Alice")
    out = svc.register("ADMIN", "Alice")
    assert out["display_name"] == "Alice"
    assert len(out["token"]) > 10


def test_invalid_token_rejected() -> None:
    svc = _service()
    with pytest.raises(AuthError):
        svc.submit_strategy("nope", weights={"ma_crossover": 1.0}, watchlist=["AAPL"])


def test_submit_then_run_cycle_then_standings() -> None:
    import asyncio

    svc = _service()
    tok = svc.register("ADMIN", "Alice")["token"]
    svc.submit_strategy(
        tok, weights={"ma_crossover": 0.5, "price_action": 0.5}, watchlist=["AAPL"]
    )

    async def flow() -> None:
        cycle = await svc.run_cycle(tok)
        assert cycle["orders"] >= 1
        acct = await svc.my_account(tok)
        assert acct["cycles_run"] == 1
        board = await svc.standings(tok)
        assert board[0]["player"] == "Alice"
        assert board[0]["rank"] == 1

    asyncio.run(flow())


def test_backtest_via_service() -> None:
    import asyncio

    svc = _service()
    tok = svc.register("ADMIN", "Bob")["token"]
    svc.submit_strategy(tok, weights={"ma_crossover": 1.0}, watchlist=["AAPL"])
    metrics = asyncio.run(svc.run_backtest(tok, "2022-01-01", "2023-01-01"))
    assert metrics["trades"] >= 1
    assert "total_return_pct" in metrics


def test_standings_requires_valid_token() -> None:
    import asyncio

    svc = _service()
    with pytest.raises(AuthError):
        asyncio.run(svc.standings("bogus"))


def test_mcp_server_builds_with_tools() -> None:
    # The thin transport wires the service into FastMCP without error.
    from waystone3.mcp_server import build_mcp

    mcp = build_mcp(_service())
    assert mcp.name == "waystone-arena"
