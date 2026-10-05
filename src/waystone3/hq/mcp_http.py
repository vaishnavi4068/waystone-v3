"""Read-only HQ MCP endpoint served by the dashboard API at ``/api/mcp``.

It reuses the dashboard's members: the bearer token is the one ``POST /api/login`` returns,
so a Claude connector (Cowork, claude.ai, Desktop, Code) sends ``Authorization: Bearer
<token>``. Only the ``hq_*`` tools are exposed; nothing here can place orders.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from waystone3.hq.mcp_tools import register_hq_tools
from waystone3.hq.reader import HqReader

MCP_PATH = "/api/mcp"

_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("hq_mcp_token", default=None)

Authenticate = Callable[[str], str | None]


def build_hq_mcp(reader: HqReader, authenticate: Authenticate) -> FastMCP:
    mcp = FastMCP(
        "waystone-hq",
        instructions=(
            "Read-only Waystone futures HQ: live paper trading vs backtest replay, daily P&L, "
            "trades, signals, fills, engine events and workbook KPIs for es_v221, nq_v221 and "
            "r2_mnq. Dates are session dates (YYYY-MM-DD, US/Eastern). Start with "
            "hq_strategies or hq_sync; use hq_compare for one strategy's day and hq_paper_day "
            "for its trades and data freshness."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path=MCP_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def authorize() -> None:
        if authenticate(_token.get() or "") is None:
            raise PermissionError("invalid or missing dashboard token")

    register_hq_tools(mcp, reader, authorize)
    return mcp


class HqMcpMount:
    """ASGI middleware: requests under ``MCP_PATH`` go to the MCP app, the rest to the API."""

    def __init__(self, app: Any, mcp_app: Any, authenticate: Authenticate) -> None:
        self.app = app
        self.mcp_app = mcp_app
        self.authenticate = authenticate

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == MCP_PATH or path.startswith(MCP_PATH + "/")):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or self.authenticate(token) is None:
            await _unauthorized(send)
            return
        reset = _token.set(token)
        try:
            await self.mcp_app(scope, receive, send)
        finally:
            _token.reset(reset)


async def _unauthorized(send: Any) -> None:
    body = json.dumps({"error": "missing or invalid bearer token"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
