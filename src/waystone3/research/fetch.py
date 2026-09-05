"""Fill local wsbt data/: NSDQ250 first, then Massive / Yahoo."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from waystone3.research.catalog import (
    needed_daily_symbols,
    needed_intraday_roots,
)
from waystone3.research.nsdq250 import sync_daily_symbol, sync_intraday_root
from waystone3.research.paths import toolkit_root
from waystone3.research.window import default_window


@dataclass
class FetchReport:
    from_nsdq250: list[str] = field(default_factory=list)
    from_s3: list[str] = field(default_factory=list)
    from_api: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _has_daily(data_dir: Path, symbol: str) -> bool:
    return (data_dir / "daily" / f"{symbol.upper()}.csv").is_file()


def _has_intraday(data_dir: Path, root: str) -> bool:
    return (data_dir / "intraday" / f"{root.upper()}_1min.csv").is_file()


def _massive_key() -> str:
    return (os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY") or "").strip()


def _run_tool(script: str, args: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    if "POLYGON_API_KEY" not in env and _massive_key():
        env["POLYGON_API_KEY"] = _massive_key()
    cmd = [sys.executable, str(cwd / "tools" / script), *args]
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def fetch_market_data(
    *,
    strategy_id: str | None = None,
    allow_api: bool = True,
    data_dir: Path | None = None,
    years: int = 5,
    sync_flatfiles: bool = False,
) -> FetchReport:
    """Prefer ``gs://$IBKR_REPORTS_BUCKET/NSDQ250``. Only then Massive or Yahoo."""
    root = toolkit_root()
    dest = data_dir or (root / "data")
    dest.mkdir(parents=True, exist_ok=True)
    report = FetchReport()

    for symbol in needed_daily_symbols(strategy_id):
        if _has_daily(dest, symbol):
            report.notes.append(f"{symbol}: already local")
            continue
        try:
            path = sync_daily_symbol(symbol, dest)
        except Exception as exc:
            path = None
            report.notes.append(f"{symbol}: NSDQ250 miss ({exc})")
        if path is not None:
            report.from_nsdq250.append(symbol)
            continue
        if not allow_api:
            report.missing.append(symbol)
            continue
        if _fetch_daily_via_api(symbol, root, years=years):
            report.from_api.append(symbol)
        else:
            report.missing.append(symbol)

    for fut in needed_intraday_roots(strategy_id):
        if _has_intraday(dest, fut):
            report.notes.append(f"{fut} 1min: already local")
            continue
        try:
            path = sync_intraday_root(fut, dest)
        except Exception as exc:
            path = None
            report.notes.append(f"{fut}: NSDQ250 futures miss ({exc})")
        if path is not None:
            report.from_nsdq250.append(f"{fut}_1min")
            continue
        if not allow_api:
            report.missing.append(f"{fut}_1min")
            continue
        if _fetch_futures_via_massive(fut, root, years=years):
            report.from_api.append(f"{fut}_1min")
        else:
            report.missing.append(f"{fut}_1min")

    gex = strategy_id is None or strategy_id == "02_gex_dealer_gamma"
    if allow_api and sync_flatfiles and gex:
        from waystone3.research.massive_s3 import s3_config, sync_prefix

        if s3_config() is not None:
            start, _ = default_window(years)
            written = sync_prefix(
                f"us_options_opra/day_aggs_v1/{start[:4]}/",
                dest / "flatfiles",
                max_keys=int(os.getenv("WAYSTONE_FLATFILE_MAX_KEYS", "80")),
            )
            if written:
                report.from_s3.append(f"flatfiles:{len(written)}")
            else:
                report.notes.append("Massive S3 flatfiles: no objects synced")

    return report


def _fetch_daily_via_api(symbol: str, root: Path, *, years: int) -> bool:
    """Massive REST first, Yahoo second. Mac Studio only — never called from GKE."""
    start, _ = default_window(years)
    if _massive_key():
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            _run_tool(
                "fetch_polygon.py",
                ["indices", "--symbols", symbol, "--start", start],
                root,
            )
            if (root / "data" / "daily" / f"{symbol}.csv").is_file():
                return True
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            _run_tool(
                "fetch_polygon.py",
                ["indices", "--symbols", symbol.removeprefix("I:"), "--start", start],
                root,
            )
    try:
        _run_tool("fetch_yf.py", ["--symbols", symbol, "--start", start], root)
        return (root / "data" / "daily" / f"{symbol}.csv").is_file()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _fetch_futures_via_massive(root_symbol: str, root: Path, *, years: int) -> bool:
    if not _massive_key():
        return False
    start, _ = default_window(years)
    try:
        _run_tool(
            "fetch_polygon.py",
            ["futures", "--root", root_symbol, "--start", start, "--resolution", "1min"],
            root,
        )
        return (root / "data" / "intraday" / f"{root_symbol}_1min.csv").is_file()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
