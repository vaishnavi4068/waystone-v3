"""Alert router — fan an alert out to matching recipients over their channels.

Dedupes identical (title, recipient) pairs so a repeating condition doesn't spam, and
records every dispatch attempt (delivered or not) to the audit log.
"""

from __future__ import annotations

import structlog

from waystone3.alerts.audit import AuditLog
from waystone3.alerts.channels import Channel
from waystone3.alerts.models import Alert, Recipient
from waystone3.alerts.recipients import RecipientStore

log = structlog.get_logger()


class AlertRouter:
    def __init__(
        self,
        channels: dict[str, Channel],
        store: RecipientStore,
        audit: AuditLog | None = None,
    ) -> None:
        self.channels = channels
        self.store = store
        self.audit = audit or AuditLog()
        self._seen: set[tuple[str, int]] = set()

    async def dispatch(self, alert: Alert) -> list[tuple[Recipient, bool]]:
        results: list[tuple[Recipient, bool]] = []
        for recipient in self.store.for_alert(alert):
            key = (alert.title, recipient.id)
            if key in self._seen:
                continue
            self._seen.add(key)
            channel = self.channels.get(recipient.channel)
            delivered = await channel.send(alert, recipient) if channel is not None else False
            if channel is None:
                log.warning("no_channel", channel=recipient.channel, to=recipient.name)
            self.audit.record(alert, recipient, delivered)
            results.append((recipient, delivered))
        return results
