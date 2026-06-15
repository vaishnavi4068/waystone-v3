from __future__ import annotations

from waystone3.agents.base import AgentContext
from waystone3.agents.logging_agent import LoggingAgent
from waystone3.agents.registry import AgentRegistry
from waystone3.brokers.paper import PaperBroker
from waystone3.bus.bus import EventBus
from waystone3.bus.events import (
    CycleCompleted,
    DecisionMade,
    Event,
    OrderFilled,
    StrongSignal,
)
from waystone3.core.types import Timeframe
from waystone3.data.stub import StubDataSource
from waystone3.decision.engine import DecisionEngine
from waystone3.risk.guard import RiskGuard
from waystone3.runner.config import default_contributors, default_weights
from waystone3.runner.cycle import run_cycle


class _Recorder:
    name = "recorder"
    kind = "observe"
    subscribes_to = (Event,)
    enabled = True

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event, ctx: AgentContext) -> None:
        self.events.append(event)


async def _run(bus: EventBus | None, strong_threshold: float = 7.0) -> None:
    from decimal import Decimal

    await run_cycle(
        data=StubDataSource(default_slope=1.0),
        broker=PaperBroker(),
        guard=RiskGuard(is_paper=True),
        contributors=default_contributors(),
        weights=default_weights(),
        engine=DecisionEngine(),
        watchlist=["AAPL"],
        timeframe=Timeframe.D1,
        bars_lookback=80,
        bus=bus,
        cycle=1,
        strong_signal_threshold=Decimal(str(strong_threshold)),
    )


async def test_agent_receives_cycle_events() -> None:
    bus = EventBus()
    rec = _Recorder()
    reg = AgentRegistry()
    reg.register(rec)
    reg.install(bus, AgentContext(bus=bus))

    await _run(bus)

    kinds = {type(e) for e in rec.events}
    assert DecisionMade in kinds
    assert OrderFilled in kinds  # uptrend -> a buy fills
    assert CycleCompleted in kinds
    completed = next(e for e in rec.events if isinstance(e, CycleCompleted))
    assert completed.cycle == 1
    assert completed.num_orders == 1


async def test_strong_signal_emitted_for_high_score() -> None:
    bus = EventBus()
    rec = _Recorder()
    reg = AgentRegistry()
    reg.register(rec)
    reg.install(bus, AgentContext(bus=bus))
    await _run(bus, strong_threshold=5.0)
    # composite ~6 in a steady uptrend -> crosses a 5.0 strong-signal threshold
    assert any(isinstance(e, StrongSignal) for e in rec.events)


async def test_disabled_agent_not_wired() -> None:
    bus = EventBus()
    rec = _Recorder()
    rec.enabled = False
    reg = AgentRegistry()
    reg.register(rec)
    reg.install(bus, AgentContext(bus=bus))
    await _run(bus)
    assert rec.events == []


async def test_cycle_without_bus_still_runs() -> None:
    # the core must not depend on the agent layer
    await _run(None)  # no exception, no bus


async def test_logging_agent_is_an_agent() -> None:
    from waystone3.agents.base import Agent

    assert isinstance(LoggingAgent(), Agent)
