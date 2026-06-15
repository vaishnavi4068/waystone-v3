"""Token-authenticated facade over the competition.

This holds all the multi-user logic — every method takes the caller's token, authenticates,
and returns plain JSON-able dicts. The MCP server is a thin transport over this, so the
behavior here is fully unit-tested without any MCP/HTTP runtime.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from waystone3.competition.competition import Competition
from waystone3.competition.models import StrategyConfig


class AuthError(Exception):
    """Raised when a token is missing, unknown, or lacks the required role."""


class CompetitionService:
    def __init__(self, competition: Competition, admin_token: str | None = None) -> None:
        self.comp = competition
        self.admin_token = admin_token

    def _user_id(self, token: str | None) -> str:
        entry = self.comp.authenticate(token or "")
        if entry is None:
            raise AuthError("invalid or missing token")
        return entry.user_id

    def register(self, admin_token: str | None, display_name: str) -> dict[str, Any]:
        if not self.admin_token or admin_token != self.admin_token:
            raise AuthError("admin token required to register players")
        entry = self.comp.register(display_name)
        return {
            "user_id": entry.user_id,
            "display_name": entry.display_name,
            "token": entry.token,
        }

    def submit_strategy(
        self,
        token: str | None,
        *,
        weights: dict[str, float],
        watchlist: list[str],
        bullish_threshold: float = 3.0,
        bearish_threshold: float = -3.0,
        notional_per_trade: float = 2000.0,
        lookback: int = 80,
    ) -> dict[str, Any]:
        user_id = self._user_id(token)
        config = StrategyConfig(
            weights=weights,
            watchlist=[s.upper() for s in watchlist],
            bullish_threshold=Decimal(str(bullish_threshold)),
            bearish_threshold=Decimal(str(bearish_threshold)),
            notional_per_trade=Decimal(str(notional_per_trade)),
            lookback=lookback,
        )
        self.comp.submit_strategy(user_id, config)
        return {"ok": True, "weights": weights, "watchlist": config.watchlist}

    async def run_cycle(self, token: str | None) -> dict[str, Any]:
        user_id = self._user_id(token)
        report = await self.comp.run_cycle(user_id)
        return {
            "orders": len(report.orders),
            "decisions": [
                {"symbol": c.symbol, "score": float(c.score)} for c in report.composites
            ],
        }

    async def run_backtest(
        self, token: str | None, start: str, end: str
    ) -> dict[str, Any]:
        user_id = self._user_id(token)
        metrics = await self.comp.run_backtest(
            user_id, _parse(start), _parse(end)
        )
        return {
            "total_return_pct": float(metrics.total_return * 100),
            "max_drawdown_pct": float(metrics.max_drawdown * 100),
            "win_rate_pct": float(metrics.win_rate * 100),
            "trades": metrics.num_trades,
        }

    async def my_account(self, token: str | None) -> dict[str, Any]:
        user_id = self._user_id(token)
        entry = self.comp.get(user_id)
        assert entry is not None
        acct = await entry.broker.get_account()
        positions = await entry.broker.get_positions()
        return {
            "cash": float(acct.cash),
            "equity": float(acct.equity),
            "positions": [
                {"symbol": p.symbol, "qty": float(p.qty)} for p in positions
            ],
            "cycles_run": entry.cycles_run,
        }

    async def standings(self, token: str | None) -> list[dict[str, Any]]:
        self._user_id(token)  # any valid player may view standings
        rows = await self.comp.standings()
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


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt
