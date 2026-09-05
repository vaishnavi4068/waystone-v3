"""Research catalog, dated GCS publish, and HQ /api/strategies."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.ibkr.store import LocalFsStore
from waystone3.research.catalog import list_strategies
from waystone3.research.nsdq250 import ohlc_csv_to_wsbt, parse_daily_ohlc_key
from waystone3.research.ops import (
    ack_instruction,
    add_instruction,
    list_inbox,
    post_status,
    read_status,
)
from waystone3.research.paths import success_key
from waystone3.research.publish import publish_results
from waystone3.research.reader import list_days, load_run
from waystone3.research.window import default_window
from waystone3.workspace.workspace import TradingWorkspace


def test_catalog_has_eight_books() -> None:
    rows = list_strategies()
    assert len(rows) == 8
    books = {row["book"] for row in rows}
    assert books == {"equities", "options", "futures"}


def test_default_window_is_five_years() -> None:
    start, end = default_window(5)
    assert start < end
    assert int(end[:4]) - int(start[:4]) == 5


def test_parse_nsdq250_daily_key() -> None:
    parsed = parse_daily_ohlc_key("NSDQ250/AAPL_daily_ohlc_2021-07-05_to_2026-07-04.csv")
    assert parsed is not None
    assert parsed.symbol == "AAPL"
    assert parsed.span_days == (date(2026, 7, 4) - date(2021, 7, 5)).days


def test_ohlc_mapper_adds_adj_close() -> None:
    raw = "date,open,high,low,close,volume\n2021-07-06,1,2,0.5,1.5,100\n"
    out = ohlc_csv_to_wsbt(raw)
    assert "adj_close" in out.splitlines()[0]
    assert "1.5" in out


def test_publish_writes_dated_success(tmp_path: Path, monkeypatch) -> None:
    results = tmp_path / "results" / "01_mean_reversion_bb"
    results.mkdir(parents=True)
    (results / "metrics.json").write_text(
        '{"strategy":"01_mean_reversion_bb","synthetic":false,"params":{},"stats":{"sharpe":1.1}}'
    )
    (results / "equity.csv").write_text("date,equity,daily_ret\n2026-08-14,101000,0.01\n")
    monkeypatch.setenv("WSBT_RESULTS_DIR", str(tmp_path / "results"))
    store = LocalFsStore(tmp_path / "gcs")
    published = publish_results(store=store, host="mac-studio")
    assert published[0]["date"] == "2026-08-14"
    assert published[0]["variant"] == "bb"
    assert store.exists(success_key("01_mean_reversion", "2026-08-14", "bb"))
    assert list_days(store, "01_mean_reversion") == ["2026-08-14"]
    run = load_run(store, "01_mean_reversion")
    assert run is not None
    assert run["stats"]["sharpe"] == 1.1
    assert run["date"] == "2026-08-14"


def _client() -> tuple[TestClient, str]:
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj")
    app = build_app(workspace_factory=lambda: ws, report_store=None)
    return TestClient(app), member.token


def test_ops_status_and_inbox(tmp_path: Path) -> None:
    store = LocalFsStore(tmp_path)
    post_status("run", "started", store=store)
    latest = read_status(store)
    assert latest is not None
    assert latest["phase"] == "run"
    row = add_instruction("publish now", action="approve-publish", store=store)
    pending = list_inbox(store=store)
    assert pending[0]["id"] == row["id"]
    assert pending[0]["action"] == "approve-publish"
    assert ack_instruction(row["id"], store=store) is True
    assert list_inbox(store=store) == []


def test_api_grok_inbox(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROK_BOT_INBOX_TOKEN", "inbox-secret")
    store = LocalFsStore(tmp_path)
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj")
    app = build_app(workspace_factory=lambda: ws, report_store=store)
    client = TestClient(app)
    denied = client.post(
        "/api/research/ops/inbox",
        json={"text": "go", "action": "approve-run"},
    )
    assert denied.status_code == 401
    ok = client.post(
        "/api/research/ops/inbox",
        headers={"X-Grok-Bot-Key": "inbox-secret"},
        json={"text": "go", "action": "approve-run"},
    )
    assert ok.status_code == 200
    assert ok.json()["action"] == "approve-run"
    assert ok.json()["source"] == "grok_bot"
    ops = client.get("/api/research/ops", headers={"Authorization": f"Bearer {member.token}"})
    assert ops.status_code == 200
    assert ops.json()["writable"] is True
    assert ops.json()["inbox"][0]["action"] == "approve-run"
    hq = client.post(
        "/api/research/ops/inbox",
        headers={"Authorization": f"Bearer {member.token}"},
        json={"text": "publish now", "action": "approve-publish"},
    )
    assert hq.status_code == 200
    assert hq.json()["source"] == "hq"
    acked = client.post(
        f"/api/research/ops/inbox/{ok.json()['id']}/ack",
        headers={"Authorization": f"Bearer {member.token}"},
    )
    assert acked.status_code == 200


def test_api_strategies_preview_without_bucket() -> None:
    client, token = _client()
    headers = {"Authorization": f"Bearer {token}"}
    data = client.get("/api/strategies", headers=headers).json()
    assert len(data["strategies"]) == 8
    first = data["strategies"][0]
    assert first["latest"]["date"] == "2026-08-14"
    detail = client.get(f"/api/strategies/{first['id']}", headers=headers).json()
    assert detail["rule_sketch"]
    assert detail["latest"]["stats"]["sharpe"] is not None
    runs = client.get(f"/api/strategies/{first['id']}/runs", headers=headers).json()
    assert runs["strategy_id"] == first["id"]
    assert runs["runs"][0]["date"] == "2026-08-14"
    ops = client.get("/api/research/ops", headers=headers).json()
    assert ops["writable"] is False
    assert ops["inbox"] == []
