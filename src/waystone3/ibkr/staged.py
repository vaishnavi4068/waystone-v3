"""Staged sample week (10–14 Aug 2026). Source labels stay as-is."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

STAGED_WEEK_START = date(2026, 8, 10)
STAGED_WEEK_END = date(2026, 8, 14)
STAGED_WEEK_DAYS: tuple[date, ...] = (
    date(2026, 8, 10),
    date(2026, 8, 11),
    date(2026, 8, 12),
    date(2026, 8, 13),
    date(2026, 8, 14),
)
STAGED_WEEK_LABEL = "week of 10 Aug 2026"
STAGED_ISO_WEEK = "2026-W33"

# Placeholder numbers for MANUAL KPI rows (source column stays MANUAL).
STAGED_OPTIONS_MANUAL: dict[str, float] = {
    "pbo": 18.0,
    "trial_log": 1.0,
    "net_vega": 0.08,
    "net_delta": 7.0,
    "incubation_months": 3.0,
    "incubation_trades": 55.0,
    "slippage_ratio_inc": 1.1,
    "incubation_sharpe_ratio": 0.75,
    "ops_errors": 0.5,
    "kill_switch": 1.0,
    "dd_stop": 1.0,
    "missed_fills": 0.0,
    "missed_fills_pct": 3.0,
    "avg_slippage_pct": 1.5,
    "slippage_ratio": 1.1,
}

STAGED_FUTURES_MANUAL: dict[str, float] = {
    "n_trials": 12.0,
    "deflated_sharpe": 0.96,
    "param_robustness": 0.82,
    "information_ratio": 0.8,
    "time_in_market": 0.45,
    "slippage_realism": 1.05,
    "capacity": 30_000_000.0,
    "roll_cost_drag": 0.06,
}


def is_staged_day(day: date | str | None) -> bool:
    if day is None:
        return False
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day[:10])
        except ValueError:
            return False
    return day in STAGED_WEEK_DAYS


def staged_meta(day: date | str | None = None, *, any_staged: bool = False) -> dict[str, object]:
    flagged = any_staged or is_staged_day(day)
    return {
        "staged": flagged,
        "staged_week": STAGED_WEEK_LABEL if flagged else None,
        "staged_days": [d.isoformat() for d in STAGED_WEEK_DAYS] if flagged else [],
        "staged_iso_week": STAGED_ISO_WEEK if flagged else None,
    }


def apply_staged_manual(
    values: dict[str, float | None], extras: dict[str, float]
) -> dict[str, float | None]:
    out = dict(values)
    for key, value in extras.items():
        if out.get(key) is None:
            out[key] = value
    return out


_STAGED_STORE = None


def staged_fixture_store():
    """In-memory-on-disk dump for the staged week + 40 weekdays (cached)."""
    global _STAGED_STORE
    if _STAGED_STORE is None:
        import tempfile

        from waystone3.ibkr.demo import seed_demo
        from waystone3.ibkr.store import LocalFsStore

        root = Path(tempfile.mkdtemp(prefix="waystone-staged-"))
        seed_demo(root, STAGED_WEEK_END, history_days=40)
        _STAGED_STORE = LocalFsStore(root)
    return _STAGED_STORE


class OverlayStore:
    """Read primary first; fall back to the staged fixture so KPI pages always have the week."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self.primary = primary
        self.fallback = fallback

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.primary.put(key, data, content_type)

    def get(self, key: str) -> bytes | None:
        if self._staged_key(key) and self.fallback.exists(key):
            return self.fallback.get(key)
        if self.primary.exists(key):
            return self.primary.get(key)
        return self.fallback.get(key)

    def exists(self, key: str) -> bool:
        return self.primary.exists(key) or self.fallback.exists(key)

    @staticmethod
    def _staged_key(key: str) -> bool:
        return any(f"dt={d.isoformat()}" in key for d in STAGED_WEEK_DAYS)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(set(self.primary.list_keys(prefix)) | set(self.fallback.list_keys(prefix)))
