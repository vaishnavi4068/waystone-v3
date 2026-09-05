"""Load the checked-in research catalog."""

from __future__ import annotations

import json
from typing import Any

from waystone3.research.paths import toolkit_root


def load_catalog() -> dict[str, Any]:
    path = toolkit_root() / "catalog.json"
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or "strategies" not in payload:
        raise ValueError(f"invalid catalog at {path}")
    return payload


def list_strategies() -> list[dict[str, Any]]:
    return list(load_catalog()["strategies"])


def get_strategy(strategy_id: str) -> dict[str, Any] | None:
    for row in list_strategies():
        if row.get("id") == strategy_id:
            return row
    return None


def needed_daily_symbols(strategy_id: str | None = None) -> list[str]:
    rows = [get_strategy(strategy_id)] if strategy_id else list_strategies()
    seen: list[str] = []
    for row in rows:
        if not row:
            continue
        for sym in row.get("daily_symbols") or []:
            if sym not in seen:
                seen.append(sym)
    return seen


def needed_intraday_roots(strategy_id: str | None = None) -> list[str]:
    rows = [get_strategy(strategy_id)] if strategy_id else list_strategies()
    seen: list[str] = []
    for row in rows:
        if not row:
            continue
        for root in row.get("intraday_roots") or []:
            if root not in seen:
                seen.append(root)
    return seen
