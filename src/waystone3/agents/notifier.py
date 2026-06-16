"""NotifierAgent — bridges bus events to the alert router.

Observe-only: it translates notable events (filled/blocked orders, submitted strategies,
strong signals, agent actions, agent errors) into alerts and hands them to the router, which
handles recipient matching, channel delivery, dedup, and audit. Order fills and strategy
submissions are what surface to the team group (e.g. a Zoho Cliq channel) — both are routed
to the TRADER role so a single team recipient receives them.
"""

from __future__ import annotations

from waystone3.agents.base import AgentContext
from waystone3.alerts.models import Alert, Role, Severity
from waystone3.alerts.router import AlertRouter
from waystone3.bus.events import (
    AgentAction,
    AgentError,
    AlertRaised,
    Event,
    OrderBlocked,
    OrderFilled,
    StrategySubmitted,
    StrongSignal,
)


class NotifierAgent:
    name = "notifier"
    kind = "observe"
    subscribes_to: tuple[type[Event], ...] = (
        AlertRaised,
        StrongSignal,
        OrderFilled,
        OrderBlocked,
        StrategySubmitted,
        AgentAction,
        AgentError,
    )

    def __init__(self, router: AlertRouter, enabled: bool = True) -> None:
        self.router = router
        self.enabled = enabled

    def _to_alert(self, event: Event) -> Alert | None:
        if isinstance(event, AlertRaised):
            return Alert(
                severity=Severity(event.severity),
                role=Role(event.role),
                title=event.title,
                body=event.body,
            )
        if isinstance(event, StrongSignal):
            return Alert(
                Severity.INFO, Role.TRADER, f"Strong signal: {event.symbol}",
                f"score={event.score}",
            )
        if isinstance(event, OrderFilled):
            who = f"{event.actor}: " if event.actor else ""
            return Alert(
                Severity.INFO, Role.TRADER, f"Order filled: {event.symbol}",
                f"{who}{event.side.upper()} {event.qty} {event.symbol} @ {event.price}",
            )
        if isinstance(event, StrategySubmitted):
            return Alert(
                Severity.INFO, Role.TRADER, f"Strategy submitted by {event.actor}",
                event.summary,
            )
        if isinstance(event, OrderBlocked):
            return Alert(
                Severity.WARN, Role.TRADER, f"Order blocked: {event.symbol}", event.reason
            )
        if isinstance(event, AgentAction):
            return Alert(
                Severity.WARN, Role.OPS, f"Agent action: {event.kind}",
                f"{event.agent} -> {event.detail} (approved={event.approved})",
            )
        if isinstance(event, AgentError):
            return Alert(
                Severity.CRITICAL, Role.ENGINEER, f"Agent error: {event.agent}", event.error
            )
        return None

    async def handle(self, event: Event, ctx: AgentContext) -> None:
        if not self.enabled:
            return
        alert = self._to_alert(event)
        if alert is not None:
            await self.router.dispatch(alert)
