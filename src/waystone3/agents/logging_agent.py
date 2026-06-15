"""A trivial observe-only agent: logs notable events. Useful as a template and default."""

from __future__ import annotations

import structlog

from waystone3.agents.base import AgentContext
from waystone3.bus.events import CycleCompleted, Event, OrderBlocked, StrongSignal

log = structlog.get_logger()


class LoggingAgent:
    name = "logger"
    kind = "observe"
    subscribes_to: tuple[type[Event], ...] = (CycleCompleted, StrongSignal, OrderBlocked)

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    async def handle(self, event: Event, ctx: AgentContext) -> None:
        if isinstance(event, CycleCompleted):
            log.info("cycle_completed", cycle=event.cycle, orders=event.num_orders)
        elif isinstance(event, StrongSignal):
            log.info("strong_signal", symbol=event.symbol, score=str(event.score))
        elif isinstance(event, OrderBlocked):
            log.warning("order_blocked", symbol=event.symbol, reason=event.reason)
