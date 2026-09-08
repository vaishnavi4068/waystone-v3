"""Delivery channels.

`LogChannel` always works (zero config). The Twilio channels are key-optional: without
credentials (or without the `twilio` package installed) they degrade to a logged no-op
and report `delivered=False` rather than raising — so the platform runs end-to-end with no
external accounts, and lights up when configured. `WhatsAppGroupChannel` follows the same
key-optional contract but posts to a *group* chat via an HTTP gateway.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import httpx
import structlog

from waystone3.alerts.models import Alert, Recipient

log = structlog.get_logger()


@runtime_checkable
class Channel(Protocol):
    name: str

    async def send(self, alert: Alert, recipient: Recipient) -> bool: ...


class LogChannel:
    name = "log"

    async def send(self, alert: Alert, recipient: Recipient) -> bool:
        log.info(
            "alert",
            severity=alert.severity.value,
            title=alert.title,
            to=recipient.name,
            body=alert.body,
        )
        return True


class _TwilioBase:
    name = "twilio"
    _prefix = ""  # "" for SMS, "whatsapp:" for WhatsApp

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
    ) -> None:
        self.account_sid = account_sid or os.getenv("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.getenv("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.getenv("TWILIO_FROM_NUMBER", "")

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)

    async def send(self, alert: Alert, recipient: Recipient) -> bool:
        if not self.configured or not recipient.contact:
            log.info(
                "alert_not_sent_unconfigured",
                channel=self.name,
                title=alert.title,
                to=recipient.name,
            )
            return False
        try:
            from twilio.rest import Client  # type: ignore[import-not-found]
        except ImportError:
            log.warning("twilio_not_installed", channel=self.name)
            return False
        client = Client(self.account_sid, self.auth_token)
        client.messages.create(
            from_=f"{self._prefix}{self.from_number}",
            to=f"{self._prefix}{recipient.contact}",
            body=f"[{alert.severity.value.upper()}] {alert.title}: {alert.body}",
        )
        return True


class TwilioSmsChannel(_TwilioBase):
    name = "sms"
    _prefix = ""


class TwilioWhatsAppChannel(_TwilioBase):
    name = "whatsapp"
    _prefix = "whatsapp:"


class WhatsAppGroupChannel:
    """Post alerts into a WhatsApp *group* chat via an HTTP gateway.

    Twilio and the official WhatsApp Cloud API can only message individual numbers — they
    cannot post to a group chat. Posting to a real group needs a group-capable gateway
    (e.g. Whapi.cloud, Maytapi, or a self-hosted whatsapp-web.js bridge) that addresses the
    group by its id/JID (the ``<digits>@g.us`` string). This channel speaks the common JSON
    shape those gateways expose and is key-optional: with no url/token it degrades to a
    logged no-op (``delivered=False``) instead of raising.

    Variables you pass — constructor args, each with an env-var fallback:
      - ``api_url``    / ``WHATSAPP_GROUP_API_URL``    — the gateway's send-message endpoint
      - ``api_token``  / ``WHATSAPP_GROUP_API_TOKEN``  — bearer token for the gateway
      - ``group_id``   / ``WHATSAPP_GROUP_ID``         — target group id/JID to post into
      - ``group_name`` / ``WHATSAPP_GROUP_NAME``       — human label, used only in logs

    Gateways disagree on JSON key names, so ``group_field``/``message_field`` are
    overridable (defaults ``"group_id"`` / ``"message"``). A Recipient with
    ``channel="whatsapp_group"`` may set ``contact`` to a different group id to fan the same
    alert into another group.
    """

    name = "whatsapp_group"

    def __init__(
        self,
        api_url: str | None = None,
        api_token: str | None = None,
        group_id: str | None = None,
        group_name: str | None = None,
        *,
        group_field: str = "group_id",
        message_field: str = "message",
    ) -> None:
        self.api_url: str = api_url or os.getenv("WHATSAPP_GROUP_API_URL") or ""
        self.api_token: str = api_token or os.getenv("WHATSAPP_GROUP_API_TOKEN") or ""
        self.group_id: str = group_id or os.getenv("WHATSAPP_GROUP_ID") or ""
        self.group_name: str = group_name or os.getenv("WHATSAPP_GROUP_NAME") or ""
        self.group_field = group_field
        self.message_field = message_field

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.api_token)

    async def send(self, alert: Alert, recipient: Recipient) -> bool:
        target = recipient.contact or self.group_id
        if not self.configured or not target:
            log.info(
                "alert_not_sent_unconfigured",
                channel=self.name,
                title=alert.title,
                to=recipient.name,
            )
            return False
        body = f"[{alert.severity.value.upper()}] {alert.title}: {alert.body}"
        payload = {self.group_field: target, self.message_field: body}
        headers = {"Authorization": f"Bearer {self.api_token}"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.api_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("whatsapp_group_send_failed", channel=self.name, error=str(exc))
            return False
        if not resp.is_success:
            log.warning(
                "whatsapp_group_send_rejected",
                channel=self.name,
                status=resp.status_code,
                group=self.group_name or target,
            )
            return False
        return True


class ZohoCliqChannel:
    """Post alerts into a Zoho Cliq *channel* (team group) via an incoming webhook.

    A Cliq channel exposes an "Incoming Webhook" connection whose URL already carries its
    own ``zapikey``, so posting is a single ``POST {"text": ...}`` — no separate auth step,
    no Meta/template gymnastics, real group semantics. Key-optional like the other channels:
    with no webhook url it degrades to a logged no-op (``delivered=False``).

    Variables you pass — constructor args, each with an env-var fallback:
      - ``webhook_url``  / ``ZOHO_CLIQ_WEBHOOK_URL`` — the channel's incoming-webhook URL
        (Cliq → channel settings → Connect → Incoming Webhook; the key is embedded in it)
      - ``channel_name`` / ``ZOHO_CLIQ_CHANNEL``     — human label, used only in logs
      - ``oauth_token``  / ``ZOHO_CLIQ_OAUTH_TOKEN`` — optional; only if you target the REST
        API (``.../message``) instead of a key-bearing webhook url

    Per-recipient override: a Recipient with ``channel="cliq"`` may set ``contact`` to a
    different channel's webhook url to fan the same alert into another Cliq channel.
    """

    name = "cliq"

    def __init__(
        self,
        webhook_url: str | None = None,
        channel_name: str | None = None,
        oauth_token: str | None = None,
    ) -> None:
        self.webhook_url: str = webhook_url or os.getenv("ZOHO_CLIQ_WEBHOOK_URL") or ""
        self.channel_name: str = channel_name or os.getenv("ZOHO_CLIQ_CHANNEL") or ""
        self.oauth_token: str = oauth_token or os.getenv("ZOHO_CLIQ_OAUTH_TOKEN") or ""

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, alert: Alert, recipient: Recipient) -> bool:
        url = recipient.contact or self.webhook_url
        if not url:
            log.info(
                "alert_not_sent_unconfigured",
                channel=self.name,
                title=alert.title,
                to=recipient.name,
            )
            return False
        text = f"[{alert.severity.value.upper()}] {alert.title}: {alert.body}"
        headers = (
            {"Authorization": f"Zoho-oauthtoken {self.oauth_token}"}
            if self.oauth_token
            else None
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json={"text": text}, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("cliq_send_failed", channel=self.name, error=str(exc))
            return False
        if not resp.is_success:
            log.warning(
                "cliq_send_rejected",
                channel=self.name,
                status=resp.status_code,
                cliq_channel=self.channel_name or recipient.name,
            )
            return False
        return True


class GrokBotChannel:
    """Wake a Grok Bot webhook routine with a status JSON body.

    Create a routine in the Grok Bot desktop app (trigger: when a webhook fires).
    Copy the POST URL and sender key (``crsr_…``) from the trigger card — not shown on iOS.
    Key-optional: with no URL/key this degrades to a logged no-op.

    Env: ``GROK_BOT_WEBHOOK_URL``, ``GROK_BOT_WEBHOOK_KEY`` (or ``GROK_BOT_SENDER_KEY``).
    """

    name = "grok_bot"

    def __init__(
        self,
        webhook_url: str | None = None,
        sender_key: str | None = None,
    ) -> None:
        self.webhook_url: str = webhook_url or os.getenv("GROK_BOT_WEBHOOK_URL") or ""
        self.sender_key: str = (
            sender_key
            or os.getenv("GROK_BOT_WEBHOOK_KEY")
            or os.getenv("GROK_BOT_SENDER_KEY")
            or ""
        )

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url and self.sender_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.sender_key}",
            "X-Automation-Key": self.sender_key,
            "Content-Type": "application/json",
        }

    def post_sync(self, payload: dict[str, object]) -> bool:
        if not self.configured:
            log.info("alert_not_sent_unconfigured", channel=self.name, title="grok_bot")
            return False
        try:
            resp = httpx.post(
                self.webhook_url, json=payload, headers=self._headers(), timeout=10
            )
        except httpx.HTTPError as exc:
            log.warning("grok_bot_send_failed", error=str(exc))
            return False
        if not resp.is_success:
            log.warning("grok_bot_send_rejected", status=resp.status_code)
            return False
        return True

    async def send(self, alert: Alert, recipient: Recipient) -> bool:
        del recipient
        if not self.configured:
            log.info("alert_not_sent_unconfigured", channel=self.name, title=alert.title)
            return False
        payload: dict[str, object] = {
            "event": "waystone.alert",
            "severity": alert.severity.value,
            "title": alert.title,
            "body": alert.body,
            "source": "waystone3",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self.webhook_url, json=payload, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            log.warning("grok_bot_send_failed", error=str(exc))
            return False
        if not resp.is_success:
            log.warning("grok_bot_send_rejected", status=resp.status_code)
            return False
        return True
