from __future__ import annotations

from decimal import Decimal

from waystone3.agents.base import AgentContext
from waystone3.agents.notifier import NotifierAgent
from waystone3.agents.registry import AgentRegistry
from waystone3.alerts.channels import LogChannel, TwilioSmsChannel
from waystone3.alerts.models import Alert, Role, Severity
from waystone3.alerts.recipients import RecipientStore
from waystone3.alerts.router import AlertRouter
from waystone3.bus.bus import EventBus
from waystone3.bus.events import AlertRaised, OrderBlocked


def _router() -> tuple[AlertRouter, RecipientStore]:
    store = RecipientStore()
    router = AlertRouter(channels={"log": LogChannel()}, store=store)
    return router, store


def test_recipient_crud() -> None:
    store = RecipientStore()
    r = store.create("Manoj", Role.TRADER, "log")
    assert store.get(r.id) is r
    assert store.update(r.id, name="M2").name == "M2"
    assert len(store.list_all()) == 1
    assert store.delete(r.id) is True
    assert store.get(r.id) is None


def test_severity_threshold_matching() -> None:
    store = RecipientStore()
    store.create("warn-trader", Role.TRADER, "log", min_severity=Severity.WARN)
    store.create("crit-trader", Role.TRADER, "log", min_severity=Severity.CRITICAL)
    info = Alert(Severity.INFO, Role.TRADER, "t", "b")
    crit = Alert(Severity.CRITICAL, Role.TRADER, "t", "b")
    assert store.for_alert(info) == []  # neither wants INFO
    assert len(store.for_alert(crit)) == 2  # both want CRITICAL


def test_role_routing() -> None:
    store = RecipientStore()
    store.create("trader", Role.TRADER, "log", min_severity=Severity.INFO)
    store.create("eng", Role.ENGINEER, "log", min_severity=Severity.INFO)
    alert = Alert(Severity.INFO, Role.TRADER, "t", "b")
    matched = store.for_alert(alert)
    assert [r.name for r in matched] == ["trader"]


async def test_router_dispatch_and_audit() -> None:
    router, store = _router()
    store.create("trader", Role.TRADER, "log", min_severity=Severity.INFO)
    results = await router.dispatch(Alert(Severity.INFO, Role.TRADER, "hello", "world"))
    assert results and results[0][1] is True
    assert len(router.audit.records) == 1
    assert router.audit.records[0].delivered is True


async def test_router_dedupes_repeated_alert() -> None:
    router, store = _router()
    store.create("trader", Role.TRADER, "log", min_severity=Severity.INFO)
    alert = Alert(Severity.WARN, Role.TRADER, "dupe", "x")
    await router.dispatch(alert)
    await router.dispatch(alert)  # same (title, recipient) -> skipped
    assert len(router.audit.records) == 1


async def test_unconfigured_twilio_degrades_not_raises() -> None:
    store = RecipientStore()
    router = AlertRouter(channels={"sms": TwilioSmsChannel("", "", "")}, store=store)
    store.create("trader", Role.TRADER, "sms", contact="+1555", min_severity=Severity.INFO)
    results = await router.dispatch(Alert(Severity.INFO, Role.TRADER, "t", "b"))
    assert results[0][1] is False  # not delivered, but no exception
    assert router.audit.records[0].delivered is False


async def test_notifier_agent_bridges_events_to_router() -> None:
    bus = EventBus()
    router, store = _router()
    store.create("trader", Role.TRADER, "log", min_severity=Severity.INFO)
    store.create("eng", Role.ENGINEER, "log", min_severity=Severity.INFO)

    reg = AgentRegistry()
    reg.register(NotifierAgent(router=router))
    reg.install(bus, AgentContext(bus=bus))

    # an explicit alert for traders
    await bus.publish(
        AlertRaised(severity="info", role="trader", title="news", body="up")
    )
    # a risk block also maps to a trader WARN alert
    await bus.publish(OrderBlocked("AAPL", "buy", Decimal(1), "cap exceeded"))

    titles = {r.title for r in router.audit.records}
    assert "news" in titles
    assert any("Order blocked" in t for t in titles)
