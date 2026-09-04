"""Token-authenticated facade over the shared workspace (all MCP/API logic, fully tested)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from waystone3.competition.models import StrategyConfig
from waystone3.decision.engine import DecisionConfig
from waystone3.runner.backtest import run_backtest
from waystone3.signals.registry import build_contributor
from waystone3.workspace.workspace import TradingWorkspace


class AuthError(Exception):
    pass


class WorkspaceService:
    def __init__(self, workspace: TradingWorkspace, admin_token: str | None = None) -> None:
        self.ws = workspace
        self.admin_token = admin_token

    def _actor(self, token: str | None) -> str:
        name = self.ws.authenticate(token or "")
        if name is None:
            raise AuthError("invalid or missing token")
        return name

    def register(
        self, admin_token: str | None, name: str, password: str | None = None
    ) -> dict[str, Any]:
        if not self.admin_token or admin_token != self.admin_token:
            raise AuthError("admin token required to add team members")
        member = self.ws.register_member(name, password=password)
        return {"name": member.name, "token": member.token, "password": member.password}

    def login(self, name: str, password: str) -> dict[str, str]:
        token = self.ws.authenticate_password(name, password)
        if token is None:
            raise AuthError("invalid username or password")
        actor = self.ws.authenticate(token)
        if actor is None:
            raise AuthError("invalid username or password")
        return {"name": actor, "token": token}

    def set_strategy(
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
        actor = self._actor(token)
        config = StrategyConfig(
            weights=weights,
            watchlist=[s.upper() for s in watchlist],
            bullish_threshold=Decimal(str(bullish_threshold)),
            bearish_threshold=Decimal(str(bearish_threshold)),
            notional_per_trade=Decimal(str(notional_per_trade)),
            lookback=lookback,
        )
        self.ws.set_strategy(config, actor)
        return {"ok": True, "by": actor, "weights": weights, "watchlist": config.watchlist}

    def get_strategy(self, token: str | None) -> dict[str, Any]:
        self._actor(token)
        cfg = self.ws.strategy
        if cfg is None:
            return {"strategy": None}
        return {
            "weights": cfg.weights,
            "watchlist": cfg.watchlist,
            "bullish_threshold": float(cfg.bullish_threshold),
            "bearish_threshold": float(cfg.bearish_threshold),
            "notional_per_trade": float(cfg.notional_per_trade),
        }

    async def run_cycle(self, token: str | None) -> dict[str, Any]:
        actor = self._actor(token)
        report = await self.ws.run_cycle(actor)
        return {
            "by": actor,
            "orders_submitted": len(report.orders),
            "orders": [
                {
                    "symbol": o.symbol,
                    "side": o.side.value,
                    "qty": float(o.qty),
                    "status": o.status.value,
                }
                for o in report.orders
            ],
            "decisions": [
                {"symbol": c.symbol, "score": float(c.score)} for c in report.composites
            ],
        }

    async def account(self, token: str | None) -> dict[str, Any]:
        self._actor(token)
        acct = await self.ws.broker.get_account()
        return {
            "broker": self.ws.broker.name,
            "is_paper": self.ws.broker.is_paper,
            "trading_enabled": self.ws.trading_enabled,
            "cash": float(acct.cash),
            "equity": float(acct.equity),
            "buying_power": float(acct.buying_power),
        }

    async def positions(self, token: str | None) -> list[dict[str, Any]]:
        self._actor(token)
        positions = await self.ws.broker.get_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "market_price": float(p.market_price) if p.market_price else None,
                "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else None,
            }
            for p in positions
        ]

    async def orders(self, token: str | None, limit: int = 20) -> list[dict[str, Any]]:
        self._actor(token)
        orders = await self.ws.broker.list_orders(limit)
        return [
            {
                "symbol": o.symbol,
                "side": o.side.value,
                "qty": float(o.qty),
                "status": o.status.value,
                "avg_fill_price": float(o.avg_fill_price) if o.avg_fill_price else None,
                "submitted_at": o.submitted_at.isoformat(),
            }
            for o in orders
        ]

    def halt(self, token: str | None, reason: str = "") -> dict[str, Any]:
        actor = self._actor(token)
        self.ws.set_trading(False, actor)
        return {"trading_enabled": False, "by": actor, "reason": reason}

    def resume(self, token: str | None) -> dict[str, Any]:
        actor = self._actor(token)
        self.ws.set_trading(True, actor)
        return {"trading_enabled": True, "by": actor}

    def audit(self, token: str | None, limit: int = 50) -> list[dict[str, Any]]:
        self._actor(token)
        return [
            {"seq": e.seq, "actor": e.actor, "action": e.action, "detail": e.detail}
            for e in self.ws.audit[-limit:][::-1]
        ]

    async def backtest(self, token: str | None, start: str, end: str) -> dict[str, Any]:
        self._actor(token)
        cfg = self.ws.strategy
        if cfg is None:
            raise AuthError("no shared strategy set")  # reuse 401-ish surface; caller maps
        contributors = [build_contributor(n) for n, w in cfg.weights.items() if w > 0]
        result = await run_backtest(
            data=self.ws.data,
            symbols=cfg.watchlist,
            timeframe=cfg.timeframe,
            start=_parse(start),
            end=_parse(end),
            lookback=cfg.lookback,
            contributors=contributors,
            weights=cfg.weights,
            decision=DecisionConfig(
                bullish_threshold=cfg.bullish_threshold,
                bearish_threshold=cfg.bearish_threshold,
                notional_per_trade=cfg.notional_per_trade,
            ),
        )
        m = result.metrics
        return {
            "total_return_pct": float(m.total_return * 100),
            "max_drawdown_pct": float(m.max_drawdown * 100),
            "win_rate_pct": float(m.win_rate * 100),
            "trades": m.num_trades,
        }


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
