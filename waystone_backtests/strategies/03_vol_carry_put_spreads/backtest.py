#!/usr/bin/env python3
"""03 — Volatility carry: SPX/XSP put credit spreads gated by the VIX term structure.

Sells a defined-risk put spread only when the term structure is in contango (VIX/VIX3M below a
threshold) and IV is not at the bottom of its range; manages at 50% of max profit, 21 DTE, or a
loss stop.  Option prices are MODELLED (Black-Scholes with VIX as the vol input plus a skew
multiplier) because free daily option-chain history does not exist — see README for what that
does and does not tell you.

    python backtest.py --spx ^GSPC --vix ^VIX --vix3m ^VIX3M
    python backtest.py --synthetic --grid
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
from wsbt import costs, data as D, metrics as M, report  # noqa: E402
from wsbt.calendar_utils import next_expiry_on_or_after  # noqa: E402

NAME = "03_vol_carry_put_spreads"
R = 0.04


def bs_put(S, K, T, sigma, r=R):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def iv_at(vix: float, S: float, K: float, slope: float) -> float:
    """Put-wing skew: IV rises `slope` x (percent OTM) above the VIX level.  slope=2 -> 7% OTM = +14%."""
    return vix / 100.0 * (1.0 + slope * max(0.0, (S - K) / S))


def strike_for_put_delta(S, T, sigma, delta_abs, step, r=R):
    d1 = -norm.ppf(delta_abs)
    K = S * math.exp(-(d1 * sigma * math.sqrt(T) - (r + 0.5 * sigma * sigma) * T))
    return math.floor(K / step) * step


class PolygonMarks:
    """Real daily option prices from Polygon/Massive for the two legs (cached under data/option_bars/).
    Returns None for a (date) with no bar so the caller can fall back to the model for that day."""

    def __init__(self, root: str):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import fetch_polygon as FP
        self.FP, self.client, self.root = FP, FP.Client(), root
        self.used = 0
        self.fallback = 0

    def legs(self, expiry, k_short, k_long, start, end) -> pd.DataFrame | None:
        e = pd.Timestamp(expiry).date()
        frames = {}
        for name, k in (("short", k_short), ("long", k_long)):
            tk = self.FP.option_ticker(self.root, e, "P", k)
            df = self.FP.option_bars_cached(self.client, tk, pd.Timestamp(start).date().isoformat(), pd.Timestamp(end).date().isoformat())
            if df.empty and self.root == "SPX":                                   # monthly listed as SPXW? try the weekly root
                df = self.FP.option_bars_cached(self.client, self.FP.option_ticker("SPXW", e, "P", k), pd.Timestamp(start).date().isoformat(), pd.Timestamp(end).date().isoformat())
            if df.empty:
                return None
            frames[name] = df.set_index("date")[["open", "close"]].rename(columns={"open": f"{name}_open", "close": f"{name}_close"})
        return frames["short"].join(frames["long"], how="inner")


def run(px: pd.DataFrame, ratio_max: float, delta: float, width_pct: float, dte_entry: int, dte_exit: int,
        take_profit: float, stop_mult: float, skew_slope: float, vix_pct_min: float,
        n_spreads: int, step: float, mult: float, leg_cost: costs.CostModel, capital: float,
        marks: "PolygonMarks | None" = None):
    """px: DataFrame with columns spx_open, spx_close, vix_open, vix_close, vix3m_close, ratio, vix_pct.
    marks: optional real-price provider; when a day has real bars they replace the Black-Scholes mark."""
    idx = px.index
    n = len(px)
    pnl = np.zeros(n)
    trades = []
    pos = None
    for i in range(1, n):
        row = px.iloc[i]
        prev = px.iloc[i - 1]
        day_pnl = 0.0
        # ---- manage an open spread at today's close ----
        if pos is not None:
            T = max((pos["expiry"] - idx[i]).days, 0) / 365.0
            Sc = row["spx_close"]
            value = None
            if pos.get("legs") is not None and idx[i] in pos["legs"].index:
                lg = pos["legs"].loc[idx[i]]
                value = float(lg["short_close"] - lg["long_close"])
                if marks: marks.used += 1
            if value is None:
                if marks: marks.fallback += 1
                value = (bs_put(Sc, pos["k_short"], T, iv_at(row["vix_close"], Sc, pos["k_short"], skew_slope))
                         - bs_put(Sc, pos["k_long"], T, iv_at(row["vix_close"], Sc, pos["k_long"], skew_slope)))
            value = max(value, 0.0)
            reason = None
            if value <= pos["credit"] * (1 - take_profit):
                reason = "take_profit"
            elif value >= pos["credit"] * (1 + stop_mult):
                reason = "stop"
            elif (pos["expiry"] - idx[i]).days <= dte_exit:
                reason = "dte_exit"
            elif T <= 0:
                reason = "expiry"
            if reason:
                exit_px = value + 2 * leg_cost.slippage_abs                        # pay the spread to close
                comm = 2 * leg_cost.commission * n_spreads
                day_pnl += (pos["mark"] - exit_px) * mult * n_spreads - comm
                total = (pos["credit_net"] - exit_px) * mult * n_spreads - comm - pos["entry_comm"]
                trades.append({"entry_date": pos["entry_date"], "exit_date": idx[i], "expiry": pos["expiry"], "k_short": pos["k_short"],
                               "k_long": pos["k_long"], "credit": round(pos["credit"], 2), "exit_value": round(exit_px, 2),
                               "pnl": round(total, 2), "days_held": i - pos["i0"], "reason": reason, "side": -1, "units": n_spreads,
                               "entry_vix": pos["entry_vix"], "entry_ratio": pos["entry_ratio"], "pricing": pos.get("source", "model")})
                pos = None
            else:
                day_pnl += (pos["mark"] - value) * mult * n_spreads
                pos["mark"] = value
        # ---- entry at today's open, decided on yesterday's close ----
        if pos is None and prev["ratio"] < ratio_max and prev["vix_pct"] >= vix_pct_min and not np.isnan(prev["vix3m_close"]):
            expiry = next_expiry_on_or_after(idx[i], dte_entry)
            T = (expiry - idx[i]).days / 365.0
            S = row["spx_open"]
            # solve the short strike with skew: iterate once (delta at skewed IV)
            k_short = strike_for_put_delta(S, T, row["vix_open"] / 100 * 1.1, delta, step)
            k_short = strike_for_put_delta(S, T, iv_at(row["vix_open"], S, k_short, skew_slope), delta, step)
            k_long = k_short - max(step, math.floor(S * width_pct / step) * step)
            legs = marks.legs(expiry, k_short, k_long, idx[i], expiry) if marks else None
            if legs is not None and idx[i] in legs.index:
                credit = float(legs.loc[idx[i], "short_open"] - legs.loc[idx[i], "long_open"])
                source = "polygon"
            else:
                credit = (bs_put(S, k_short, T, iv_at(row["vix_open"], S, k_short, skew_slope))
                          - bs_put(S, k_long, T, iv_at(row["vix_open"], S, k_long, skew_slope)))
                source = "model"
            if credit <= 0.05:
                continue
            credit_net = credit - 2 * leg_cost.slippage_abs
            comm = 2 * leg_cost.commission * n_spreads
            # mark at today's close
            T_c = max((expiry - idx[i]).days, 0) / 365.0
            Sc = row["spx_close"]
            if legs is not None and idx[i] in legs.index:
                value_c = float(legs.loc[idx[i], "short_close"] - legs.loc[idx[i], "long_close"])
            else:
                value_c = (bs_put(Sc, k_short, T_c, iv_at(row["vix_close"], Sc, k_short, skew_slope))
                           - bs_put(Sc, k_long, T_c, iv_at(row["vix_close"], Sc, k_long, skew_slope)))
            day_pnl += (credit_net - value_c) * mult * n_spreads - comm
            pos = {"entry_date": idx[i], "i0": i, "expiry": expiry, "k_short": k_short, "k_long": k_long, "credit": credit,
                   "credit_net": credit_net, "mark": value_c, "entry_comm": comm, "entry_vix": round(float(row["vix_open"]), 2),
                   "entry_ratio": round(float(prev["ratio"]), 3), "legs": legs, "source": source}
        pnl[i] = day_pnl
    daily_ret = pd.Series(pnl / capital, index=idx)
    return daily_ret, pd.DataFrame(trades), M.equity_from_returns(daily_ret, capital)


def build_panel(spx: pd.DataFrame, vix: pd.DataFrame, vix3m: pd.DataFrame) -> pd.DataFrame:
    px = pd.DataFrame({"spx_open": spx["open"], "spx_close": spx["close"], "vix_open": vix["open"], "vix_close": vix["close"],
                       "vix3m_close": vix3m["close"]}).dropna(subset=["spx_close", "vix_close"])
    px["vix3m_close"] = px["vix3m_close"].ffill()
    px["ratio"] = px["vix_close"] / px["vix3m_close"]
    px["vix_pct"] = px["vix_close"].rolling(252, min_periods=60).rank(pct=True)
    return px


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spx", default="^GSPC")
    ap.add_argument("--vix", default="^VIX")
    ap.add_argument("--vix3m", default="^VIX3M")
    ap.add_argument("--product", choices=["SPX", "XSP"], default="XSP")
    ap.add_argument("--pricing", choices=["model", "polygon"], default="model",
                    help="polygon = real daily option prices from Polygon/Massive for the two legs (needs POLYGON_API_KEY)")
    ap.add_argument("--ratio-max", type=float, default=0.95, help="enter only if VIX/VIX3M below this (contango)")
    ap.add_argument("--vix-pct-min", type=float, default=0.15, help="skip when VIX is in the bottom x of its 1y range")
    ap.add_argument("--delta", type=float, default=0.15)
    ap.add_argument("--width-pct", type=float, default=0.01, help="spread width as fraction of spot (1%% = 50 SPX / 5 XSP points)")
    ap.add_argument("--dte-entry", type=int, default=45)
    ap.add_argument("--dte-exit", type=int, default=21)
    ap.add_argument("--take-profit", type=float, default=0.5)
    ap.add_argument("--stop-mult", type=float, default=2.0, help="close when loss = stop_mult x credit")
    ap.add_argument("--skew-slope", type=float, default=2.0, help="put-wing skew: IV = VIX x (1 + slope x %%OTM)")
    ap.add_argument("--n-spreads", type=int, default=1)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    if a.synthetic:
        spx = D.synthetic_daily(2500, s0=4500.0, ann_vol=0.16, seed=12)
        v = D.synthetic_vix(spx)
        vix = pd.DataFrame({"open": v["vix"].shift(1).bfill(), "close": v["vix"]})
        vix3m = pd.DataFrame({"close": v["vix3m"]})
    else:
        spx, vix, vix3m = D.load_daily(a.spx, a.start), D.load_daily(a.vix, a.start), D.load_daily(a.vix3m, a.start)
    px = build_panel(spx, vix, vix3m)
    step, mult, leg = (5.0, 100.0, costs.SPX_OPTION_LEG) if a.product == "SPX" else (1.0, 100.0, costs.XSP_OPTION_LEG)
    if a.product == "XSP":
        px[["spx_open", "spx_close"]] = px[["spx_open", "spx_close"]] / 10.0
    params = dict(product=a.product, pricing=a.pricing, ratio_max=a.ratio_max, vix_pct_min=a.vix_pct_min, delta=a.delta, width_pct=a.width_pct,
                  dte_entry=a.dte_entry, dte_exit=a.dte_exit, take_profit=a.take_profit, stop_mult=a.stop_mult,
                  skew_slope=a.skew_slope, n_spreads=a.n_spreads)
    kw = dict(width_pct=a.width_pct, dte_entry=a.dte_entry, dte_exit=a.dte_exit, stop_mult=a.stop_mult, skew_slope=a.skew_slope,
              vix_pct_min=a.vix_pct_min, n_spreads=a.n_spreads, step=step, mult=mult, leg_cost=leg, capital=a.capital)
    marks = PolygonMarks(a.product) if a.pricing == "polygon" and not a.synthetic else None
    r, tr, eq = run(px, a.ratio_max, a.delta, take_profit=a.take_profit, marks=marks, **kw)
    bh = M.summary(px["spx_close"].pct_change().fillna(0))
    extra = {"pct_days_in_contango": round(100 * float((px["ratio"] < a.ratio_max).mean()), 1),
             "benchmark_spx_buy_hold": f"CAGR {bh.get('cagr_pct')}%  Sharpe {bh.get('sharpe')}  maxDD {bh.get('max_drawdown_pct')}%",
             "pricing": (f"POLYGON daily option prices: {marks.used} real marks, {marks.fallback} model fallbacks" if marks
                         else "MODELLED (Black-Scholes, VIX x skew) — not traded prices")}
    report.save_and_print(NAME, r, tr, eq, params, extra, synthetic=a.synthetic)
    if a.grid:
        grid = {"ratio_max": [0.9, 0.95, 1.0], "delta": [0.10, 0.15, 0.20], "take_profit": [0.5, 0.75]}
        tab, best, dsr, win = engine_grid(px, grid, kw)
        report.print_grid(tab, best, dsr, win)


def engine_grid(px, grid, kw):
    from wsbt import engine
    return engine.grid_search(lambda ratio_max, delta, take_profit: run(px, ratio_max, delta, take_profit=take_profit, **kw)[0], grid, px.index)


if __name__ == "__main__":
    main()
