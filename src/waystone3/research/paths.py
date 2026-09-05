"""GCS / local keys for dated research backtest results."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

RESEARCH_PREFIX = "research/v1"
CATALOG_KEY = f"{RESEARCH_PREFIX}/catalog.json"
NSDQ250_PREFIX = os.getenv("WAYSTONE_NSDQ250_PREFIX", "NSDQ250").strip() or "NSDQ250"


def toolkit_root() -> Path:
    env = os.getenv("WAYSTONE_BACKTESTS_ROOT", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "waystone_backtests"
    if candidate.is_dir():
        return candidate
    cwd = Path.cwd() / "waystone_backtests"
    if cwd.is_dir():
        return cwd
    return candidate


def _day(day: date | str) -> str:
    return day if isinstance(day, str) else day.isoformat()


def strategy_day_prefix(strategy_id: str, day: date | str, variant: str = "default") -> str:
    return f"{RESEARCH_PREFIX}/{strategy_id}/dt={_day(day)}/{variant}"


def metrics_key(strategy_id: str, day: date | str, variant: str = "default") -> str:
    return f"{strategy_day_prefix(strategy_id, day, variant)}/metrics.json"


def equity_key(strategy_id: str, day: date | str, variant: str = "default") -> str:
    return f"{strategy_day_prefix(strategy_id, day, variant)}/equity.csv"


def trades_key(strategy_id: str, day: date | str, variant: str = "default") -> str:
    return f"{strategy_day_prefix(strategy_id, day, variant)}/trades.csv"


def manifest_key(strategy_id: str, day: date | str, variant: str = "default") -> str:
    return f"{strategy_day_prefix(strategy_id, day, variant)}/_manifest.json"


def success_key(strategy_id: str, day: date | str, variant: str = "default") -> str:
    return f"{strategy_day_prefix(strategy_id, day, variant)}/_SUCCESS"


def latest_key(strategy_id: str) -> str:
    return f"{RESEARCH_PREFIX}/{strategy_id}/latest.json"
