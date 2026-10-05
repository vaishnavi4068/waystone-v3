"""MCP tools over the HQ database views (futures paper vs backtest, P&L, workbook KPIs)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from waystone3.hq.reader import HqReader


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc


def register_hq_tools(mcp: FastMCP, reader: HqReader, authorize: Callable[[], None]) -> None:
    """Add ``hq_*`` tools. ``authorize`` raises when the caller's token is not valid."""

    def known(strategy: str) -> str:
        codes = {s["strategy_code"] for s in reader.strategies()}
        if strategy not in codes:
            raise ValueError(f"unknown strategy {strategy!r}; one of {', '.join(sorted(codes))}")
        return strategy

    @mcp.tool()
    def hq_strategies() -> list[dict[str, Any]]:
        """Futures strategies (es_v221, nq_v221, r2_mnq, ...) with the latest ITD scorecard
        (gate, net P&L, return, equity, red/amber/green KPI counts) and latest day status."""
        authorize()
        return reader.strategies()

    @mcp.tool()
    def hq_kpis(strategy: str, as_of: str | None = None) -> dict[str, Any]:
        """Workbook KPI scorecard for one strategy on an as-of date (YYYY-MM-DD, default
        latest): WEEK/MTD/ITD gates plus every KPI value, status and green/amber band."""
        authorize()
        return reader.kpis(known(strategy), _day(as_of, "as_of"))

    @mcp.tool()
    def hq_sync(date: str | None = None) -> dict[str, Any]:
        """Live paper vs backtest replay for every strategy on one session (default latest):
        trades, net P&L, delta $ and %, exit-reason match, loss cap, OK/FLAG/N/A status."""
        authorize()
        dates = reader.sync_dates()
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        rows = reader.daily_sync(None, day, day) if day else []
        return {"session_date": day.isoformat() if day else None, "dates": dates, "rows": rows}

    @mcp.tool()
    def hq_paper_day(strategy: str | None = None, date: str | None = None) -> dict[str, Any]:
        """Paper engine activity for one session (default latest), one or all strategies:
        trades (open and closed), IB fills, signals entered/blocked, engine events, and data
        freshness (last VM sync, last log line, when the loader read it)."""
        authorize()
        code = known(strategy) if strategy else None
        dates = reader.paper_dates(code)
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        if day is None:
            return {"strategy_code": code, "session_date": None, "dates": dates}
        return {"dates": dates, **reader.paper_day(code, day)}

    @mcp.tool()
    def hq_compare(strategy: str, date: str | None = None) -> dict[str, Any]:
        """One strategy's session in detail (default latest): daily sync row, paper trades
        with fills/slippage/MAE/MFE, backtest trades and the trade-by-trade matching."""
        authorize()
        code = known(strategy)
        day = _day(date, "date")
        if day is None:
            rows = reader.daily_sync(code)
            if not rows:
                return {"strategy_code": code, "session_date": None}
            day = _day(rows[0]["session_date"], "date")
        assert day is not None
        return reader.comparison(code, day)

    @mcp.tool()
    def hq_daily_pnl(
        strategy: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Daily P&L for one strategy: trades, gross, commission, slippage, net, equity,
        daily return and ITD drawdown, oldest first. Dates are YYYY-MM-DD."""
        authorize()
        return reader.daily_pnl(known(strategy), _day(start, "start"), _day(end, "end"))

    @mcp.tool()
    def hq_trades(
        strategy: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, Any]]:
        """Paper trades for one strategy between two sessions (YYYY-MM-DD, inclusive)."""
        authorize()
        return reader.paper_trades(known(strategy), _day(start, "start"), _day(end, "end"))

    @mcp.tool()
    def hq_returns(strategy: str) -> dict[str, Any]:
        """Weekly and monthly returns plus all-time live-vs-backtest sync statistics."""
        authorize()
        return reader.returns(known(strategy))

    @mcp.tool()
    def hq_load_status() -> dict[str, Any]:
        """Last log-loader run per job (paper/backtest/backfill) and recent day statuses
        (paper FINAL/PRELIMINARY/INTRADAY, backtest LOADED/MISSING/PENDING)."""
        authorize()
        return {"loads": reader.load_health(), "days": reader.day_status()[:30]}
