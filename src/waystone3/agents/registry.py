"""Agent registry — collects agents and wires the enabled ones onto the bus."""

from __future__ import annotations

from waystone3.agents.base import Agent, AgentContext
from waystone3.bus.bus import EventBus
from waystone3.bus.events import Event


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: list[Agent] = []

    def register(self, agent: Agent) -> None:
        self._agents.append(agent)

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents)

    def install(self, bus: EventBus, ctx: AgentContext) -> None:
        """Subscribe every enabled agent to its event types on ``bus``."""
        for agent in self._agents:
            if not agent.enabled:
                continue
            for event_type in agent.subscribes_to:
                bus.subscribe(event_type, _bind(agent, ctx), name=agent.name)


def _bind(agent: Agent, ctx: AgentContext):  # type: ignore[no-untyped-def]
    async def handler(event: Event) -> None:
        await agent.handle(event, ctx)

    return handler
