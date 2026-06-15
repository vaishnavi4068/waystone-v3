"""Async in-process event bus.

Best-effort pub/sub: handlers for an event run concurrently, and a handler that raises
is isolated — its failure becomes an :class:`AgentError` event rather than breaking the
publisher or the other handlers. Subscribing to the base :class:`Event` receives all
events.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from waystone3.bus.events import AgentError, Event

Handler = Callable[[Event], Awaitable[None]]

log = structlog.get_logger()


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[type[Event], list[tuple[str, Handler]]] = {}

    def subscribe(self, event_type: type[Event], handler: Handler, *, name: str = "?") -> None:
        self._subs.setdefault(event_type, []).append((name, handler))

    def _handlers_for(self, event: Event) -> list[tuple[str, Handler]]:
        out: list[tuple[str, Handler]] = []
        for etype, handlers in self._subs.items():
            if isinstance(event, etype):
                out.extend(handlers)
        return out

    async def publish(self, event: Event) -> None:
        handlers = self._handlers_for(event)
        if not handlers:
            return
        await asyncio.gather(*(self._safe(name, h, event) for name, h in handlers))

    async def _safe(self, name: str, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception as exc:  # isolation is the whole point
            log.warning("agent_handler_failed", agent=name, error=str(exc))
            # Avoid an error storm: don't re-publish when handling an AgentError.
            if not isinstance(event, AgentError):
                await self.publish(
                    AgentError(agent=name, event=type(event).__name__, error=str(exc))
                )
