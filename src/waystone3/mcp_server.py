"""Hosted multi-operator MCP server for the shared team trading workspace.

Thin transport over ``WorkspaceService`` (which holds the tested logic). The five team
members each connect from their own Claude with their own bearer token (per-operator audit),
but every tool acts on the **one shared account** — real Alpaca paper execution on live
Polygon data. An auth middleware reads ``Authorization: Bearer <token>`` into a contextvar
each tool reads; the organizer holds an admin token (``WAYSTONE_ADMIN_TOKEN``) to add members.
"""

from __future__ import annotations

import contextvars
from typing import Any

from mcp.server.fastmcp import FastMCP

from waystone3.workspace.service import AuthError, WorkspaceService

_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("token", default=None)


def build_mcp(service: WorkspaceService) -> FastMCP:
    mcp = FastMCP(
        "waystone-arena",
        instructions=(
            "Waystone is a shared team trading workspace: one Alpaca paper account the team "
            "operates together. Set the shared strategy, run cycles (which submit real orders "
            "to the shared account on live Polygon data), and inspect account/positions/orders. "
            "Tools act on the ONE shared account and are attributed to the calling member."
        ),
    )

    @mcp.tool()
    def set_strategy(
        weights: dict[str, float],
        watchlist: list[str],
        bullish_threshold: float = 3.0,
        bearish_threshold: float = -3.0,
        notional_per_trade: float = 2000.0,
        lookback: int = 80,
    ) -> dict[str, Any]:
        """Set the team's shared strategy. weights keys: ma_crossover, price_action, volume,
        sentiment. Example: {"ma_crossover": 0.6, "price_action": 0.4}."""
        return service.set_strategy(
            _token.get(),
            weights=weights,
            watchlist=watchlist,
            bullish_threshold=bullish_threshold,
            bearish_threshold=bearish_threshold,
            notional_per_trade=notional_per_trade,
            lookback=lookback,
        )

    @mcp.tool()
    def get_strategy() -> dict[str, Any]:
        """The current shared strategy."""
        return service.get_strategy(_token.get())

    @mcp.tool()
    async def run_cycle() -> dict[str, Any]:
        """Run one cycle: score live Polygon bars and SUBMIT real orders to the shared Alpaca
        account. Orders may be 'pending' until they fill (market hours)."""
        return await service.run_cycle(_token.get())

    @mcp.tool()
    async def account() -> dict[str, Any]:
        """Shared account: broker, paper flag, trading enabled, cash, equity, buying power."""
        return await service.account(_token.get())

    @mcp.tool()
    async def positions() -> list[dict[str, Any]]:
        """Open positions on the shared account (live from the broker)."""
        return await service.positions(_token.get())

    @mcp.tool()
    async def orders(limit: int = 20) -> list[dict[str, Any]]:
        """Recent orders on the shared account (live from the broker)."""
        return await service.orders(_token.get(), limit)

    @mcp.tool()
    async def backtest(start: str, end: str) -> dict[str, Any]:
        """Backtest the shared strategy over a date range (ISO dates) on Polygon history."""
        return await service.backtest(_token.get(), start, end)

    @mcp.tool()
    def halt(reason: str = "") -> dict[str, Any]:
        """Kill-switch: stop the workspace from placing any new orders."""
        return service.halt(_token.get(), reason)

    @mcp.tool()
    def resume() -> dict[str, Any]:
        """Re-enable trading after a halt."""
        return service.resume(_token.get())

    @mcp.tool()
    def activity(limit: int = 50) -> list[dict[str, Any]]:
        """Audit log: who set the strategy / ran cycles / halted, most recent first."""
        return service.audit(_token.get(), limit)

    @mcp.tool()
    def add_member(admin_token: str, name: str) -> dict[str, Any]:
        """Organizer-only: add a team member and return their access token."""
        return service.register(admin_token, name)

    return mcp


def build_app(service: WorkspaceService) -> Any:
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
    from waystone3.workspace.runtime import build_service_from_env

    service = build_service_from_env()
    if transport in ("http", "streamable-http"):
        import uvicorn

        uvicorn.run(build_app(service), host=host, port=port, log_level="warning")
        return
    build_mcp(service).run(transport="stdio")


__all__ = ["AuthError", "build_app", "build_mcp", "run"]


if __name__ == "__main__":
    run()
