"""Agent OS — wires the trading core to the reactive control plane.

This is the only module that depends on *both* the core (runner/broker/data) and the
agent layer (bus/agents/alerts). It runs N cycles, publishing events to the bus after each
one; agents observe and (when allowed) act through the gateway, mutating ``RuntimeState``
that the next cycle reads. The core ``run_cycle`` never imports any of this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from waystone3.agents.actions import ActionGateway, ApprovalPolicy, RuntimeState
from waystone3.agents.analyst import AnalystAgent
from waystone3.agents.base import AgentContext
from waystone3.agents.claude import Completer
from waystone3.agents.logging_agent import LoggingAgent
from waystone3.agents.notifier import NotifierAgent
from waystone3.agents.registry import AgentRegistry
from waystone3.agents.supervisor import RiskSupervisorAgent
from waystone3.agents.tuner import TuningAgent
from waystone3.alerts.audit import DispatchRecord
from waystone3.alerts.channels import LogChannel, TwilioSmsChannel, TwilioWhatsAppChannel
from waystone3.alerts.recipients import RecipientStore
from waystone3.alerts.router import AlertRouter
from waystone3.brokers.base import Broker
from waystone3.bus.bus import EventBus
from waystone3.bus.events import CycleJudged, Event
from waystone3.core.types import Timeframe
from waystone3.data.base import MarketDataSource
from waystone3.decision.engine import DecisionConfig, DecisionEngine
from waystone3.risk.guard import RiskGuard
from waystone3.runner.config import default_contributors, default_weights
from waystone3.runner.cycle import run_cycle


@dataclass
class AgentOSResult:
    cycles: int
    total_orders: int
    final_weights: dict[str, float]
    trading_enabled: bool
    actions: list[tuple[str, str, str]]  # (kind, detail, status)
    alerts: list[DispatchRecord]
    judgements: list[tuple[int, str, str]]  # (cycle, quality, summary)


@dataclass
class AgentOS:
    bus: EventBus
    state: RuntimeState
    gateway: ActionGateway
    router: AlertRouter
    registry: AgentRegistry
    recipients: RecipientStore = field(default_factory=RecipientStore)


def build_agent_os(
    *,
    is_paper: bool,
    policy: ApprovalPolicy = ApprovalPolicy.AUTO,
    completer: Completer | None = None,
    recipients: RecipientStore | None = None,
) -> AgentOS:
    """Assemble the control plane: bus, state, gateway, alerts, and the agent roster."""
    bus = EventBus()
    state = RuntimeState(weights=default_weights(), is_paper=is_paper)
    gateway = ActionGateway(state=state, policy=policy, bus=bus)

    store = recipients or RecipientStore()
    router = AlertRouter(
        channels={
            "log": LogChannel(),
            "sms": TwilioSmsChannel(),
            "whatsapp": TwilioWhatsAppChannel(),
        },
        store=store,
    )

    registry = AgentRegistry()
    registry.register(LoggingAgent())
    registry.register(RiskSupervisorAgent())
    registry.register(NotifierAgent(router=router))
    # Claude agents: key-optional. With no key and no completer they self-disable.
    registry.register(AnalystAgent(complete_fn=completer))
    registry.register(TuningAgent(complete_fn=completer))

    return AgentOS(
        bus=bus,
        state=state,
        gateway=gateway,
        router=router,
        registry=registry,
        recipients=store,
    )


async def serve(
    *,
    data: MarketDataSource,
    broker: Broker,
    watchlist: list[str],
    cycles: int = 3,
    timeframe: Timeframe = Timeframe.D1,
    lookback: int = 80,
    base_notional: Decimal = Decimal(2000),
    policy: ApprovalPolicy = ApprovalPolicy.AUTO,
    completer: Completer | None = None,
    recipients: RecipientStore | None = None,
) -> AgentOSResult:
    os_ = build_agent_os(
        is_paper=broker.is_paper, policy=policy, completer=completer, recipients=recipients
    )

    judgements: list[tuple[int, str, str]] = []

    async def collect_judgement(event: Event) -> None:
        if isinstance(event, CycleJudged):
            judgements.append((event.cycle, event.quality, event.summary))

    os_.bus.subscribe(CycleJudged, collect_judgement, name="collector")
    os_.registry.install(
        os_.bus, AgentContext(bus=os_.bus, gateway=os_.gateway, state=os_.state)
    )

    guard = RiskGuard(is_paper=broker.is_paper)
    total_orders = 0
    for cycle in range(cycles):
        scaled = (base_notional * os_.state.exposure_scale).quantize(Decimal(1))
        engine = DecisionEngine(DecisionConfig(notional_per_trade=scaled))
        report = await run_cycle(
            data=data,
            broker=broker,
            guard=guard,
            contributors=default_contributors(),
            weights=os_.state.weights,
            engine=engine,
            watchlist=watchlist,
            timeframe=timeframe,
            bars_lookback=lookback,
            bus=os_.bus,
            cycle=cycle,
            trading_enabled=os_.state.trading_enabled,
        )
        total_orders += len(report.orders)

    return AgentOSResult(
        cycles=cycles,
        total_orders=total_orders,
        final_weights=dict(os_.state.weights),
        trading_enabled=os_.state.trading_enabled,
        actions=[(a.kind, a.detail, a.status) for a in os_.gateway.audit],
        alerts=list(os_.router.audit.records),
        judgements=judgements,
    )
