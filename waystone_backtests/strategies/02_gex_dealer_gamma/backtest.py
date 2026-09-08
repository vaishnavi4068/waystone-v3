#!/usr/bin/env python3
"""02 — Dealer gamma exposure (GEX) regime on the index.

From each day's option-chain snapshot (OI by strike/expiry/right, IV) compute net dealer gamma.
Positive GEX -> dealers are long gamma -> they sell rallies and buy dips -> intraday mean reversion.
Negative GEX -> dealers short gamma -> hedging chases price -> intraday trend.

Modes:
  gap_fade     next day: if GEX(t) > +thr fade the opening gap back toward the prior close (exit at close);
               if GEX(t) < -thr go WITH the gap.  Flat when the gap is smaller than --gap-min.
  wall_revert  positive-GEX days only: at the open trade TOWARD the largest-gamma strike, exit at the close.

    python backtest.py --underlying SPX --mode gap_fade
    python backtest.py --synthetic --mode wall_revert
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, engine, report  # noqa: E402
from wsbt.engine import TradeSpec  # noqa: E402

NAME = "02_gex_dealer_gamma"
RISK_FREE = 0.04


# ══════════════════════════════════════════════════════════════════════════════
# GEX computation — reusable live
# ══════════════════════════════════════════════════════════════════════════════
def bs_gamma(spot: np.ndarray, strike: np.ndarray, t_years: np.ndarray, iv: np.ndarray, r: float = RISK_FREE) -> np.ndarray:
    t = np.maximum(t_years, 1 / 365 / 24)
    iv = np.maximum(iv, 0.01)
    d1 = (np.log(spot / strike) + (r + 0.5 * iv ** 2) * t) / (iv * np.sqrt(t))
    return norm.pdf(d1) / (spot * iv * np.sqrt(t))


def daily_gex(chain: pd.DataFrame, max_dte: int = 60) -> pd.DataFrame:
    """One row per date: total_gex ($ per 1% move), flip, max_gamma_strike, call_wall, put_wall, gex_z."""
    ch = chain.copy()
    ch["dte"] = (ch["expiry"] - ch["date"]).dt.days
    ch = ch[(ch["dte"] > 0) & (ch["dte"] <= max_dte)]
    if "gamma" not in ch or ch["gamma"].isna().all():
        ch["gamma"] = bs_gamma(ch["spot"].to_numpy(float), ch["strike"].to_numpy(float),
                               ch["dte"].to_numpy(float) / 365.0, ch["iv"].to_numpy(float))
    sign = np.where(ch["right"] == "C", 1.0, -1.0)
    ch["gex"] = sign * ch["gamma"] * ch["oi"] * 100.0 * ch["spot"] ** 2 * 0.01
    rows = []
    for dt, g in ch.groupby("date"):
        spot = float(g["spot"].iloc[0])
        by_k = g.groupby("strike")["gex"].sum().sort_index()
        cum = by_k.cumsum()
        flip = np.nan
        s = np.sign(cum.to_numpy())
        for i in range(1, len(s)):
            if s[i - 1] < 0 <= s[i]:
                flip = float(by_k.index[i]); break
        calls = g[g["right"] == "C"].groupby("strike")["gex"].sum()
        puts = g[g["right"] == "P"].groupby("strike")["gex"].sum()
        rows.append({"date": dt, "spot": spot, "total_gex": float(by_k.sum()),
                     "max_gamma_strike": float(by_k.abs().idxmax()) if len(by_k) else np.nan,
                     "call_wall": float(calls.idxmax()) if len(calls) else np.nan,
                     "put_wall": float(puts.idxmin()) if len(puts) else np.nan, "flip": flip})
    out = pd.DataFrame(rows).set_index("date").sort_index()
    roll = out["total_gex"].rolling(60, min_periods=20)
    out["gex_z"] = (out["total_gex"] - roll.mean()) / roll.std()
    out["gex_norm"] = out["total_gex"] / (out["spot"] ** 2 * 0.01)      # in "OI-gamma units", comparable across levels
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Strategy
# ══════════════════════════════════════════════════════════════════════════════
def build_specs(bars: pd.DataFrame, gex: pd.DataFrame, mode: str, thr_z: float, gap_min: float,
                stop_atr: float | None, min_dist: float, max_dist: float) -> list[TradeSpec]:
    df = bars.join(gex[["total_gex", "gex_z", "max_gamma_strike", "flip"]], how="left")
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    specs = []
    idx = df.index
    for i in range(1, len(df)):
        prev = df.iloc[i - 1]                 # chain snapshot and close of day t
        if np.isnan(prev["gex_z"]) or np.isnan(prev["atr"]):
            continue
        today = idx[i]
        o = df["open"].iloc[i]                # the open we act on — known at 09:30 of day t+1
        side = 0
        if mode == "gap_fade":
            gap = o / prev["close"] - 1.0
            if abs(gap) < gap_min:
                continue
            if prev["gex_z"] > thr_z:
                side = -1 if gap > 0 else 1   # fade
            elif prev["gex_z"] < -thr_z:
                side = 1 if gap > 0 else -1   # follow
        elif mode == "wall_revert":
            if prev["gex_z"] <= thr_z or np.isnan(prev["max_gamma_strike"]):
                continue
            dist = prev["max_gamma_strike"] / o - 1.0
            if abs(dist) < min_dist or abs(dist) > max_dist:
                continue
            side = 1 if dist > 0 else -1
        if side == 0:
            continue
        stop = o - side * stop_atr * prev["atr"] if stop_atr else None
        target = prev["close"] if mode == "gap_fade" and prev["gex_z"] > thr_z else (prev["max_gamma_strike"] if mode == "wall_revert" else None)
        specs.append(TradeSpec(date=today, side=side, entry="open", stop=stop, target=target, max_hold=1, tag=mode,
                               meta={"gex_z": round(float(prev["gex_z"]), 2), "total_gex_bn": round(float(prev["total_gex"]) / 1e9, 2)}))
    return specs


def run(bars, gex, mode, thr_z, gap_min, stop_atr, min_dist, max_dist, cost, units, capital):
    specs = build_specs(bars, gex, mode, thr_z, gap_min, stop_atr, min_dist, max_dist)
    return engine.simulate_trades(bars, specs, cost, {"units": units}, capital=capital, max_concurrent=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="SPX", help="chain file data/chains/<UNDERLYING>_chain.csv")
    ap.add_argument("--bars-symbol", default="^GSPC", help="daily OHLC used for fills (index or ES/MES continuous)")
    ap.add_argument("--mode", choices=["gap_fade", "wall_revert"], default="gap_fade")
    ap.add_argument("--thr-z", type=float, default=0.5, help="GEX z-score threshold (60-day)")
    ap.add_argument("--gap-min", type=float, default=0.002, help="min |open gap| to act, fraction")
    ap.add_argument("--stop-atr", type=float, default=1.0)
    ap.add_argument("--min-dist", type=float, default=0.002)
    ap.add_argument("--max-dist", type=float, default=0.02)
    ap.add_argument("--units", type=float, default=1, help="contracts (MES)")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    if a.synthetic:
        bars = D.synthetic_daily(500, s0=5000.0, ann_vol=0.16, seed=4)
        chain = D.synthetic_chain(bars["close"], strike_step=25.0)
        cost = costs.MES
    else:
        chain = D.load_chain_history(a.underlying)
        bars = D.load_daily(a.bars_symbol)
        bars = bars[bars.index.isin(chain["date"].unique())]
        cost = costs.MES
    gex = daily_gex(chain)
    params = dict(mode=a.mode, thr_z=a.thr_z, gap_min=a.gap_min, stop_atr=a.stop_atr, min_dist=a.min_dist, max_dist=a.max_dist, units=a.units)
    r, tr, eq = run(bars, gex, a.mode, a.thr_z, a.gap_min, a.stop_atr, a.min_dist, a.max_dist, cost, a.units, a.capital)
    extra = {"days_with_chain": len(gex), "pct_days_positive_gex": round(100 * float((gex["total_gex"] > 0).mean()), 1),
             "median_total_gex_bn": round(float(gex["total_gex"].median()) / 1e9, 2)}
    report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, extra, synthetic=a.synthetic)
    gex.to_csv(report.RESULTS / f"{NAME}_{a.mode}" / "gex_daily.csv")
    if a.grid:
        grid = {"thr_z": [0.0, 0.5, 1.0], "stop_atr": [0.75, 1.0, 1.5]}
        tab, best, dsr, win = engine.grid_search(
            lambda thr_z, stop_atr: run(bars, gex, a.mode, thr_z, a.gap_min, stop_atr, a.min_dist, a.max_dist, cost, a.units, a.capital)[0],
            grid, bars.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
