"""Run research-sleeve backtests on the Mac (local compute)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from waystone3.research.catalog import get_strategy, list_strategies
from waystone3.research.paths import toolkit_root
from waystone3.research.window import MAX_YEARS, MIN_YEARS, TunedWindow, resolve_window


@dataclass
class RunResult:
    strategy_id: str
    ok: bool
    command: list[str]
    output: str = ""
    window: TunedWindow | None = None
    skipped: bool = False


@dataclass
class RunReport:
    results: list[RunResult] = field(default_factory=list)


def run_strategies(
    *,
    strategy_id: str | None = None,
    synthetic: bool = False,
    extra_args: list[str] | None = None,
    years: float = MAX_YEARS,
    min_years: float = MIN_YEARS,
) -> RunReport:
    root = toolkit_root()
    data_dir = Path(os.environ.get("WSBT_DATA_DIR", root / "data"))
    rows = [get_strategy(strategy_id)] if strategy_id else list_strategies()
    report = RunReport()
    env = os.environ.copy()
    env.setdefault("WSBT_DATA_DIR", str(data_dir))
    env.setdefault("WSBT_RESULTS_DIR", str(root / "results"))
    extra = list(extra_args or [])
    if synthetic and "--synthetic" not in extra:
        extra.append("--synthetic")
    for row in rows:
        if not row:
            continue
        script = root / row["folder"] / "backtest.py"
        tuned = resolve_window(
            list(row.get("daily_symbols") or []),
            data_dir,
            roots=list(row.get("intraday_roots") or []),
            min_years=min_years,
            max_years=years,
        )
        if not synthetic and tuned is None:
            report.results.append(
                RunResult(
                    strategy_id=str(row["id"]),
                    ok=True,
                    command=[],
                    output=(
                        f"skip: overlap shorter than {min_years:g} years "
                        "(need 2-5 years of data)"
                    ),
                    skipped=True,
                )
            )
            continue
        window: list[str] = []
        if not synthetic and "--start" in script.read_text() and tuned is not None:
            window = tuned.args
        for spec in row.get("scripts") or [{}]:
            args = [sys.executable, str(script), *list(spec.get("args") or []), *window, *extra]
            proc = subprocess.run(
                args,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            report.results.append(
                RunResult(
                    strategy_id=str(row["id"]),
                    ok=proc.returncode == 0,
                    command=args,
                    output=((proc.stdout or "") + (proc.stderr or ""))[-4000:],
                    window=tuned,
                )
            )
    return report
