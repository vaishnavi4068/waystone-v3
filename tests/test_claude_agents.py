from __future__ import annotations

from decimal import Decimal
from typing import Any

from waystone3.agents.actions import ActionGateway, ApprovalPolicy, RuntimeState
from waystone3.agents.analyst import AnalystAgent
from waystone3.agents.base import AgentContext
from waystone3.agents.claude import ClaudeAgent
from waystone3.agents.registry import AgentRegistry
from waystone3.agents.tuner import TuningAgent
from waystone3.bus.bus import EventBus
from waystone3.bus.events import CycleCompleted, CycleJudged, Event


def make_completer(payload: dict[str, Any]):
    async def _complete(system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return payload

    return _complete


def test_claude_agent_disabled_without_key_or_completer(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ClaudeAgent().enabled is False


def test_claude_agent_enabled_with_completer() -> None:
    assert ClaudeAgent(complete_fn=make_completer({})).enabled is True


async def test_analyst_emits_cyclejudged() -> None:
    bus = EventBus()
    judged: list[CycleJudged] = []

    async def on(e: Event) -> None:
        assert isinstance(e, CycleJudged)
        judged.append(e)

    bus.subscribe(CycleJudged, on, name="t")
    analyst = AnalystAgent(
        complete_fn=make_completer({"quality": "concerning", "summary": "thin volume"})
    )
    reg = AgentRegistry()
    reg.register(analyst)
    reg.install(bus, AgentContext(bus=bus))

    await bus.publish(
        CycleCompleted(cycle=3, num_orders=2, decisions=[("AAPL", Decimal(6), "buy")])
    )
    assert len(judged) == 1
    assert judged[0].quality == "concerning"
    assert judged[0].by == "analyst"
    assert judged[0].cycle == 3


async def test_tuner_acts_on_concerning_judgement_via_gateway() -> None:
    bus = EventBus()
    state = RuntimeState(weights={"ma_crossover": 0.4, "price_action": 0.4, "volume": 0.2})
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO, bus=bus)
    tuner = TuningAgent(
        complete_fn=make_completer(
            {"adjust": True, "emphasis": "volume", "reason": "low-vol whipsaws"}
        )
    )
    reg = AgentRegistry()
    reg.register(tuner)
    reg.install(bus, AgentContext(bus=bus, gateway=gw, state=state))

    await bus.publish(CycleJudged(cycle=1, quality="concerning", summary="x", by="analyst"))
    # tuner picked the "volume" preset and applied it through the gateway
    assert state.weights["volume"] == 0.35
    assert any(e.kind == "adjust_weights" and e.status == "applied" for e in gw.audit)


async def test_tuner_ignores_good_cycles() -> None:
    bus = EventBus()
    state = RuntimeState(weights={"ma_crossover": 0.4, "price_action": 0.4, "volume": 0.2})
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO, bus=bus)
    tuner = TuningAgent(
        complete_fn=make_completer({"adjust": True, "emphasis": "volume", "reason": "x"})
    )
    reg = AgentRegistry()
    reg.register(tuner)
    reg.install(bus, AgentContext(bus=bus, gateway=gw, state=state))

    await bus.publish(CycleJudged(cycle=1, quality="good", summary="fine", by="analyst"))
    assert gw.audit == []  # good cycle -> no action requested


async def test_tuner_declines_when_llm_says_no() -> None:
    bus = EventBus()
    state = RuntimeState(weights={"ma_crossover": 0.4, "price_action": 0.4, "volume": 0.2})
    gw = ActionGateway(state=state, policy=ApprovalPolicy.AUTO, bus=bus)
    tuner = TuningAgent(
        complete_fn=make_completer(
            {"adjust": False, "emphasis": "balanced", "reason": "noise"}
        )
    )
    reg = AgentRegistry()
    reg.register(tuner)
    reg.install(bus, AgentContext(bus=bus, gateway=gw, state=state))

    await bus.publish(CycleJudged(cycle=1, quality="bad", summary="bad", by="analyst"))
    assert gw.audit == []  # LLM declined -> no change
