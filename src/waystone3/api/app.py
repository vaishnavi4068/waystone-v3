"""Read-only API backing the dashboard — shared-account model.

Every team member signs in with their unique password (`POST /api/login`) and then
authenticates with the issued bearer token. When ``IBKR_REPORTS_BUCKET`` (or
``IBKR_REPORTS_LOCAL_DIR``) is set, account / positions / orders and the IBKR daily
report are served from published dumps. Otherwise the broker (Alpaca in prod,
PaperBroker in tests) is read live. Algo onboarding is POST/PUT/DELETE on ``/api/algos``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from waystone3.core.types import Timeframe
from waystone3.fusion.fuse import fuse
from waystone3.ibkr.algo_registry import AlgoConfig, ensure_registry, save_registry
from waystone3.ibkr.compare import compare_algo_day, list_compare_days
from waystone3.ibkr.futures_kpis import compute_futures_kpis
from waystone3.ibkr.kpis import compute_options_kpis
from waystone3.ibkr.models import AccountSnapshot, Book
from waystone3.ibkr.reader import load_latest, load_report
from waystone3.ibkr.settings import IbkrSettings
from waystone3.ibkr.store import ReportStore, build_report_store_from_env
from waystone3.ibkr.views import account_dict, days_dict, order_dict, position_dict, report_dict
from waystone3.runner.backtest import run_backtest
from waystone3.runner.config import default_contributors, default_weights
from waystone3.runner.cycle import score_all
from waystone3.signals.registry import build_contributor
from waystone3.workspace.runtime import build_workspace_from_env
from waystone3.workspace.workspace import TradingWorkspace


def _symbols(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _weights(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in raw.split(","):
        if ":" in part:
            name, value = part.split(":", 1)
            out[name.strip()] = float(value)
    return out


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _watchlist(ws: TradingWorkspace, raw: str) -> list[str]:
    if raw.strip():
        return _symbols(raw)
    return list(ws.strategy.watchlist) if ws.strategy else []


def _strategy_payload(ws: TradingWorkspace) -> dict[str, Any] | None:
    cfg = ws.strategy
    if cfg is None:
        return None
    return {
        "weights": cfg.weights,
        "watchlist": cfg.watchlist,
        "bullish_threshold": float(cfg.bullish_threshold),
        "bearish_threshold": float(cfg.bearish_threshold),
        "notional_per_trade": float(cfg.notional_per_trade),
    }


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AlgoBody(BaseModel):
    id: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1)
    book: Book = Book.OTHER
    live_prefix: str = ""
    replay_prefix: str = ""
    client_id: int | None = None
    enabled: bool = True
    notes: str = ""


def build_app(
    workspace_factory: Callable[[], TradingWorkspace] | None = None,
    report_store: ReportStore | None = None,
    *,
    ibkr_paper: bool | None = None,
) -> FastAPI:
    # Default: one workspace for the process. Building Alpaca/Polygon clients on every
    # request (the old per-call factory) made the dashboard feel hung under 15s polling.
    # Tests inject a single shared instance (the in-process PaperBroker isn't cross-process).
    if workspace_factory is None:
        _workspace = build_workspace_from_env()

        def factory() -> TradingWorkspace:
            return _workspace
    else:
        factory = workspace_factory
    store = report_store if report_store is not None else build_report_store_from_env()
    paper = IbkrSettings().ibkr_paper if ibkr_paper is None else ibkr_paper

    app = FastAPI(title="Waystone v3 — read-only dashboard API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    async def _session(
        authorization: str = Header(default=""),
    ) -> tuple[TradingWorkspace, str]:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        ws = factory()
        name = ws.authenticate(authorization[7:])
        if name is None:
            raise HTTPException(status_code=401, detail="invalid token")
        return ws, name

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: LoginBody) -> dict[str, str]:
        ws = factory()
        token = ws.authenticate_password(body.username, body.password)
        if token is None:
            raise HTTPException(status_code=401, detail="invalid username or password")
        name = ws.authenticate(token)
        if name is None:
            raise HTTPException(status_code=401, detail="invalid username or password")
        return {"name": name, "token": token}

    @app.get("/api/account")
    async def account(ctx: tuple[TradingWorkspace, str] = Depends(_session)) -> dict[str, Any]:
        ws, name = ctx
        if store is not None:
            report = load_latest(store)
            acct = report.account if report else AccountSnapshot()
            extra = account_dict(acct) if report else {}
            days = days_dict(store)
            return {
                "you": name,
                "team": ws.members(),
                "broker": "ibkr",
                "is_paper": paper,
                "trading_enabled": ws.trading_enabled,
                "cash": acct.cash,
                "equity": acct.nlv,
                "buying_power": acct.buying_power,
                "nlv": extra.get("nlv", acct.nlv),
                "excess_liquidity": extra.get("excess_liquidity", 0.0),
                "maint_margin": extra.get("maint_margin", 0.0),
                "currency": extra.get("currency", "USD"),
                "report_date": report.date if report else None,
                "as_of": report.generated_at.isoformat() if report else None,
                "published": report is not None,
                "today_published": bool(days["today_published"]),
                "strategy": _strategy_payload(ws),
                "staged": bool(days.get("staged")),
                "staged_week": days.get("staged_week"),
            }
        broker_acct = await ws.broker.get_account()
        return {
            "you": name,
            "team": ws.members(),
            "broker": ws.broker.name,
            "is_paper": ws.broker.is_paper,
            "trading_enabled": ws.trading_enabled,
            "cash": float(broker_acct.cash),
            "equity": float(broker_acct.equity),
            "buying_power": float(broker_acct.buying_power),
            "strategy": _strategy_payload(ws),
        }

    @app.get("/api/positions")
    async def positions(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> list[dict[str, Any]]:
        ws, _ = ctx
        if store is not None:
            report = load_latest(store)
            return [position_dict(p) for p in (report.positions if report else [])]
        rows = await ws.broker.get_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "market_price": float(p.market_price) if p.market_price else None,
                "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else None,
            }
            for p in rows
        ]

    @app.get("/api/orders")
    async def orders(
        limit: int = 20, ctx: tuple[TradingWorkspace, str] = Depends(_session)
    ) -> list[dict[str, Any]]:
        ws, _ = ctx
        if store is not None:
            report = load_latest(store)
            fills = report.executions if report else []
            fills = sorted(fills, key=lambda e: e.time, reverse=True)[:limit]
            return [order_dict(e) for e in fills]
        rows = await ws.broker.list_orders(limit)
        return [
            {
                "symbol": o.symbol,
                "side": o.side.value,
                "qty": float(o.qty),
                "status": o.status.value,
                "avg_fill_price": float(o.avg_fill_price) if o.avg_fill_price else None,
                "submitted_at": o.submitted_at.isoformat(),
            }
            for o in rows
        ]

    @app.get("/api/ibkr/days")
    async def ibkr_days(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        if store is None:
            raise HTTPException(status_code=404, detail="IBKR reports not configured")
        return days_dict(store)

    @app.get("/api/ibkr/report")
    async def ibkr_report(
        date: str | None = None,
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        if store is None:
            raise HTTPException(status_code=404, detail="IBKR reports not configured")
        if date:
            try:
                day = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
            report = load_report(store, day)
            if report is None:
                raise HTTPException(status_code=404, detail=f"no published IBKR report for {date}")
            return report_dict(report, store)
        report = load_latest(store)
        if report is None:
            raise HTTPException(status_code=404, detail="no published IBKR report")
        return report_dict(report, store)

    @app.get("/api/ibkr/options-kpis")
    async def ibkr_options_kpis(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        if store is None:
            raise HTTPException(status_code=404, detail="IBKR reports not configured")
        return compute_options_kpis(store)

    @app.get("/api/ibkr/futures-kpis")
    async def ibkr_futures_kpis(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        if store is None:
            raise HTTPException(status_code=404, detail="IBKR reports not configured")
        return compute_futures_kpis(store)

    def _reports() -> ReportStore:
        if store is None:
            raise HTTPException(status_code=404, detail="IBKR reports not configured")
        return store

    @app.get("/api/algos")
    async def algo_list(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        registry = ensure_registry(_reports())
        return {"algos": [row.model_dump(mode="json") for row in registry.algos]}

    @app.post("/api/algos")
    async def algo_create(
        body: AlgoBody, ctx: tuple[TradingWorkspace, str] = Depends(_session)
    ) -> dict[str, Any]:
        del ctx
        reports = _reports()
        registry = ensure_registry(reports)
        if registry.get(body.id) is not None:
            raise HTTPException(status_code=409, detail=f"algo {body.id} already exists")
        try:
            algo = registry.upsert(AlgoConfig.model_validate(body.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_registry(reports, registry)
        return algo.model_dump(mode="json")

    @app.put("/api/algos/{algo_id}")
    async def algo_update(
        algo_id: str,
        body: AlgoBody,
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        if body.id != algo_id:
            raise HTTPException(status_code=400, detail="id in body must match path")
        reports = _reports()
        registry = ensure_registry(reports)
        try:
            algo = registry.upsert(AlgoConfig.model_validate(body.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_registry(reports, registry)
        return algo.model_dump(mode="json")

    @app.delete("/api/algos/{algo_id}")
    async def algo_delete(
        algo_id: str, ctx: tuple[TradingWorkspace, str] = Depends(_session)
    ) -> dict[str, bool]:
        del ctx
        reports = _reports()
        registry = ensure_registry(reports)
        if not registry.remove(algo_id):
            raise HTTPException(status_code=404, detail=f"unknown algo {algo_id}")
        save_registry(reports, registry)
        return {"ok": True}

    @app.get("/api/algos/compare-days")
    async def algo_compare_days(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        reports = _reports()
        registry = ensure_registry(reports)
        days = list_compare_days(reports, registry)
        from waystone3.ibkr.staged import is_staged_day, staged_meta

        latest = days[-1] if days else None
        return {
            "days": days,
            "latest": latest,
            **staged_meta(latest, any_staged=any(is_staged_day(d) for d in days)),
        }

    @app.get("/api/algos/{algo_id}/compare")
    async def algo_compare(
        algo_id: str,
        date: str | None = None,
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        del ctx
        reports = _reports()
        registry = ensure_registry(reports)
        algo = registry.get(algo_id)
        if algo is None:
            raise HTTPException(status_code=404, detail=f"unknown algo {algo_id}")
        days = list_compare_days(reports, registry)
        if date:
            try:
                day = datetime.strptime(date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
        elif days:
            day = datetime.strptime(days[-1], "%Y-%m-%d").date()
        else:
            raise HTTPException(status_code=404, detail="no compare days published")
        return compare_algo_day(reports, algo, day)

    @app.get("/api/activity")
    async def activity(
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> list[dict[str, Any]]:
        ws, _ = ctx
        return [
            {"seq": e.seq, "actor": e.actor, "action": e.action, "detail": e.detail}
            for e in ws.audit[-50:][::-1]
        ]

    @app.get("/api/signals")
    async def signals(
        symbols: str = "",
        timeframe: Timeframe = Timeframe.D1,
        lookback: int = 120,
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> list[dict[str, Any]]:
        ws, _ = ctx
        syms = _watchlist(ws, symbols)
        if not syms:
            return []
        bars = {s: await ws.data.get_bars(s, timeframe, lookback) for s in syms}
        composites = fuse(score_all(default_contributors(), bars), default_weights())
        return [
            {
                "symbol": c.symbol,
                "score": float(c.score),
                "per_contributor": {k: float(v) for k, v in c.per_contributor.items()},
                "drivers": c.drivers[:6],
            }
            for c in composites
        ]

    @app.get("/api/bars")
    async def bars(
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        lookback: int = 200,
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> list[dict[str, Any]]:
        ws, _ = ctx
        rows = await ws.data.get_bars(symbol.upper(), timeframe, lookback)
        return [
            {
                "time": int(b.timestamp.timestamp()),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume),
            }
            for b in rows
        ]

    @app.get("/api/backtest")
    async def backtest(
        symbols: str,
        start: str,
        end: str,
        weights: str = "ma_crossover:0.5,price_action:0.5",
        timeframe: Timeframe = Timeframe.D1,
        lookback: int = 80,
        ctx: tuple[TradingWorkspace, str] = Depends(_session),
    ) -> dict[str, Any]:
        ws, _ = ctx
        w = _weights(weights)
        contributors = [build_contributor(n) for n, v in w.items() if v > 0]
        result = await run_backtest(
            data=ws.data,
            symbols=_symbols(symbols),
            timeframe=timeframe,
            start=_dt(start),
            end=_dt(end),
            lookback=lookback,
            contributors=contributors,
            weights=w,
        )
        m = result.metrics
        return {
            "metrics": {
                "total_return_pct": float(m.total_return * 100),
                "max_drawdown_pct": float(m.max_drawdown * 100),
                "win_rate_pct": float(m.win_rate * 100),
                "trades": m.num_trades,
            },
            "equity": [float(x) for x in result.equity_curve],
        }

    @app.get("/api/news")
    async def news(
        symbols: str = "", ctx: tuple[TradingWorkspace, str] = Depends(_session)
    ) -> list[dict[str, Any]]:
        ws, _ = ctx
        syms = _watchlist(ws, symbols)
        if not syms:
            return []
        try:
            from waystone3.news.polygon_news import PolygonNewsSource

            articles = await PolygonNewsSource().fetch(syms)
        except Exception:
            return []
        return [
            {
                "title": a.title,
                "source": a.source,
                "url": a.url,
                "symbols": list(a.symbols),
                "published_at": a.published_at.isoformat(),
            }
            for a in articles
        ]

    return app


def run(host: str = "0.0.0.0", port: int = 9200) -> None:
    import uvicorn

    uvicorn.run(build_app(), host=host, port=port, log_level="warning")
