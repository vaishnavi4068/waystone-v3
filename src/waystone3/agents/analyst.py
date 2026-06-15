"""AnalystAgent — an observe-only Claude agent that judges each cycle.

Subscribes to ``CycleCompleted``, asks Claude to assess whether the cycle's decisions
looked sound, and publishes a ``CycleJudged`` event for downstream agents (e.g. the tuner)
and operators to consume. It never acts on the market.
"""

from __future__ import annotations

from typing import Any

import structlog

from waystone3.agents.base import AgentContext
from waystone3.agents.claude import ClaudeAgent
from waystone3.bus.events import CycleCompleted, CycleJudged, Event

log = structlog.get_logger()

_SYSTEM = (
    "You are a risk-aware trading-cycle analyst for a momentum platform. "
    "Given a summary of one paper-trading cycle, judge whether the decisions look "
    "sound and internally consistent. Be terse and concrete."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quality": {"type": "string", "enum": ["good", "concerning", "bad"]},
        "summary": {"type": "string"},
    },
    "required": ["quality", "summary"],
    "additionalProperties": False,
}


class AnalystAgent(ClaudeAgent):
    name = "analyst"
    kind = "observe"
    subscribes_to: tuple[type[Event], ...] = (CycleCompleted,)

    async def handle(self, event: Event, ctx: AgentContext) -> None:
        if not isinstance(event, CycleCompleted) or not self.enabled:
            return
        lines = [
            f"{sym}: score={score:.2f} action={action}"
            for sym, score, action in event.decisions
        ]
        user = (
            f"Cycle {event.cycle}: {event.num_orders} order(s) placed.\n"
            f"Per-symbol decisions:\n" + "\n".join(lines)
        )
        result = await self.complete(_SYSTEM, user, _SCHEMA)
        await ctx.bus.publish(
            CycleJudged(
                cycle=event.cycle,
                quality=str(result.get("quality", "good")),
                summary=str(result.get("summary", "")),
                by=self.name,
            )
        )
