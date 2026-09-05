"""Live paper vs same-day replay comparison and algo onboarding."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.ibkr.algo_registry import AlgoConfig, ensure_registry, load_registry, save_registry
from waystone3.ibkr.compare import compare_algo_day, list_compare_days, publish_blotter
from waystone3.ibkr.demo import demo_executions, seed_demo
from waystone3.ibkr.models import Book
from waystone3.ibkr.store import LocalFsStore
from waystone3.ibkr.timeutil import NY
from waystone3.workspace.workspace import TradingWorkspace


def _client(tmp_path: Path) -> tuple[TestClient, str]:
    seed_demo(tmp_path, datetime(2026, 9, 1, tzinfo=NY).date())
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj")
    app = build_app(
        workspace_factory=lambda: ws,
        report_store=LocalFsStore(tmp_path),
        ibkr_paper=True,
    )
    return TestClient(app), member.token


def test_seed_writes_three_algos_and_blotters(tmp_path: Path) -> None:
    seed_demo(tmp_path, date(2026, 9, 1))
    store = LocalFsStore(tmp_path)
    registry = load_registry(store)
    assert [row.id for row in registry.algos] == ["es_futures", "nq_futures", "s5_options"]
    assert store.exists("algos/v1/s5_options/live/dt=2026-09-01/_SUCCESS")
    assert store.exists("algos/v1/s5_options/replay/dt=2026-09-01/_SUCCESS")
    assert store.exists("algos/v1/nq_futures/live/dt=2026-09-01/_SUCCESS")


def test_compare_matches_and_flags_replay_only(tmp_path: Path) -> None:
    seed_demo(tmp_path, date(2026, 9, 1))
    store = LocalFsStore(tmp_path)
    algo = load_registry(store).get("s5_options")
    assert algo is not None
    payload = compare_algo_day(store, algo, date(2026, 9, 1))
    assert payload["live_source"] == "algo_live"
    assert payload["replay_source"] == "algo_replay"
    assert payload["matched"] == 2
    assert payload["live_only"] == 0
    assert payload["replay_only"] == 1
    assert payload["live"]["fills"] == 2
    assert payload["replay"]["fills"] == 3
    assert payload["deltas"]["fills"] == -1
    assert payload["avg_price_delta"] is not None
    statuses = {row["status"] for row in payload["rows"]}
    assert statuses == {"matched", "replay_only"}


def test_compare_nq_uses_synthesized_live_fill(tmp_path: Path) -> None:
    seed_demo(tmp_path, date(2026, 9, 1))
    store = LocalFsStore(tmp_path)
    algo = load_registry(store).get("nq_futures")
    assert algo is not None
    payload = compare_algo_day(store, algo, date(2026, 9, 1))
    assert payload["matched"] == 1
    assert payload["live"]["fills"] == 1
    assert payload["replay"]["fills"] == 1
    assert payload["rows"][0]["symbol"] == "NQ"


def test_register_custom_prefix_is_listed(tmp_path: Path) -> None:
    store = LocalFsStore(tmp_path)
    registry = ensure_registry(store)
    algo = registry.upsert(
        AlgoConfig(
            id="cl_futures",
            name="CL futures",
            book=Book.FUTURES,
            live_prefix="custom/live-cl",
            replay_prefix="custom/replay-cl",
        )
    )
    save_registry(store, registry)
    fills = [row for row in demo_executions(date(2026, 8, 15)) if row.book is Book.FUTURES][:1]
    publish_blotter(store, algo.resolved_live(), date(2026, 8, 15), fills)
    publish_blotter(store, algo.resolved_replay(), date(2026, 8, 15), fills)
    days = list_compare_days(store, load_registry(store))
    assert "2026-08-15" in days


def test_api_list_compare_and_onboard(tmp_path: Path) -> None:
    client, token = _client(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}

    listed = client.get("/api/algos", headers=headers).json()
    assert {row["id"] for row in listed["algos"]} == {"s5_options", "nq_futures", "es_futures"}

    days = client.get("/api/algos/compare-days", headers=headers).json()
    assert "2026-09-01" in days["days"]
    assert days["latest"] == days["days"][-1]

    s5 = client.get("/api/algos/s5_options/compare?date=2026-09-01", headers=headers).json()
    assert s5["algo"]["id"] == "s5_options"
    assert s5["matched"] == 2
    assert s5["replay_only"] == 1

    conflict = client.post(
        "/api/algos",
        headers=headers,
        json={"id": "s5_options", "name": "dup", "book": "options"},
    )
    assert conflict.status_code == 409

    bad = client.post(
        "/api/algos",
        headers=headers,
        json={"id": "Bad-Id", "name": "nope", "book": "futures"},
    )
    assert bad.status_code == 400

    created = client.post(
        "/api/algos",
        headers=headers,
        json={
            "id": "cl_futures",
            "name": "CL futures",
            "book": "futures",
            "notes": "Paper CL vs replay",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["id"] == "cl_futures"
    assert body["live_prefix"] == "algos/v1/cl_futures/live"
    assert body["replay_prefix"] == "algos/v1/cl_futures/replay"

    after = client.get("/api/algos", headers=headers).json()
    assert len(after["algos"]) == 4

    fresh = client.get("/api/algos/cl_futures/compare?date=2026-09-01", headers=headers).json()
    assert fresh["algo"]["id"] == "cl_futures"
    assert fresh["replay_source"] == "missing"

    removed = client.delete("/api/algos/cl_futures", headers=headers)
    assert removed.status_code == 200
    leftover = client.get("/api/algos", headers=headers).json()
    assert {row["id"] for row in leftover["algos"]} == {"s5_options", "nq_futures", "es_futures"}


def test_unknown_algo_compare_404(tmp_path: Path) -> None:
    client, token = _client(tmp_path)
    missing = client.get(
        "/api/algos/no_such/compare?date=2026-09-01",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert missing.status_code == 404
