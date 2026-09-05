#!/usr/bin/env python3
"""06 — Relative-value sector rotation on the 11 SPDR sector ETFs.

Each month-end rank sectors by blended momentum (mean of 3-, 6- and 12-month returns, skipping the last
`skip` days), hold the top N equal-weight, but only those above their 200-day SMA (a slot that fails the
filter goes to cash).  Rebalance at the next open.  Benchmark: SPY buy-and-hold.

    python backtest.py
    python backtest.py --top-n 3 --no-sma-filter --grid
    python backtest.py --synthetic
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import data as D, engine, metrics as M, report  # noqa: E402

NAME = "06_sector_rotation"
SECTORS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


def momentum_score(closes: pd.DataFrame, lookbacks=(63, 126, 252), skip: int = 5) -> pd.DataFrame:
    base = closes.shift(skip)
    parts = [base / base.shift(lb) - 1.0 for lb in lookbacks]
    return sum(parts) / len(parts)


def weights_monthly(closes: pd.DataFrame, top_n: int, lookbacks, skip: int, sma_filter: bool, sma_len: int = 200) -> pd.DataFrame:
    score = momentum_score(closes, lookbacks, skip)
    sma = closes.rolling(sma_len).mean()
    month_end = closes.index.to_series().groupby(closes.index.to_period("M")).transform("max") == closes.index.to_series()
    rows = {}
    for dt in closes.index[month_end.to_numpy()]:
        s = score.loc[dt].dropna()
        if len(s) < top_n:
            continue
        top = s.sort_values(ascending=False).index[:top_n]
        w = pd.Series(0.0, index=closes.columns)
        for sym in top:
            ok = (not sma_filter) or (closes.at[dt, sym] > sma.at[dt, sym])
            w[sym] = 1.0 / top_n if ok else 0.0
        rows[dt] = w
    return pd.DataFrame(rows).T.sort_index()


def run(frames: dict, top_n: int, lookbacks, skip: int, sma_filter: bool, cost_bps: float, capital: float):
    closes = D.closes_panel(frames).dropna(how="all")
    w = weights_monthly(closes, top_n, lookbacks, skip, sma_filter)
    return engine.simulate_weights(frames, w, cost_bps=cost_bps, capital=capital)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--lookbacks", type=int, nargs="+", default=[63, 126, 252])
    ap.add_argument("--skip", type=int, default=5)
    ap.add_argument("--no-sma-filter", action="store_true")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    if a.synthetic:
        frames = D.synthetic_panel(SECTORS + ["SPY"], n=2600, seed=6)
    else:
        frames = D.load_many(SECTORS + ["SPY"], a.start, strict=False)
        missing = [s for s in SECTORS if s not in frames]
        if missing:
            print(f"missing {missing} — run: python tools/fetch_yf.py --sectors")
    spy = frames.pop("SPY", None)
    params = dict(top_n=a.top_n, lookbacks=a.lookbacks, skip=a.skip, sma_filter=not a.no_sma_filter, cost_bps=a.cost_bps,
                  universe=sorted(frames))
    r, tr, eq = run(frames, a.top_n, tuple(a.lookbacks), a.skip, not a.no_sma_filter, a.cost_bps, a.capital)
    extra = {"rebalances": int(len(tr)), "avg_turnover_pct": round(100 * float(tr["turnover"].mean()), 1) if len(tr) else 0}
    if spy is not None:
        bh = M.summary(spy["close"].reindex(r.index).pct_change().fillna(0))
        extra["benchmark_spy"] = f"CAGR {bh.get('cagr_pct')}%  Sharpe {bh.get('sharpe')}  maxDD {bh.get('max_drawdown_pct')}%"
    report.save_and_print(NAME, r, None, eq, params, extra, synthetic=a.synthetic)
    if a.grid:
        grid = {"top_n": [2, 3, 4], "sma_filter": [True, False]}
        tab, best, dsr, win = engine.grid_search(
            lambda top_n, sma_filter: run(frames, top_n, tuple(a.lookbacks), a.skip, sma_filter, a.cost_bps, a.capital)[0], grid, r.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
