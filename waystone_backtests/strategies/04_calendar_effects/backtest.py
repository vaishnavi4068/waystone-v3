#!/usr/bin/env python3
"""04 — Calendar effects on the index (ES/MES via SPY or ^GSPC daily bars).

Modes:
  tom    turn-of-month: long for the last `pre` and first `post` trading days of each month, entered at
         the open of the first such day and exited at the open of the day after the last (default 4 / 3).
  fomc   pre-announcement drift: long for the `--fomc-days-before` sessions ending on the decision day, i.e.
         from the OPEN of day D-(k-1) to the CLOSE of decision day D (k=2 -> two sessions).
         Dates from data/fomc_dates.csv (VERIFY against federalreserve.gov).
  opex   long every day EXCEPT the week of the monthly third-Friday expiration; compared with buy-and-hold.
  all    tom + fomc positions merged (long if either says long).

    python backtest.py --symbol SPY --mode tom
    python backtest.py --symbol SPY --mode fomc --fomc-days-before 2
    python backtest.py --synthetic --mode all --grid
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, engine, metrics as M, report  # noqa: E402
from wsbt.calendar_utils import is_opex_week, month_position  # noqa: E402

NAME = "04_calendar_effects"


def load_fomc() -> pd.DatetimeIndex:
    p = D.DATA_DIR / "fomc_dates.csv"
    df = pd.read_csv(p, comment="#", parse_dates=["date"])
    return pd.DatetimeIndex(sorted(df["date"].dt.normalize()))


def target_tom(idx: pd.DatetimeIndex, pre: int, post: int) -> pd.Series:
    """Position to HOLD after each close: long from the close of the day with from_end == pre+1
    (so the first day of exposure is the pre-th last day) through the close of from_start == post."""
    mp = month_position(idx)
    hold = (mp["from_end"] <= pre + 1) | (mp["from_start"] < post)
    # target[t] = position held from the open of t+1 -> we want exposure on days with from_end<=pre or from_start<=post
    want = ((mp["from_end"] <= pre) | (mp["from_start"] <= post)).astype(float)
    return want.shift(-1).fillna(0.0)          # decided at the close before the day we want exposure


def target_fomc(idx: pd.DatetimeIndex, fomc: pd.DatetimeIndex, days_before: int) -> pd.Series:
    want = pd.Series(0.0, index=idx)
    pos = {d: i for i, d in enumerate(idx)}
    for d in fomc:
        # decision day may not be a trading day in the index (holiday) -> take the next available
        cand = idx[idx >= d]
        if len(cand) == 0:
            continue
        i_dec = pos[cand[0]]
        i0 = max(0, i_dec - days_before + 1)      # first day of exposure
        want.iloc[i0:i_dec + 1] = 1.0
    return want.shift(-1).fillna(0.0)


def target_opex(idx: pd.DatetimeIndex) -> pd.Series:
    want = (~is_opex_week(idx)).astype(float)
    return want.shift(-1).fillna(0.0)


def run(bars: pd.DataFrame, mode: str, pre: int, post: int, days_before: int, fomc: pd.DatetimeIndex,
        cost: costs.CostModel, units: float, capital: float):
    idx = bars.index
    if mode == "tom":
        tgt = target_tom(idx, pre, post)
    elif mode == "fomc":
        tgt = target_fomc(idx, fomc, days_before)
    elif mode == "opex":
        tgt = target_opex(idx)
    else:
        tgt = ((target_tom(idx, pre, post) + target_fomc(idx, fomc, days_before)) > 0).astype(float)
    return engine.simulate_positions(bars, tgt, cost, {"units": units}, capital=capital)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY", help="daily bars; use an ES/MES continuous CSV for futures-accurate fills")
    ap.add_argument("--mode", choices=["tom", "fomc", "opex", "all"], default="tom")
    ap.add_argument("--pre", type=int, default=4)
    ap.add_argument("--post", type=int, default=3)
    ap.add_argument("--fomc-days-before", type=int, default=2)
    ap.add_argument("--units", type=float, default=100, help="shares of SPY (or contracts if bars are MES)")
    ap.add_argument("--futures", action="store_true", help="bars are MES prices: use MES costs/multiplier")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    bars = D.synthetic_daily(2800, s0=400.0, seed=8) if a.synthetic else D.load_daily(a.symbol, a.start)
    fomc = load_fomc()
    cost = costs.MES if a.futures else costs.US_ETF
    units = 1 if a.futures and a.units == 100 else a.units
    params = dict(symbol="SYNTH" if a.synthetic else a.symbol, mode=a.mode, pre=a.pre, post=a.post, fomc_days_before=a.fomc_days_before, units=units)
    r, tr, eq = run(bars, a.mode, a.pre, a.post, a.fomc_days_before, fomc, cost, units, a.capital)
    bh = M.summary(bars["close"].pct_change().fillna(0))
    # same-period benchmark scaled to the same notional so the numbers are comparable
    extra = {"benchmark_buy_hold": f"CAGR {bh.get('cagr_pct')}%  Sharpe {bh.get('sharpe')}  maxDD {bh.get('max_drawdown_pct')}%",
             "fomc_dates_in_sample": int(((fomc >= bars.index[0]) & (fomc <= bars.index[-1])).sum()),
             "note": "fomc_dates.csv is hand-entered — verify against federalreserve.gov before trusting the fomc mode"}
    report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, extra, synthetic=a.synthetic)
    if a.grid and a.mode in ("tom", "all"):
        grid = {"pre": [2, 4, 6], "post": [1, 3, 5]}
        tab, best, dsr, win = engine.grid_search(
            lambda pre, post: run(bars, a.mode, pre, post, a.fomc_days_before, fomc, cost, units, a.capital)[0], grid, bars.index)
        report.print_grid(tab, best, dsr, win)
    elif a.grid and a.mode == "fomc":
        grid = {"days_before": [1, 2, 3, 5]}
        tab, best, dsr, win = engine.grid_search(
            lambda days_before: run(bars, a.mode, a.pre, a.post, days_before, fomc, cost, units, a.capital)[0], grid, bars.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
