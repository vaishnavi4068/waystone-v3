"""Run research-sleeve backtests on the Mac (local compute)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field

from waystone3.research.catalog import get_strategy, list_strategies
from waystone3.research.paths import toolkit_root
from waystone3.research.window import default_window


@dataclass
class RunResult:
    strategy_id: str
    ok: bool
    command: list[str]
    output: str = ""


@dataclass
class RunReport:
    results: list[RunResult] = field(default_factory=list)


def run_strategies(
    *,
    strategy_id: str | None = None,
    synthetic: bool = False,
    extra_args: list[str] | None = None,
    years: int = 5,
) -> RunReport:
    root = toolkit_root()
    rows = [get_strategy(strategy_id)] if strategy_id else list_strategies()
    report = RunReport()
    env = os.environ.copy()
    env.setdefault("WSBT_DATA_DIR", str(root / "data"))
    env.setdefault("WSBT_RESULTS_DIR", str(root / "results"))
    extra = list(extra_args or [])
    if synthetic and "--synthetic" not in extra:
        extra.append("--synthetic")
    start, end = default_window(years)
    for row in rows:
        if not row:
            continue
        script = root / row["folder"] / "backtest.py"
        window: list[str] = []
        if not synthetic and "--start" in script.read_text():
            window = ["--start", start, "--end", end]
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
                )
            )
    return report
