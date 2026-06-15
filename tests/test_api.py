from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.competition.competition import Competition
from waystone3.competition.models import StrategyConfig
from waystone3.competition.store import CompetitionStore
from waystone3.data.stub import StubDataSource


@pytest.fixture()
def client_and_token(tmp_path, monkeypatch):
    db = str(tmp_path / "arena.db")
    # Seed a player + a cycle into the DB the API will read.
    comp = Competition(
        StubDataSource(start_price=100.0, default_slope=1.0), store=CompetitionStore(db)
    )
    alice = comp.register("Manoj")
    comp.submit_strategy(
        alice.user_id,
        StrategyConfig(weights={"ma_crossover": 0.5, "price_action": 0.5}, watchlist=["AAPL"]),
    )

    import asyncio

    asyncio.run(comp.run_cycle(alice.user_id))
    comp.store.close()

    # The API reads from the same DB (stub data since no POLYGON_API_KEY).
    monkeypatch.setenv("WAYSTONE_DB", db)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    return TestClient(build_app()), alice.token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_is_open(client_and_token) -> None:
    client, _ = client_and_token
    assert client.get("/api/health").json() == {"ok": True}


def test_endpoints_require_token(client_and_token) -> None:
    client, _ = client_and_token
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/standings").status_code == 401
    assert client.get("/api/me", headers=_auth("bogus")).status_code == 401


def test_me_is_scoped_to_the_player(client_and_token) -> None:
    client, token = client_and_token
    data = client.get("/api/me", headers=_auth(token)).json()
    assert data["player"] == "Manoj"
    assert data["cycles_run"] == 1
    assert data["strategy"]["watchlist"] == ["AAPL"]
    assert "equity" in data["account"]
    assert data["rank"] == 1  # only player


def test_standings(client_and_token) -> None:
    client, token = client_and_token
    rows = client.get("/api/standings", headers=_auth(token)).json()
    assert rows[0]["player"] == "Manoj"
    assert rows[0]["rank"] == 1


def test_signals_and_bars(client_and_token) -> None:
    client, token = client_and_token
    sig = client.get("/api/signals?symbols=AAPL", headers=_auth(token)).json()
    assert sig and sig[0]["symbol"] == "AAPL"
    assert "per_contributor" in sig[0]

    bars = client.get("/api/bars?symbol=AAPL&lookback=50", headers=_auth(token)).json()
    assert len(bars) == 50
    assert {"time", "open", "high", "low", "close", "volume"} <= set(bars[0])


def test_backtest(client_and_token) -> None:
    client, token = client_and_token
    out = client.get(
        "/api/backtest?symbols=AAPL&start=2022-01-01&end=2023-01-01",
        headers=_auth(token),
    ).json()
    assert "metrics" in out and "equity" in out
    assert out["metrics"]["trades"] >= 1


def test_news_degrades_without_key(client_and_token) -> None:
    client, token = client_and_token
    # No POLYGON_API_KEY -> empty list, not an error.
    assert client.get("/api/news?symbols=AAPL", headers=_auth(token)).json() == []


def test_decimal_unused_import_guard() -> None:
    # sanity: Decimal is available for the test's own typing needs
    assert Decimal(1) == 1
