from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.competition.models import StrategyConfig
from waystone3.data.stub import StubDataSource
from waystone3.workspace.workspace import TradingWorkspace


@pytest.fixture()
def client_and_token():
    # One shared in-memory workspace (in prod the shared backend is Alpaca + SQLite; with
    # the in-process PaperBroker we inject a single instance so the API sees the same state).
    ws = TradingWorkspace(StubDataSource(start_price=100.0, default_slope=1.0), PaperBroker())
    m = ws.register_member("Manoj")
    ws.set_strategy(
        StrategyConfig(weights={"ma_crossover": 0.6, "price_action": 0.4}, watchlist=["AAPL"]),
        "Manoj",
    )
    asyncio.run(ws.run_cycle("Manoj"))
    return TestClient(build_app(workspace_factory=lambda: ws)), m.token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_open(client_and_token) -> None:
    client, _ = client_and_token
    assert client.get("/api/health").json() == {"ok": True}


def test_login_issues_token() -> None:
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj", password="unique-pass-1")
    client = TestClient(build_app(workspace_factory=lambda: ws))

    denied = client.post("/api/login", json={"username": "Manoj", "password": "nope"})
    assert denied.status_code == 401

    ok = client.post("/api/login", json={"username": "Manoj", "password": "unique-pass-1"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["name"] == "Manoj"
    assert body["token"] == member.token
    assert client.get("/api/account", headers=_auth(body["token"])).json()["you"] == "Manoj"


def test_auth_required(client_and_token) -> None:
    client, _ = client_and_token
    assert client.get("/api/account").status_code == 401
    assert client.get("/api/account", headers=_auth("bogus")).status_code == 401


def test_account_is_shared(client_and_token) -> None:
    client, token = client_and_token
    data = client.get("/api/account", headers=_auth(token)).json()
    assert data["you"] == "Manoj"
    assert "Manoj" in data["team"]
    assert data["is_paper"] is True
    assert data["strategy"]["watchlist"] == ["AAPL"]
    # a cycle ran in the fixture, so cash dropped below the 100k start
    assert data["cash"] < 100_000


def test_positions_orders_activity(client_and_token) -> None:
    client, token = client_and_token
    assert client.get("/api/positions", headers=_auth(token)).json()  # has a position
    assert client.get("/api/orders", headers=_auth(token)).json()  # has an order
    activity = client.get("/api/activity", headers=_auth(token)).json()
    assert any(a["action"] == "run_cycle" for a in activity)


def test_signals_and_bars(client_and_token) -> None:
    client, token = client_and_token
    sig = client.get("/api/signals?symbols=AAPL", headers=_auth(token)).json()
    assert sig and sig[0]["symbol"] == "AAPL" and "per_contributor" in sig[0]
    bars = client.get("/api/bars?symbol=AAPL&lookback=50", headers=_auth(token)).json()
    assert len(bars) == 50


def test_backtest_and_news(client_and_token) -> None:
    client, token = client_and_token
    bt = client.get(
        "/api/backtest?symbols=AAPL&start=2022-01-01&end=2023-01-01", headers=_auth(token)
    ).json()
    assert bt["metrics"]["trades"] >= 1
    assert client.get("/api/news?symbols=AAPL", headers=_auth(token)).json() == []  # no key


def test_hq_routes_404_without_database(client_and_token) -> None:
    client, token = client_and_token
    assert client.get("/api/hq/strategies", headers=_auth(token)).status_code == 404


def test_hq_dsn_from_env() -> None:
    from waystone3.hq.reader import hq_dsn_from_env

    assert hq_dsn_from_env({}) is None
    assert hq_dsn_from_env({"WAYSTONE_HQ_DSN": "host=x"}) == "host=x"
    dsn = hq_dsn_from_env({"WAYSTONE_HQ_DB_HOST": "10.1.2.3", "WAYSTONE_HQ_DB_PASSWORD": "p w"})
    assert dsn is not None
    assert "host=10.1.2.3" in dsn and "user=waystone_read" in dsn and "sslmode=require" in dsn


SECRET = "projects/p/secrets/waystone-db-read-password/versions/latest"


def test_hq_password_from_secret_manager() -> None:
    from waystone3.hq.reader import hq_dsn_from_env

    seen: list[str] = []

    def fetch(resource: str) -> str:
        seen.append(resource)
        return "from-sm"

    env = {
        "WAYSTONE_HQ_DB_HOST": "10.1.2.3",
        "WAYSTONE_HQ_DB_PASSWORD_SECRET": SECRET,
    }
    dsn = hq_dsn_from_env(env, fetch_secret=fetch)
    assert dsn is not None and "password=from-sm" in dsn
    assert seen == [SECRET]
    assert "password=direct" in (
        hq_dsn_from_env({**env, "WAYSTONE_HQ_DB_PASSWORD": "direct"}, fetch_secret=fetch) or ""
    )
    assert len(seen) == 1
