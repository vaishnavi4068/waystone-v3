"""In-memory recipient store with CRUD and alert-matching."""

from __future__ import annotations

import itertools

from waystone3.alerts.models import Alert, Recipient, Role, Severity, severity_rank


class RecipientStore:
    def __init__(self) -> None:
        self._by_id: dict[int, Recipient] = {}
        self._ids = itertools.count(1)

    def create(
        self,
        name: str,
        role: Role,
        channel: str,
        contact: str = "",
        min_severity: Severity = Severity.WARN,
    ) -> Recipient:
        rec = Recipient(
            id=next(self._ids),
            name=name,
            role=role,
            channel=channel,
            contact=contact,
            min_severity=min_severity,
        )
        self._by_id[rec.id] = rec
        return rec

    def get(self, recipient_id: int) -> Recipient | None:
        return self._by_id.get(recipient_id)

    def list_all(self) -> list[Recipient]:
        return list(self._by_id.values())

    def update(self, recipient_id: int, **changes: object) -> Recipient | None:
        rec = self._by_id.get(recipient_id)
        if rec is None:
            return None
        for key, value in changes.items():
            if hasattr(rec, key):
                setattr(rec, key, value)
        return rec

    def delete(self, recipient_id: int) -> bool:
        return self._by_id.pop(recipient_id, None) is not None

    def for_alert(self, alert: Alert) -> list[Recipient]:
        """Recipients whose role matches and whose threshold the severity clears."""
        threshold = severity_rank(alert.severity)
        return [
            r
            for r in self._by_id.values()
            if r.role == alert.role and threshold >= severity_rank(r.min_severity)
        ]
