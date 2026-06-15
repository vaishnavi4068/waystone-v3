"""Append-only dispatch audit log."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from waystone3.alerts.models import Alert, Recipient


@dataclass
class DispatchRecord:
    id: int
    title: str
    severity: str
    recipient: str
    channel: str
    delivered: bool


class AuditLog:
    def __init__(self) -> None:
        self.records: list[DispatchRecord] = []
        self._ids = itertools.count(1)

    def record(self, alert: Alert, recipient: Recipient, delivered: bool) -> DispatchRecord:
        rec = DispatchRecord(
            id=next(self._ids),
            title=alert.title,
            severity=alert.severity.value,
            recipient=recipient.name,
            channel=recipient.channel,
            delivered=delivered,
        )
        self.records.append(rec)
        return rec
