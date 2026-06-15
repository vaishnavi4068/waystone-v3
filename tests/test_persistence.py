from __future__ import annotations

from decimal import Decimal

from waystone3.brokers.paper import PaperBroker
from waystone3.competition.competition import Competition
from waystone3.competition.models import StrategyConfig
from waystone3.competition.store import CompetitionStore, config_from_json, config_to_json
from waystone3.core.types import OrderSide, Timeframe
from waystone3.data.stub import StubDataSource


async def test_paper_broker_state_roundtrip() -> None:
    b = PaperBroker(initial_cash=Decimal(100_000))
    await b.submit_order("AAPL", OrderSide.BUY, Decimal(10), ref_price=Decimal(150))
    b.set_mark("AAPL", Decimal(160))
    state = b.export_state()

    restored = PaperBroker.from_state(state)
    acct = await restored.get_account()
    assert acct.cash == Decimal(100_000) - Decimal(1500)
    positions = await restored.get_positions()
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == Decimal(10)
    assert positions[0].unrealized_pnl == Decimal(100)  # marked 160, entry 150, qty 10


def test_config_json_roundtrip() -> None:
    cfg = StrategyConfig(
        weights={"ma_crossover": 0.5, "price_action": 0.5},
        watchlist=["AAPL", "MSFT"],
        bullish_threshold=Decimal("2.5"),
        timeframe=Timeframe.D1,
    )
    back = config_from_json(config_to_json(cfg))
    assert back.weights == cfg.weights
    assert back.watchlist == cfg.watchlist
    assert back.bullish_threshold == Decimal("2.5")


def _comp(db: str) -> Competition:
    return Competition(
        StubDataSource(start_price=100.0, default_slope=1.0), store=CompetitionStore(db)
    )


def _cfg() -> StrategyConfig:
    return StrategyConfig(weights={"ma_crossover": 1.0}, watchlist=["AAPL"])


async def test_competition_survives_restart(tmp_path) -> None:
    db = str(tmp_path / "arena.db")

    # Session 1: register, submit, run a cycle.
    comp = _comp(db)
    alice = comp.register("Alice")
    token = alice.token
    comp.submit_strategy(alice.user_id, _cfg())
    await comp.run_cycle(alice.user_id)
    acct_before = await alice.broker.get_account()
    comp.store.close()

    # Session 2: brand-new Competition pointed at the same DB — state restored.
    comp2 = _comp(db)
    reloaded = comp2.authenticate(token)
    assert reloaded is not None
    assert reloaded.display_name == "Alice"
    assert reloaded.cycles_run == 1
    assert reloaded.config is not None
    acct_after = await reloaded.broker.get_account()
    assert acct_after.cash == acct_before.cash  # paper account preserved

    # New registrations continue numbering past the restored players.
    bob = comp2.register("Bob")
    assert bob.user_id != alice.user_id


async def test_standings_after_restart(tmp_path) -> None:
    db = str(tmp_path / "arena2.db")
    comp = _comp(db)
    a = comp.register("Alice")
    comp.submit_strategy(a.user_id, _cfg())
    await comp.run_cycle(a.user_id)
    comp.store.close()

    comp2 = _comp(db)
    standings = await comp2.standings()
    assert standings[0].display_name == "Alice"
    assert standings[0].cycles_run == 1
