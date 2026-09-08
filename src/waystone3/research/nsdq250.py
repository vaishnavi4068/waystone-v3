"""Read Polygon dumps already stored under gs://waystone-data/NSDQ250."""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from waystone3.research.paths import NSDQ250_PREFIX, toolkit_root

_OHLC = re.compile(
    rf"^{re.escape(NSDQ250_PREFIX)}/([A-Z0-9.]+)_daily_ohlc_"
    r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.csv$"
)


@dataclass(frozen=True)
class DailyOhlcObject:
    key: str
    symbol: str
    start: date
    end: date

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days


def _bucket() -> str:
    return os.getenv("IBKR_REPORTS_BUCKET", "waystone-data").strip() or "waystone-data"


def _client():
    from google.cloud import storage

    return storage.Client()


def parse_daily_ohlc_key(key: str) -> DailyOhlcObject | None:
    match = _OHLC.match(key)
    if match is None:
        return None
    return DailyOhlcObject(
        key=key,
        symbol=match.group(1),
        start=date.fromisoformat(match.group(2)),
        end=date.fromisoformat(match.group(3)),
    )


def list_daily_ohlc(symbol: str) -> list[DailyOhlcObject]:
    """List NSDQ250 daily files for one ticker. Does not scan the whole prefix."""
    client = _client()
    prefix = f"{NSDQ250_PREFIX}/{symbol.upper()}_daily_ohlc_"
    out: list[DailyOhlcObject] = []
    for blob in client.list_blobs(_bucket(), prefix=prefix):
        parsed = parse_daily_ohlc_key(blob.name)
        if parsed is not None:
            out.append(parsed)
    return sorted(out, key=lambda row: (-row.span_days, row.end), reverse=False)


def pick_widest_daily(symbol: str) -> DailyOhlcObject | None:
    rows = list_daily_ohlc(symbol)
    if not rows:
        return None
    return max(rows, key=lambda row: (row.span_days, row.end))


def futures_continuous_key(root: str) -> str:
    name = root.upper()
    return f"{NSDQ250_PREFIX}/FUTURES_1MIN/{name}/{name}_continuous_front.csv"


def futures_continuous_exists(root: str) -> bool:
    from google.cloud import storage

    bucket = storage.Client().bucket(_bucket())
    return bool(bucket.blob(futures_continuous_key(root)).exists())


def _download_text(key: str) -> str:
    from google.cloud import storage

    blob = storage.Client().bucket(_bucket()).blob(key)
    if not blob.exists():
        raise FileNotFoundError(key)
    return blob.download_as_text()


def ohlc_csv_to_wsbt(raw: str) -> str:
    """Map NSDQ250 daily columns onto the wsbt daily contract."""
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise ValueError("empty CSV")
    fields = {name.lower(): name for name in reader.fieldnames}
    required = ("date", "open", "high", "low", "close", "volume")
    missing = [col for col in required if col not in fields]
    if missing:
        raise ValueError(f"NSDQ250 daily CSV missing {missing}")
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["date", "open", "high", "low", "close", "adj_close", "volume"]
    )
    writer.writeheader()
    for row in reader:
        close = row[fields["close"]]
        adj = row[fields["adj_close"]] if "adj_close" in fields else close
        writer.writerow(
            {
                "date": row[fields["date"]],
                "open": row[fields["open"]],
                "high": row[fields["high"]],
                "low": row[fields["low"]],
                "close": close,
                "adj_close": adj,
                "volume": row[fields["volume"]],
            }
        )
    return buf.getvalue()


def futures_1min_to_wsbt(raw: str) -> str:
    """Map NSDQ250 continuous 1-min (datetime_et, …) onto wsbt intraday contract."""
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise ValueError("empty CSV")
    fields = {name.lower(): name for name in reader.fieldnames}
    ts_col = fields.get("datetime_et") or fields.get("ts") or fields.get("timestamp")
    if ts_col is None:
        raise ValueError("NSDQ250 futures CSV missing datetime_et/ts")
    required = ("open", "high", "low", "close", "volume")
    missing = [col for col in required if col not in fields]
    if missing:
        raise ValueError(f"NSDQ250 futures CSV missing {missing}")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ts", "open", "high", "low", "close", "volume"])
    writer.writeheader()
    for row in reader:
        writer.writerow(
            {
                "ts": row[ts_col],
                "open": row[fields["open"]],
                "high": row[fields["high"]],
                "low": row[fields["low"]],
                "close": row[fields["close"]],
                "volume": row[fields["volume"]],
            }
        )
    return buf.getvalue()


def sync_daily_symbol(symbol: str, data_dir: Path | None = None) -> Path | None:
    picked = pick_widest_daily(symbol)
    if picked is None:
        return None
    dest_dir = (data_dir or (toolkit_root() / "data")) / "daily"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{symbol.upper()}.csv"
    dest.write_text(ohlc_csv_to_wsbt(_download_text(picked.key)))
    return dest


def sync_intraday_root(root: str, data_dir: Path | None = None) -> Path | None:
    key = futures_continuous_key(root)
    from google.cloud import storage

    blob = storage.Client().bucket(_bucket()).blob(key)
    if not blob.exists():
        return None
    dest_dir = (data_dir or (toolkit_root() / "data")) / "intraday"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{root.upper()}_1min.csv"
    dest.write_text(futures_1min_to_wsbt(blob.download_as_text()))
    return dest
