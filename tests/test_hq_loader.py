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

from waystone3.hq.calendar import NY
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


def test_dashboard_api_serves_read_only_hq_mcp(db: str, tmp_path: Path) -> None:
    import json

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
    app = build_app(workspace_factory=lambda: ws, hq_reader=HqReader(read_dsn))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def rpc(method: str, params: dict[str, object], auth: str | None = token) -> Any:
        h = {**headers, **({"Authorization": f"Bearer {auth}"} if auth else {})}
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return client.post("/api/mcp", headers=h, json=body)

    with TestClient(app) as client:
        assert rpc("tools/list", {}, auth=None).status_code == 401
        assert rpc("tools/list", {}, auth="wrong").status_code == 401
        init = rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        assert (
            init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "waystone-hq"
        )
        tools = {t["name"] for t in rpc("tools/list", {}).json()["result"]["tools"]}
        assert {"hq_strategies", "hq_sync", "hq_compare", "hq_paper_day"} <= tools
        assert not tools & {"set_strategy", "run_cycle", "register_member"}
        call = rpc("tools/call", {"name": "hq_sync", "arguments": {"date": "2026-10-02"}}).json()
        payload = json.loads(call["result"]["content"][0]["text"])
        assert {r["strategy_code"] for r in payload["rows"]} >= {"nq_v221", "r2_mnq"}
        assert client.get("/api/health").json() == {"ok": True}


def test_recompute_drops_stale_duplicate_trades_and_accepts_gross_summary(
    db: str, tmp_path: Path
) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.paper_trade (strategy_id, session_date, trade_no, direction, "
                "contracts, entry_ts, exit_ts, entry_px, exit_px, points, gross_pnl, commission, "
                "net_pnl, file_id) "
                "SELECT strategy_id, session_date, trade_no, direction, contracts, "
                "entry_ts + interval '67 milliseconds', exit_ts + interval '467 milliseconds', "
                "entry_px, exit_px, points, gross_pnl, commission, net_pnl, NULL "
                "FROM core.paper_trade t JOIN ref.strategy s USING (strategy_id) "
                "WHERE s.strategy_code = 'nq_v221' AND t.session_date = '2026-10-02'"
            )
            cur.execute(
                "UPDATE ops.day_status d SET paper_status = 'PARTIAL', checks = jsonb_set("
                "checks, '{paper,net_reported}', to_jsonb((SELECT sum(gross_pnl) "
                "FROM core.paper_trade t WHERE t.strategy_id = d.strategy_id "
                "AND t.session_date = d.session_date AND t.file_id IS NOT NULL))) "
                "FROM ref.strategy s WHERE s.strategy_id = d.strategy_id "
                "AND s.strategy_code = 'nq_v221' AND d.session_date = '2026-10-02'"
            )
        conn.commit()
        assert (
            _rows(
                db,
                "SELECT live_net_pnl FROM api.v_daily_sync "
                "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
            )[0]["live_net_pnl"]
            is not None
        )
        Loader(conn, LocalSource(src), now=NOW).run("recompute", ["nq_v221"])
        trades = _rows(
            db,
            "SELECT trade_no FROM api.v_paper_trades "
            "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
        )
        assert [t["trade_no"] for t in trades] == [9]
        sync = _rows(
            db,
            "SELECT live_trades, live_net_pnl FROM api.v_daily_sync "
            "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
        )[0]
        assert sync["live_trades"] == 1 and float(sync["live_net_pnl"]) == -7983.96
        status = _rows(
            db,
            "SELECT paper_status, checks->'paper' AS paper FROM api.v_day_status "
            "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
        )[0]
        assert status["paper_status"] == "FINAL"
        assert status["paper"]["net_match"] is True and status["paper"]["net_basis"] == "gross"


def test_recompute_reparses_stored_replay_lines_with_offset_timestamps(
    db: str, tmp_path: Path
) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT f.file_id FROM raw.source_file f JOIN ref.strategy s USING (strategy_id) "
                "WHERE s.strategy_code = 'nq_v221' AND f.source_kind = 'backtest_daily' "
                "AND f.session_date = '2026-10-02' AND f.is_current"
            )
            file_id = cur.fetchone()["file_id"]
            cur.execute("DELETE FROM raw.source_line WHERE file_id = %s", (file_id,))
            lines = [
                "NQ same-day replay 2026-10-02",
                "2026-10-02 09:31:00-04:00 SHORT -> 2026-10-02 10:05:00-04:00  pts -150.00  "
                "net $-6,008.96  reason=yeah_its_failing",
                "total trades: 1  total net: -6008.96",
            ]
            for no, content in enumerate(lines, start=1):
                cur.execute(
                    "INSERT INTO raw.source_line (file_id, line_no, content) VALUES (%s, %s, %s)",
                    (file_id, no, content),
                )
            cur.execute(
                "UPDATE core.backtest_trade SET trade_seq = 2026, entry_px = 9, exit_px = 4, "
                "points = 5, net_pnl = 191.04 WHERE session_date = '2026-10-02' AND strategy_id = "
                "(SELECT strategy_id FROM ref.strategy WHERE strategy_code = 'nq_v221')"
            )
        conn.commit()
        Loader(conn, LocalSource(src), now=NOW).run("recompute", ["nq_v221"])
    [bt] = _rows(
        db,
        "SELECT b.trade_seq, b.entry_px, b.exit_px, b.points, b.net_pnl, b.net_pnl_derived, "
        "b.exit_ts FROM core.backtest_trade b JOIN ref.strategy s USING (strategy_id) "
        "WHERE s.strategy_code = 'nq_v221' AND b.session_date = '2026-10-02'",
    )
    assert bt["trade_seq"] == 1 and bt["entry_px"] is None and bt["exit_px"] is None
    assert float(bt["points"]) == -150.0 and float(bt["net_pnl"]) == -6008.96
    assert bt["net_pnl_derived"] is False
    assert bt["exit_ts"].astimezone(NY).strftime("%H:%M") == "10:05"
    [status] = _rows(
        db,
        "SELECT backtest_status, checks->'backtest' AS bt FROM api.v_day_status "
        "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
    )
    assert status["backtest_status"] == "LOADED" and status["bt"]["net_match"] is True


def test_replay_trades_that_disagree_with_the_file_total_mark_the_day_incomplete(
    db: str, tmp_path: Path
) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    bad = src / "raw" / "backtest" / "NQ_2026-10-02_back_daily.txt"
    bad.write_text(bad.read_text().replace("total net: -6008.96", "total net: -9999.00"))
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
    [status] = _rows(
        db,
        "SELECT backtest_status, checks->'backtest' AS bt FROM api.v_day_status "
        "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
    )
    assert status["backtest_status"] == "DATA_INCOMPLETE"
    assert status["bt"]["net_match"] is False and status["bt"]["net_reported"] == -9999.0


def test_r2_mnq_replay_files_load_as_r2_backtests(db: str, tmp_path: Path) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    (src / "raw" / "backtest" / "R2_MNQ_2026-10-02_back_daily.txt").write_text(
        "R2 MNQ same-day replay 2026-10-02\n"
        "params fp: bb6649148c\n"
        "2026-10-02 09:36:00-04:00 LONG -> 2026-10-02 10:05:00-04:00  pts -154.75  "
        "net $-310.72  reason=yeah_its_failing\n"
        "2026-10-02 11:21:00-04:00 SHORT -> 2026-10-02 12:02:00-04:00  pts -109.25  "
        "net $-219.72  reason=yeah_its_failing\n"
        "2026-10-02 14:10:00-04:00 SHORT -> 2026-10-02 15:55:00-04:00  pts -34.75  "
        "net $-70.72  reason=SESSION_FLATTEN\n"
        "total trades: 3  total net: -601.16\n"
    )
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
    [sync] = _rows(
        db,
        "SELECT backtest_status, bt_trades, bt_net_pnl, live_trades FROM api.v_daily_sync "
        "WHERE strategy_code = 'r2_mnq' AND session_date = '2026-10-02'",
    )
    assert sync["backtest_status"] == "LOADED"
    assert sync["bt_trades"] == 3 and float(sync["bt_net_pnl"]) == -601.16
    assert sync["live_trades"] == 3
    [status] = _rows(
        db,
        "SELECT checks->'backtest' AS bt FROM api.v_day_status "
        "WHERE strategy_code = 'r2_mnq' AND session_date = '2026-10-02'",
    )
    assert status["bt"]["net_match"] is True and status["bt"]["trades_parsed"] == 3
    owners = _rows(
        db,
        "SELECT s.strategy_code, f.gcs_uri FROM raw.source_file f "
        "JOIN ref.strategy s USING (strategy_id) WHERE f.source_kind = 'backtest_daily'",
    )
    assert {o["gcs_uri"].rsplit("/", 1)[-1]: o["strategy_code"] for o in owners} == {
        "ES_2026-10-01_back_daily.txt": "es_v221",
        "NQ_2026-10-02_back_daily.txt": "nq_v221",
        "R2_MNQ_2026-10-02_back_daily.txt": "r2_mnq",
    }


def test_replay_file_with_nothing_recognisable_is_not_reported_as_loaded(
    db: str, tmp_path: Path
) -> None:
    src = tmp_path / "bucket"
    shutil.copytree(FIXTURES, src)
    (src / "raw" / "backtest" / "NQ_2026-10-02_back_daily.txt").write_text(
        "NQ same-day replay 2026-10-02\nreplay finished\n"
    )
    with connect(db) as conn:
        Loader(conn, LocalSource(src), now=NOW).run("backfill")
    [status] = _rows(
        db,
        "SELECT backtest_status FROM api.v_day_status "
        "WHERE strategy_code = 'nq_v221' AND session_date = '2026-10-02'",
    )
    assert status["backtest_status"] == "DATA_INCOMPLETE"
