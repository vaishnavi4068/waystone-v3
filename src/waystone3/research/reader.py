"""Read dated research runs from the report store (GCS or local)."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from waystone3.ibkr.store import ReportStore
from waystone3.research.catalog import get_strategy, list_strategies
from waystone3.research.paths import (
    RESEARCH_PREFIX,
    equity_key,
    latest_key,
    manifest_key,
    metrics_key,
    success_key,
)

_SUCCESS = re.compile(
    rf"^{re.escape(RESEARCH_PREFIX)}/([^/]+)/dt=(\d{{4}}-\d{{2}}-\d{{2}})/([^/]+)/_SUCCESS$"
)


def _json(store: ReportStore, key: str) -> dict[str, Any] | None:
    raw = store.get(key)
    if raw is None:
        return None
    payload = json.loads(raw.decode())
    return payload if isinstance(payload, dict) else None


def list_run_refs(store: ReportStore, strategy_id: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    prefix = f"{RESEARCH_PREFIX}/{strategy_id}/dt="
    for key in store.list_keys(prefix):
        match = _SUCCESS.match(key)
        if match and match.group(1) == strategy_id:
            refs.append((match.group(2), match.group(3)))
    return sorted(refs)


def list_days(store: ReportStore, strategy_id: str) -> list[str]:
    return sorted({day for day, _ in list_run_refs(store, strategy_id)})


def latest_ref(store: ReportStore, strategy_id: str) -> tuple[str, str] | None:
    pointer = _json(store, latest_key(strategy_id))
    if pointer and isinstance(pointer.get("date"), str):
        day = pointer["date"]
        variant = str(pointer.get("variant") or "default")
        if store.exists(success_key(strategy_id, day, variant)):
            return day, variant
    refs = list_run_refs(store, strategy_id)
    return refs[-1] if refs else None


def _equity_points(
    store: ReportStore, strategy_id: str, day: str, variant: str, limit: int = 400
) -> list[float]:
    raw = store.get(equity_key(strategy_id, day, variant))
    if raw is None:
        return []
    values: list[float] = []
    for line in raw.decode(errors="replace").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            values.append(float(parts[1]))
        except ValueError:
            continue
    if len(values) <= limit:
        return values
    step = max(1, len(values) // limit)
    return values[::step][:limit]


def load_run(
    store: ReportStore,
    strategy_id: str,
    day: str | date | None = None,
    variant: str | None = None,
) -> dict[str, Any] | None:
    if day is None or variant is None:
        ref = latest_ref(store, strategy_id)
        if ref is None:
            return None
        use_day, use_variant = ref
        if day is not None:
            use_day = day if isinstance(day, str) else day.isoformat()
            matches = [v for d, v in list_run_refs(store, strategy_id) if d == use_day]
            use_variant = variant or (matches[-1] if matches else use_variant)
        elif variant is not None:
            use_variant = variant
    else:
        use_day = day if isinstance(day, str) else day.isoformat()
        use_variant = variant
    if not store.exists(success_key(strategy_id, use_day, use_variant)):
        return None
    metrics = _json(store, metrics_key(strategy_id, use_day, use_variant)) or {}
    manifest = _json(store, manifest_key(strategy_id, use_day, use_variant)) or {}
    stats = metrics.get("stats") if isinstance(metrics.get("stats"), dict) else {}
    return {
        "date": use_day,
        "variant": use_variant,
        "run_id": manifest.get("run_id"),
        "synthetic": bool(metrics.get("synthetic") or manifest.get("synthetic")),
        "params": metrics.get("params") or {},
        "stats": stats,
        "extra": metrics.get("extra") or {},
        "manifest": manifest,
        "equity": _equity_points(store, strategy_id, use_day, use_variant),
    }


def strategy_payload(store: ReportStore | None, row: dict[str, Any]) -> dict[str, Any]:
    sid = str(row["id"])
    latest = load_run(store, sid) if store is not None else None
    days = list_days(store, sid) if store is not None else []
    return {
        "id": sid,
        "name": row.get("name"),
        "book": row.get("book"),
        "instruments": row.get("instruments"),
        "holding_period": row.get("holding_period"),
        "summary": row.get("summary"),
        "rule_sketch": row.get("rule_sketch"),
        "data_sources": row.get("data_sources") or [],
        "modes": row.get("modes") or [],
        "days": days,
        "latest": latest,
    }


def list_runs(store: ReportStore | None, strategy_id: str) -> list[dict[str, Any]]:
    """Published dated runs for GET /api/strategies/{id}/runs."""
    if store is None:
        return []
    rows: list[dict[str, Any]] = []
    for day, variant in list_run_refs(store, strategy_id):
        run = load_run(store, strategy_id, day, variant)
        if run is None:
            continue
        rows.append(
            {
                "date": run["date"],
                "variant": run["variant"],
                "run_id": run.get("run_id"),
                "synthetic": run["synthetic"],
                "stats": run["stats"],
            }
        )
    return rows


def list_strategy_payloads(store: ReportStore | None) -> list[dict[str, Any]]:
    return [strategy_payload(store, row) for row in list_strategies()]


def get_strategy_payload(
    store: ReportStore | None,
    strategy_id: str,
    day: str | None = None,
    variant: str | None = None,
) -> dict[str, Any] | None:
    row = get_strategy(strategy_id)
    if row is None:
        return None
    payload = strategy_payload(store, row)
    if store is not None and (day or variant):
        payload["latest"] = load_run(store, strategy_id, day, variant)
    return payload
