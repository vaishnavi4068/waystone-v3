"""Agent protocol and the context handed to every handler.

An agent declares which event types it cares about and implements ``handle``. It receives
an :class:`AgentContext` giving it the bus (to emit follow-on events) and — for *acting*
agents — the :class:`ActionGateway` (the only sanctioned way to change runtime state) and
the current :class:`RuntimeState`. Observe-only agents simply ignore the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from waystone3.bus.bus import EventBus
from waystone3.bus.events import Event

if TYPE_CHECKING:
    from waystone3.agents.actions import ActionGateway, RuntimeState


@dataclass
class AgentContext:
    bus: EventBus
    gateway: ActionGateway | None = None
    state: RuntimeState | None = None


@runtime_checkable
class Agent(Protocol):
    name: str
    kind: str  # "observe" | "act"
    subscribes_to: tuple[type[Event], ...]
    enabled: bool

    async def handle(self, event: Event, ctx: AgentContext) -> None: ...
