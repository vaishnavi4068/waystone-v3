from __future__ import annotations

from decimal import Decimal

from waystone3.bus.bus import EventBus
from waystone3.bus.events import AgentError, CycleCompleted, Event, StrongSignal


async def test_exact_type_delivery() -> None:
    bus = EventBus()
    got: list[StrongSignal] = []

    async def h(e: Event) -> None:
        assert isinstance(e, StrongSignal)
        got.append(e)

    bus.subscribe(StrongSignal, h, name="t")
    await bus.publish(StrongSignal(symbol="X", score=Decimal(9)))
    await bus.publish(CycleCompleted(cycle=1, num_orders=0, decisions=[]))
    assert len(got) == 1
    assert got[0].symbol == "X"


async def test_base_event_subscriber_gets_all() -> None:
    bus = EventBus()
    count = 0

    async def h(e: Event) -> None:
        nonlocal count
        count += 1

    bus.subscribe(Event, h, name="all")
    await bus.publish(StrongSignal(symbol="X", score=Decimal(1)))
    await bus.publish(CycleCompleted(cycle=1, num_orders=0, decisions=[]))
    assert count == 2


async def test_failing_handler_isolated_and_emits_agent_error() -> None:
    bus = EventBus()
    errors: list[AgentError] = []
    ok_calls = 0

    async def boom(e: Event) -> None:
        raise RuntimeError("kaboom")

    async def fine(e: Event) -> None:
        nonlocal ok_calls
        ok_calls += 1

    async def on_err(e: Event) -> None:
        assert isinstance(e, AgentError)
        errors.append(e)

    bus.subscribe(StrongSignal, boom, name="boom")
    bus.subscribe(StrongSignal, fine, name="fine")
    bus.subscribe(AgentError, on_err, name="err")

    await bus.publish(StrongSignal(symbol="X", score=Decimal(5)))
    assert ok_calls == 1  # the good handler still ran
    assert len(errors) == 1
    assert errors[0].agent == "boom"


async def test_agent_error_handler_failure_does_not_recurse() -> None:
    bus = EventBus()

    async def boom(e: Event) -> None:
        raise RuntimeError("again")

    # An AgentError handler that itself fails must NOT spawn another AgentError forever.
    bus.subscribe(AgentError, boom, name="boom")
    await bus.publish(AgentError(agent="x", event="E", error="e"))  # returns cleanly


async def test_no_subscribers_is_noop() -> None:
    bus = EventBus()
    await bus.publish(StrongSignal(symbol="X", score=Decimal(1)))
