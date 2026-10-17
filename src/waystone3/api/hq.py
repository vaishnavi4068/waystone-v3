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
from pydantic import BaseModel, Field

from waystone3.hq.reader import HqReader
from waystone3.hq.strategy_runner import HqStrategyRunner


class StrategyConfigureBody(BaseModel):
    account_id: str | None = None
    client_id: int | None = None
    ibkr_host: str | None = None
    ibkr_port: int | None = None
    contracts: int | None = None
    dry_run: bool | None = None
    hold_overnight: bool | None = None
    overrides: dict[str, Any] | None = None


class StrategyStartBody(BaseModel):
    account_id: str | None = None
    contracts: int | None = None
    dry_run: bool | None = None
    hold_overnight: bool | None = None
    spawn: bool = True


class StrategyHaltBody(BaseModel):
    reason: str = "halted via API"
    stop_process: bool = False


class StrategyEvaluateBody(BaseModel):
    bars: list[dict[str, Any]] = Field(default_factory=list)
    fng_prior_day: float | None = None
    use_saved_state: bool = False


class IbkrExecuteBody(BaseModel):
    action: str = Field(min_length=1)
    qty: int | None = None
    dry_run: bool | None = None
    reason: str = "grok_bot"


def _day(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD") from exc


def build_hq_router(
    reader: HqReader | None,
    session: Callable[..., Any],
    runner: HqStrategyRunner | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/hq", dependencies=[Depends(session)])
    strategy_runner = runner if runner is not None else HqStrategyRunner()

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

    @router.get("/sentiment")
    def hq_sentiment(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Per-session sentiment summaries and verdicts between two dates (newest first)."""
        lo, hi = _day(start, "start"), _day(end, "end")
        return {
            "dates": _call(_reader().sentiment_dates),
            "days": _call(lambda: _reader().sentiment_range(lo, hi)),
        }

    @router.get("/sentiment/now")
    def hq_sentiment_now() -> dict[str, Any]:
        """Newest interval: summary, ranked recommendations and gate states."""
        return _call(_reader().sentiment_latest)  # type: ignore[no-any-return]

    @router.get("/sentiment/gates")
    def hq_sentiment_gates(
        start: str | None = None,
        end: str | None = None,
        strategy: str | None = None,
        gate: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        lo, hi = _day(start, "start"), _day(end, "end")
        return {"gates": _call(lambda: _reader().sentiment_gates(lo, hi, strategy, gate, state))}

    @router.get("/sentiment/day")
    def hq_sentiment_day(date: str | None = None, strategy: str | None = None) -> dict[str, Any]:
        """One session: intervals, recommendations, gate audit rows, every score, headlines."""
        if strategy:
            _strategy(strategy)
        dates = _call(_reader().sentiment_dates)
        day = _day(date, "date") or (_day(dates[-1], "date") if dates else None)
        if day is None:
            return {"dates": dates, "session_date": None}
        picked = day
        return {
            "dates": dates,
            **_call(lambda: _reader().sentiment_day(picked, strategy)),
            "upcoming": _call(lambda: _reader().sentiment_upcoming_events(picked)),
        }

    @router.get("/sentiment/series")
    def hq_sentiment_series(
        layer: str, component: str, start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        lo, hi = _day(start, "start"), _day(end, "end")
        return {
            "layer": layer,
            "component": component,
            "points": _call(lambda: _reader().sentiment_series(layer, component, lo, hi)),
        }

    @router.get("/sentiment/quality")
    def hq_sentiment_quality() -> dict[str, Any]:
        """Gate efficacy vs P&L, drift (PSI), source health and active overrides."""
        return {
            "efficacy": _call(_reader().sentiment_efficacy),
            **_call(_reader().sentiment_health),
        }

    @router.get("/status")
    def hq_status(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        lo, hi = _day(start, "start"), _day(end, "end")
        return {
            "loads": _call(_reader().load_health),
            "days": _call(lambda: _reader().day_status(None, lo, hi)),
        }

    # ── HQ Strategy Runtime & Agentic IBKR Execution (immutable thresholds) ──
    @router.get("/runtime")
    def hq_runtime_all() -> dict[str, Any]:
        return strategy_runner.status()

    @router.get("/runtime/thresholds")
    def hq_runtime_thresholds(strategy: str | None = None) -> dict[str, Any]:
        try:
            if strategy:
                spec = strategy_runner.spec(strategy)
                return {
                    "strategy_code": spec.code,
                    "thresholds_locked": True,
                    "thresholds": spec.thresholds.to_dict(),
                }
            return {
                "thresholds_locked": True,
                "strategies": {
                    code: strategy_runner.spec(code).thresholds.to_dict()
                    for code in ("nq_v221", "es_v221", "r2_mnq")
                },
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/runtime/{code}")
    def hq_runtime_one(code: str) -> dict[str, Any]:
        try:
            return strategy_runner.status(code)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/runtime/{code}/configure")
    def hq_runtime_configure(code: str, body: StrategyConfigureBody) -> dict[str, Any]:
        try:
            return strategy_runner.configure_account(
                code,
                account_id=body.account_id,
                client_id=body.client_id,
                ibkr_host=body.ibkr_host,
                ibkr_port=body.ibkr_port,
                contracts=body.contracts,
                dry_run=body.dry_run,
                hold_overnight=body.hold_overnight,
                actor="api",
                extra_overrides=body.overrides,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runtime/{code}/start")
    def hq_runtime_start(code: str, body: StrategyStartBody) -> dict[str, Any]:
        try:
            return strategy_runner.start_strategy(
                code,
                account_id=body.account_id,
                contracts=body.contracts,
                dry_run=body.dry_run,
                hold_overnight=body.hold_overnight,
                spawn=body.spawn,
                actor="api",
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runtime/{code}/halt")
    def hq_runtime_halt(code: str, body: StrategyHaltBody) -> dict[str, Any]:
        try:
            return strategy_runner.halt_strategy(
                code, reason=body.reason, stop_process=body.stop_process
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runtime/{code}/resume")
    def hq_runtime_resume(code: str) -> dict[str, Any]:
        try:
            return strategy_runner.resume_strategy(code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runtime/{code}/evaluate")
    def hq_runtime_evaluate(code: str, body: StrategyEvaluateBody) -> dict[str, Any]:
        try:
            return strategy_runner.evaluate_bars(
                code,
                body.bars,
                fng_prior_day=body.fng_prior_day,
                use_saved_state=body.use_saved_state,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/runtime/{code}/ibkr-account")
    def hq_runtime_ibkr_account(code: str) -> dict[str, Any]:
        try:
            return strategy_runner.ibkr_account_state(code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runtime/{code}/ibkr-execute")
    def hq_runtime_ibkr_execute(code: str, body: IbkrExecuteBody) -> dict[str, Any]:
        try:
            return strategy_runner.ibkr_execute_signal(
                code,
                body.action,
                qty=body.qty,
                dry_run=body.dry_run,
                reason=body.reason,
            )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
