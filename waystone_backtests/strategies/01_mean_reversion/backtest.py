#!/usr/bin/env python3
"""01 — Mean reversion / range compression on liquid ETFs and large caps.

Two modes (see README.md):
  bb   Bollinger fade: %b < 0 with close > SMA200 -> long next open; exit when close > SMA20 or after max_hold.
  nr7  Narrow-range-7 expansion: day t has the narrowest range of the last 7 -> next day buy-stop at
       high_t (+tick) / sell-stop at low_t (-tick); ATR stop; exit at the close of day max_hold.

    python backtest.py --symbol SPY --mode bb
    python backtest.py --symbol QQQ --mode nr7 --max-hold 2 --grid
    python backtest.py --synthetic --mode bb
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, engine, report  # noqa: E402
from wsbt.engine import TradeSpec  # noqa: E402

NAME = "01_mean_reversion"


def indicators(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    df["sma200"] = df["close"].rolling(200).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    sd = df["close"].rolling(20).std(ddof=0)
    df["bb_lo"], df["bb_hi"] = df["sma20"] - 2 * sd, df["sma20"] + 2 * sd
    df["pct_b"] = (df["close"] - df["bb_lo"]) / (df["bb_hi"] - df["bb_lo"])
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["range"] = df["high"] - df["low"]
    df["nr7"] = df["range"] == df["range"].rolling(7).min()
    return df


def specs_bb(df: pd.DataFrame, max_hold: int, allow_short: bool, stop_atr: float | None) -> list[TradeSpec]:
    out = []
    idx = df.index
    for i in range(200, len(df) - 1):
        row = df.iloc[i]
        if np.isnan(row["pct_b"]) or np.isnan(row["sma200"]):
            continue
        nxt = idx[i + 1]
        if row["pct_b"] < 0 and row["close"] > row["sma200"]:
            stop = row["close"] - stop_atr * row["atr"] if stop_atr else None
            out.append(TradeSpec(date=nxt, side=1, entry="open", stop=stop, max_hold=max_hold,
                                 exit_on=lambda r, j: r["close"] > r["sma20"], tag="bb_long",
                                 meta={"signal_date": idx[i], "pct_b": round(row["pct_b"], 3)}))
        elif allow_short and row["pct_b"] > 1 and row["close"] < row["sma200"]:
            stop = row["close"] + stop_atr * row["atr"] if stop_atr else None
            out.append(TradeSpec(date=nxt, side=-1, entry="open", stop=stop, max_hold=max_hold,
                                 exit_on=lambda r, j: r["close"] < r["sma20"], tag="bb_short",
                                 meta={"signal_date": idx[i], "pct_b": round(row["pct_b"], 3)}))
    return out


def specs_nr7(df: pd.DataFrame, max_hold: int, stop_atr: float, tick: float, trend_filter: bool) -> list[TradeSpec]:
    out = []
    idx = df.index
    for i in range(200, len(df) - 1):
        row = df.iloc[i]
        if not row["nr7"] or np.isnan(row["atr"]):
            continue
        nxt = idx[i + 1]
        long_ok = (not trend_filter) or row["close"] > row["sma200"]
        short_ok = (not trend_filter) or row["close"] < row["sma200"]
        if long_ok:
            out.append(TradeSpec(date=nxt, side=1, entry="stop", entry_level=row["high"] + tick,
                                 stop=row["high"] + tick - stop_atr * row["atr"], max_hold=max_hold, tag="nr7_long",
                                 meta={"signal_date": idx[i], "range": round(row["range"], 3)}))
        if short_ok:
            out.append(TradeSpec(date=nxt, side=-1, entry="stop", entry_level=row["low"] - tick,
                                 stop=row["low"] - tick + stop_atr * row["atr"], max_hold=max_hold, tag="nr7_short",
                                 meta={"signal_date": idx[i], "range": round(row["range"], 3)}))
    return out


def run(bars: pd.DataFrame, mode: str, max_hold: int, stop_atr: float | None, allow_short: bool,
        trend_filter: bool, notional: float, capital: float, cost: costs.CostModel):
    df = indicators(bars)
    if mode == "bb":
        specs = specs_bb(df, max_hold, allow_short, stop_atr)
        max_conc = 1
    else:
        specs = specs_nr7(df, max_hold, stop_atr or 1.5, tick=0.01, trend_filter=trend_filter)
        max_conc = 1            # buy-stop and sell-stop on the same day act as an OCO: first fill wins
    return engine.simulate_trades(df, specs, cost, {"notional": notional}, capital=capital, max_concurrent=max_conc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--mode", choices=["bb", "nr7"], default="bb")
    ap.add_argument("--max-hold", type=int, default=10)
    ap.add_argument("--stop-atr", type=float, default=None, help="protective stop in ATR(14) (bb: optional; nr7: default 1.5)")
    ap.add_argument("--short", action="store_true", help="bb: also fade %%b > 1 below SMA200")
    ap.add_argument("--no-trend-filter", action="store_true", help="nr7: ignore the SMA200 direction filter")
    ap.add_argument("--notional", type=float, default=25_000.0)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    bars = D.synthetic_daily(2500, seed=1) if a.synthetic else D.load_daily(a.symbol, a.start, a.end)
    if a.mode == "nr7" and a.max_hold == 10:
        a.max_hold = 2
    params = dict(symbol=a.symbol if not a.synthetic else "SYNTH", mode=a.mode, max_hold=a.max_hold, stop_atr=a.stop_atr,
                  short=a.short, trend_filter=not a.no_trend_filter, notional=a.notional)
    r, tr, eq = run(bars, a.mode, a.max_hold, a.stop_atr, a.short, not a.no_trend_filter, a.notional, a.capital, costs.US_ETF)
    report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, synthetic=a.synthetic)
    if a.grid:
        grid = {"max_hold": [3, 5, 10], "stop_atr": [None, 2.0]} if a.mode == "bb" else {"max_hold": [1, 2, 3], "stop_atr": [1.0, 1.5, 2.5]}
        tab, best, dsr, win = engine.grid_search(
            lambda max_hold, stop_atr: run(bars, a.mode, max_hold, stop_atr, a.short, not a.no_trend_filter, a.notional, a.capital, costs.US_ETF)[0],
            grid, bars.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
