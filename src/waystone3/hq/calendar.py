"""Trading-day calendar and the workbook's session-date rule."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def is_trading_day(day: date, holidays: Collection[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def next_trading_day(day: date, holidays: Collection[date]) -> date:
    nxt = day + timedelta(days=1)
    while not is_trading_day(nxt, holidays):
        nxt += timedelta(days=1)
    return nxt


def session_date_for(ts: datetime, roll_hour: int, holidays: Collection[date]) -> date:
    """Exits at/after the roll hour (18:00 ET) count toward the next trading day."""
    local = ts.astimezone(NY)
    day = local.date()
    if local.hour >= roll_hour or not is_trading_day(day, holidays):
        return next_trading_day(day, holidays)
    return day


def trading_days(start: date, end: date, holidays: Collection[date]) -> Iterator[date]:
    day = start
    while day <= end:
        if is_trading_day(day, holidays):
            yield day
        day += timedelta(days=1)


def week_end(day: date) -> date:
    return day + timedelta(days=4 - day.weekday())


def month_start(day: date) -> date:
    return day.replace(day=1)
