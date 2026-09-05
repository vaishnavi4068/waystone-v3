"""Built-in dated research metrics so HQ preview works before Mac publish."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from waystone3.ibkr.store import ReportStore
from waystone3.ibkr.timeutil import NY
from waystone3.research.catalog import list_strategies, load_catalog
from waystone3.research.paths import (
    CATALOG_KEY,
    equity_key,
    latest_key,
    manifest_key,
    metrics_key,
    success_key,
)

PREVIEW_DATE = "2026-08-14"


def _metrics(name: str, sharpe: float, cagr: float, dd: float, trades: int) -> bytes:
    payload = {
        "strategy": name,
        "synthetic": True,
        "params": {"preview": True, "window_years": 5},
        "stats": {
            "days": 1260,
            "years": 5.0,
            "total_return_pct": round(cagr * 5, 2),
            "cagr_pct": cagr,
            "ann_vol_pct": 12.0,
            "sharpe": sharpe,
            "sortino": round(sharpe * 1.1, 2),
            "max_drawdown_pct": dd,
            "calmar": round(abs(cagr / dd), 2) if dd else None,
            "trade_count": trades,
            "win_rate_pct": 54.0,
        },
        "extra": {"source": "staged_preview"},
    }
    return json.dumps(payload, indent=2).encode()


def _equity_csv() -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "equity", "daily_ret"])
    eq = 100_000.0
    for i in range(60):
        eq *= 1.001
        writer.writerow([f"2026-06-{(i % 28) + 1:02d}", f"{eq:.2f}", "0.001"])
    writer.writerow([PREVIEW_DATE, f"{eq:.2f}", "0.001"])
    return buf.getvalue().encode()


_FIXTURE: ReportStore | None = None

_PREVIEW = (
    (1.10, 8.5, -7.2, 120),
    (0.85, 6.1, -9.0, 80),
    (0.95, 9.4, -11.5, 40),
    (0.70, 5.2, -6.0, 36),
    (1.25, 11.0, -8.4, 200),
    (0.90, 7.8, -10.1, 24),
    (0.40, 2.1, -4.5, 0),
    (0.80, 6.6, -9.8, 55),
)


def staged_research_store() -> ReportStore:
    global _FIXTURE
    if _FIXTURE is not None:
        return _FIXTURE
    import tempfile

    from waystone3.ibkr.store import LocalFsStore

    store = LocalFsStore(Path(tempfile.mkdtemp(prefix="waystone-research-staged-")))
    store.put(CATALOG_KEY, json.dumps(load_catalog()).encode(), "application/json")
    now = datetime.now(NY).isoformat()
    for row, nums in zip(list_strategies(), _PREVIEW, strict=False):
        sid = str(row["id"])
        store.put(metrics_key(sid, PREVIEW_DATE), _metrics(sid, *nums), "application/json")
        store.put(equity_key(sid, PREVIEW_DATE), _equity_csv(), "text/csv")
        store.put(
            manifest_key(sid, PREVIEW_DATE),
            json.dumps(
                {
                    "strategy_id": sid,
                    "variant": "default",
                    "date": PREVIEW_DATE,
                    "run_id": "staged-preview",
                    "synthetic": True,
                    "host": "staged",
                    "published_at": now,
                    "window_years": 5,
                }
            ).encode(),
            "application/json",
        )
        store.put(success_key(sid, PREVIEW_DATE), b"ok\n", "text/plain")
        store.put(
            latest_key(sid),
            json.dumps(
                {
                    "date": PREVIEW_DATE,
                    "variant": "default",
                    "run_id": "staged-preview",
                    "synthetic": True,
                }
            ).encode(),
        )
    _FIXTURE = store
    return store


def research_store(primary: ReportStore | None) -> ReportStore:
    """Prefer published dated runs; otherwise the staged preview."""
    if primary is None:
        return staged_research_store()
    from waystone3.research.paths import RESEARCH_PREFIX

    keys = primary.list_keys(f"{RESEARCH_PREFIX}/")
    if any("/dt=" in key and key.endswith("/_SUCCESS") for key in keys):
        return primary
    return staged_research_store()
