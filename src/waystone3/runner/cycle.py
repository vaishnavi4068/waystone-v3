"""The trading cycle: data -> signals -> fusion -> decision -> risk -> order.

One pass over a watchlist. Pure orchestration — every step lives in its own module, so
this stays short and the same pipeline is reused by the backtester.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from waystone3.brokers.base import Broker
from waystone3.bus.bus import EventBus
from waystone3.bus.events import (
    CycleCompleted,
    DecisionMade,
    Event,
    OrderBlocked,
    OrderFilled,
    StrongSignal,
)
from waystone3.core.types import Bar, Order, Timeframe
from waystone3.data.base import MarketDataSource
from waystone3.decision.engine import DecisionEngine, Intent
from waystone3.fusion.fuse import CompositeScore, fuse
from waystone3.risk.guard import RiskGuard, RiskViolationError
from waystone3.signals.base import ContributorScore, SignalContributor

log = structlog.get_logger()


@dataclass(frozen=True)
class DecisionRow:
    composite: CompositeScore
    intent: Intent | None
    order: Order | None
    blocked: str | None = None  # risk-violation reason, if any


@dataclass(frozen=True)
class CycleReport:
    composites: list[CompositeScore]
    rows: list[DecisionRow]
    orders: list[Order] = field(default_factory=list)


async def _gather_bars(
    data: MarketDataSource, watchlist: list[str], timeframe: Timeframe, lookback: int
) -> dict[str, list[Bar]]:
    bars: dict[str, list[Bar]] = {}
    for symbol in watchlist:
        series = await data.get_bars(symbol, timeframe, lookback)
        if series:
            bars[symbol] = series
    return bars


def score_all(
    contributors: list[SignalContributor], bars: dict[str, list[Bar]]
) -> dict[str, dict[str, ContributorScore]]:
    return {c.name: c.score(bars) for c in contributors}


async def run_cycle(
    *,
    data: MarketDataSource,
    broker: Broker,
    guard: RiskGuard,
    contributors: list[SignalContributor],
    weights: dict[str, float],
    engine: DecisionEngine,
    watchlist: list[str],
    timeframe: Timeframe,
    bars_lookback: int,
    bus: EventBus | None = None,
    cycle: int = 0,
    strong_signal_threshold: Decimal = Decimal(7),
    trading_enabled: bool = True,
    actor: str = "",
) -> CycleReport:
    async def emit(event: Event) -> None:
        if bus is not None:
            await bus.publish(event)

    bars = await _gather_bars(data, watchlist, timeframe, bars_lookback)
    per_contributor = score_all(contributors, bars)
    composites = fuse(per_contributor, weights)

    positions = {p.symbol: p.qty for p in await broker.get_positions()}
    guard.sync_positions(positions)

    rows: list[DecisionRow] = []
    orders: list[Order] = []
    decisions: list[tuple[str, Decimal, str]] = []
    for comp in composites:
        last_close = bars[comp.symbol][-1].close
        intent = engine.decide(comp, positions.get(comp.symbol, Decimal(0)), last_close)
        action = intent.side.value if intent is not None else "hold"
        decisions.append((comp.symbol, comp.score, action))
        await emit(DecisionMade(comp.symbol, comp.score, action, comp.drivers[:4]))
        if abs(comp.score) >= strong_signal_threshold:
            await emit(StrongSignal(comp.symbol, comp.score, comp.drivers[:4]))

        if intent is None:
            rows.append(DecisionRow(comp, None, None))
            continue
        if not trading_enabled:
            reason = "trading disabled"
            rows.append(DecisionRow(comp, intent, None, blocked=reason))
            await emit(OrderBlocked(intent.symbol, intent.side.value, intent.qty, reason))
            continue
        try:
            guard.check(intent.symbol, intent.side, intent.qty, last_close)
        except RiskViolationError as exc:
            log.warning("risk_blocked", symbol=intent.symbol, reason=str(exc))
            rows.append(DecisionRow(comp, intent, None, blocked=str(exc)))
            await emit(
                OrderBlocked(intent.symbol, intent.side.value, intent.qty, str(exc))
            )
            continue
        order = await broker.submit_order(
            intent.symbol, intent.side, intent.qty, ref_price=last_close
        )
        guard.apply_fill(intent.symbol, intent.side, intent.qty)
        positions[comp.symbol] = positions.get(comp.symbol, Decimal(0)) + (
            intent.qty if intent.side.value == "buy" else -intent.qty
        )
        orders.append(order)
        rows.append(DecisionRow(comp, intent, order))
        await emit(
            OrderFilled(
                intent.symbol,
                intent.side.value,
                intent.qty,
                order.avg_fill_price or last_close,
                order.id,
                actor,
            )
        )

    await emit(CycleCompleted(cycle=cycle, num_orders=len(orders), decisions=decisions))
    return CycleReport(composites=composites, rows=rows, orders=orders)
