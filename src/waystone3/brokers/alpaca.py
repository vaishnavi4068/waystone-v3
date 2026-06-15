"""Alpaca broker adapter — drop-in for live(-paper) stock trading.

Optional: requires the ``alpaca`` extra (``uv sync --extra alpaca``) and credentials in
the environment (``ALPACA_API_KEY``, ``ALPACA_API_SECRET``, ``ALPACA_PAPER``). Satisfies
the same :class:`~waystone3.brokers.base.Broker` protocol as the paper broker, so the
runner is unchanged. Paper trading is the default and the only mode wired into the CLI.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings, SettingsConfigDict

from waystone3.core.types import (
    AccountInfo,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

if TYPE_CHECKING:
    from alpaca.trading.models import Order as AlpacaOrder
    from alpaca.trading.models import Position as AlpacaPosition
    from alpaca.trading.models import TradeAccount


class AlpacaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALPACA_")

    api_key: str = ""
    api_secret: str = ""
    paper: bool = True


_STATUS_FROM_ALPACA = {
    "new": OrderStatus.PENDING,
    "accepted": OrderStatus.PENDING,
    "pending_new": OrderStatus.PENDING,
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PENDING,
    "rejected": OrderStatus.REJECTED,
}


class AlpacaBroker:
    name = "alpaca"

    def __init__(self, settings: AlpacaSettings | None = None) -> None:
        from alpaca.trading.client import TradingClient

        cfg = settings or AlpacaSettings()
        if not cfg.api_key or not cfg.api_secret:
            raise ValueError(
                "Alpaca credentials missing; set ALPACA_API_KEY and ALPACA_API_SECRET"
            )
        self.is_paper = cfg.paper
        self._client = TradingClient(
            api_key=cfg.api_key, secret_key=cfg.api_secret, paper=cfg.paper
        )

    async def get_account(self) -> AccountInfo:
        acct = await asyncio.to_thread(self._client.get_account)
        return _account_from_alpaca(acct)

    async def get_positions(self) -> list[Position]:
        positions = await asyncio.to_thread(self._client.get_all_positions)
        return [_position_from_alpaca(p) for p in positions]

    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        ref_price: Decimal | None = None,
    ) -> Order:
        from alpaca.trading.enums import OrderSide as AlpacaSide
        from alpaca.trading.enums import TimeInForce as AlpacaTIF
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        side_enum = AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL
        common = {"symbol": symbol, "qty": float(qty), "side": side_enum,
                  "time_in_force": AlpacaTIF.DAY}
        if order_type == OrderType.LIMIT:
            if limit_price is None:
                raise ValueError("limit_price required for LIMIT order")
            req = LimitOrderRequest(limit_price=float(limit_price), **common)
        else:
            req = MarketOrderRequest(**common)
        result = await asyncio.to_thread(self._client.submit_order, req)
        return _order_from_alpaca(result)

    async def get_order(self, order_id: str) -> Order:
        result = await asyncio.to_thread(self._client.get_order_by_id, order_id)
        return _order_from_alpaca(result)


def _account_from_alpaca(a: TradeAccount) -> AccountInfo:
    return AccountInfo(
        account_id=str(a.account_number),
        cash=Decimal(str(a.cash)),
        equity=Decimal(str(a.equity)),
        buying_power=Decimal(str(a.buying_power)),
        currency=str(a.currency),
    )


def _position_from_alpaca(p: AlpacaPosition) -> Position:
    return Position(
        symbol=p.symbol,
        qty=Decimal(str(p.qty)),
        avg_entry_price=Decimal(str(p.avg_entry_price)),
        market_price=Decimal(str(p.current_price)) if p.current_price is not None else None,
    )


def _order_from_alpaca(o: AlpacaOrder) -> Order:
    side_str = str(o.side.value if hasattr(o.side, "value") else o.side).lower()
    type_str = str(
        o.order_type.value if hasattr(o.order_type, "value") else o.order_type
    ).lower()
    status_str = str(o.status.value if hasattr(o.status, "value") else o.status).lower()
    return Order(
        id=str(o.id),
        symbol=o.symbol,
        side=OrderSide(side_str),
        type=OrderType(type_str) if type_str in (t.value for t in OrderType) else OrderType.MARKET,
        qty=Decimal(str(o.qty)),
        filled_qty=Decimal(str(o.filled_qty or 0)),
        avg_fill_price=(
            Decimal(str(o.filled_avg_price)) if o.filled_avg_price is not None else None
        ),
        status=_STATUS_FROM_ALPACA.get(status_str, OrderStatus.PENDING),
        submitted_at=o.submitted_at,
        filled_at=o.filled_at,
    )
