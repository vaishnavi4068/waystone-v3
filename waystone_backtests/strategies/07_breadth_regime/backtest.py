#!/usr/bin/env python3
"""07 — Breadth as a regime filter (not a strategy on its own).

Computes two breadth series from S&P 500 constituents — % of names above their 50-day SMA and the
McClellan oscillator — and shows what each does when used as an ON/OFF switch on a simple base sleeve
(SPY long when above its 200-day SMA).  The output is a comparison table: base alone vs base+filter.

    python backtest.py --max-symbols 150
    python backtest.py --synthetic
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wsbt import costs, data as D, engine, metrics as M, report  # noqa: E402

NAME = "07_breadth_regime"


def breadth_series(closes: pd.DataFrame) -> pd.DataFrame:
    """closes: constituents (columns) x dates.  Symbols without data on a day are excluded from that day."""
    sma50 = closes.rolling(50).mean()
    above = (closes > sma50).where(closes.notna() & sma50.notna())
    pct_above_50 = above.mean(axis=1, skipna=True) * 100.0
    ret = closes.pct_change()
    adv = (ret > 0).sum(axis=1)
    dec = (ret < 0).sum(axis=1)
    rana = 1000.0 * (adv - dec) / (adv + dec).replace(0, np.nan)
    mcclellan = rana.ewm(span=19, adjust=False).mean() - rana.ewm(span=39, adjust=False).mean()
    out = pd.DataFrame({"pct_above_50": pct_above_50, "mcclellan": mcclellan, "n_names": closes.notna().sum(axis=1)})
    out["pct_above_50_chg10"] = out["pct_above_50"].diff(10)
    return out


def targets(spy: pd.DataFrame, br: pd.DataFrame, mode: str, thr: float) -> pd.Series:
    sma200 = spy["close"].rolling(200).mean()
    base = (spy["close"] > sma200)
    b = br.reindex(spy.index).ffill()
    if mode == "base":
        on = base
    elif mode == "pct50":
        on = base & (b["pct_above_50"] > thr)
    elif mode == "mcclellan":
        on = base & (b["mcclellan"] > 0)
    elif mode == "thrust":                      # breadth improving: filter is on when %>50d rose over 10 days OR is already high
        on = base & ((b["pct_above_50_chg10"] > 0) | (b["pct_above_50"] > 60))
    elif mode == "pct50_only":                  # breadth without the price trend
        on = b["pct_above_50"] > thr
    else:
        raise ValueError(mode)
    return on.astype(float)


def run(spy, br, mode, thr, capital):
    tgt = targets(spy, br, mode, thr)
    return engine.simulate_positions(spy, tgt, costs.US_ETF, {"notional": capital}, capital=capital)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-symbol", default="SPY")
    ap.add_argument("--list", default="sp500.csv")
    ap.add_argument("--max-symbols", type=int, default=150, help="constituents to load (first N of the list)")
    ap.add_argument("--mode", choices=["base", "pct50", "mcclellan", "thrust", "pct50_only"], default="pct50")
    ap.add_argument("--thr", type=float, default=50.0, help="pct_above_50 threshold")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()

    if a.synthetic:
        syms = [f"S{i:03d}" for i in range(80)]
        panel = D.synthetic_panel(syms + ["SPY"], n=2600, seed=17)
        spy = panel.pop("SPY")
        closes = D.closes_panel(panel)
    else:
        syms = D.load_symbol_list(a.list, a.max_symbols)
        frames = D.load_many(syms, a.start, strict=False)
        if len(frames) < 30:
            raise SystemExit(f"only {len(frames)} constituent files found — run: python tools/fetch_yf.py --sp500 --max-symbols {a.max_symbols}")
        closes = D.closes_panel(frames)
        spy = D.load_daily(a.index_symbol, a.start)
    br = breadth_series(closes)
    report.RESULTS.mkdir(parents=True, exist_ok=True)
    br.to_csv(report.RESULTS / f"{NAME}_breadth.csv")

    # comparison table across filters
    rows = []
    for mode in ["base", "pct50", "mcclellan", "thrust", "pct50_only"]:
        r, tr, eq = run(spy, br, mode, a.thr, a.capital)
        s = M.summary(r, tr)
        rows.append({"filter": mode, "cagr_pct": s.get("cagr_pct"), "sharpe": s.get("sharpe"), "max_dd_pct": s.get("max_drawdown_pct"),
                     "exposure_pct": s.get("exposure_pct"), "trades": s.get("trades")})
    bh = M.summary(spy["close"].pct_change().fillna(0))
    rows.append({"filter": "SPY buy&hold", "cagr_pct": bh.get("cagr_pct"), "sharpe": bh.get("sharpe"), "max_dd_pct": bh.get("max_drawdown_pct"),
                 "exposure_pct": 100.0, "trades": None})
    table = pd.DataFrame(rows)
    print("\n  Breadth filter comparison (base = SPY long when above SMA200)")
    print(table.to_string(index=False))
    print(f"  constituents used: {int(br['n_names'].median())} (median per day)   breadth series -> results/{NAME}_breadth.csv\n")

    r, tr, eq = run(spy, br, a.mode, a.thr, a.capital)
    params = dict(mode=a.mode, thr=a.thr, constituents=int(br["n_names"].median()), index=a.index_symbol)
    report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, {"comparison": table.set_index("filter")["sharpe"].to_dict()}, synthetic=a.synthetic)


if __name__ == "__main__":
    main()
