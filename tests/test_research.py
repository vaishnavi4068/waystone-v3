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
from waystone3.research.reader import list_days, load_run, load_scorecard
from waystone3.research.scorecard import build_scorecard, eval_kpi, render_scorecard_html
from waystone3.research.window import (
    clamp_span,
    default_window,
    intersect_spans,
    resolve_window,
    tune_years,
)
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


def test_tune_years_follows_gcs_availability() -> None:
    assert tune_years(4.0) == 4.0
    assert tune_years(5.5) == 5.0
    assert tune_years(2.0) == 2.0
    assert tune_years(1.5) is None


def test_clamp_span_uses_four_years_when_that_is_what_exists() -> None:
    start, end = clamp_span(date(2022, 7, 4), date(2026, 7, 4)) or (None, None)
    assert start == date(2022, 7, 4)
    assert end == date(2026, 7, 4)


def test_clamp_span_caps_at_five_and_rejects_under_two() -> None:
    capped = clamp_span(date(2018, 1, 1), date(2026, 1, 1))
    assert capped is not None
    assert (capped[1] - capped[0]).days <= int(5.1 * 365.25)
    assert clamp_span(date(2025, 6, 1), date(2026, 1, 1)) is None


def test_resolve_window_from_local_csvs(tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "SPY.csv").write_text(
        "date,open,high,low,close,volume\n2022-01-03,1,1,1,1,1\n2026-01-02,1,1,1,1,1\n"
    )
    (daily / "QQQ.csv").write_text(
        "date,open,high,low,close,volume\n2021-06-01,1,1,1,1,1\n2026-01-02,1,1,1,1,1\n"
    )
    tuned = resolve_window(["SPY", "QQQ"], tmp_path)
    assert tuned is not None
    assert tuned.source == "data"
    assert tuned.start == "2022-01-03"
    assert tuned.end == "2026-01-02"
    assert 3.9 < tuned.years < 4.1


def test_intersect_spans_takes_overlap() -> None:
    overlap = intersect_spans(
        [(date(2021, 7, 5), date(2026, 7, 4)), (date(2022, 1, 1), date(2026, 7, 4))]
    )
    assert overlap == (date(2022, 1, 1), date(2026, 7, 4))


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
    assert store.exists("research/v1/01_mean_reversion/dt=2026-08-14/bb/scorecard.html")
    card = load_scorecard(store, "01_mean_reversion", "2026-08-14", "bb")
    assert card is not None
    assert card["overall"] in {"PASS", "WARN", "FAIL", "N/A"}
    assert "Stage 1" in (store.get("research/v1/01_mean_reversion/dt=2026-08-14/bb/scorecard.html") or b"").decode()


def _client(monkeypatch=None) -> tuple[TestClient, str]:
    if monkeypatch is not None:
        monkeypatch.delenv("IBKR_REPORTS_BUCKET", raising=False)
        monkeypatch.delenv("IBKR_REPORTS_LOCAL_DIR", raising=False)
        monkeypatch.setenv("IBKR_STAGED", "0")
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


def test_api_strategies_preview_without_bucket(monkeypatch) -> None:
    client, token = _client(monkeypatch)
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
    assert first.get("scorecard") is not None
    card = client.get(f"/api/strategies/{first['id']}/scorecard", headers=headers).json()
    assert card["overall"] in {"PASS", "WARN", "FAIL", "N/A"}
    assert card["stages"]
    html = client.get(f"/api/strategies/{first['id']}/scorecard.html", headers=headers)
    assert html.status_code == 200
    assert "Stage-Gate Scorecard" in html.text
    ops = client.get("/api/research/ops", headers=headers).json()
    assert ops["writable"] is False
    assert ops["inbox"] == []


def test_scorecard_gates_from_equity_and_trades() -> None:
    assert eval_kpi("gte", 1.6, 1.5, 1.0) == "pass"
    assert eval_kpi("lte", 20, 15, 25) == "warn"
    assert eval_kpi("bool", True, None, None) == "pass"
    metrics = {
        "stats": {
            "sharpe": 1.2,
            "sortino": 1.4,
            "max_drawdown_pct": -8.0,
            "cagr_pct": 10.0,
            "calmar": 1.25,
            "trades": 12,
            "win_rate_pct": 58.0,
            "profit_factor": 1.8,
            "expectancy_per_trade": 40.0,
            "final_equity": 110000,
            "years": 3.0,
            "days": 750,
            "exposure_pct": 40.0,
        }
    }
    equity = "date,equity,daily_ret\n" + "".join(
        f"2023-01-{(i % 28) + 1:02d},100000,{0.001 if i % 3 else -0.0005}\n" for i in range(80)
    )
    trades = "exit_date,pnl\n2023-01-10,120\n2023-01-20,-40\n2023-06-01,80\n"
    card = build_scorecard(
        strategy={
            "id": "01_mean_reversion",
            "name": "Mean reversion",
            "book": "equities",
            "summary": "test",
            "rule_sketch": "fade",
            "instruments": "SPY",
            "holding_period": "days",
        },
        variant="bb",
        day="2023-01-28",
        metrics=metrics,
        equity_csv=equity,
        trades_csv=trades,
    )
    assert card["values"]["ntrades"] == 12
    assert card["values"]["maxdd"] == 8.0
    assert card["banner"]["trades"] == 12
    html = render_scorecard_html(card)
    assert "Mean reversion" in html
    assert "Stage 1" in html
