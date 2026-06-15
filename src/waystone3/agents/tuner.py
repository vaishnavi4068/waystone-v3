"""TuningAgent — an *acting* Claude agent that proposes weight changes.

When the analyst judges a cycle "concerning" or "bad", this agent asks Claude how to
re-emphasize the technical signals, then submits the change **through the action gateway**
— which enforces paper-only, the approval policy, and the audit trail. The agent itself
never mutates weights directly; it only requests. The LLM picks among a small fixed set of
emphases, and the agent maps that to concrete weights, so the action surface stays auditable.
"""

from __future__ import annotations

from typing import Any

import structlog

from waystone3.agents.actions import AdjustWeights
from waystone3.agents.base import AgentContext
from waystone3.agents.claude import ClaudeAgent
from waystone3.bus.events import CycleJudged, Event

log = structlog.get_logger()

# Fixed, auditable weight presets the LLM may select among.
_PRESETS: dict[str, dict[str, float]] = {
    "momentum": {"ma_crossover": 0.45, "price_action": 0.45, "volume": 0.10},
    "volume": {"ma_crossover": 0.35, "price_action": 0.30, "volume": 0.35},
    "balanced": {"ma_crossover": 0.40, "price_action": 0.40, "volume": 0.20},
}

_SYSTEM = (
    "You tune a momentum trading platform's signal weights. Given a concerning cycle "
    "judgement, decide whether to re-emphasize signals and pick one preset. Only adjust "
    "when it's clearly warranted."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "adjust": {"type": "boolean"},
        "emphasis": {"type": "string", "enum": list(_PRESETS)},
        "reason": {"type": "string"},
    },
    "required": ["adjust", "emphasis", "reason"],
    "additionalProperties": False,
}


class TuningAgent(ClaudeAgent):
    name = "tuner"
    kind = "act"
    subscribes_to: tuple[type[Event], ...] = (CycleJudged,)

    async def handle(self, event: Event, ctx: AgentContext) -> None:
        if not isinstance(event, CycleJudged) or not self.enabled:
            return
        if event.quality not in ("concerning", "bad") or ctx.gateway is None:
            return
        user = (
            f"Cycle {event.cycle} judged '{event.quality}': {event.summary}\n"
            f"Choose a preset emphasis or decline to adjust."
        )
        result = await self.complete(_SYSTEM, user, _SCHEMA)
        if not result.get("adjust"):
            return
        emphasis = str(result.get("emphasis", "balanced"))
        changes = _PRESETS.get(emphasis, _PRESETS["balanced"])
        await ctx.gateway.request(
            self.name,
            AdjustWeights(
                changes=dict(changes),
                reason=f"{emphasis}: {result.get('reason', '')}",
            ),
        )
