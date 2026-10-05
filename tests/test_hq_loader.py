"""End-to-end loader test against a real Postgres built from deploy/db/sql.

Opt-in: set WAYSTONE_TEST_PG_ADMIN_DSN to a superuser DSN, e.g.
    WAYSTONE_TEST_PG_ADMIN_DSN="host=/tmp port=5499 user=postgres dbname=postgres"
psql (15+) must be on PATH, because the bootstrap SQL uses psql meta-commands.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import conninfo

from waystone3.hq.loader import Loader, connect
from waystone3.hq.sources import LocalSource

ADMIN_DSN = os.environ.get("WAYSTONE_TEST_PG_ADMIN_DSN", "")
SQL_DIR = Path(__file__).parents[1] / "deploy" / "db" / "sql"
FIXTURES = Path(__file__).parent / "fixtures" / "hq"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.skipif(
    not ADMIN_DSN or shutil.which("psql") is None,
    reason="needs WAYSTONE_TEST_PG_ADMIN_DSN and psql",
)


def _psql(dsn: str, path: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(path)],
        check=True,
        env={**os.environ, **(env or {})},
        capture_output=True,
    )


@pytest.fixture
def db() -> Iterator[str]:
    name = f"waystone_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo.conninfo_to_dict(ADMIN_DSN)
    admin_db = conninfo.make_conninfo(**{**params, "dbname": name})  # type: ignore[arg-type]
    load_db = conninfo.make_conninfo(
        **{**params, "dbname": name, "user": "waystone_load", "password": "test-load"}  # type: ignore[arg-type]
    )
    try:
        _psql(
            admin_db,
            SQL_DIR / "01_roles.sql",
            {"WAYSTONE_DB": name, "WAYSTONE_LOAD_PW": "test-load", "WAYSTONE_READ_PW": "test-read"},
        )
        for path in sorted(SQL_DIR.glob("0[2-8]_*.sql")):
            _psql(load_db, path)
        yield load_db
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _rows(dsn: str, sql: str) -> list[dict[str, Any]]:
    with connect(dsn) as conn:
        return list(conn.execute(sql).fetchall())


def test_backfill_loads_all_three_strategies(db: str, tmp_path: Path) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    with connect(db) as conn:
        report = Loader(conn, LocalSource(src), now=NOW).run("backfill")
    assert report.status == "OK", report.failures
    assert report.files_loaded == 5
    assert sorted(report.recomputed) == ["es_v221", "nq_v221", "r2_mnq"]

    sync = {
        r["strategy_code"]: r
        for r in _rows(db, "SELECT * FROM api.v_daily_sync ORDER BY strategy_code, session_date")
    }
    assert sync["es_v221"]["sync_status"] == "OK"
    assert str(sync["es_v221"]["pnl_delta"]) == "75.04"
    assert sync["nq_v221"]["sync_status"] == "FLAG"
    assert str(sync["nq_v221"]["live_net_pnl"]) == "-7983.96"
    assert sync["r2_mnq"]["sync_status"] == "N/A"
    assert sync["r2_mnq"]["backtest_status"] == "MISSING"
    assert sync["r2_mnq"]["live_trades"] == 3

    trades = _rows(db, "SELECT * FROM api.v_paper_trades WHERE strategy_code = 'nq_v221'")
    assert len(trades) == 1
    nq = trades[0]
    assert (nq["trade_no"], nq["direction"], nq["instrument"]) == (9, "SHORT", "NQZ6")
    assert str(nq["slippage_cost"]) == "315.00"
    assert str(nq["pnl_at_signal"]) == "-7660.00"

    comparison = _rows(
        db,
        "SELECT match_type, unmatched_reason FROM api.v_comparison WHERE strategy_code = 'es_v221' "
        "ORDER BY match_seq",
    )
    assert [(r["match_type"], r["unmatched_reason"]) for r in comparison] == [
        ("MATCHED", None),
        ("BACKTEST_ONLY", "LOSS_CAP_BLOCKED"),
    ]

    pnl = _rows(
        db, "SELECT * FROM api.v_daily_pnl WHERE strategy_code = 'nq_v221' ORDER BY session_date"
    )
    assert pnl[0]["session_date"].isoformat() == "2026-09-21"
    assert str(pnl[-1]["equity_end"]) == "92016.04"

    gate = _rows(
        db,
        "SELECT overall_gate FROM api.v_scorecard WHERE strategy_code = 'r2_mnq' "
        "AND kpi_window = 'ITD' ORDER BY as_of_date DESC LIMIT 1",
    )
    assert gate[0]["overall_gate"].startswith("INSUFFICIENT SAMPLE — 3 trades")
    kpi_codes = _rows(
        db, "SELECT DISTINCT kpi_code FROM api.v_kpi_latest WHERE strategy_code = 'es_v221'"
    )
    assert {"fut_sharpe", "fut_max_dd", "fut_cost_drag", "hdr_net_pnl"} <= {
        r["kpi_code"] for r in kpi_codes
    }
    assert _rows(db, "SELECT count(*) AS n FROM kpi.returns_weekly")[0]["n"] > 0
    assert _rows(db, "SELECT count(*) AS n FROM kpi.sync_rolling")[0]["n"] == 3


def test_rerun_is_noop_and_growing_log_replaces_in_place(db: str, tmp_path: Path) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
    with connect(db) as conn:
        again = Loader(conn, LocalSource(src), now=NOW).run("backfill")
    assert again.status == "NOOP" and again.files_loaded == 0

    log = src / "raw" / "paper" / "nq_v221" / "2026-10-02.log"
    with log.open("a") as fh:
        fh.write("2026-10-02 17:05:00 [IB 1100] Connectivity between IB and TWS has been lost.\n")
    with connect(db) as conn:
        grown = Loader(conn, LocalSource(src), now=NOW).run("paper", ["nq_v221"])
    assert grown.status == "OK" and grown.files_loaded == 1

    versions = _rows(
        db,
        "SELECT is_current, (SELECT count(*) FROM raw.source_line l WHERE l.file_id = f.file_id) "
        "AS lines FROM raw.source_file f WHERE gcs_uri LIKE '%nq_v221/2026-10-02.log' "
        "ORDER BY file_id",
    )
    assert [(v["is_current"], v["lines"]) for v in versions] == [(False, 0), (True, 36)]
    assert _rows(db, "SELECT count(*) AS n FROM core.paper_trade")[0]["n"] == 5
    codes = _rows(
        db,
        "SELECT code, count(*) AS n FROM core.ops_event WHERE strategy_id = "
        "(SELECT strategy_id FROM ref.strategy WHERE strategy_code = 'nq_v221') GROUP BY code",
    )
    counts = {r["code"]: r["n"] for r in codes}
    assert counts["IB_1100"] == 1 and counts["LOSS_CAP"] == 1
    actions = _rows(db, "SELECT action FROM ops.load_file ORDER BY run_id DESC LIMIT 1")
    assert actions[0]["action"] == "REPLACED"


def test_bad_file_fails_alone(db: str, tmp_path: Path) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    (src / "raw" / "paper" / "es_v221" / "events_2026-10-01.jsonl").write_text(
        '{"type": "x"}\nnot json\n'
    )
    (src / "raw" / "paper" / "es_v221" / "2026-10-01_info.txt").write_text("ES V221 info\n")
    with connect(db) as conn:
        report = Loader(conn, LocalSource(src), now=NOW).run("paper", ["es_v221"])
    assert report.status == "OK"
    statuses = {
        r["source_kind"]: r["parse_status"]
        for r in _rows(db, "SELECT source_kind, parse_status FROM raw.source_file")
    }
    assert statuses == {"paper_log": "PARSED", "paper_events": "PARTIAL", "paper_info": "SKIPPED"}
    assert _rows(db, "SELECT count(*) AS n FROM raw.source_event")[0]["n"] == 1


def test_dashboard_api_reads_views_as_waystone_read(db: str, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from waystone3.api.app import build_app
    from waystone3.brokers.paper import PaperBroker
    from waystone3.data.stub import StubDataSource
    from waystone3.hq.reader import HqReader
    from waystone3.workspace.workspace import TradingWorkspace

    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
    read_dsn = conninfo.make_conninfo(db, user="waystone_read", password="test-read")
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    token = ws.register_member("Manoj").token
    client = TestClient(build_app(workspace_factory=lambda: ws, hq_reader=HqReader(read_dsn)))
    auth = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/hq/strategies").status_code == 401
    codes = [
        s["strategy_code"]
        for s in client.get("/api/hq/strategies", headers=auth).json()["strategies"]
    ]
    assert {"es_v221", "nq_v221", "r2_mnq"} <= set(codes)

    r2 = client.get("/api/hq/strategies/r2_mnq", headers=auth).json()
    assert r2["overall_gate"].startswith("INSUFFICIENT SAMPLE") and r2["kpi_dates"]

    kpis = client.get("/api/hq/strategies/es_v221/kpis", headers=auth).json()
    assert [c["kpi_window"] for c in kpis["scorecard"]] == ["WEEK", "MTD", "ITD"]
    assert any(k["kpi_code"] == "fut_sharpe" for k in kpis["kpis"])

    compare = client.get("/api/hq/strategies/es_v221/compare", headers=auth).json()
    assert compare["session_date"] == "2026-10-01"
    assert compare["sync"]["pnl_delta"] == 75.04
    assert [m["unmatched_reason"] for m in compare["matches"]] == [None, "LOSS_CAP_BLOCKED"]
    ctx = compare["context"]
    assert ctx["settings"]["point_value"] == 50.0
    assert ctx["backtest_run"]["total_net_reported"] == -5468.0
    assert ctx["live_params"]["params_fp"] == ctx["backtest_run"]["params_fp"]
    assert {"outcome": "ENTERED", "block_reason": None, "n": 1} in ctx["signals"]
    assert ctx["day_status"]["checks"]["paper"]["net_match"] is True

    sync = client.get("/api/hq/sync?date=2026-10-02", headers=auth).json()
    by_code = {r["strategy_code"]: r for r in sync["rows"]}
    assert by_code["nq_v221"]["sync_status"] == "FLAG"
    assert by_code["r2_mnq"]["backtest_status"] == "MISSING"

    daily = client.get("/api/hq/strategies/nq_v221/daily?start=2026-10-01", headers=auth).json()
    assert daily["days"][-1]["equity_end"] == 92016.04
    trades = client.get("/api/hq/strategies/r2_mnq/trades", headers=auth).json()["trades"]
    assert [t["trade_no"] for t in trades] == [21, 22, 23]
    assert client.get("/api/hq/strategies/nq_v221/returns", headers=auth).json()["weekly"]
    assert {r["job"] for r in client.get("/api/hq/status", headers=auth).json()["loads"]} == {
        "backfill"
    }
    paper = client.get("/api/hq/paper?date=2026-10-02", headers=auth).json()
    assert paper["dates"][-1] == "2026-10-02"
    assert {t["strategy_code"] for t in paper["trades"]} == {"nq_v221", "r2_mnq"}
    assert paper["fills"] and paper["signals"] and paper["events"]
    nq = {f["strategy_code"]: f for f in paper["freshness"]}["nq_v221"]
    assert nq["paper_status"] == "FINAL" and nq["last_log_line_ts"] and nq["gcs_updated_at"]
    one = client.get("/api/hq/paper?strategy=nq_v221", headers=auth).json()
    assert one["session_date"] == "2026-10-02" and len(one["freshness"]) == 1
    assert client.get("/api/hq/paper?strategy=nope", headers=auth).status_code == 404
    assert client.get("/api/hq/strategies/nope", headers=auth).status_code == 404
    assert client.get("/api/hq/sync?date=bad", headers=auth).status_code == 400


def test_mcp_hq_tools_read_views(db: str, tmp_path: Path) -> None:
    import asyncio
    import json

    from waystone3.brokers.paper import PaperBroker
    from waystone3.data.stub import StubDataSource
    from waystone3.hq.reader import HqReader
    from waystone3.mcp_server import _token, build_mcp
    from waystone3.workspace.service import WorkspaceService
    from waystone3.workspace.workspace import TradingWorkspace

    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
    read_dsn = conninfo.make_conninfo(db, user="waystone_read", password="test-read")
    ws = TradingWorkspace(StubDataSource(), PaperBroker())
    token = ws.register_member("Manoj").token
    mcp = build_mcp(WorkspaceService(ws), HqReader(read_dsn))

    def call(name: str, args: dict[str, Any] | None = None) -> Any:
        result = asyncio.run(mcp.call_tool(name, args or {}))
        if isinstance(result, tuple):
            structured = result[1]
            return structured["result"] if set(structured) == {"result"} else structured
        return json.loads(result[0].text)  # type: ignore[index, union-attr]

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"hq_strategies", "hq_kpis", "hq_sync", "hq_compare", "hq_daily_pnl"} <= names

    reset = _token.set("bogus")
    try:
        with pytest.raises(Exception, match="invalid or missing token"):
            asyncio.run(mcp.call_tool("hq_strategies", {}))
    finally:
        _token.reset(reset)

    reset = _token.set(token)
    try:
        sync = call("hq_sync", {"date": "2026-10-02"})
        assert {r["strategy_code"]: r["sync_status"] for r in sync["rows"]} == {
            "nq_v221": "FLAG",
            "r2_mnq": "N/A",
        }
        es = call("hq_compare", {"strategy": "es_v221"})
        assert es["session_date"] == "2026-10-01" and es["sync"]["pnl_delta"] == 75.04
        kpis = call("hq_kpis", {"strategy": "r2_mnq"})
        assert kpis["scorecard"][-1]["overall_gate"].startswith("INSUFFICIENT SAMPLE")
        assert call("hq_daily_pnl", {"strategy": "nq_v221"})[-1]["equity_end"] == 92016.04
        paper = call("hq_paper_day", {"strategy": "r2_mnq"})
        assert paper["session_date"] == "2026-10-02"
        assert [t["trade_no"] for t in paper["trades"]] == [21, 22, 23]
        with pytest.raises(Exception, match="unknown strategy"):
            asyncio.run(mcp.call_tool("hq_kpis", {"strategy": "nope"}))
    finally:
        _token.reset(reset)
