"""Default research window: five years, or whatever history exists."""

from __future__ import annotations

from datetime import date


def default_window(years: int = 5) -> tuple[str, str]:
    end = date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    return start.isoformat(), end.isoformat()
