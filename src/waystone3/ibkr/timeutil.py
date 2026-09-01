"""America/New_York calendar helpers for daily dumps."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def today_ny(now: datetime | None = None) -> date:
    clock = now if now is not None else datetime.now(NY)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    return clock.astimezone(NY).date()


def as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=NY)
    return value


def ny_date(value: datetime) -> date:
    return as_aware(value).astimezone(NY).date()


def parse_ib_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return as_aware(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text)
            return as_aware(parsed)
        except ValueError:
            pass
        for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=NY)
            except ValueError:
                continue
    return datetime.now(NY)
