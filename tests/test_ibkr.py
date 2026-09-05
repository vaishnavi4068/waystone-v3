"""IBKR daily dump: classify, ledger, GCS layout, API reader."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.ibkr.classify import book_for, parse_client_books
from waystone3.ibkr.convert import execution_from_fill
from waystone3.ibkr.demo import demo_report, seed_demo
from waystone3.ibkr.export import assemble_report, publish_report
from waystone3.ibkr.futures_kpis import FUTURES_SPECS, compute_futures_kpis
from waystone3.ibkr.kpis import SPECS, KpiStatus, compute_options_kpis, evaluate
from waystone3.ibkr.ledger import ExecutionLedger
from waystone3.ibkr.models import Book
from waystone3.ibkr.paths import success_key
from waystone3.ibkr.reader import is_published, latest_published_day, load_report
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.store import LocalFsStore
from waystone3.ibkr.summary import summarize
from waystone3.ibkr.timeutil import NY
from waystone3.workspace.workspace import TradingWorkspace


def test_book_from_sec_type() -> None:
    assert book_for("FUT") is Book.FUTURES
    assert book_for("OPT") is Book.OPTIONS
    assert book_for("FOP") is Book.OPTIONS
    assert book_for("STK") is Book.OTHER


def test_client_id_overrides_sec_type() -> None:
    books = parse_client_books("1=futures,2=options")
    assert book_for("OPT", client_id=1, client_books=books) is Book.FUTURES
    assert book_for("FUT", client_id=2, client_books=books) is Book.OPTIONS


def test_summary_splits_futures_and_options() -> None:
    report = demo_report(datetime(2026, 9, 1, tzinfo=NY).date())
    stats = summarize(report.date, report.executions)
    assert stats.futures.fills == 2
    assert stats.options.fills == 2
    assert stats.totals.fills == 4
    assert stats.futures.realized_pnl == 287.70
    assert stats.options.realized_pnl == 268.50
    assert stats.totals.commission == pytest.approx(9.40)
    assert stats.futures.notional == pytest.approx(2 * 5234.25 * 50 + 1 * 5240.00 * 50)


def test_exec_id_dedup_and_update(tmp_path: Path) -> None:
    day = datetime(2026, 9, 1, tzinfo=NY).date()
    ledger = ExecutionLedger(tmp_path)
    fills = demo_report(day).executions
    assert ledger.merge(day, fills[:2]) == 2
    assert ledger.merge(day, fills) == 2  # two new
    updated = fills[0].model_copy(update={"commission": 9.99})
    assert ledger.merge(day, [updated]) == 0
    loaded = {row.exec_id: row for row in ledger.load(day)}
    assert loaded["fut-1"].commission == 9.99
    assert len(loaded) == 4


def test_success_gates_published_day(tmp_path: Path) -> None:
    store = LocalFsStore(tmp_path)
    day = datetime(2026, 9, 1, tzinfo=NY).date()
    report = demo_report(day)
    assert is_published(store, day) is False
    publish_report(store, report)
    assert store.exists(success_key(day))
    assert is_published(store, day) is True
    assert latest_published_day(store) == day
    loaded = load_report(store, day)
    assert loaded is not None
    assert loaded.summary.totals.fills == 4


def test_unpublished_prefix_is_ignored(tmp_path: Path) -> None:
    store = LocalFsStore(tmp_path)
    day = datetime(2026, 9, 1, tzinfo=NY).date()
    report = assemble_report(
        day,
        demo_report(day).executions,
        demo_report(day).positions,
        demo_report(day).account,
        IbkrSettings(),
        tws_connected=False,
    )
    store.put(f"ibkr/v1/dt={day.isoformat()}/account.json", b"{}")
    assert load_report(store, day) is None
    publish_report(store, report)
    assert load_report(store, day) is not None


def test_fill_conversion_from_duck_types() -> None:
    fill = SimpleNamespace(
        time=datetime(2026, 9, 1, 13, 0, tzinfo=NY),
        contract=SimpleNamespace(
            secType="FUT",
            symbol="NQ",
            localSymbol="NQU6",
            conId=9,
            exchange="CME",
            currency="USD",
            lastTradeDateOrContractMonth="20260918",
            strike=0.0,
            right="",
            multiplier="20",
        ),
        execution=SimpleNamespace(
            execId="x1",
            permId=1,
            orderId=2,
            time=None,
            acctNumber="U1",
            exchange="CME",
            side="BOT",
            shares=1,
            price=18_000.25,
            clientId=1,
            cumQty=1,
            avgPrice=18_000.25,
        ),
        commissionReport=SimpleNamespace(commission=2.15, currency="USD", realizedPNL=0),
    )
    row = execution_from_fill(fill, parse_client_books("1=futures"))
    assert row.book is Book.FUTURES
    assert row.strike is None
    assert row.qty == 1
    assert row.commission == 2.15


def _ibkr_client(tmp_path: Path) -> tuple[TestClient, str, Path]:
    seed_demo(tmp_path, datetime(2026, 9, 1, tzinfo=NY).date())
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj")
    app = build_app(
        workspace_factory=lambda: ws,
        report_store=LocalFsStore(tmp_path),
        ibkr_paper=False,
    )
    return TestClient(app), member.token, tmp_path


def test_api_ibkr_days_and_report(tmp_path: Path) -> None:
    client, token, _ = _ibkr_client(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    days = client.get("/api/ibkr/days", headers=headers).json()
    assert days["days"] == ["2026-09-01"]
    assert days["latest"] == "2026-09-01"
    missing = client.get("/api/ibkr/report?date=2026-08-01", headers=headers)
    assert missing.status_code == 404
    report = client.get("/api/ibkr/report?date=2026-09-01", headers=headers).json()
    assert report["summary"]["futures"]["fills"] == 2
    assert report["summary"]["options"]["fills"] == 2
    assert report["account"]["nlv"] == 1_052_340.12


def test_api_account_positions_orders_switch_to_ibkr(tmp_path: Path) -> None:
    client, token, _ = _ibkr_client(tmp_path)
    headers = {"Authorization": f"Bearer {token}"}
    acct = client.get("/api/account", headers=headers).json()
    assert acct["broker"] == "ibkr"
    assert acct["is_paper"] is False
    assert acct["equity"] == 1_052_340.12
    assert acct["report_date"] == "2026-09-01"
    positions = client.get("/api/positions", headers=headers).json()
    assert positions[0]["local_symbol"] == "ESU6"
    assert positions[0]["book"] == "futures"
    orders = client.get("/api/orders", headers=headers).json()
    assert {o["book"] for o in orders} == {"futures", "options"}
    assert all(o["status"] == "filled" for o in orders)


def test_api_without_store_stays_on_paper_broker() -> None:
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj")
    client = TestClient(build_app(workspace_factory=lambda: ws))
    acct = client.get("/api/account", headers={"Authorization": f"Bearer {member.token}"}).json()
    assert acct["broker"] == "paper"
    days = client.get("/api/ibkr/days", headers={"Authorization": f"Bearer {member.token}"})
    assert days.status_code == 404


def _spec(key: str):
    return next(s for s in SPECS if s.key == key)


def test_evaluate_sheet_thresholds() -> None:
    sharpe = _spec("weekly_sharpe")
    assert evaluate(1.6, sharpe) is KpiStatus.PASS
    assert evaluate(1.2, sharpe) is KpiStatus.WARN
    assert evaluate(0.5, sharpe) is KpiStatus.FAIL
    drawdown = _spec("max_dd")
    assert evaluate(10.0, drawdown) is KpiStatus.PASS
    assert evaluate(20.0, drawdown) is KpiStatus.WARN
    assert evaluate(30.0, drawdown) is KpiStatus.FAIL
    assert evaluate(None, sharpe) is KpiStatus.EMPTY


def test_options_kpis_history_computes_stage1(tmp_path: Path) -> None:
    seed_demo(tmp_path, datetime(2026, 9, 1, tzinfo=NY).date(), history_days=40)
    payload = compute_options_kpis(LocalFsStore(tmp_path))
    assert payload["days"] == 41
    assert payload["assumptions"]["nav"] == 100_000
    s1 = next(s for s in payload["stages"] if s["id"] == "s1")
    by_key = {row["key"]: row for row in s1["kpis"]}
    assert by_key["trade_count"]["value"] is not None
    assert by_key["trade_count"]["value"] >= 40
    assert by_key["weekly_sharpe"]["value"] is not None
    vega = next(
        row
        for stage in payload["stages"]
        if stage["id"] == "s3"
        for row in stage["kpis"]
        if row["key"] == "net_vega"
    )
    assert vega["status"] == "—"
    assert {s["id"] for s in payload["stages"]} == {"s1", "s2", "s3", "s4", "s5", "exec"}


def test_api_options_kpis(tmp_path: Path) -> None:
    client, token, _ = _ibkr_client(tmp_path)
    data = client.get("/api/ibkr/options-kpis", headers={"Authorization": f"Bearer {token}"}).json()
    assert data["assumptions"]["option_multiplier"] == 100
    assert any(s["name"].startswith("Stage 1") for s in data["stages"])


def test_futures_kpis_history_computes_tiers(tmp_path: Path) -> None:
    seed_demo(tmp_path, datetime(2026, 9, 1, tzinfo=NY).date(), history_days=40)
    payload = compute_futures_kpis(LocalFsStore(tmp_path))
    assert payload["days"] == 41
    assert payload["assumptions"]["point_value"] == 20
    assert payload["trade_count"] >= 40
    ids = {s["id"] for s in payload["stages"]}
    assert ids == {"t0", "t1", "t2", "t3", "t4"}
    t1 = next(s for s in payload["stages"] if s["id"] == "t1")
    by_key = {row["key"]: row for row in t1["kpis"]}
    assert by_key["sharpe"]["value"] is not None
    ir = next(
        row
        for stage in payload["stages"]
        if stage["id"] == "t1"
        for row in stage["kpis"]
        if row["key"] == "information_ratio"
    )
    assert ir["status"] == "—"
    t0 = next(s for s in payload["stages"] if s["id"] == "t0")
    assert any(row["key"] == "trade_count" and row["status"] != "—" for row in t0["kpis"])
    assert {s.key for s in FUTURES_SPECS} >= {"sharpe", "max_dd", "cost_drag"}


def test_api_futures_kpis(tmp_path: Path) -> None:
    client, token, _ = _ibkr_client(tmp_path)
    data = client.get("/api/ibkr/futures-kpis", headers={"Authorization": f"Bearer {token}"}).json()
    assert data["assumptions"]["point_value"] == 20
    assert any(s["name"].startswith("Tier 0") for s in data["stages"])
