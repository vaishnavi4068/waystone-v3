#!/usr/bin/env python3
"""05 — Order-flow: cumulative volume delta (CVD) divergence at VWAP bands, MNQ 1-minute bars.

Long setup:  price prints a new session low below VWAP - k*sigma while the session CVD does NOT make a
             new low (buyers absorbing) -> long at the next bar's open, target VWAP, stop below the low.
Short setup: mirror above VWAP + k*sigma.
Delta per bar = ask_volume - bid_volume when the CSV has them (recorded tick-by-tick), otherwise a
proxy from the bar's shape: volume x (close-open)/(high-low).

    python backtest.py --symbol MNQ
    python backtest.py --synthetic --grid
"""
from __future__ import annotations

import argparse
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, metrics as M, report  # noqa: E402

NAME = "05_orderflow_cvd_mnq"


def session_features(bars: pd.DataFrame, session_start: dtime) -> pd.DataFrame:
    df = bars.copy()
    t = df.index.time
    # session id: a new session begins at session_start each day
    day = df.index.date
    starts = pd.Series([f"{d}" if tm >= session_start else f"{d}-pre" for d, tm in zip(day, t)], index=df.index)
    df["session"] = starts
    if {"bid_volume", "ask_volume"} <= set(df.columns):
        df["delta"] = df["ask_volume"] - df["bid_volume"]
        df["delta_source"] = "tick"
    else:
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        shape = ((df["close"] - df["open"]) / rng).clip(-1, 1)
        tick_rule = np.sign(df["close"].diff()).fillna(0)
        df["delta"] = df["volume"] * shape.fillna(tick_rule)
        df["delta_source"] = "proxy"
    g = df.groupby("session", sort=False)
    df["cvd"] = g["delta"].cumsum()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    cum_v = g["volume"].cumsum().replace(0, np.nan)
    df["vwap"] = pv.groupby(df["session"]).cumsum() / cum_v
    dev2 = df["volume"] * (tp - df["vwap"]) ** 2
    df["sd"] = np.sqrt(dev2.groupby(df["session"]).cumsum() / cum_v)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(), (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["bar_in_session"] = g.cumcount()
    return df


def run(bars: pd.DataFrame, k_sigma: float, lookback: int, stop_atr: float, max_bars: int, cvd_margin: float,
        trade_start: dtime, trade_end: dtime, flat_at: dtime, session_start: dtime, cost: costs.CostModel,
        units: int, capital: float):
    df = session_features(bars, session_start)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    vwap, sd, atr, cvd = (df[k].to_numpy() for k in ("vwap", "sd", "atr", "cvd"))
    bis = df["bar_in_session"].to_numpy()
    times = df.index.time
    sess = df["session"].to_numpy()
    n = len(df)
    pnl = np.zeros(n)
    trades = []
    pos = None
    pending = None
    for i in range(1, n):
        # ---- fill a pending entry at this bar's open ----
        if pending is not None and sess[i] == pending["session"]:
            side = pending["side"]
            fill = cost.fill(o[i], side)
            pos = {"side": side, "fill": fill, "i0": i, "stop": pending["stop"], "target": pending["target"], "mark": fill}
            pending = None
        elif pending is not None:
            pending = None
        # ---- manage ----
        if pos is not None:
            side = pos["side"]
            exit_px, reason = None, None
            hit_stop = (side > 0 and l[i] <= pos["stop"]) or (side < 0 and h[i] >= pos["stop"])
            hit_tgt = (side > 0 and h[i] >= pos["target"]) or (side < 0 and l[i] <= pos["target"])
            if hit_stop:                                   # conservative: stop first
                exit_px, reason = (min(o[i], pos["stop"]) if side > 0 else max(o[i], pos["stop"])), "stop"
            elif hit_tgt:
                exit_px, reason = (max(o[i], pos["target"]) if side > 0 else min(o[i], pos["target"])), "target"
            elif i - pos["i0"] >= max_bars:
                exit_px, reason = c[i], "time"
            elif times[i] >= flat_at or sess[i] != sess[pos["i0"]] or i == n - 1:
                exit_px, reason = c[i], "flat"
            if exit_px is not None:
                fill = cost.fill(exit_px, -side)
                gross = (fill - pos["fill"]) * side * units * cost.multiplier
                comm = 2 * cost.commission * units
                pnl[i] += (fill - pos["mark"]) * side * units * cost.multiplier - comm
                trades.append({"entry_ts": df.index[pos["i0"]], "exit_ts": df.index[i], "side": side, "units": units,
                               "entry": pos["fill"], "exit": fill, "pnl": gross - comm, "bars_held": i - pos["i0"], "reason": reason,
                               "stop": pos["stop"], "target": pos["target"]})
                pos = None
            else:
                pnl[i] += (c[i] - pos["mark"]) * side * units * cost.multiplier
                pos["mark"] = c[i]
        # ---- signal at the close of bar i (acts at the open of i+1) ----
        if pos is None and pending is None and trade_start <= times[i] <= trade_end and bis[i] >= lookback and not np.isnan(atr[i]) and sd[i] > 0:
            w0 = i - lookback
            new_low = l[i] <= l[w0:i].min()
            new_high = h[i] >= h[w0:i].max()
            if new_low and c[i] < vwap[i] - k_sigma * sd[i] and cvd[i] > cvd[w0:i].min() + cvd_margin * abs(cvd[w0:i].min() or 1):
                pending = {"side": 1, "session": sess[i], "stop": l[i] - stop_atr * atr[i], "target": vwap[i]}
            elif new_high and c[i] > vwap[i] + k_sigma * sd[i] and cvd[i] < cvd[w0:i].max() - cvd_margin * abs(cvd[w0:i].max() or 1):
                pending = {"side": -1, "session": sess[i], "stop": h[i] + stop_atr * atr[i], "target": vwap[i]}
    # daily P&L by session date
    daily = pd.Series(pnl, index=df.index).groupby(df.index.date).sum()
    daily.index = pd.to_datetime(daily.index)
    daily_ret = daily / capital
    tr = pd.DataFrame(trades)
    return daily_ret, tr, M.equity_from_returns(daily_ret, capital), df["delta_source"].iloc[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ", help="data/intraday/<symbol>_1min.csv")
    ap.add_argument("--k-sigma", type=float, default=1.5)
    ap.add_argument("--lookback", type=int, default=15)
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--max-bars", type=int, default=60)
    ap.add_argument("--cvd-margin", type=float, default=0.0, help="require CVD low/high to be this fraction above/below the window extreme")
    ap.add_argument("--units", type=int, default=1)
    ap.add_argument("--capital", type=float, default=50_000.0)
    ap.add_argument("--session", choices=["rth", "globex"], default="rth", help="where VWAP/CVD reset")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    bars = D.synthetic_intraday(days=60, seed=5) if a.synthetic else D.load_intraday(a.symbol, a.start, a.end)
    session_start = dtime(9, 30) if a.session == "rth" else dtime(18, 0)
    cost = costs.MNQ if a.symbol.upper().startswith("MNQ") or a.synthetic else costs.NQ
    fixed = dict(trade_start=dtime(9, 45), trade_end=dtime(15, 30), flat_at=dtime(15, 55), session_start=session_start,
                 cost=cost, units=a.units, capital=a.capital)
    params = dict(symbol="SYNTH" if a.synthetic else a.symbol, k_sigma=a.k_sigma, lookback=a.lookback, stop_atr=a.stop_atr,
                  max_bars=a.max_bars, cvd_margin=a.cvd_margin, session=a.session, units=a.units)
    r, tr, eq, src = run(bars, a.k_sigma, a.lookback, a.stop_atr, a.max_bars, a.cvd_margin, **fixed)
    extra = {"delta_source": src, "sessions": int(r.index.nunique()), "bars": len(bars)}
    if len(tr):
        extra["exit_reasons"] = tr["reason"].value_counts().to_dict()
    report.save_and_print(NAME, r, tr, eq, params, extra, synthetic=a.synthetic)
    if a.grid:
        from wsbt import engine
        grid = {"k_sigma": [1.0, 1.5, 2.0], "stop_atr": [1.0, 1.5, 2.5]}
        tab, best, dsr, win = engine.grid_search(
            lambda k_sigma, stop_atr: run(bars, k_sigma, a.lookback, stop_atr, a.max_bars, a.cvd_margin, **fixed)[0], grid, r.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
