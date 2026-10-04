"""``/api/hq/*``: futures strategy KPIs, comparison and P&L from the HQ database.

Reads only the ``api.*`` views as ``waystone_read``. When no HQ DSN is configured
every route returns 404 so the rest of the dashboard keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from waystone3.hq.reader import HqReader


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD") from exc


def build_hq_router(reader: HqReader | None, session: Callable[..., Any]) -> APIRouter:
    router = APIRouter(prefix="/api/hq", dependencies=[Depends(session)])

    def _reader() -> HqReader:
        if reader is None:
            raise HTTPException(status_code=404, detail="HQ database not configured")
        return reader

    def _call(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except psycopg.OperationalError as exc:
            raise HTTPException(status_code=503, detail="HQ database unavailable") from exc

    def _strategy(code: str) -> dict[str, Any]:
        row = _call(lambda: _reader().strategy(code))
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown strategy {code}")
        return row  # type: ignore[no-any-return]

    @router.get("/strategies")
    def hq_strategies() -> dict[str, Any]:
        return {"strategies": _call(_reader().strategies)}

    @router.get("/strategies/{code}")
    def hq_strategy(code: str) -> dict[str, Any]:
        row = _strategy(code)
        return {**row, "kpi_dates": _call(lambda: _reader().kpi_dates(code))}

    @router.get("/strategies/{code}/kpis")
    def hq_kpis(code: str, as_of: str | None = None) -> dict[str, Any]:
        _strategy(code)
        day = _day(as_of, "as_of")
        return _call(lambda: _reader().kpis(code, day))  # type: ignore[no-any-return]

    @router.get("/strategies/{code}/daily")
    def hq_daily(code: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        _strategy(code)
        lo, hi = _day(start, "start"), _day(end, "end")
        return {
            "strategy_code": code,
            "days": _call(lambda: _reader().daily_pnl(code, lo, hi)),
            "sync": _call(lambda: _reader().daily_sync(code, lo, hi)),
        }

    @router.get("/strategies/{code}/trades")
    def hq_trades(code: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        _strategy(code)
        lo, hi = _day(start, "start"), _day(end, "end")
        return {
            "strategy_code": code,
            "trades": _call(lambda: _reader().paper_trades(code, lo, hi)),
        }

    @router.get("/strategies/{code}/compare")
    def hq_compare(code: str, date: str | None = None) -> dict[str, Any]:
        _strategy(code)
        day = _day(date, "date")
        if day is None:
            rows = _call(lambda: _reader().daily_sync(code))
            if not rows:
                raise HTTPException(status_code=404, detail=f"no comparison days for {code}")
            day = datetime.strptime(rows[0]["session_date"], "%Y-%m-%d").date()
        picked = day
        return _call(lambda: _reader().comparison(code, picked))  # type: ignore[no-any-return]

    @router.get("/strategies/{code}/returns")
    def hq_returns(code: str) -> dict[str, Any]:
        _strategy(code)
        return _call(lambda: _reader().returns(code))  # type: ignore[no-any-return]

    @router.get("/sync")
    def hq_sync(date: str | None = None) -> dict[str, Any]:
        """Every strategy's live-vs-backtest row for one session (latest by default)."""
        dates = _call(_reader().sync_dates)
        day = _day(date, "date") or (
            datetime.strptime(dates[-1], "%Y-%m-%d").date() if dates else None
        )
        rows = _call(lambda: _reader().daily_sync(None, day, day)) if day else []
        return {"dates": dates, "session_date": day.isoformat() if day else None, "rows": rows}

    @router.get("/paper")
    def hq_paper(date: str | None = None, strategy: str | None = None) -> dict[str, Any]:
        """Paper engine activity for one session (latest by default), one or all strategies."""
        if strategy:
            _strategy(strategy)
        dates = _call(lambda: _reader().paper_dates(strategy))
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        if day is None:
            return {"dates": dates, "session_date": None, "strategy_code": strategy}
        picked = day
        return {"dates": dates, **_call(lambda: _reader().paper_day(strategy, picked))}

    @router.get("/status")
    def hq_status(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        lo, hi = _day(start, "start"), _day(end, "end")
        return {
            "loads": _call(_reader().load_health),
            "days": _call(lambda: _reader().day_status(None, lo, hi)),
        }

    return router
