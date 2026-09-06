"""Mirror the free-sentiment CSVs (data/macro, data/news, data/events) to GCS.

The free feeds (CNN Fear & Greed, Yahoo RSS, SEC 8-K, GDELT) only give history going
forward, so every collecting machine pushes its merged files here and any new machine
pulls before it starts. Files are merged row-wise on push so two collectors never
overwrite each other's days.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from pathlib import Path

from waystone3.ibkr.store import ReportStore, build_report_store_from_env
from waystone3.research.paths import RESEARCH_PREFIX, toolkit_root

SENTIMENT_PREFIX = f"{RESEARCH_PREFIX}/sentiment"
SUBDIRS = ("macro", "news", "events")


@dataclass
class SyncReport:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def data_dir() -> Path:
    return Path(os.environ.get("WSBT_DATA_DIR", toolkit_root() / "data"))


def _key(sub: str, name: str) -> str:
    return f"{SENTIMENT_PREFIX}/{sub}/{name}"


def _rows(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="ignore")))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def merge_csv(local: bytes, remote: bytes | None) -> bytes:
    """Union of two CSV files on (symbol, url) for news rows or on date for macro series."""
    if not remote:
        return local
    header, rows_l = _rows(local)
    header_r, rows_r = _rows(remote)
    if not header:
        return remote
    cols = header + [c for c in header_r if c not in header]
    key_cols = ("symbol", "url") if "url" in cols else ("date",)
    seen: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows_r + rows_l:  # local last so a re-fetch wins on ties
        seen[tuple(row.get(c, "") for c in key_cols)] = row
    ordered = sorted(seen.values(), key=lambda r: (r.get("date", ""), r.get("ts", "")))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(ordered)
    return out.getvalue().encode()


def sync(*, push: bool = False, pull: bool = False, store: ReportStore | None = None) -> SyncReport:
    store = store or build_report_store_from_env()
    report = SyncReport()
    if store is None:
        report.skipped.append("no report store configured (IBKR_REPORTS_BUCKET)")
        return report
    root = data_dir()
    for sub in SUBDIRS:
        folder = root / sub
        remote_keys = set(store.list_keys(f"{SENTIMENT_PREFIX}/{sub}/"))
        if push and folder.is_dir():
            for path in sorted(folder.glob("*.csv")):
                key = _key(sub, path.name)
                merged = merge_csv(
                    path.read_bytes(), store.get(key) if key in remote_keys else None
                )
                store.put(key, merged, "text/csv")
                path.write_bytes(merged)
                report.pushed.append(key)
        if pull:
            folder.mkdir(parents=True, exist_ok=True)
            for key in sorted(remote_keys):
                name = key.rsplit("/", 1)[-1]
                if not name.endswith(".csv"):
                    continue
                remote = store.get(key)
                if remote is None:
                    continue
                local = folder / name
                merged = merge_csv(local.read_bytes(), remote) if local.is_file() else remote
                local.write_bytes(merged)
                report.pulled.append(key)
    return report
