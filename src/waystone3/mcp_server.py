"""Hosted multi-user MCP server for the strategy competition.

Thin transport over ``CompetitionService`` (which holds the tested logic). Mirrors v2's
FastMCP + bearer-token pattern, extended to *per-user* tokens: an auth middleware reads
``Authorization: Bearer <token>`` and stashes it in a contextvar that each tool reads, so
the 5 players connect from their own Claude (Desktop/Code) and act as themselves. The
organizer holds an admin token (``WAYSTONE_ADMIN_TOKEN``) used only to register players.
"""

from __future__ import annotations

import contextvars
from typing import Any

from mcp.server.fastmcp import FastMCP

from waystone3.competition.service import AuthError, CompetitionService

# Per-request bearer token, set by the auth middleware and read inside tools.
_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("token", default=None)


def build_mcp(service: CompetitionService) -> FastMCP:
    mcp = FastMCP(
        "waystone-arena",
        instructions=(
            "Waystone Arena is a paper-trading strategy competition. Submit a strategy "
            "(contributor weights + watchlist + thresholds), run cycles or backtests, and "
            "check the leaderboard. Everything is paper money. Tools act as the "
            "authenticated player."
        ),
    )

    @mcp.tool()
    def submit_strategy(
        weights: dict[str, float],
        watchlist: list[str],
        bullish_threshold: float = 3.0,
        bearish_threshold: float = -3.0,
        notional_per_trade: float = 2000.0,
        lookback: int = 80,
    ) -> dict[str, Any]:
        """Submit/replace your strategy. weights keys: ma_crossover, price_action, volume,
        sentiment. Example: {"ma_crossover": 0.5, "price_action": 0.5}."""
        return service.submit_strategy(
            _token.get(),
            weights=weights,
            watchlist=watchlist,
            bullish_threshold=bullish_threshold,
            bearish_threshold=bearish_threshold,
            notional_per_trade=notional_per_trade,
            lookback=lookback,
        )

    @mcp.tool()
    async def run_cycle() -> dict[str, Any]:
        """Run one paper-trading cycle with your strategy (places paper orders on your account)."""
        return await service.run_cycle(_token.get())

    @mcp.tool()
    async def run_backtest(start: str, end: str) -> dict[str, Any]:
        """Backtest your strategy over a date range (ISO dates, e.g. 2023-01-01)."""
        return await service.run_backtest(_token.get(), start, end)

    @mcp.tool()
    async def my_account() -> dict[str, Any]:
        """Your paper account: cash, equity, positions, cycles run."""
        return await service.my_account(_token.get())

    @mcp.tool()
    async def standings() -> list[dict[str, Any]]:
        """The competition leaderboard, ranked by paper-account return."""
        return await service.standings(_token.get())

    @mcp.tool()
    def register_player(admin_token: str, display_name: str) -> dict[str, Any]:
        """Organizer-only: register a player and return their access token."""
        return service.register(admin_token, display_name)

    return mcp


def build_app(service: CompetitionService) -> Any:
    """ASGI app for remote hosting: bearer-token auth middleware + open /healthz."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    mcp = build_mcp(service)
    app = mcp.streamable_http_app()

    class _Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Any, call_next: Any) -> Any:
            if request.url.path == "/healthz":
                return PlainTextResponse("ok")
            header = request.headers.get("authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            # Admin token reaches register_player; player tokens authenticate via the service.
            if not token:
                return JSONResponse({"error": "missing bearer token"}, status_code=401)
            reset = _token.set(token)
            try:
                return await call_next(request)
            finally:
                _token.reset(reset)

    app.add_middleware(_Auth)
    return app


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 9100) -> None:
    """Serve the arena, configured entirely from env (POLYGON_API_KEY, WAYSTONE_DB,
    WAYSTONE_ADMIN_TOKEN). Players are read from the DB; seed them with `arena-seed`."""
    from waystone3.competition.arena import build_service_from_env

    service = build_service_from_env()
    if transport in ("http", "streamable-http"):
        import uvicorn

        uvicorn.run(build_app(service), host=host, port=port, log_level="warning")
        return
    build_mcp(service).run(transport="stdio")


__all__ = ["AuthError", "build_app", "build_mcp", "run"]


if __name__ == "__main__":
    run()
