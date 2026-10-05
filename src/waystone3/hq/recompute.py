"""Rebuilds every derived table for one strategy from core paper and replay trades.

Derived tables are small (one row per trading day, or per KPI per day), so each run
recomputes the strategy in full inside one transaction. That keeps the results
identical to a from-scratch backfill and lets a settings change apply retroactively.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from waystone3.hq import kpis
from waystone3.hq.calendar import NY
from waystone3.hq.compare import Match, SyncRow, TradeRef, daily_sync, match_trades
from waystone3.hq.db import Conn, Cursor, Row, copy_rows, delete_for_strategy, upsert
from waystone3.hq.refdata import RefData, Strategy, dec
from waystone3.hq.v221_log import reconcile_summary

BACKTEST_DUE = time(17, 30)
_KPI_TABLES = (
    "kpi.kpi_value",
    "kpi.scorecard",
    "kpi.returns_weekly",
    "kpi.returns_monthly",
    "kpi.sync_rolling",
    "core.daily_pnl",
    "core.comparison_trade",
)
_DAILY_PNL_COLS = (
    "strategy_id", "session_date", "week_end", "month_start", "trades", "gross_pnl", "commission",
    "slippage_cost", "net_pnl", "hold_min_total", "equity_start", "equity_end", "daily_return",
    "peak_itd", "dd_itd", "uw_days_itd", "peak_week", "dd_week", "uw_days_week", "peak_month",
    "dd_month", "uw_days_month",
)  # fmt: skip
_KPI_COLS = (
    "strategy_id", "as_of_date", "kpi_window", "kpi_code", "num_value", "text_value", "status",
    "window_start", "window_end",
)  # fmt: skip


def _f(value: Any, default: float = 0.0) -> float:
    return default if value is None else float(value)


def _r(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def _ref(row: Row, key: str) -> TradeRef:
    return TradeRef(
        row[key],
        row["direction"],
        row["entry_ts"],
        row["exit_ts"],
        dec(row["points"]),
        dec(row["net_pnl"]),
        row["exit_reason"],
        dec(row["gross_pnl"]),
        dec(row["commission"]),
    )


def backtest_status(strategy: Strategy, day: date, run: Row | None, now: datetime) -> str:
    if run is not None:
        return "LOADED" if run["status"] == "COMPLETE" else "DATA_INCOMPLETE"
    if not strategy.backtest_file_prefix:
        return "NOT_APPLICABLE"
    due = datetime.combine(day, BACKTEST_DUE, tzinfo=NY)
    return "MISSING" if now >= due else "PENDING"


_PAPER_TABLES = ("paper_trade", "paper_fill", "signal_event", "ops_event", "account_snapshot")


def drop_stale_paper_rows(cur: Cursor, sid: int) -> int:
    """Keep only rows written from the current paper log of each session.

    Rows whose file was replaced under another name, or whose file record is gone, would
    otherwise be counted alongside the current load (the same trade twice).
    """
    dropped = 0
    for table in _PAPER_TABLES:
        cur.execute(
            f"DELETE FROM core.{table} x WHERE x.strategy_id = %s AND ("
            "x.file_id IS NULL OR x.file_id NOT IN ("
            "SELECT DISTINCT ON (f.session_date) f.file_id FROM raw.source_file f "
            "WHERE f.strategy_id = %s AND f.source_kind = 'paper_log' AND f.is_current "
            "ORDER BY f.session_date, f.loaded_at DESC, f.file_id DESC))",
            (sid, sid),
        )
        dropped += cur.rowcount
    return dropped


def _recheck_paper_days(cur: Cursor, sid: int, paper_rows: list[Row]) -> None:
    """Re-run the DAILY SUMMARY reconciliation against the trades now stored."""
    cur.execute(
        "SELECT session_date, paper_status, checks->'paper' AS paper FROM ops.day_status "
        "WHERE strategy_id = %s AND checks ? 'paper'",
        (sid,),
    )
    for status in cur.fetchall():
        stored = status["paper"] or {}
        day = status["session_date"]
        todays = [r for r in paper_rows if r["session_date"] == day]
        net = sum((dec(r["net_pnl"]) or Decimal(0) for r in todays), Decimal(0))
        gross = (
            None
            if any(r["gross_pnl"] is None for r in todays)
            else sum((dec(r["gross_pnl"]) or Decimal(0) for r in todays), Decimal(0))
        )
        reported = stored.get("net_reported")
        checks = {
            **stored,
            "trades_parsed": len(todays),
            **reconcile_summary(
                len(todays),
                net,
                gross,
                stored.get("closed_reported"),
                None if reported is None else Decimal(str(reported)),
            ),
        }
        consistent = checks.get("closed_match", True) is not False and (
            checks.get("net_match", True) is not False
        )
        paper_status = status["paper_status"]
        if paper_status == "PARTIAL" and consistent:
            paper_status = "FINAL" if checks.get("summary_present") else "PRELIMINARY"
        elif paper_status in ("FINAL", "PRELIMINARY", "INTRADAY") and not consistent:
            paper_status = "PARTIAL"
        cur.execute(
            "UPDATE ops.day_status SET paper_status = %s, "
            "checks = checks || jsonb_build_object('paper', %s::jsonb) "
            "WHERE strategy_id = %s AND session_date = %s",
            (paper_status, Jsonb(checks), sid, day),
        )


def recompute(conn: Conn, ref: RefData, strategy: Strategy, now: datetime) -> None:
    sid = strategy.strategy_id
    with conn.cursor() as cur:
        drop_stale_paper_rows(cur, sid)
        cur.execute(
            "SELECT * FROM core.paper_trade WHERE strategy_id = %s AND is_closed ORDER BY entry_ts",
            (sid,),
        )
        paper_rows = cur.fetchall()
        _recheck_paper_days(cur, sid, paper_rows)
        cur.execute(
            "SELECT * FROM core.backtest_trade WHERE strategy_id = %s ORDER BY entry_ts", (sid,)
        )
        bt_rows = cur.fetchall()
        cur.execute("SELECT * FROM core.backtest_run WHERE strategy_id = %s", (sid,))
        runs = {r["session_date"]: r for r in cur.fetchall()}
        cur.execute("SELECT * FROM core.paper_session WHERE strategy_id = %s", (sid,))
        sessions = {r["session_date"]: r for r in cur.fetchall()}
        cur.execute(
            "SELECT session_date, min(event_ts) AS at FROM core.ops_event "
            "WHERE strategy_id = %s AND code = 'LOSS_CAP' GROUP BY session_date",
            (sid,),
        )
        loss_cap_at = {r["session_date"]: r["at"] for r in cur.fetchall()}
        cur.execute(
            "SELECT i.symbol FROM core.paper_session ps "
            "JOIN ref.instrument i USING (instrument_id) "
            "WHERE ps.strategy_id = %s ORDER BY ps.session_date DESC LIMIT 1",
            (sid,),
        )
        symbol_row = cur.fetchone()
        delete_for_strategy(cur, _KPI_TABLES, sid)

        paper_by_day: dict[date, list[TradeRef]] = {}
        for r in paper_rows:
            paper_by_day.setdefault(r["session_date"], []).append(_ref(r, "trade_id"))
        bt_by_day: dict[date, list[TradeRef]] = {}
        for r in bt_rows:
            bt_by_day.setdefault(r["session_date"], []).append(_ref(r, "bt_trade_id"))

        days = sorted(set(sessions) | set(runs) | set(paper_by_day))
        sync_points: list[kpis.SyncPoint] = []
        for day in days:
            settings = ref.settings_for(sid, day)
            session = sessions.get(day)
            paper = paper_by_day.get(day, [])
            matches = (
                match_trades(paper, bt_by_day.get(day, []), loss_cap_at.get(day))
                if day in runs
                else None
            )
            _write_matches(cur, sid, day, matches or [])
            row = daily_sync(
                paper,
                matches,
                loss_cap_hit=bool(session and session["loss_cap_hit"]),
                pct_threshold=dec(settings["sync_pct_threshold"]) or Decimal("0.15"),
                usd_floor=dec(settings["sync_usd_floor"]) or Decimal(0),
            )
            _write_sync(cur, sid, day, symbol_row["symbol"] if symbol_row else None, row)
            sync_points.append(
                kpis.SyncPoint(
                    day,
                    row.live_trades,
                    _f(row.live_net_pnl),
                    None if row.bt_net_pnl is None else float(row.bt_net_pnl),
                    None if row.pnl_delta_pct is None else float(row.pnl_delta_pct),
                    row.sync_status,
                )
            )
            bt_status = backtest_status(strategy, day, runs.get(day), now)
            final = bool(session and session["summary_present"]) and bt_status == "LOADED"
            cur.execute(
                """
                INSERT INTO ops.day_status (strategy_id, session_date, paper_status,
                                            backtest_status, sync_status, finalized_at)
                VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)
                ON CONFLICT (strategy_id, session_date) DO UPDATE SET
                    backtest_status = EXCLUDED.backtest_status,
                    sync_status = EXCLUDED.sync_status,
                    finalized_at = CASE WHEN %s
                        THEN COALESCE(ops.day_status.finalized_at, now()) END
                """,
                (
                    sid,
                    day,
                    "NONE" if session is None else "PRELIMINARY",
                    bt_status,
                    row.sync_status,
                    final,
                    final,
                ),
            )
            if day in runs:
                cur.execute(
                    "UPDATE ops.day_status SET checks = COALESCE(checks, '{}'::jsonb) || %s "
                    "WHERE strategy_id = %s AND session_date = %s",
                    (Jsonb({"sync_notes": row.notes}), sid, day),
                )
        for roll in kpis.sync_rolling(sync_points):
            upsert(
                cur,
                "kpi.sync_rolling",
                {
                    "strategy_id": sid,
                    "as_of_date": roll.as_of,
                    "days_logged": roll.days_logged,
                    "all_time_trades": roll.all_time_trades,
                    "all_time_live_pnl": _r(roll.all_time_live_pnl),
                    "all_time_bt_pnl": _r(roll.all_time_bt_pnl),
                    "avg_pnl_delta_pct": _r(roll.avg_pnl_delta_pct, 4),
                    "all_time_flags": roll.all_time_flags,
                    "flag_rate": _r(roll.flag_rate, 4),
                    "last7_live_pnl": _r(roll.last7_live_pnl),
                    "last7_flags": roll.last7_flags,
                },
            )
        _write_kpis(cur, ref, strategy, paper_rows, days, now)


def _write_matches(cur: Cursor, sid: int, day: date, matches: list[Match]) -> None:
    for m in matches:
        upsert(
            cur,
            "core.comparison_trade",
            {
                "strategy_id": sid,
                "session_date": day,
                "match_seq": m.seq,
                "match_type": m.match_type,
                "paper_trade_id": m.paper.key if m.paper else None,
                "bt_trade_id": m.backtest.key if m.backtest else None,
                "unmatched_reason": m.unmatched_reason,
                "entry_gap_s": m.entry_gap_s,
                "exit_gap_min": m.exit_gap_min,
                "points_gap": m.points_gap,
                "pnl_delta": m.pnl_delta,
                "pnl_delta_pct": m.pnl_delta_pct,
                "exit_reason_match": m.exit_reason_match,
            },
        )


def _write_sync(cur: Cursor, sid: int, day: date, symbol: str | None, row: SyncRow) -> None:
    values = {k: v for k, v in vars(row).items() if k != "notes"}
    upsert(
        cur,
        "core.daily_sync",
        {
            "strategy_id": sid,
            "session_date": day,
            "instrument_symbol": symbol,
            **values,
            "notes_auto": "; ".join(row.notes) or None,
            "computed_at": datetime.now(NY),
        },
        ("strategy_id", "session_date"),
    )


def _write_kpis(
    cur: Cursor,
    ref: RefData,
    strategy: Strategy,
    paper_rows: list[Row],
    days: list[date],
    now: datetime,
) -> None:
    sid = strategy.strategy_id
    trade_days = [r["session_date"] for r in paper_rows]
    start = strategy.paper_start_date or (min(days) if days else None)
    if start is None or not (trade_days or days):
        return
    end = min(max([*trade_days, *days]), now.date())
    if end < start:
        return
    s = ref.settings_for(sid, end)
    settings = kpis.Settings(
        starting_capital=float(s["starting_capital"]),
        session_minutes=int(s["session_minutes"]),
        ann_days=int(s["ann_days"]),
        min_days_ratio=int(s["min_days_ratio"]),
        min_days_calmar=int(s["min_days_calmar"]),
        risk_free_rate=float(s["risk_free_rate"]),
        model_slip_rt_per_contract=float(s["model_slip_rt_per_contract"]),
    )
    trades = [
        kpis.ClosedTrade(
            r["session_date"],
            _f(r["net_pnl"]),
            _f(r["gross_pnl"]),
            _f(r["commission"]),
            _f(r["slippage_cost"]),
            _f(r["pnl_at_signal"], _f(r["gross_pnl"])),
            _f(r["hold_min"]),
            _f(r["contracts"]),
        )
        for r in paper_rows
        if start <= r["session_date"] <= end
    ]
    rows = kpis.daily_calc(trades, settings, start, end, ref.holidays)
    copy_rows(
        cur,
        "core.daily_pnl",
        _DAILY_PNL_COLS,
        (
            (
                sid,
                d.session_date,
                d.week_end,
                d.month_start,
                d.trades,
                _r(d.gross_pnl),
                _r(d.commission),
                _r(d.slippage_cost),
                _r(d.net_pnl),
                _r(d.hold_min_total),
                _r(d.equity_start),
                _r(d.equity_end),
                _r(d.daily_return, 10),
                _r(d.peak_itd),
                _r(d.dd_itd, 10),
                d.uw_days_itd,
                _r(d.peak_week),
                _r(d.dd_week, 10),
                d.uw_days_week,
                _r(d.peak_month),
                _r(d.dd_month, 10),
                d.uw_days_month,
            )
            for d in rows
        ),
    )

    kpi_rows: list[tuple[Any, ...]] = []
    for day_row in rows:
        as_of = day_row.session_date
        for window, (lo, hi) in kpis.window_bounds(as_of, start).items():
            result = kpis.window_kpis(window, rows, trades, settings, max(lo, start), hi)
            scored = kpis.score_window(result, ref.kpi_defs)
            for code, value in result.values.items():
                if code not in ref.kpi_defs:
                    continue
                num = float(value) if isinstance(value, int | float) else None
                text = value if isinstance(value, str) else None
                status = scored.statuses[code]
                kpi_rows.append(
                    (sid, as_of, window, code, num, text, status, result.start, result.end)
                )
            upsert(
                cur,
                "kpi.scorecard",
                {
                    "strategy_id": sid,
                    "as_of_date": as_of,
                    "kpi_window": window,
                    "window_start": result.start,
                    "window_end": result.end,
                    "trading_days": result.trading_days,
                    "trades": result.trades,
                    "net_pnl": _r(result.net_pnl),
                    "return_pct": _r(result.return_pct, 10),
                    "equity_end": _r(result.equity_end),
                    "red_count": scored.red,
                    "amber_count": scored.amber,
                    "green_count": scored.green,
                    "overall_gate": scored.gate,
                },
            )
    copy_rows(cur, "kpi.kpi_value", _KPI_COLS, kpi_rows)

    for p in kpis.period_returns(rows, trades, settings, "week"):
        upsert(
            cur,
            "kpi.returns_weekly",
            {
                "strategy_id": sid,
                "week_end": p.key,
                "week_no": p.number,
                "week_start": p.period_start,
                "trading_days": p.trading_days,
                "trades": p.trades,
                "net_pnl": _r(p.net_pnl),
                "start_equity": _r(p.start_equity),
                "return_pct": _r(p.return_pct, 10),
                "max_dd": _r(p.max_dd, 10),
                "win_rate": _r(p.win_rate, 4),
                "cum_pnl": _r(p.cum_pnl),
                "account_value": _r(p.account_value),
                "cum_return": _r(p.cum_return, 10),
                "peak_account": _r(p.peak_account),
                "dd_from_peak": _r(p.dd_from_peak, 10),
            },
        )
    for p in kpis.period_returns(rows, trades, settings, "month"):
        upsert(
            cur,
            "kpi.returns_monthly",
            {
                "strategy_id": sid,
                "month_start": p.key,
                "month_no": p.number,
                "trading_days": p.trading_days,
                "trades": p.trades,
                "net_pnl": _r(p.net_pnl),
                "start_equity": _r(p.start_equity),
                "return_pct": _r(p.return_pct, 10),
                "max_dd": _r(p.max_dd, 10),
                "sharpe": _r(p.sharpe, 6),
                "win_rate": _r(p.win_rate, 4),
                "profit_factor": _r(p.profit_factor, 6),
                "cum_pnl": _r(p.cum_pnl),
                "account_value": _r(p.account_value),
                "cum_return": _r(p.cum_return, 10),
                "dd_from_peak": _r(p.dd_from_peak, 10),
            },
        )
