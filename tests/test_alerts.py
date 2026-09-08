from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from waystone3.agents.base import AgentContext
from waystone3.agents.notifier import NotifierAgent
from waystone3.agents.registry import AgentRegistry
from waystone3.alerts import channels as channels_mod
from waystone3.alerts.channels import (
    GrokBotChannel,
    LogChannel,
    TwilioSmsChannel,
    WhatsAppGroupChannel,
    ZohoCliqChannel,
)
from waystone3.alerts.models import Alert, Recipient, Role, Severity
from waystone3.alerts.recipients import RecipientStore
from waystone3.alerts.router import AlertRouter
from waystone3.bus.bus import EventBus
from waystone3.bus.events import AlertRaised, OrderBlocked, OrderFilled, StrategySubmitted


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


class _FakeResp:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300


class _FakeClient:
    """Stand-in for httpx.AsyncClient that records the last POST."""

    captured: ClassVar[dict[str, object]] = {}
    status_code: ClassVar[int] = 200

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(
        self, url: str, json: object = None, headers: object = None
    ) -> _FakeResp:
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeResp(_FakeClient.status_code)


async def test_unconfigured_whatsapp_group_degrades_not_raises() -> None:
    channel = WhatsAppGroupChannel(api_url="", api_token="")
    rec = Recipient(
        id=1, name="Traders group", role=Role.TRADER,
        channel="whatsapp_group", contact="12036000@g.us",
    )
    delivered = await channel.send(Alert(Severity.WARN, Role.TRADER, "t", "b"), rec)
    assert delivered is False  # no url/token -> logged no-op, no exception


async def test_whatsapp_group_posts_to_gateway(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 200
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = WhatsAppGroupChannel(
        api_url="https://gateway.example/send",
        api_token="tok-123",
        group_id="12036000@g.us",
        group_name="Waystone Traders",
    )
    rec = Recipient(id=1, name="grp", role=Role.TRADER, channel="whatsapp_group")
    delivered = await channel.send(
        Alert(Severity.WARN, Role.TRADER, "Order filled", "BUY 1 NVDA @ 100"), rec
    )
    assert delivered is True
    assert _FakeClient.captured["url"] == "https://gateway.example/send"
    assert _FakeClient.captured["json"] == {
        "group_id": "12036000@g.us",
        "message": "[WARN] Order filled: BUY 1 NVDA @ 100",
    }
    assert _FakeClient.captured["headers"] == {"Authorization": "Bearer tok-123"}  # type: ignore[index]


async def test_whatsapp_group_recipient_contact_overrides_group(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 200
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = WhatsAppGroupChannel(
        api_url="https://gateway.example/send", api_token="tok", group_id="default@g.us"
    )
    rec = Recipient(
        id=2, name="other", role=Role.TRADER,
        channel="whatsapp_group", contact="99999@g.us",
    )
    await channel.send(Alert(Severity.WARN, Role.TRADER, "t", "b"), rec)
    assert _FakeClient.captured["json"]["group_id"] == "99999@g.us"  # type: ignore[index]


async def test_whatsapp_group_non_2xx_reports_not_delivered(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 500
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = WhatsAppGroupChannel(
        api_url="https://gateway.example/send", api_token="tok", group_id="g@g.us"
    )
    rec = Recipient(id=3, name="grp", role=Role.TRADER, channel="whatsapp_group")
    delivered = await channel.send(Alert(Severity.WARN, Role.TRADER, "t", "b"), rec)
    assert delivered is False


async def test_unconfigured_cliq_degrades_not_raises() -> None:
    channel = ZohoCliqChannel(webhook_url="")
    rec = Recipient(id=1, name="Team channel", role=Role.TRADER, channel="cliq")
    delivered = await channel.send(Alert(Severity.WARN, Role.TRADER, "t", "b"), rec)
    assert delivered is False  # no webhook -> logged no-op, no exception


async def test_cliq_posts_to_webhook(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 200
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = ZohoCliqChannel(
        webhook_url="https://cliq.zoho.com/api/v2/channelsbyname/traders/message?zapikey=K",
        channel_name="Waystone Traders",
    )
    rec = Recipient(id=1, name="grp", role=Role.TRADER, channel="cliq")
    delivered = await channel.send(
        Alert(Severity.INFO, Role.TRADER, "Strategy run", "Mark: Momentum-v2 P&L +$240"),
        rec,
    )
    assert delivered is True
    assert _FakeClient.captured["url"].endswith("zapikey=K")  # type: ignore[union-attr]
    assert _FakeClient.captured["json"] == {
        "text": "[INFO] Strategy run: Mark: Momentum-v2 P&L +$240"
    }
    assert _FakeClient.captured["headers"] is None  # key in url -> no auth header


async def test_cliq_recipient_contact_overrides_webhook(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 200
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = ZohoCliqChannel(webhook_url="https://cliq.zoho.com/default?zapikey=A")
    rec = Recipient(
        id=2, name="other", role=Role.TRADER,
        channel="cliq", contact="https://cliq.zoho.com/other?zapikey=B",
    )
    await channel.send(Alert(Severity.WARN, Role.TRADER, "t", "b"), rec)
    assert _FakeClient.captured["url"] == "https://cliq.zoho.com/other?zapikey=B"


async def test_cliq_oauth_token_sets_header(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 200
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = ZohoCliqChannel(
        webhook_url="https://cliq.zoho.com/api/v2/channelsbyname/traders/message",
        oauth_token="tok-xyz",
    )
    rec = Recipient(id=3, name="grp", role=Role.TRADER, channel="cliq")
    await channel.send(Alert(Severity.WARN, Role.TRADER, "t", "b"), rec)
    assert _FakeClient.captured["headers"] == {"Authorization": "Zoho-oauthtoken tok-xyz"}


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


class _CaptureChannel:
    """Records every Alert it is asked to send (the audit log keeps only the title)."""

    name = "capture"

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert, recipient: Recipient) -> bool:
        self.alerts.append(alert)
        return True


def _notifier_with_capture() -> tuple[EventBus, _CaptureChannel]:
    cap = _CaptureChannel()
    store = RecipientStore()
    store.create("Team channel", Role.TRADER, "capture", min_severity=Severity.INFO)
    router = AlertRouter(channels={"capture": cap}, store=store)
    bus = EventBus()
    reg = AgentRegistry()
    reg.register(NotifierAgent(router=router))
    reg.install(bus, AgentContext(bus=bus))
    return bus, cap


async def test_notifier_bridges_order_filled_with_actor() -> None:
    bus, cap = _notifier_with_capture()
    await bus.publish(
        OrderFilled("NVDA", "buy", Decimal(1), Decimal(100), "ord-1", actor="Mark")
    )
    assert cap.alerts[-1].title == "Order filled: NVDA"
    assert cap.alerts[-1].body == "Mark: BUY 1 NVDA @ 100"


async def test_notifier_order_filled_without_actor_omits_prefix() -> None:
    bus, cap = _notifier_with_capture()
    await bus.publish(OrderFilled("AAPL", "sell", Decimal(2), Decimal(190), "ord-2"))
    assert cap.alerts[-1].body == "SELL 2 AAPL @ 190"  # shared/system loop -> no "who:" prefix


async def test_notifier_bridges_strategy_submitted() -> None:
    bus, cap = _notifier_with_capture()
    await bus.publish(
        StrategySubmitted(actor="Akash", summary="weights={'momentum': 1.0}", watchlist=["NVDA"])
    )
    assert cap.alerts[-1].title == "Strategy submitted by Akash"
    assert "momentum" in cap.alerts[-1].body


async def test_unconfigured_grok_bot_degrades_not_raises() -> None:
    channel = GrokBotChannel(webhook_url="", sender_key="")
    rec = Recipient(id=1, name="Grok Bot", role=Role.OPS, channel="grok_bot")
    delivered = await channel.send(Alert(Severity.INFO, Role.OPS, "t", "b"), rec)
    assert delivered is False


async def test_grok_bot_posts_webhook(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _FakeClient.captured = {}
    _FakeClient.status_code = 200
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _FakeClient)
    channel = GrokBotChannel(
        webhook_url="https://api2.cursor.sh/automations/webhook/r1",
        sender_key="crsr_test",
    )
    rec = Recipient(id=1, name="Grok Bot", role=Role.OPS, channel="grok_bot")
    delivered = await channel.send(Alert(Severity.INFO, Role.OPS, "Fetch done", "nsdq=3"), rec)
    assert delivered is True
    assert _FakeClient.captured["url"] == "https://api2.cursor.sh/automations/webhook/r1"
    body = _FakeClient.captured["json"]
    assert body["event"] == "waystone.alert"
    assert body["title"] == "Fetch done"
    headers = _FakeClient.captured["headers"]
    assert headers["Authorization"] == "Bearer crsr_test"
