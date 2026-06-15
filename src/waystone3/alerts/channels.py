"""Delivery channels.

`LogChannel` always works (zero config). The Twilio channels are key-optional: without
credentials (or without the `twilio` package installed) they degrade to a logged no-op
and report `delivered=False` rather than raising — so the platform runs end-to-end with no
external accounts, and lights up when configured.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

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
