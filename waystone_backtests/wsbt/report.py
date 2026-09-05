"""Writes results/<strategy>/{metrics.json, trades.csv, equity.csv} and prints the report."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import metrics as M
from .data import ROOT

RESULTS = Path(__import__("os").environ.get("WSBT_RESULTS_DIR", ROOT / "results"))


def save_and_print(name: str, daily_ret: pd.Series, trades: pd.DataFrame | None, equity: pd.Series,
                   params: dict, extra: dict | None = None, synthetic: bool = False, quiet: bool = False) -> dict:
    stats = M.summary(daily_ret, trades)
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps({"strategy": name, "synthetic": synthetic, "params": params,
                                                      "stats": stats, "extra": extra or {}}, indent=1, default=str))
    if trades is not None and len(trades):
        trades.to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame({"equity": equity, "daily_ret": daily_ret}).to_csv(out_dir / "equity.csv")
    title = f"{name}  {'[SYNTHETIC DATA — mechanics only, ignore the numbers]' if synthetic else ''}"
    if not quiet:
        print(M.format_report(title, stats, {"params": params, **(extra or {})}))
        print(f"  written -> {out_dir}/metrics.json, trades.csv, equity.csv")
    return stats


def print_grid(tab: pd.DataFrame, best: dict, dsr: dict, windows) -> None:
    is0, is1, oos0, oos1 = windows
    print("-" * 78)
    print(f"  GRID  in-sample {is0.date()}..{is1.date()}   out-of-sample {oos0.date()}..{oos1.date()}")
    with pd.option_context("display.width", 140, "display.max_rows", 200):
        print(tab.sort_values("is_sharpe", ascending=False).to_string(index=False))
    print(f"  best by IS Sharpe: {best}")
    print(f"  deflated Sharpe   : {dsr}   (dsr_probability < 0.95 = the IS result is not distinguishable from luck)")
    print("-" * 78)
