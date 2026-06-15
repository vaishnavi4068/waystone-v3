"""RiskSupervisorAgent — an acting agent that halts trading after repeated risk blocks.

Demonstrates the guarded acting path: it never touches the broker; it requests
``GateTrading(enabled=False)`` through the gateway, which enforces paper-only + policy +
audit. A burst of risk blocks is a sign something is wrong, so it pulls the master switch.
"""

from __future__ import annotations

import structlog

from waystone3.agents.actions import GateTrading
from waystone3.agents.base import AgentContext
from waystone3.bus.events import Event, OrderBlocked

log = structlog.get_logger()


class RiskSupervisorAgent:
    name = "risk_supervisor"
    kind = "act"
    subscribes_to: tuple[type[Event], ...] = (OrderBlocked,)

    def __init__(self, max_blocks: int = 3, enabled: bool = True) -> None:
        self.max_blocks = max_blocks
        self.enabled = enabled
        self._blocks = 0
        self._tripped = False

    async def handle(self, event: Event, ctx: AgentContext) -> None:
        if not isinstance(event, OrderBlocked) or ctx.gateway is None or self._tripped:
            return
        self._blocks += 1
        if self._blocks >= self.max_blocks:
            self._tripped = True
            await ctx.gateway.request(
                self.name,
                GateTrading(
                    enabled=False,
                    reason=f"{self._blocks} risk blocks reached; halting trading",
                ),
            )
            log.warning("risk_supervisor_halt", blocks=self._blocks)
