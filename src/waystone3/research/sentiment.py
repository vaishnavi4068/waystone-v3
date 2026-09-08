"""Mirror sentiment CSVs (data/macro, data/news, data/events, data/sentiment, data/lists) to GCS.

Massive/Polygon news, scored daily sentiment, macro gauges, and event classifiers are merged
row-wise on push so Mac Studio, cloud agents, and future boxes share one canonical copy under:

  gs://$IBKR_REPORTS_BUCKET/research/v1/sentiment/
    macro/          fng.csv, aaii.csv, pcr.csv, gdelt_*.csv
    news/           <SYM>.csv  (Massive LLM insights + title/text)
    events/         <SYM>.csv, <SYM>_8k.csv
    sentiment/      <SYM>_daily.csv  (Massive-scored shock_z features)
    lists/          sp500.csv  (503 S&P 500 constituents)
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
SUBDIRS = ("macro", "news", "events", "sentiment", "lists")
MERGE_KEYS: dict[str, tuple[str, ...]] = {
    "macro": ("date",),
    "news": ("symbol", "url"),
    "events": ("symbol", "url"),
    "sentiment": ("date",),
    "lists": ("symbol",),
}


@dataclass
class SyncReport:
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def data_dir() -> Path:
    root = Path(os.environ.get("WSBT_DATA_DIR", toolkit_root() / "data"))
    (root / "lists").mkdir(parents=True, exist_ok=True)
    # Keep sp500.csv in data/lists/ for GCS sync; fall back to legacy data/sp500.csv.
    legacy = root / "sp500.csv"
    canonical = root / "lists" / "sp500.csv"
    if legacy.is_file() and not canonical.is_file():
        canonical.write_bytes(legacy.read_bytes())
    return root


def _local_folder(root: Path, sub: str) -> Path:
    if sub == "lists":
        return root / "lists"
    return root / sub


def _key(sub: str, name: str) -> str:
    return f"{SENTIMENT_PREFIX}/{sub}/{name}"


def _rows(raw: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="ignore")))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def merge_csv(local: bytes, remote: bytes | None, *, sub: str = "macro") -> bytes:
    """Union two CSV files using merge keys per sentiment subfolder."""
    if not remote:
        return local
    header, rows_l = _rows(local)
    header_r, rows_r = _rows(remote)
    if not header:
        return remote
    cols = header + [c for c in header_r if c not in header]
    key_cols = MERGE_KEYS.get(sub)
    if not key_cols:
        key_cols = ("symbol", "url") if "url" in cols else ("date",)
    elif key_cols == ("symbol",) and "symbol" not in cols:
        key_cols = (cols[0],)
    seen: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows_r + rows_l:  # local last so a re-fetch wins on ties
        seen[tuple(row.get(c, "") for c in key_cols)] = row
    sort_cols = [c for c in ("date", "ts", "symbol") if c in cols]
    ordered = sorted(seen.values(), key=lambda r: tuple(r.get(c, "") for c in sort_cols))
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
        folder = _local_folder(root, sub)
        remote_keys = set(store.list_keys(f"{SENTIMENT_PREFIX}/{sub}/"))
        if push and folder.is_dir():
            for path in sorted(folder.glob("*.csv")):
                key = _key(sub, path.name)
                merged = merge_csv(
                    path.read_bytes(),
                    store.get(key) if key in remote_keys else None,
                    sub=sub,
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
                merged = merge_csv(local.read_bytes(), remote, sub=sub) if local.is_file() else remote
                local.write_bytes(merged)
                report.pulled.append(key)
                if sub == "lists" and name == "sp500.csv":
                    legacy = root / "sp500.csv"
                    legacy.write_bytes(merged)
    return report
