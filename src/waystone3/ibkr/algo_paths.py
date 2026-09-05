"""GCS (and local-mirror) keys for per-algo live paper vs replay blotters."""

from __future__ import annotations

import re
from datetime import date

ALGOS_PREFIX = "algos/v1"
REGISTRY_KEY = f"{ALGOS_PREFIX}/registry.json"
_SUCCESS = re.compile(
    rf"^{re.escape(ALGOS_PREFIX)}/([^/]+)/(live|replay)/dt=(\d{{4}}-\d{{2}}-\d{{2}})/_SUCCESS$"
)


def default_live_prefix(algo_id: str) -> str:
    return f"{ALGOS_PREFIX}/{algo_id}/live"


def default_replay_prefix(algo_id: str) -> str:
    return f"{ALGOS_PREFIX}/{algo_id}/replay"


def blotter_prefix(root: str, day: date | str) -> str:
    ds = day if isinstance(day, str) else day.isoformat()
    return f"{root.rstrip('/')}/dt={ds}"


def blotter_success_key(root: str, day: date | str) -> str:
    return f"{blotter_prefix(root, day)}/_SUCCESS"


def blotter_executions_key(root: str, day: date | str) -> str:
    return f"{blotter_prefix(root, day)}/executions.jsonl"


def blotter_summary_key(root: str, day: date | str) -> str:
    return f"{blotter_prefix(root, day)}/summary.json"


def day_from_algo_success(key: str) -> tuple[str, str, date] | None:
    match = _SUCCESS.match(key)
    if match is None:
        return None
    return match.group(1), match.group(2), date.fromisoformat(match.group(3))
