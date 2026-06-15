from __future__ import annotations

from decimal import Decimal

from waystone3.agents.actions import (
    ActionGateway,
    AdjustWeights,
    ApprovalPolicy,
    GateTrading,
    RuntimeState,
    ScaleExposure,
)
from waystone3.agents.base import AgentContext
from waystone3.agents.registry import AgentRegistry
from waystone3.agents.supervisor import RiskSupervisorAgent
from waystone3.bus.bus import EventBus
from waystone3.bus.events import AgentAction, Event, OrderBlocked


def _state() -> RuntimeState:
    return RuntimeState(weights={"ma_crossover": 0.4})


async def test_auto_policy_applies_immediately() -> None:
    state = _state()
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO)
    entry = await gw.request("a", AdjustWeights(reason="r", changes={"ma_crossover": 0.6}))
    assert entry.status == "applied"
    assert state.weights["ma_crossover"] == 0.6


async def test_manual_policy_pends_then_approve_applies() -> None:
    state = _state()
    gw = ActionGateway(state=state, policy=ApprovalPolicy.MANUAL)
    entry = await gw.request("a", GateTrading(reason="halt", enabled=False))
    assert entry.status == "pending"
    assert state.trading_enabled is True  # not applied yet
    assert len(gw.pending) == 1

    assert gw.approve(entry.id) is True
    assert state.trading_enabled is False
    assert gw.pending == []


async def test_paper_only_hard_gate_denies_when_live() -> None:
    state = _state()
    state.is_paper = False
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO)
    entry = await gw.request("a", GateTrading(reason="x", enabled=False))
    assert entry.status == "denied"
    assert state.trading_enabled is True  # untouched


async def test_deny_policy_blocks() -> None:
    state = _state()
    gw = ActionGateway(state=state, policy=ApprovalPolicy.DENY)
    entry = await gw.request("a", ScaleExposure(reason="x", scale=Decimal("0.5")))
    assert entry.status == "denied"
    assert state.exposure_scale == Decimal(1)


async def test_scale_exposure_clamped() -> None:
    state = _state()
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO)
    await gw.request("a", ScaleExposure(reason="x", scale=Decimal("5")))
    assert state.exposure_scale == Decimal(1)  # clamped to 1


async def test_action_emits_agentaction_event() -> None:
    bus = EventBus()
    seen: list[AgentAction] = []

    async def on(e: Event) -> None:
        assert isinstance(e, AgentAction)
        seen.append(e)

    bus.subscribe(AgentAction, on, name="t")
    gw = ActionGateway(state=_state(), policy=ApprovalPolicy.AUTO, bus=bus)
    await gw.request("a", GateTrading(reason="r", enabled=False))
    assert len(seen) == 1
    assert seen[0].approved is True


async def test_supervisor_halts_after_threshold_via_gateway() -> None:
    bus = EventBus()
    state = _state()
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO, bus=bus)
    reg = AgentRegistry()
    reg.register(RiskSupervisorAgent(max_blocks=3))
    reg.install(bus, AgentContext(bus=bus, gateway=gw, state=state))

    for _ in range(2):
        await bus.publish(OrderBlocked("X", "buy", Decimal(1), "cap"))
    assert state.trading_enabled is True  # not yet

    await bus.publish(OrderBlocked("X", "buy", Decimal(1), "cap"))
    assert state.trading_enabled is False  # tripped on the 3rd block
    assert any(e.kind == "gate_trading" and e.status == "applied" for e in gw.audit)
