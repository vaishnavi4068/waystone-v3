"""Tests for HQ Strategy Runner, MCP tools, REST API, and GrokBot inbox execution.

Verifies:
  1. All strategy signal/exit/risk thresholds are strictly immutable and match
     ``hq-strategy`` (`NQ_FUTURE`, `ES_ALGO`, `R2_MNQ_ALGO`).
  2. Paper account validation (`D...` required; live accounts rejected).
  3. Bar evaluation runs the real `V221Engine` and `R2Engine` from `/agent/repos/hq-strategy`.
  4. MCP tools (`hq_strategy_*`, `ibkr_futures_*`) and `/api/hq/runtime/*` endpoints work.
  5. GrokBot inbox actions (`strategy-configure`, `strategy-start`, `strategy-halt`,
     `strategy-resume`, `strategy-execute`) execute and reject threshold overrides.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.hq.strategy_runner import (
    HqStrategyRunner,
    assert_no_threshold_changes,
    validate_paper_account,
)
from waystone3.ibkr.store import LocalFsStore
from waystone3.mcp_server import _token, build_mcp
from waystone3.research.ops import (
    add_instruction,
    list_inbox,
    process_strategy_inbox,
)
from waystone3.workspace.service import WorkspaceService
from waystone3.workspace.workspace import TradingWorkspace

HQ_STRATEGY_PATH = Path("/agent/repos/hq-strategy")


def _runner(tmp_path: Path) -> HqStrategyRunner:
    # Use a temporary copy of strategy dirs when testing file writes (KILL_SWITCH / state)
    # while linking to the real engine files in /agent/repos/hq-strategy if present.
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


def test_thresholds_are_locked_and_match_hq_strategy(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    nq = runner.spec("nq_v221").thresholds
    es = runner.spec("es_v221").thresholds
    r2 = runner.spec("r2_mnq").thresholds

    # V221 NQ & ES locked constants
    assert (nq.fast_ema, nq.slow_ema, nq.signal_ema) == (45, 60, 20)
    assert (nq.brick_min, nq.brick_max) == (0.0, 10.0)
    assert (nq.fng_threshold, nq.chop_threshold) == (30.0, 0.005)
    assert (nq.hard_stop_pct, nq.broker_stop_pct) == (-0.8, 1.0)
    assert es.broker_stop_pct == 0.7

    # R2 MNQ locked constants
    assert (r2.fast_ema, r2.slow_ema, r2.signal_ema) == (8, 20, 10)
    assert (r2.brick_min, r2.brick_max) == (0.0, 6.5)
    assert (r2.fng_threshold, r2.chop_threshold) == (30.0, 0.06)
    assert (r2.hard_stop_pct, r2.broker_stop_pct) == (-0.6, 0.8)
    assert (r2.pt_level_low, r2.pt_level_mid, r2.pt_level_high, r2.pt_min_duration) == (
        0.5,
        1.0,
        1.5,
        1,
    )

    for bad_key in (
        "fng_threshold",
        "chop_threshold",
        "hard_stop_pct",
        "stop_pct",
        "brick_max",
        "fast",
        "bullish_threshold",
    ):
        with pytest.raises(ValueError, match="immutable"):
            assert_no_threshold_changes({bad_key: 99})
        with pytest.raises(ValueError, match="immutable"):
            runner.configure_account("nq_v221", extra_overrides={bad_key: 99})


def test_paper_account_validation(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    assert validate_paper_account("dur842608") == "DUR842608"
    with pytest.raises(ValueError, match="PAPER accounts starting with 'D'"):
        validate_paper_account("U1234567")
    with pytest.raises(ValueError, match="PAPER accounts starting with 'D'"):
        runner.configure_account("nq_v221", account_id="U999999")
    with pytest.raises(ValueError, match="reserved for the options algo"):
        runner.configure_account("nq_v221", client_id=42)


def test_configure_halt_resume_and_stage_start(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    cfg = runner.configure_account(
        "r2_mnq",
        account_id="DUR842608",
        client_id=91,
        contracts=2,
        dry_run=True,
    )
    assert cfg["binding"]["account_id"] == "DUR842608"
    assert cfg["binding"]["client_id"] == 91
    assert cfg["binding"]["contracts"] == 2
    assert cfg["thresholds_locked"] is True

    halted = runner.halt_strategy("r2_mnq", reason="FOMC announcement")
    assert halted["kill_switch_active"] is True
    assert runner.status("r2_mnq")["kill_switch_active"] is True

    resumed = runner.resume_strategy("r2_mnq")
    assert resumed["kill_switch_active"] is False
    assert resumed["was_active"] is True

    started = runner.start_strategy("r2_mnq", spawn=False)
    assert started["status"] == "staged"
    assert started["account_binding"]["account_id"] == "DUR842608"
    assert started["thresholds"]["hard_stop_pct"] == -0.6


@pytest.mark.skipif(not HQ_STRATEGY_PATH.is_dir(), reason="hq-strategy repo not mounted")
def test_evaluate_bars_runs_real_v221_and_r2_engines(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    bars = [
        {
            "datetime_et": f"2026-10-02T09:{30 + i:02d}:00-04:00",
            "open": 20000.0 + i * 12.0,
            "high": 20015.0 + i * 12.0,
            "low": 19995.0 + i * 12.0,
            "close": 20012.0 + i * 12.0,
            "volume": 1000.0 if i < 22 else 2500.0,
            "vol_price": 21.5,
        }
        for i in range(25)
    ]
    nq_eval = runner.evaluate_bars("nq_v221", bars, fng_prior_day=55.0)
    assert nq_eval["strategy_code"] == "nq_v221"
    assert nq_eval["bars_evaluated"] == 25
    assert nq_eval["engine_snapshot"]["bars"] == 25
    assert nq_eval["engine_snapshot"]["bricks"] >= 1
    assert nq_eval["thresholds"]["fng_threshold"] == 30.0

    r2_eval = runner.evaluate_bars("r2_mnq", bars, fng_prior_day=55.0)
    assert r2_eval["strategy_code"] == "r2_mnq"
    assert r2_eval["bars_evaluated"] == 25
    assert r2_eval["engine_snapshot"]["bricks"] >= 1
    assert r2_eval["thresholds"]["brick_max"] == 6.5


def test_ibkr_execute_dry_run_enforces_locked_stop(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    res = runner.ibkr_execute_signal("es_v221", "BUY", qty=2, dry_run=True, reason="unit_test")
    assert res["ok"] is True
    assert res["dry_run"] is True
    assert res["strategy_code"] == "es_v221"
    assert res["locked_stop_pct"] == 0.7
    assert res["account_id"].startswith("D")


def test_mcp_server_exposes_hq_strategy_and_ibkr_tools(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    svc = WorkspaceService(ws, admin_token="adm")
    member = svc.register("adm", "Manoj")
    mcp = build_mcp(svc, hq=None, strategy_runner=runner)

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {
        "hq_strategy_runtime",
        "hq_strategy_thresholds",
        "hq_strategy_configure_account",
        "hq_strategy_start",
        "hq_strategy_halt",
        "hq_strategy_resume",
        "hq_strategy_evaluate_bars",
        "ibkr_futures_account",
        "ibkr_futures_execute",
    } <= tool_names

    # Verify set_strategy does not allow changing bullish/bearish thresholds
    tok = _token.set(member["token"])
    try:
        strat = mcp._tool_manager.get_tool("set_strategy").fn(
            weights={"ma_crossover": 0.7, "price_action": 0.3},
            watchlist=["SPY"],
        )
        assert strat["ok"] is True
        saved = mcp._tool_manager.get_tool("get_strategy").fn()
        assert saved["bullish_threshold"] == 3.0
        assert saved["bearish_threshold"] == -3.0

        cfg = mcp._tool_manager.get_tool("hq_strategy_configure_account").fn(
            strategy="nq_v221",
            account_id="DUR842608",
            contracts=2,
            dry_run=True,
        )
        assert cfg["binding"]["account_id"] == "DUR842608"
        assert cfg["thresholds_locked"] is True

        exec_res = mcp._tool_manager.get_tool("ibkr_futures_execute").fn(
            strategy="nq_v221",
            action="BUY",
            qty=2,
            dry_run=True,
        )
        assert exec_res["ok"] is True
        assert exec_res["locked_stop_pct"] == 1.0
    finally:
        _token.reset(tok)


def test_api_hq_runtime_and_grokbot_inbox_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROK_BOT_INBOX_TOKEN", "grok-secret")
    runner = _runner(tmp_path)
    store = LocalFsStore(tmp_path / "store")
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = ws.register_member("Manoj")
    client = TestClient(
        build_app(
            workspace_factory=lambda: ws,
            report_store=store,
            strategy_runner=runner,
        )
    )
    headers = {"Authorization": f"Bearer {member.token}"}

    # 1. Runtime status & locked thresholds
    rt = client.get("/api/hq/runtime", headers=headers)
    assert rt.status_code == 200
    assert rt.json()["thresholds_locked"] is True
    assert len(rt.json()["strategies"]) == 3

    thr = client.get("/api/hq/runtime/thresholds?strategy=r2_mnq", headers=headers)
    assert thr.status_code == 200
    assert thr.json()["thresholds"]["chop_threshold"] == 0.06

    # 2. Reject threshold override on configure endpoint
    bad_cfg = client.post(
        "/api/hq/runtime/nq_v221/configure",
        headers=headers,
        json={"account_id": "DUR842608", "overrides": {"fng_threshold": 50}},
    )
    assert bad_cfg.status_code == 400
    assert "immutable" in bad_cfg.json()["detail"]

    # 3. Valid account configuration + dry-run IBKR execution via API
    ok_cfg = client.post(
        "/api/hq/runtime/nq_v221/configure",
        headers=headers,
        json={"account_id": "DUR842608", "contracts": 2, "dry_run": True},
    )
    assert ok_cfg.status_code == 200
    assert ok_cfg.json()["binding"]["account_id"] == "DUR842608"

    exec_resp = client.post(
        "/api/hq/runtime/nq_v221/ibkr-execute",
        headers=headers,
        json={"action": "SELL", "qty": 2, "dry_run": True},
    )
    assert exec_resp.status_code == 200
    assert exec_resp.json()["locked_stop_pct"] == 1.0

    # 4. GrokBot webhook inbox immediate execution & threshold protection
    bad_inbox = client.post(
        "/api/research/ops/inbox",
        headers={"X-Grok-Bot-Key": "grok-secret"},
        json={
            "text": "change stop",
            "action": "strategy-configure",
            "params": {"strategy": "nq_v221", "stop_pct": 2.5},
        },
    )
    assert bad_inbox.status_code == 400

    inbox_halt = client.post(
        "/api/research/ops/inbox",
        headers={"X-Grok-Bot-Key": "grok-secret"},
        json={
            "text": "Halt ES before CPI",
            "action": "strategy-halt",
            "params": {"strategy": "es_v221", "reason": "CPI release"},
            "execute_now": True,
        },
    )
    assert inbox_halt.status_code == 200
    assert inbox_halt.json()["acked"] is True
    assert inbox_halt.json()["execution"]["kill_switch_active"] is True
    assert runner.status("es_v221")["kill_switch_active"] is True

    # 5. Queued GrokBot instruction processed by worker
    add_instruction(
        "Resume ES after CPI",
        action="strategy-resume",
        params={"strategy": "es_v221"},
        store=store,
    )
    assert len(list_inbox(store=store)) == 1
    processed = process_strategy_inbox(store=store, runner=runner)
    assert len(processed) == 1 and processed[0]["ok"] is True
    assert runner.status("es_v221")["kill_switch_active"] is False
    assert list_inbox(store=store) == []
