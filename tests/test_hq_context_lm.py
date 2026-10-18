"""Tests for the HQ Context Language Model (CLM) across Claude Cowork MCP and GrokBot."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.hq.context_lm import (
    HqContextLanguageModel,
    analyze_position_risk,
    diagnose_execution_drift,
)
from waystone3.hq.strategy_runner import STRATEGY_SPECS, HqStrategyRunner
from waystone3.ibkr.store import LocalFsStore
from waystone3.mcp_server import _token, build_mcp
from waystone3.workspace.service import WorkspaceService
from waystone3.workspace.workspace import TradingWorkspace


def _seed_runner(tmp_path: Path) -> HqStrategyRunner:
    root = tmp_path / "hq-strategy"
    for rel in ("NQ_FUTURE", "ES_ALGO", "R2_MNQ_ALGO/R2_MNQ"):
        dest = root / rel
        (dest / "v221_logs" / "state").mkdir(parents=True, exist_ok=True)
        (dest / "v221_futures_live.py").write_text("#!/usr/bin/env python3\n")
        (dest / "v221_futures_live_r2.py").write_text("#!/usr/bin/env python3\n")
    return HqStrategyRunner(repo_root=root, state_file=tmp_path / "accounts.json")


def _seed_open_nq_trade_and_state(runner: HqStrategyRunner, *, today_loss: float = -1950.0) -> None:
    nq_dir = runner.strategy_dir("nq_v221")
    state_file = nq_dir / "v221_logs" / "state" / "engine_NQ_1m.json"
    state_file.write_text(
        json.dumps(
            {
                "bars_seen": 140,
                "bricks_total": 18,
                "brick_size": 8.5,
                "atr": 8.5,
                "renko_closes": [20100.0],
                "up_box": 20108.5,
                "down_box": 20095.75,
                "mco_list": [-4.2],
                "mco_sig_list": [-1.8],
                "algo_status": "going_no_where",
                "vol_ma_status": "barf",
                "correlation_status": "no_correlation",
                "cached_chop_ratio": 0.018,
                "position": "SHORT",
                "entry_price": 20000.0,
                "trade_duration": 12,
                "unrealized_gain_list": [0.25, 0.40],
                "pending_signal": None,
                "last_trigger": "SELL",
                "last_bar_ts": "2026-10-02T10:15:00-04:00",
            }
        )
    )
    db_path = nq_dir / "v221_logs" / "v221_futures.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            local_symbol TEXT,
            direction INTEGER,
            qty INTEGER,
            entry_ts TEXT,
            entry_px REAL,
            stop_px REAL,
            stop_order_id TEXT,
            oca_group TEXT,
            stop_confirmed INTEGER,
            exit_ts TEXT,
            exit_px REAL,
            exit_reason TEXT,
            points REAL,
            gross_pnl REAL,
            commission REAL,
            net_pnl REAL
        )
        """
    )
    from datetime import datetime

    from waystone3.ibkr.timeutil import NY

    today = datetime.now(NY).date().isoformat()
    # Closed losing trade earlier today
    con.execute(
        "INSERT INTO trades (trade_date, local_symbol, direction, qty, entry_ts, entry_px, "
        "exit_ts, exit_px, exit_reason, points, gross_pnl, commission, net_pnl) "
        "VALUES (?, 'NQZ6', 1, 2, '2026-10-02T09:35:00-04:00', 20100.0, "
        "'2026-10-02T09:50:00-04:00', 20051.5, 'yeah_its_failing', -48.5, -1940.0, 10.0, ?)",
        (today, today_loss),
    )
    # Currently open SHORT 2 NQZ6 @ 20,000.0 with confirmed stop at 20,200.0 (+1.0%)
    con.execute(
        "INSERT INTO trades (trade_date, local_symbol, direction, qty, entry_ts, entry_px, "
        "stop_px, stop_order_id, oca_group, stop_confirmed, exit_ts) "
        "VALUES (?, 'NQZ6', -1, 2, '2026-10-02T10:03:00-04:00', 20000.0, "
        "20200.0, '901', 'V221-900', 1, NULL)",
        (today,),
    )
    con.commit()
    con.close()


def test_position_xray_calculates_exact_locked_exit_ladder(tmp_path: Path) -> None:
    runner = _seed_runner(tmp_path)
    _seed_open_nq_trade_and_state(runner)
    st = runner.status("nq_v221")
    xray = analyze_position_risk(STRATEGY_SPECS["nq_v221"], st["engine_state"], st["db"])

    assert xray["in_position"] is True
    assert xray["direction"] == "SHORT"
    assert xray["contracts"] == 2
    assert xray["entry_price"] == 20000.0
    ladder = xray["exit_ladder"]

    # SHORT from 20,000:
    # -0.8% hard stop ("fts") is at 20,160.0 (-$6,400 on 2 NQ @ $20/pt)
    assert ladder["engine_hard_stop_fts"]["price"] == 20160.0
    assert ladder["engine_hard_stop_fts"]["est_pnl_usd"] == -6400.0
    # -1.0% broker GTC stop is at 20,200.0 (-$8,000)
    assert ladder["broker_disaster_stop"]["price"] == 20200.0
    assert ladder["broker_disaster_stop"]["est_pnl_usd"] == -8000.0
    # -0.5% going-nowhere stop is at 20,100.0 and active_now=True
    assert ladder["going_nowhere_stop"]["price"] == 20100.0
    assert ladder["going_nowhere_stop"]["active_now"] is True
    # +2% / +3% / +4% profit take for SHORT are below 20,000
    assert ladder["profit_take_low"]["price"] == 19600.0
    assert ladder["profit_take_mid"]["price"] == 19400.0
    assert ladder["profit_take_high"]["price"] == 19200.0
    assert "SHORT 2x NQ from 20000.00" in xray["narrative"]


def test_execution_drift_detects_loss_cap_proximity_and_shifts_to_defensive_micro(
    tmp_path: Path,
) -> None:
    runner = _seed_runner(tmp_path)
    # -$1,950 realized loss is 78% of the -$2,500 daily loss cap -> CAUTION -> REDUCE NQ
    _seed_open_nq_trade_and_state(runner, today_loss=-1950.0)
    clm = HqContextLanguageModel(runner=runner)

    brief = clm.generate_brief(question="Should we reduce NQ and run R2 MNQ?", use_llm=False)
    assert brief["thresholds_locked"] is True
    assert brief["portfolio_posture"] == "DEFENSIVE_MICRO"
    by_code = {v["strategy_code"]: v for v in brief["sleeve_verdicts"]}
    assert by_code["nq_v221"]["verdict"] == "REDUCE"
    assert by_code["nq_v221"]["recommended_contracts"] == 1  # shrunk from 2 -> 1
    assert by_code["r2_mnq"]["verdict"] == "TRADE"
    assert "Preferred Nasdaq-100 sleeve" in by_code["r2_mnq"]["rationale"]


def test_clm_with_claude_completer_enforces_downward_only_sizing(tmp_path: Path) -> None:
    runner = _seed_runner(tmp_path)

    async def fake_claude(_sys: str, _user: str, _schema: dict[str, Any]) -> dict[str, Any]:
        # Even if an LLM tries to recommend 8 contracts, the CLM clamps to default_contracts (2)
        return {
            "headline": "Claude Cowork synthesis: selective posture",
            "regime_assessment": "VIX contango, normal tape",
            "portfolio_posture": "SELECTIVE",
            "sleeve_verdicts": [
                {
                    "strategy_code": "nq_v221",
                    "verdict": "TRADE",
                    "recommended_contracts": 8,
                    "rationale": "Strong trend",
                },
                {
                    "strategy_code": "es_v221",
                    "verdict": "STAND_DOWN",
                    "recommended_contracts": 2,
                    "rationale": "Stand down ES",
                },
                {
                    "strategy_code": "r2_mnq",
                    "verdict": "TRADE",
                    "recommended_contracts": 5,
                    "rationale": "Run micro",
                },
            ],
            "execution_watch": "All fills clean",
            "operator_actions": ["Keep NQ at max 2 contracts, halt ES"],
        }

    clm = HqContextLanguageModel(runner=runner, complete_fn=fake_claude)
    brief = clm.generate_brief(question="Desk check", use_llm=True)
    assert brief["source"].startswith("claude_clm")
    by_code = {v["strategy_code"]: v for v in brief["sleeve_verdicts"]}
    # Clamped to validated max contracts (2 for NQ, 0 for STAND_DOWN ES, 1 for R2 MNQ)
    assert by_code["nq_v221"]["recommended_contracts"] == 2
    assert by_code["es_v221"]["recommended_contracts"] == 0
    assert by_code["r2_mnq"]["recommended_contracts"] == 1


def test_clm_apply_recommendation_halts_and_reduces_without_touching_thresholds(
    tmp_path: Path,
) -> None:
    runner = _seed_runner(tmp_path)
    # -$2,600 breaches -$2,500 cap on NQ -> STAND_DOWN -> engages KILL_SWITCH
    _seed_open_nq_trade_and_state(runner, today_loss=-2600.0)
    clm = HqContextLanguageModel(runner=runner)

    with pytest.raises(ValueError, match="immutable"):
        clm.apply_recommendation("nq_v221", overrides={"fng_threshold": 20})

    applied = clm.apply_recommendation("nq_v221", actor="claude_cowork")
    assert applied["thresholds_locked"] is True
    assert applied["results"][0]["action_taken"] == "halt"
    assert runner.status("nq_v221")["kill_switch_active"] is True


def test_mcp_and_api_and_grokbot_expose_context_lm(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROK_BOT_INBOX_TOKEN", "grok-secret")
    runner = _seed_runner(tmp_path)
    _seed_open_nq_trade_and_state(runner, today_loss=-1950.0)
    store = LocalFsStore(tmp_path / "store")
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    svc = WorkspaceService(ws, admin_token="adm")
    member = svc.register("adm", "Manoj")

    # 1. Verify Claude Cowork MCP tools
    mcp = build_mcp(svc, hq=None, strategy_runner=runner)
    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {
        "hq_context_brief",
        "hq_context_position_xray",
        "hq_context_apply_guard",
        "hq_context_notify_grokbot",
    } <= tool_names

    tok = _token.set(member["token"])
    try:
        mcp_brief = mcp._tool_manager.get_tool("hq_context_brief").fn(
            strategy="nq_v221",
            question="How is our NQ position?",
            use_llm=False,
        )
        assert mcp_brief["thresholds_locked"] is True
        assert mcp_brief["sleeve_verdicts"][0]["verdict"] == "REDUCE"

        mcp_xray = mcp._tool_manager.get_tool("hq_context_position_xray").fn(strategy="nq_v221")
        assert mcp_xray["in_position"] is True
        assert mcp_xray["exit_ladder"]["engine_hard_stop_fts"]["price"] == 20160.0

        mcp_apply = mcp._tool_manager.get_tool("hq_context_apply_guard").fn(strategy="nq_v221")
        assert mcp_apply["results"][0]["action_taken"] == "reduce_contracts"
        assert mcp_apply["results"][0]["contracts"] == 1
    finally:
        _token.reset(tok)

    # 2. Verify REST API & GrokBot inbox context actions
    client = TestClient(
        build_app(
            workspace_factory=lambda: ws,
            report_store=store,
            strategy_runner=runner,
        )
    )
    headers = {"Authorization": f"Bearer {member['token']}"}

    api_brief = client.get(
        "/api/hq/context/brief?question=Morning+check&use_llm=false",
        headers=headers,
    )
    assert api_brief.status_code == 200
    assert api_brief.json()["portfolio_posture"] == "DEFENSIVE_MICRO"

    api_xray = client.get("/api/hq/context/position-xray?strategy=nq_v221", headers=headers)
    assert api_xray.status_code == 200
    assert api_xray.json()["in_position"] is True

    grok_brief = client.post(
        "/api/research/ops/inbox",
        headers={"X-Grok-Bot-Key": "grok-secret"},
        json={
            "text": "Give me a context brief on NQ",
            "action": "strategy-context-brief",
            "params": {"strategy": "nq_v221", "use_llm": False},
            "execute_now": True,
        },
    )
    assert grok_brief.status_code == 200
    assert grok_brief.json()["acked"] is True
    assert grok_brief.json()["execution"]["thresholds_locked"] is True


def test_diagnose_execution_drift_flags_unconfirmed_stop_and_slippage() -> None:
    spec = STRATEGY_SPECS["es_v221"]
    drift = diagnose_execution_drift(
        spec,
        db_summary={
            "today_realized_pnl": -200.0,
            "today_closed_trades": 1,
            "open_trade": {"stop_confirmed": 0},
        },
        recent_events=[{"event": "exit", "slip_vs_quote_pt": 4.25}],
        sync_row={"sync_status": "FLAG", "net_pnl_delta": -450.0},
    )
    assert drift["severity"] == "HALT"
    assert any("unconfirmed IBKR GTC stop" in f for f in drift["flags"])
    assert any("Elevated exit slippage" in f for f in drift["flags"])
    assert any("Live vs backtest replay flagged" in f for f in drift["flags"])
