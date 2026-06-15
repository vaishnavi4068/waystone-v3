"""Read-only, per-user FastAPI backing the dashboard.

Each player authenticates with **their own bearer token** (the same one they use for the MCP
connector). `/api/me` is scoped to that player; `/api/standings` is the public ranking;
signals/charts/backtest/news are shared analytics. Every endpoint is GET and side-effect-free.
It reads the live competition from the same SQLite DB the Arena writes (`WAYSTONE_DB`) and
computes on demand from the configured data source (Polygon if keyed, else the stub).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from waystone3.competition.arena import build_competition_from_env
from waystone3.competition.competition import Competition
from waystone3.competition.models import Entry
from waystone3.core.types import Timeframe
from waystone3.fusion.fuse import fuse
from waystone3.runner.backtest import run_backtest
from waystone3.runner.config import default_contributors, default_weights
from waystone3.runner.cycle import score_all
from waystone3.signals.registry import build_contributor


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


async def _session(authorization: str = Header(default="")) -> tuple[Competition, Entry]:
    """Resolve the caller's bearer token to their competition entry (401 otherwise)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    comp = build_competition_from_env()
    entry = comp.authenticate(authorization[7:])
    if entry is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return comp, entry


def _watchlist(comp: Competition, raw: str) -> list[str]:
    return _symbols(raw) or sorted(
        {s for e in comp.all_entries() if e.config for s in e.config.watchlist}
    )


def build_app() -> FastAPI:
    app = FastAPI(title="Waystone v3 — read-only dashboard API")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
    )

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/me")
    async def me(ctx: tuple[Competition, Entry] = Depends(_session)) -> dict[str, Any]:
        comp, entry = ctx
        await comp.refresh_marks()
        acct = await entry.broker.get_account()
        positions = await entry.broker.get_positions()
        ret = (acct.equity - comp.initial_cash) / comp.initial_cash
        standings = await comp.standings()
        rank = next((s.rank for s in standings if s.display_name == entry.display_name), None)
        cfg = entry.config
        return {
            "player": entry.display_name,
            "rank": rank,
            "cycles_run": entry.cycles_run,
            "account": {
                "cash": float(acct.cash),
                "equity": float(acct.equity),
                "return_pct": float(ret * 100),
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "market_price": float(p.market_price) if p.market_price else None,
                    "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else None,
                }
                for p in positions
            ],
            "strategy": None
            if cfg is None
            else {
                "weights": cfg.weights,
                "watchlist": cfg.watchlist,
                "bullish_threshold": float(cfg.bullish_threshold),
                "bearish_threshold": float(cfg.bearish_threshold),
                "notional_per_trade": float(cfg.notional_per_trade),
            },
        }

    @app.get("/api/standings")
    async def standings(
        ctx: tuple[Competition, Entry] = Depends(_session),
    ) -> list[dict[str, Any]]:
        comp, _ = ctx
        rows = await comp.standings()
        return [
            {
                "rank": s.rank,
                "player": s.display_name,
                "equity": float(s.equity),
                "return_pct": float(s.return_pct * 100),
                "cycles_run": s.cycles_run,
            }
            for s in rows
        ]

    @app.get("/api/signals")
    async def signals(
        symbols: str = "",
        timeframe: Timeframe = Timeframe.D1,
        lookback: int = 120,
        ctx: tuple[Competition, Entry] = Depends(_session),
    ) -> list[dict[str, Any]]:
        comp, _ = ctx
        syms = _watchlist(comp, symbols)
        if not syms:
            return []
        bars = {s: await comp.data.get_bars(s, timeframe, lookback) for s in syms}
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
        ctx: tuple[Competition, Entry] = Depends(_session),
    ) -> list[dict[str, Any]]:
        comp, _ = ctx
        rows = await comp.data.get_bars(symbol.upper(), timeframe, lookback)
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
        ctx: tuple[Competition, Entry] = Depends(_session),
    ) -> dict[str, Any]:
        comp, _ = ctx
        w = _weights(weights)
        contributors = [build_contributor(n) for n, v in w.items() if v > 0]
        result = await run_backtest(
            data=comp.data,
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
        symbols: str = "", ctx: tuple[Competition, Entry] = Depends(_session)
    ) -> list[dict[str, Any]]:
        comp, _ = ctx
        syms = _watchlist(comp, symbols)
        if not syms:
            return []
        try:
            from waystone3.news.polygon_news import PolygonNewsSource

            articles = await PolygonNewsSource().fetch(syms)
        except Exception:  # no key / network — degrade to empty
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
