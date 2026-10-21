"""Tests for the Backtest Sandbox across Claude Cowork (MCP), GrokBot Inbox, and REST API.

Verifies:
  1. GCS historical data backtest (`hq_sandbox_backtest_gcs`) reads from the GCS/report
     store (`NSDQ250/FUTURES_1MIN/<ROOT>/<ROOT>_continuous_front.csv`), replays the real
     `V221Engine` and `R2Engine` with locked thresholds, and persists sandbox runs under
     `sandbox/v1/runs/<run_id>.json`.
  2. IBKR daily/historical backtest (`hq_sandbox_backtest_ibkr`) uses the strategy's
     **same bound IBKR paper account** (`account_id`, `client_id`), enforces `D...` paper
     account validation, and replays the locked engine.
  3. Any attempt to override locked thresholds in the sandbox is rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.hq.backtest_sandbox import HqBacktestSandbox
from waystone3.hq.strategy_runner import HqStrategyRunner
from waystone3.ibkr.store import LocalFsStore
from waystone3.mcp_server import build_mcp
from waystone3.workspace.service import WorkspaceService
from waystone3.workspace.workspace import TradingWorkspace

HQ_STRATEGY_PATH = Path("/agent/repos/hq-strategy")


def _runner(tmp_path: Path) -> HqStrategyRunner:
    root = tmp_path / "hq-strategy"
    for rel in ("NQ_FUTURE", "ES_ALGO", "R2_MNQ_ALGO/R2_MNQ"):
        dest = root / rel
        dest.mkdir(parents=True, exist_ok=True)
        src = HQ_STRATEGY_PATH / rel
        if src.is_dir():
            for py in src.glob("*.py"):
                (dest / py.name).write_bytes(py.read_bytes())
        else:
            (dest / "v221_futures_live.py").write_text("#!/usr/bin/env python3\n")
            (dest / "v221_futures_live_r2.py").write_text("#!/usr/bin/env python3\n")
    return HqStrategyRunner(repo_root=root, state_file=tmp_path / "accounts.json")


def _seed_gcs_futures_csv(store: LocalFsStore, root: str = "NQ") -> str:
    rows = ["datetime_et,open,high,low,close,volume,vol_price"]
    base = 20000.0
    # Generate 90 bars across two sessions with a strong trend + volume spike to trigger entries
    for i in range(60):
        px = base + i * 11.0
        vol = 1000.0 if i < 25 else 2400.0
        rows.append(
            f"2026-10-01T{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}:00-04:00,"
            f"{px:.2f},{px + 8.0:.2f},{px - 4.0:.2f},{px + 6.0:.2f},{vol:.0f},20.5"
        )
    for i in range(30):
        px = base + 660.0 - i * 25.0
        vol = 2600.0
        rows.append(
            f"2026-10-02T{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}:00-04:00,"
            f"{px:.2f},{px + 4.0:.2f},{px - 15.0:.2f},{px - 12.0:.2f},{vol:.0f},22.0"
        )
    key = f"NSDQ250/FUTURES_1MIN/{root}/{root}_continuous_front.csv"
    store.put(key, ("\n".join(rows) + "\n").encode(), "text/csv")
    return key


@pytest.mark.skipif(not HQ_STRATEGY_PATH.is_dir(), reason="hq-strategy repo not mounted")
def test_sandbox_gcs_backtest_runs_and_persists_with_locked_thresholds(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.configure_account("nq_v221", account_id="DUR842608", contracts=2)
    store = LocalFsStore(tmp_path / "gcs_bucket")
    _seed_gcs_futures_csv(store, "NQ")
    _seed_gcs_futures_csv(store, "MNQ")

    sbx = HqBacktestSandbox(runner=runner, store=store, data_dir=tmp_path / "data")
    ds = sbx.list_datasets()
    assert "NSDQ250/FUTURES_1MIN/NQ/NQ_continuous_front.csv" in ds["gcs_objects"]
    assert ds["bound_ibkr_accounts"]["nq_v221"]["account_id"] == "DUR842608"

    # Reject threshold overrides
    with pytest.raises(ValueError, match="immutable"):
        sbx.run_gcs_backtest("nq_v221", overrides={"fng_threshold": 50})

    # Run GCS sandbox backtest on NQ
    res = sbx.run_gcs_backtest("nq_v221", start="2026-10-01", end="2026-10-02")
    assert res["sandbox"] is True
    assert res["data_source"] == "gcs"
    assert res["account_id"] == "DUR842608"
    assert res["thresholds_locked"] is True
    assert res["metrics"]["bars_count"] == 90
    assert res["metrics"]["sessions_count"] == 2
    assert res["metrics"]["contracts"] == 2

    # Run GCS sandbox backtest on R2 MNQ
    r2_res = sbx.run_gcs_backtest("r2_mnq", start="2026-10-01", end="2026-10-02")
    assert r2_res["strategy_code"] == "r2_mnq"
    assert r2_res["metrics"]["bars_count"] == 90
    assert r2_res["thresholds"]["brick_max"] == 6.5

    # Verify runs are persisted in store
    runs = sbx.list_runs()
    assert len(runs) == 2
    loaded = sbx.get_run(res["run_id"])
    assert loaded is not None
    assert loaded["run_id"] == res["run_id"]


@pytest.mark.skipif(not HQ_STRATEGY_PATH.is_dir(), reason="hq-strategy repo not mounted")
def test_sandbox_ibkr_backtest_uses_same_bound_paper_account(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.configure_account("es_v221", account_id="DUR842609", client_id=53, contracts=2)
    store = LocalFsStore(tmp_path / "gcs_bucket")
    sbx = HqBacktestSandbox(runner=runner, store=store, data_dir=tmp_path / "data")

    # Reject live (non-D) account for IBKR backtest
    with pytest.raises(ValueError, match="PAPER accounts starting with 'D'"):
        sbx.run_ibkr_backtest("es_v221", account_id="U123456")

    sample_ibkr_bars = [
        {
            "datetime_et": f"2026-10-02T09:{30 + i:02d}:00-04:00",
            "open": 5800.0 + i * 3.0,
            "high": 5804.0 + i * 3.0,
            "low": 5798.0 + i * 3.0,
            "close": 5803.0 + i * 3.0,
            "volume": 1200.0 if i < 21 else 3000.0,
            "vol_price": 18.5,
        }
        for i in range(30)
    ]
    out = sbx.run_ibkr_backtest(
        "es_v221",
        days=5,
        bar_size="1 min",
        bars_override=sample_ibkr_bars,
    )
    assert out["sandbox"] is True
    assert out["data_source"] == "ibkr"
    assert out["account_id"] == "DUR842609"
    assert out["client_id"] == 53
    assert "ibkr://DUR842609@" in out["dataset_uri"]
    assert out["metrics"]["bars_count"] == 30
    assert out["thresholds"]["broker_stop_pct"] == 0.7


@pytest.mark.skipif(not HQ_STRATEGY_PATH.is_dir(), reason="hq-strategy repo not mounted")
def test_mcp_and_api_and_grokbot_sandbox_backtest_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROK_BOT_INBOX_TOKEN", "grok-secret")
    runner = _runner(tmp_path)
    runner.configure_account("nq_v221", account_id="DUR842608", contracts=2)
    store = LocalFsStore(tmp_path / "gcs_bucket")
    _seed_gcs_futures_csv(store, "NQ")

    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    svc = WorkspaceService(ws, admin_token="adm")
    member = svc.register("adm", "Manoj")
    sbx = HqBacktestSandbox(runner=runner, store=store, data_dir=tmp_path / "data")

    # 1. Claude Cowork MCP tools
    mcp = build_mcp(svc, hq=None, strategy_runner=runner)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {
        "hq_sandbox_datasets",
        "hq_sandbox_backtest_gcs",
        "hq_sandbox_backtest_ibkr",
        "hq_sandbox_runs",
    } <= tool_names

    # 2. REST API & GrokBot inbox sandbox execution
    client = TestClient(
        build_app(
            workspace_factory=lambda: ws,
            report_store=store,
            strategy_runner=runner,
        )
    )
    headers = {"Authorization": f"Bearer {member['token']}"}

    ds_resp = client.get("/api/hq/sandbox/datasets", headers=headers)
    assert ds_resp.status_code == 200
    assert ds_resp.json()["thresholds_locked"] is True

    gcs_bt = client.post(
        "/api/hq/sandbox/backtest/gcs",
        headers=headers,
        json={"strategy": "nq_v221", "start": "2026-10-01", "end": "2026-10-02"},
    )
    assert gcs_bt.status_code == 200
    run_id = gcs_bt.json()["run_id"]
    assert gcs_bt.json()["account_id"] == "DUR842608"

    detail = client.get(f"/api/hq/sandbox/runs/{run_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id

    # GrokBot triggers a GCS sandbox backtest via inbox
    grok_bt = client.post(
        "/api/research/ops/inbox",
        headers={"X-Grok-Bot-Key": "grok-secret"},
        json={
            "text": "Run sandbox backtest on NQ from GCS",
            "action": "sandbox-backtest-gcs",
            "params": {"strategy": "nq_v221", "start": "2026-10-01", "end": "2026-10-02"},
            "execute_now": True,
        },
    )
    assert grok_bt.status_code == 200
    assert grok_bt.json()["acked"] is True
    assert grok_bt.json()["execution"]["data_source"] == "gcs"
    assert grok_bt.json()["execution"]["account_id"] == "DUR842608"

    # Diagnose discrepancy between reference backtest and live-parity replay
    diag = client.post(
        "/api/hq/sandbox/diagnose-discrepancy",
        headers=headers,
        json={"strategy": "nq_v221", "start": "2026-10-01", "end": "2026-10-02"},
    )
    assert diag.status_code == 200
    assert diag.json()["thresholds_locked"] is True
    assert "reference_backtest" in diag.json()
    assert "live_parity_backtest" in diag.json()
    assert diag.json()["root_causes"]
    del sbx


def test_exit_reason_match_and_inferred_backtest_commission() -> None:
    from datetime import date, datetime
    from decimal import Decimal

    from waystone3.hq.backtest_daily import parse_backtest_daily
    from waystone3.hq.calendar import NY
    from waystone3.hq.compare import TradeRef, daily_sync, match_trades
    from waystone3.hq.loader import _backtest_rows

    # 1. When trade counts differ (1 live vs 2 backtest where 2nd was NOT_TAKEN_LIVE),
    # exit_reason_match is determined by the MATCHED pair ("Y"), not paper[-1] vs bt[-1]
    day = date(2026, 10, 1)
    paper = [
        TradeRef(
            1,
            "SHORT",
            datetime(2026, 10, 1, 9, 31, tzinfo=NY),
            datetime(2026, 10, 1, 12, 18, tzinfo=NY),
            Decimal("-43.5"),
            Decimal("-4358.96"),
            "yeah_its_failing",
        )
    ]
    bt = [
        TradeRef(
            10,
            "SHORT",
            datetime(2026, 10, 1, 9, 31, tzinfo=NY),
            datetime(2026, 10, 1, 12, 40, tzinfo=NY),
            Decimal("-44.25"),
            Decimal("-4434.00"),
            "yeah_its_failing",
        ),
        TradeRef(
            11,
            "LONG",
            datetime(2026, 10, 1, 12, 42, tzinfo=NY),
            datetime(2026, 10, 1, 15, 55, tzinfo=NY),
            Decimal("-10.25"),
            Decimal("-1034.00"),
            "SESSION_FLATTEN",
        ),
    ]
    matches = match_trades(paper, bt, loss_cap_at=None)
    sync_row = daily_sync(
        paper, matches, loss_cap_hit=False, pct_threshold=Decimal("0.15"), usd_floor=Decimal(0)
    )
    assert sync_row.exit_reason_match == "Y"

    # 2. Same signal + same exit reason with % exit bar timing drift is annotated clearly
    nq_paper = [
        TradeRef(
            9,
            "SHORT",
            datetime(2026, 10, 2, 9, 31, tzinfo=NY),
            datetime(2026, 10, 2, 10, 20, tzinfo=NY),
            Decimal("-199.375"),
            Decimal("-7983.96"),
            "yeah_its_failing",
        )
    ]
    nq_bt = [
        TradeRef(
            1,
            "SHORT",
            datetime(2026, 10, 2, 9, 31, tzinfo=NY),
            datetime(2026, 10, 2, 10, 5, tzinfo=NY),
            Decimal("-150.00"),
            Decimal("-6008.96"),
            "yeah_its_failing",
        )
    ]
    nq_sync = daily_sync(
        nq_paper,
        match_trades(nq_paper, nq_bt, loss_cap_at=None),
        loss_cap_hit=True,
        pct_threshold=Decimal("0.15"),
        usd_floor=Decimal(0),
    )
    assert any("entry fill price shifted % exit bar" in n for n in nq_sync.notes)

    # 3. _backtest_rows infers commission from gross - net when commission is not explicitly printed
    parsed = parse_backtest_daily(
        "ES replay 2026-10-01\n"
        "#1 SHORT entry=09:31 exit=12:40 pts=-44.25 contracts=2 "
        "net=-4434.00 reason=yeah_its_failing\n",
        day,
    )
    rows = _backtest_rows(
        parsed,
        Decimal(50),
        {"default_contracts": 2, "commission_rt_per_contract": Decimal("4.50")},
    )
    assert rows[0]["gross_pnl"] == Decimal("-4425.00")
    assert rows[0]["commission"] == Decimal("9.00")

