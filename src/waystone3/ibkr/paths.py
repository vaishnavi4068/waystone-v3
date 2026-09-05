"""GCS (and local-mirror) key layout for a daily IBKR dump."""

from __future__ import annotations

import re
from datetime import date

PREFIX = "ibkr/v1"
_DAY_SUCCESS = re.compile(rf"^{re.escape(PREFIX)}/dt=(\d{{4}}-\d{{2}}-\d{{2}})/_SUCCESS$")


def prefix_for(day: date | str) -> str:
    ds = day if isinstance(day, str) else day.isoformat()
    return f"{PREFIX}/dt={ds}"


def success_key(day: date | str) -> str:
    return f"{prefix_for(day)}/_SUCCESS"


def executions_key(day: date | str) -> str:
    return f"{prefix_for(day)}/executions.jsonl"


def positions_key(day: date | str) -> str:
    return f"{prefix_for(day)}/positions.json"


def account_key(day: date | str) -> str:
    return f"{prefix_for(day)}/account.json"


def summary_key(day: date | str) -> str:
    return f"{prefix_for(day)}/summary.json"


def manifest_key(day: date | str) -> str:
    return f"{prefix_for(day)}/_manifest.json"


def day_from_success_key(key: str) -> date | None:
    match = _DAY_SUCCESS.match(key)
    if match is None:
        return None
    return date.fromisoformat(match.group(1))
