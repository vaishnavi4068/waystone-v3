from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from waystone3.competition.competition import Competition
from waystone3.competition.models import StrategyConfig
from waystone3.data.stub import StubDataSource


def momentum_config(symbols: list[str]) -> StrategyConfig:
    return StrategyConfig(
        weights={"ma_crossover": 0.5, "price_action": 0.5}, watchlist=symbols
    )


def test_register_and_authenticate() -> None:
    comp = Competition(StubDataSource())
    entry = comp.register("Alice")
    assert entry.user_id.startswith("u")
    assert comp.authenticate(entry.token) is entry
    assert comp.authenticate("bogus") is None


def test_max_players_enforced() -> None:
    comp = Competition(StubDataSource(), max_players=2)
    comp.register("A")
    comp.register("B")
    with pytest.raises(ValueError):
        comp.register("C")


def test_submit_validates_config() -> None:
    comp = Competition(StubDataSource())
    e = comp.register("Alice")
    with pytest.raises(ValueError):
        comp.submit_strategy(e.user_id, StrategyConfig(weights={"bogus": 1.0}, watchlist=["AAPL"]))
    with pytest.raises(ValueError):
        comp.submit_strategy(e.user_id, momentum_config([]))  # empty watchlist


async def test_run_cycle_requires_strategy() -> None:
    comp = Competition(StubDataSource())
    e = comp.register("Alice")
    with pytest.raises(ValueError):
        await comp.run_cycle(e.user_id)


async def test_momentum_player_beats_idle_player() -> None:
    comp = Competition(StubDataSource(start_price=100.0, default_slope=1.0))
    alice = comp.register("Alice")
    bob = comp.register("Bob")
    comp.submit_strategy(alice.user_id, momentum_config(["AAPL"]))
    # Bob submits a strategy too but never runs a cycle -> stays flat at initial cash.
    comp.submit_strategy(bob.user_id, momentum_config(["AAPL"]))

    await comp.run_cycle(alice.user_id)  # buys into the uptrend at the current price

    # Market moves up before scoring (time passes / new bars arrive).
    comp.data = StubDataSource(start_price=160.0, default_slope=1.0)

    standings = await comp.standings()
    assert standings[0].rank == 1
    assert standings[0].display_name == "Alice"  # her long position appreciated
    assert standings[0].return_pct > Decimal(0)
    assert standings[1].display_name == "Bob"
    assert standings[1].return_pct == Decimal(0)  # never traded


async def test_run_backtest_returns_metrics() -> None:
    comp = Competition(StubDataSource(default_slope=1.0))
    e = comp.register("Alice")
    comp.submit_strategy(e.user_id, momentum_config(["AAPL"]))
    metrics = await comp.run_backtest(
        e.user_id, datetime(2022, 1, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC)
    )
    assert metrics.num_trades >= 1
    assert metrics.total_return > 0
