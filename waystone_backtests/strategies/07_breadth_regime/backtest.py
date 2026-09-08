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
    def pct_above(length: int) -> pd.Series:
        sma = closes.rolling(length).mean()
        above = (closes > sma).where(closes.notna() & sma.notna())
        return above.mean(axis=1, skipna=True) * 100.0

    pct_above_50 = pct_above(50)
    ret = closes.pct_change()
    adv = (ret > 0).sum(axis=1)
    dec = (ret < 0).sum(axis=1)
    rana = 1000.0 * (adv - dec) / (adv + dec).replace(0, np.nan)
    mcclellan = rana.ewm(span=19, adjust=False).mean() - rana.ewm(span=39, adjust=False).mean()
    out = pd.DataFrame({"pct_above_50": pct_above_50, "pct_above_200": pct_above(200), "mcclellan": mcclellan,
                        "n_names": closes.notna().sum(axis=1)})
    out["pct_above_50_chg10"] = out["pct_above_50"].diff(10)
    out["pct_above_50_sm10"] = out["pct_above_50"].rolling(10).mean()
    # Zweig-style thrust: share of advancers (10-day EMA) swings from washed-out (<40%) to strong (>61.5%) within 10 days
    adv_ratio = (adv / (adv + dec).replace(0, np.nan)).ewm(span=10, adjust=False).mean()
    out["zweig"] = ((adv_ratio > 0.615) & (adv_ratio.rolling(10).min() < 0.40)).astype(float)
    return out


def _hysteresis(x: pd.Series, hi: float, lo: float) -> pd.Series:
    """ON once x > hi, stays ON until x < lo (removes whipsaw around a single threshold)."""
    state = np.zeros(len(x))
    on = False
    vals = x.to_numpy()
    for i, v in enumerate(vals):
        if np.isnan(v):
            state[i] = float(on)
            continue
        if not on and v > hi:
            on = True
        elif on and v < lo:
            on = False
        state[i] = float(on)
    return pd.Series(state, index=x.index)


MODES = ["base", "pct50", "mcclellan", "thrust", "pct50_only", "hyst", "scaled", "pct200", "reentry", "smooth"]


def targets(spy: pd.DataFrame, br: pd.DataFrame, mode: str, thr: float, band: float = 20.0, floor: float = 0.5) -> pd.Series:
    """Exposure in [0, 1] decided at each close.  thr = breadth threshold (%), band = hysteresis width for 'hyst',
    floor = reduced size for 'scaled' when breadth is weak."""
    sma200 = spy["close"].rolling(200).mean()
    base = (spy["close"] > sma200)
    b = br.reindex(spy.index).ffill()
    if mode == "base":
        on = base.astype(float)
    elif mode == "pct50":
        on = (base & (b["pct_above_50"] > thr)).astype(float)
    elif mode == "mcclellan":
        on = (base & (b["mcclellan"] > 0)).astype(float)
    elif mode == "thrust":                      # breadth improving: filter is on when %>50d rose over 10 days OR is already high
        on = (base & ((b["pct_above_50_chg10"] > 0) | (b["pct_above_50"] > 60))).astype(float)
    elif mode == "pct50_only":                  # breadth without the price trend
        on = (b["pct_above_50"] > thr).astype(float)
    elif mode == "hyst":                        # ON above thr, OFF only below thr - band: fewer whipsaws than a single line
        on = base.astype(float) * _hysteresis(b["pct_above_50"], thr, thr - band)
    elif mode == "scaled":                      # breadth sizes the book instead of switching it: full when broad, `floor` when narrow
        on = base.astype(float) * np.where(b["pct_above_50"] > thr, 1.0, floor)
    elif mode == "pct200":                      # slower breadth: share of names above their own 200-day
        on = (base & (b["pct_above_200"] > thr)).astype(float)
    elif mode == "reentry":                     # trend filter, but a Zweig thrust re-enters early (before SPY reclaims SMA200)
        thrust = (b["zweig"].rolling(20).max() > 0)
        on = (base | thrust).astype(float)
    elif mode == "smooth":                      # 10-day mean of %>50d, removes single-day noise
        on = (base & (b["pct_above_50_sm10"] > thr)).astype(float)
    else:
        raise ValueError(mode)
    return pd.Series(on, index=spy.index, dtype=float).fillna(0.0)


def run(spy, br, mode, thr, capital, cost=costs.US_ETF, start=None, end=None, band=20.0, floor=0.5):
    tgt = targets(spy, br, mode, thr, band, floor)      # decided on full history, so the switch is warm on day 1
    bars = spy
    if start:
        bars = bars[bars.index >= pd.Timestamp(start)]
    if end:
        bars = bars[bars.index <= pd.Timestamp(end)]
    return engine.simulate_positions(bars, tgt.reindex(bars.index), cost, {"notional": capital}, capital=capital)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-symbol", default="SPY")
    ap.add_argument("--list", default="nsdq250.csv")
    ap.add_argument("--max-symbols", type=int, default=None, help="constituents to load (first N of the list)")
    ap.add_argument("--mode", choices=MODES, default="pct50")
    ap.add_argument("--thr", type=float, default=50.0, help="breadth threshold (%% of names above their SMA)")
    ap.add_argument("--band", type=float, default=20.0, help="hyst: switch OFF only below thr - band")
    ap.add_argument("--floor", type=float, default=0.5, help="scaled: exposure when breadth is below thr")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--cost-mult", type=float, default=1.0, help="scale commission + slippage (2.0 = stage-gate cost stress)")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()

    if a.synthetic:
        syms = [f"S{i:03d}" for i in range(80)]
        panel = D.synthetic_panel(syms + ["SPY"], n=2600, seed=17)
        spy = panel.pop("SPY")
        closes = D.closes_panel(panel)
    else:
        syms = D.load_symbol_list(a.list, a.max_symbols)
        frames = D.load_many(syms, None, a.end, strict=False)          # full history: breadth warms up before --start
        if len(frames) < 30:
            raise SystemExit(f"only {len(frames)} constituent files found — run: python tools/fetch_yf.py --list {a.list}")
        closes = D.closes_panel(frames)
        spy = D.load_daily(a.index_symbol, None, a.end)
    cost = costs.US_ETF.scaled(a.cost_mult)
    br = breadth_series(closes)
    report.RESULTS.mkdir(parents=True, exist_ok=True)
    br.to_csv(report.RESULTS / f"{NAME}_breadth.csv")

    # comparison table across filters
    rows = []
    for mode in MODES:
        r, tr, eq = run(spy, br, mode, a.thr, a.capital, cost, a.start, a.end, a.band, a.floor)
        s = M.summary(r, tr)
        rows.append({"filter": mode, "cagr_pct": s.get("cagr_pct"), "sharpe": s.get("sharpe"), "max_dd_pct": s.get("max_drawdown_pct"),
                     "exposure_pct": s.get("exposure_pct"), "trades": s.get("trades")})
    spy_win = spy[(spy.index >= pd.Timestamp(a.start)) if a.start else slice(None)]
    bh = M.summary(spy_win["close"].pct_change().fillna(0))
    rows.append({"filter": "SPY buy&hold", "cagr_pct": bh.get("cagr_pct"), "sharpe": bh.get("sharpe"), "max_dd_pct": bh.get("max_drawdown_pct"),
                 "exposure_pct": 100.0, "trades": None})
    table = pd.DataFrame(rows)
    print("\n  Breadth filter comparison (base = SPY long when above SMA200)")
    print(table.to_string(index=False))
    print(f"  constituents used: {int(br['n_names'].median())} (median per day)   breadth series -> results/{NAME}_breadth.csv\n")

    r, tr, eq = run(spy, br, a.mode, a.thr, a.capital, cost, a.start, a.end, a.band, a.floor)
    # regime series other sleeves can consume (--regime-file): 1 = risk-on (new entries allowed), 0 = risk-off
    regime = targets(spy, br, a.mode, a.thr, a.band, a.floor).rename("on").to_frame()
    regime.index.name = "date"
    regime.to_csv(report.RESULTS / f"{NAME}_regime.csv")
    params = dict(mode=a.mode, thr=a.thr, band=a.band, floor=a.floor, constituents=int(br["n_names"].median()), index=a.index_symbol, list=a.list,
                  cost_mult=a.cost_mult, start=a.start, end=a.end)
    base_row = table[table["filter"] == "base"].iloc[0]
    extra = {"comparison": table.set_index("filter")["sharpe"].to_dict(), "regime_file": f"{NAME}_regime.csv",
             "comparison_max_dd": table.set_index("filter")["max_dd_pct"].to_dict(),
             "base_max_dd_pct": base_row["max_dd_pct"], "base_cagr_pct": base_row["cagr_pct"],
             "risk_on_pct": round(100 * float(regime["on"].reindex(r.index).mean()), 1),
             "notes": ["Regime filter, not a trade generator: P&L shown is SPY held only while the switch is on. "
                       "Trade-count / payoff gates are evaluated through the sleeves this filter gates (01 pullback)."]}
    report.save_and_print(f"{NAME}_{a.mode}", r, tr, eq, params, extra, synthetic=a.synthetic)


if __name__ == "__main__":
    main()
