"""Strategy test assembly line: raise mismatch bugs, then wait for a person."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from waystone3.api.app import build_app
from waystone3.brokers.paper import PaperBroker
from waystone3.data.stub import StubDataSource
from waystone3.hq.factory import HqStrategyFactory, classify_sync_row
from waystone3.ibkr.store import LocalFsStore
from waystone3.mcp_server import build_mcp
from waystone3.workspace.service import WorkspaceService
from waystone3.workspace.workspace import TradingWorkspace

FILL_ROW: dict[str, Any] = {
    "strategy_code": "nq_v221",
    "session_date": "2026-10-02",
    "sync_status": "FLAG",
    "live_trades": 1,
    "bt_trades": 1,
    "pnl_delta": -1975.0,
    "notes": (
        "P&L delta -32.9% exceeds 10% "
        "(same signal & exit reason; entry fill price shifted % exit bar)"
    ),
}
EXIT_ROW: dict[str, Any] = {
    "strategy_code": "r2_mnq",
    "session_date": "2026-10-02",
    "sync_status": "FLAG",
    "notes": "exit reason differs",
}
COUNT_ROW: dict[str, Any] = {
    "strategy_code": "es_v221",
    "session_date": "2026-10-01",
    "sync_status": "FLAG",
    "notes": "trade count 1 live vs 2 backtest",
}
OK_ROW: dict[str, Any] = {
    "strategy_code": "nq_v221",
    "session_date": "2026-10-01",
    "sync_status": "OK",
    "notes": "1 backtest trade(s) excluded: live correctly blocked by the daily loss cap",
}


class _Runner:
    def status(self, strategy: str | None = None) -> dict[str, Any]:
        del strategy
        return {
            "thresholds_locked": True,
            "strategies": [
                {
                    "strategy_code": "nq_v221",
                    "kill_switch_active": False,
                    "process": {"running": False},
                    "account_binding": {"account_id": "DUR842609"},
                },
                {
                    "strategy_code": "es_v221",
                    "kill_switch_active": True,
                    "process": {"running": False},
                    "account_binding": {"account_id": "DUR842609"},
                },
            ],
        }


class _Sandbox:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def run_gcs_backtest(self, strategy: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"strategy": strategy, **kwargs})
        if self.fail:
            raise FileNotFoundError("no historical bars for that session")
        return {
            "run_id": "sbx_hist_1",
            "live_parity": kwargs.get("live_parity"),
            "thresholds_locked": True,
            "metrics": {"net_pnl_usd": -12.5},
        }


def _line(tmp_path: Path, sandbox: _Sandbox | None = None) -> HqStrategyFactory:
    return HqStrategyFactory(
        runner=_Runner(),
        sandbox=sandbox,
        store=LocalFsStore(tmp_path / "reports"),
    )


def test_ok_rows_do_not_raise_bugs() -> None:
    assert classify_sync_row(OK_ROW) == []


def test_scan_without_a_database_does_not_claim_the_compare_is_clear(tmp_path: Path) -> None:
    report = _line(tmp_path).scan(None)
    assert report["source"] == "no_hq_database"
    assert report["raised"] == []
    states = {station["id"]: station["state"] for station in report["stations"]}
    assert states["daily_backtest"] == "idle"
    assert states["compare"] == "idle"
    assert states["bugs"] == "idle"


def test_scan_raises_bugs_and_approval_does_not_change_thresholds(tmp_path: Path) -> None:
    line = _line(tmp_path)
    first = line.scan([FILL_ROW, EXIT_ROW, COUNT_ROW, OK_ROW])
    assert sorted(ticket["bucket"] for ticket in first["tickets"]) == [
        "exit_reason",
        "fill_exit_drift",
        "trade_count",
    ]
    assert first["raised"]
    assert first["thresholds_locked"] is True

    again = line.scan([FILL_ROW, EXIT_ROW, COUNT_ROW, OK_ROW])
    assert again["raised"] == []
    assert len(again["tickets"]) == 3

    fill = next(ticket for ticket in first["tickets"] if ticket["bucket"] == "fill_exit_drift")
    approved = line.approve(fill["ticket_id"], actor="ada", note="use live parity")
    assert approved["status"] == "implemented"
    assert approved["implementation"]["applied"] is True
    assert approved["implementation"]["thresholds_changed"] is False
    assert approved["implementation"]["engine_changed"] is False
    board = line.pipeline()
    assert board["policies"]["nq_v221"]["comparison_mode"] == "live_parity"
    nq = next(row for row in board["strategies"] if row["strategy_code"] == "nq_v221")
    assert nq["paper"] == "armed"
    assert nq["station"] == "continue"

    exit_bug = next(ticket for ticket in first["tickets"] if ticket["bucket"] == "exit_reason")
    proposal = line.approve(exit_bug["ticket_id"], actor="ada")
    assert proposal["status"] == "continued"
    assert proposal["implementation"]["applied"] is False
    assert "Locked thresholds" in proposal["implementation"]["blocked_reason"]
    assert "r2_mnq" not in line.pipeline()["policies"] or (
        "comparison_mode" not in line.pipeline()["policies"].get("r2_mnq", {})
    )

    count = next(ticket for ticket in first["tickets"] if ticket["bucket"] == "trade_count")
    try:
        line.approve(count["ticket_id"], overrides={"hard_stop_pct": 0.2})
    except ValueError as exc:
        assert "immutable" in str(exc)
    else:
        raise AssertionError("threshold override should have been refused")
    still = next(ticket for ticket in line.tickets() if ticket["ticket_id"] == count["ticket_id"])
    assert still["status"] == "pending_approval"
    rejected = line.reject(count["ticket_id"], actor="ada", note="not a bug")
    assert rejected["status"] == "rejected"
    assert line.scan([COUNT_ROW])["raised"] == []


def test_historical_backtest_gate(tmp_path: Path) -> None:
    sandbox = _Sandbox()
    line = _line(tmp_path, sandbox)
    ticket = line.scan([FILL_ROW])["tickets"][0]
    approved = line.approve(ticket["ticket_id"], actor="ada", run_historical_backtest=True)
    assert approved["status"] == "implemented"
    assert approved["historical_backtest"]["ok"] is True
    assert approved["historical_backtest"]["run_id"] == "sbx_hist_1"
    assert sandbox.calls[0]["live_parity"] is True
    assert sandbox.calls[0]["start"] == "2026-10-02"
    assert sandbox.calls[0]["end"] == "2026-10-02"

    failing = _Sandbox(fail=True)
    blocked = HqStrategyFactory(
        runner=_Runner(), sandbox=failing, store=LocalFsStore(tmp_path / "b")
    )
    pending = blocked.scan([EXIT_ROW])["tickets"][0]
    held = blocked.approve(pending["ticket_id"], run_historical_backtest=True)
    assert held["status"] == "pending_approval"
    assert held["historical_backtest"]["ok"] is False
    finished = blocked.approve(pending["ticket_id"], actor="ada")
    assert finished["status"] == "continued"


def test_api_mcp_and_grokbot_share_the_line(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("GROK_BOT_INBOX_TOKEN", "grok-secret")
    store = LocalFsStore(tmp_path / "reports")
    runner = _Runner()
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    member = WorkspaceService(ws, admin_token="adm").register("adm", "Manoj")
    mcp = build_mcp(WorkspaceService(ws, admin_token="adm"), hq=None, strategy_runner=runner)
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert {
        "hq_factory_line",
        "hq_factory_scan",
        "hq_factory_approve",
        "hq_factory_reject",
    } <= names

    client = TestClient(
        build_app(
            workspace_factory=lambda: ws,
            report_store=store,
            strategy_runner=runner,
            hq_reader=None,
        )
    )
    headers = {"Authorization": f"Bearer {member['token']}"}
    scanned = client.post(
        "/api/hq/factory/scan",
        headers=headers,
        json={"rows": [FILL_ROW, EXIT_ROW]},
    )
    assert scanned.status_code == 200
    body = scanned.json()
    assert body["thresholds_locked"] is True
    assert len(body["raised"]) == 2
    fill = next(ticket for ticket in body["tickets"] if ticket["bucket"] == "fill_exit_drift")

    approved = client.post(
        f"/api/hq/factory/tickets/{fill['ticket_id']}/approve",
        headers=headers,
        json={"actor": "ada", "note": "continue", "overrides": {"hard_stop_pct": 1}},
    )
    assert approved.status_code == 400
    assert "immutable" in approved.json()["detail"]

    grok = client.post(
        "/api/research/ops/inbox",
        headers={"X-Grok-Bot-Key": "grok-secret"},
        json={
            "text": "approve the fill drift and keep trading",
            "action": "factory-approve",
            "execute_now": True,
            "params": {"ticket_id": fill["ticket_id"], "note": "from grok"},
        },
    )
    assert grok.status_code == 200
    assert grok.json()["execution"]["status"] == "implemented"
    assert grok.json()["execution"]["implementation"]["thresholds_changed"] is False

    board = client.get("/api/hq/factory/line", headers=headers)
    assert board.status_code == 200
    assert board.json()["policies"]["nq_v221"]["comparison_mode"] == "live_parity"
    stations = {row["id"]: row["state"] for row in board.json()["stations"]}
    assert stations["paper"] == "mixed"
    assert stations["approval"] == "waiting"
