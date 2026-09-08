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
# liquid industry ETFs that sit one level below the SPDR sectors — same mandate, more dispersion to rank on
INDUSTRIES = ["SMH", "SOXX", "XBI", "IBB", "IGV", "KRE", "KBE", "XOP", "XME", "GDX", "ITB", "XHB", "XRT"]
UNIVERSES = {"sectors": SECTORS, "industries": SECTORS + INDUSTRIES}


def momentum_score(closes: pd.DataFrame, lookbacks=(63, 126, 252), skip: int = 5, risk_adjusted: bool = False) -> pd.DataFrame:
    """Mean of the lookback returns (skipping the last `skip` days).  risk_adjusted divides each lookback return by
    the realised vol over the same lookback, i.e. ranks on a rough Sharpe rather than raw return."""
    base = closes.shift(skip)
    parts = []
    for lb in lookbacks:
        ret = base / base.shift(lb) - 1.0
        if risk_adjusted:
            vol = base.pct_change().rolling(lb).std() * np.sqrt(252)
            ret = ret / vol.replace(0.0, np.nan)
        parts.append(ret)
    return sum(parts) / len(parts)


def rebalance_dates(index: pd.DatetimeIndex, freq: str) -> pd.DatetimeIndex:
    """Last trading day of each month ('M'), each half-month ('SM': last day <= 15th and month end) or each week ('W')."""
    s = index.to_series()
    if freq == "M":
        key = index.to_period("M")
    elif freq == "SM":
        key = pd.Index([f"{d.year}-{d.month:02d}-{'a' if d.day <= 15 else 'b'}" for d in index])
    elif freq == "W":
        key = index.to_period("W")
    else:
        raise ValueError(freq)
    last = s.groupby(np.asarray(key)).transform("max") == s
    return index[last.to_numpy()]


def weights_schedule(closes: pd.DataFrame, top_n: int, lookbacks, skip: int, sma_filter: bool, sma_len: int = 200,
                     freq: str = "M", weighting: str = "equal", vol_target: float = 0.0, vol_len: int = 63,
                     score: str = "raw", universe: list[str] | None = None, defensive: str | None = None,
                     abs_mom: bool = False) -> pd.DataFrame:
    """Target weights at each rebalance close.  weighting='invvol' sizes the winners by 1/σ (σ = vol_len-day realised),
    normalised to sum 1 before the trend filter zeroes failing slots.  A failing slot goes to `defensive` (e.g. IEF/GLD)
    when that asset is itself above its SMA, otherwise cash — the dual-momentum fallback.  abs_mom additionally requires
    a ranked sector's own momentum score to be positive.  vol_target > 0 (annualised %) scales the whole book so its
    ex-ante vol ≈ target, gross capped at 1.0."""
    universe = [c for c in (universe or list(closes.columns)) if c in closes.columns and c != defensive]
    score = momentum_score(closes, lookbacks, skip, risk_adjusted=(score == "riskadj"))
    sma = closes.rolling(sma_len).mean()
    daily = closes.pct_change()
    sigma = daily.rolling(vol_len).std() * np.sqrt(252)
    rows = {}
    for dt in rebalance_dates(closes.index, freq):
        s = score.loc[dt, universe].dropna()
        if len(s) < top_n:
            continue
        top = list(s.sort_values(ascending=False).index[:top_n])
        w = pd.Series(0.0, index=closes.columns)
        if weighting == "invvol":
            inv = pd.Series({sym: 1.0 / sigma.at[dt, sym] for sym in top if sigma.at[dt, sym] > 0})
            base = inv / inv.sum() if len(inv) else pd.Series(1.0 / top_n, index=top)
        else:
            base = pd.Series(1.0 / top_n, index=top)
        parked = 0.0
        for sym in top:
            ok = (not sma_filter) or (closes.at[dt, sym] > sma.at[dt, sym])
            if abs_mom and s[sym] <= 0:
                ok = False
            if ok:
                w[sym] = float(base.get(sym, 0.0))
            else:
                parked += float(base.get(sym, 0.0))
        if defensive and parked > 0 and defensive in closes.columns:
            d_ok = closes.at[dt, defensive] > sma.at[dt, defensive] if sma_filter else True
            if d_ok and not np.isnan(sma.at[dt, defensive]):
                w[defensive] = parked
        if vol_target > 0 and w.sum() > 0:
            held = w[w > 0]
            cov = daily.loc[:dt, held.index].tail(vol_len).cov() * 252
            port_vol = float(np.sqrt(held.to_numpy() @ cov.to_numpy() @ held.to_numpy()))
            if port_vol > 0:
                w = w * min(1.0, (vol_target / 100.0) / port_vol)
        rows[dt] = w
    return pd.DataFrame(rows).T.sort_index()


def run(frames: dict, top_n: int, lookbacks, skip: int, sma_filter: bool, cost_bps: float, capital: float,
        freq: str = "M", weighting: str = "equal", vol_target: float = 0.0, start: str | None = None,
        score: str = "raw", cash_yield: pd.Series | None = None, universe: list[str] | None = None,
        defensive: str | None = None, abs_mom: bool = False):
    closes = D.closes_panel(frames).dropna(how="all")
    w = weights_schedule(closes, top_n, lookbacks, skip, sma_filter, freq=freq, weighting=weighting, vol_target=vol_target,
                         score=score, universe=universe, defensive=defensive, abs_mom=abs_mom)
    if start:
        cut = pd.Timestamp(start)
        frames = {s: f[f.index >= cut] for s, f in frames.items()}
        first = min(f.index[0] for f in frames.values() if len(f))
        before = w[w.index < first]
        w = w[w.index >= first]
        if len(before):                                   # carry the last pre-window decision in as the opening book
            w = pd.concat([before.iloc[[-1]].set_axis([first]), w[w.index > first]]).sort_index()
    return engine.simulate_weights(frames, w, cost_bps=cost_bps, capital=capital, cash_yield=cash_yield)


def load_cash_yield(symbol: str | None) -> pd.Series | None:
    """Annualised % yield series for idle cash (e.g. ^IRX = 13-week T-bill).  None = cash earns nothing."""
    if not symbol:
        return None
    try:
        return D.load_daily(symbol)["close"]
    except D.DataMissing:
        print(f"  {symbol} not found — cash earns 0 (python tools/fetch_yf.py --symbols {symbol})")
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--lookbacks", type=int, nargs="+", default=[63, 126, 252])
    ap.add_argument("--skip", type=int, default=5)
    ap.add_argument("--no-sma-filter", action="store_true")
    ap.add_argument("--rebalance", choices=["M", "SM", "W"], default="M", help="monthly, semi-monthly or weekly")
    ap.add_argument("--weighting", choices=["equal", "invvol"], default="equal")
    ap.add_argument("--vol-target", type=float, default=0.0, help="annualised %% vol target for the book (0 = off, gross <= 1)")
    ap.add_argument("--score", choices=["raw", "riskadj"], default="raw", help="rank on raw return or return / realised vol")
    ap.add_argument("--cash-yield", default="^IRX", help="daily series (annualised %%) earned on idle cash; '' = none")
    ap.add_argument("--universe", choices=sorted(UNIVERSES), default="sectors",
                    help="sectors = 11 SPDRs; industries = sectors + liquid industry ETFs (SMH, XBI, KRE, XOP, ...)")
    ap.add_argument("--defensive", default="", help="ETF that filtered slots rotate into when it is trending (e.g. IEF, GLD); '' = cash")
    ap.add_argument("--abs-mom", action="store_true", help="also require a ranked sector's momentum score > 0")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--cost-mult", type=float, default=1.0, help="scale cost_bps (2.0 = stage-gate cost stress)")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--grid", action="store_true")
    a = ap.parse_args()

    universe = UNIVERSES[a.universe]
    defensive = a.defensive or None
    want = universe + ["SPY"] + ([defensive] if defensive else [])
    if a.synthetic:
        frames = D.synthetic_panel(want, n=2600, seed=6)
    else:
        frames = D.load_many(want, None, a.end, strict=False)      # full history: signals warm up before --start
        missing = [s for s in want if s not in frames]
        if missing:
            print(f"missing {missing} — run: python tools/fetch_yf.py --symbols {' '.join(missing)}")
        universe = [s for s in universe if s in frames]
        if defensive and defensive not in frames:
            defensive = None
    spy = frames.pop("SPY", None)
    cost_bps = a.cost_bps * a.cost_mult
    cash = None if a.synthetic else load_cash_yield(a.cash_yield)
    params = dict(top_n=a.top_n, lookbacks=a.lookbacks, skip=a.skip, sma_filter=not a.no_sma_filter, rebalance=a.rebalance,
                  weighting=a.weighting, vol_target=a.vol_target, score=a.score, cash_yield=a.cash_yield if cash is not None else None,
                  universe_name=a.universe, defensive=defensive, abs_mom=a.abs_mom,
                  cost_bps=cost_bps, cost_mult=a.cost_mult, universe=sorted(universe), start=a.start, end=a.end)
    r, tr, eq = run(frames, a.top_n, tuple(a.lookbacks), a.skip, not a.no_sma_filter, cost_bps, a.capital,
                    freq=a.rebalance, weighting=a.weighting, vol_target=a.vol_target, start=a.start, score=a.score, cash_yield=cash,
                    universe=universe, defensive=defensive, abs_mom=a.abs_mom)
    legs = tr.attrs.get("legs")
    extra = {"rebalances": int(len(tr)), "avg_turnover_pct": round(100 * float(tr["turnover"].mean()), 1) if len(tr) else 0,
             "orders": int(len(tr) * 2) if len(tr) else 0,
             "notes": ["trades = per-ETF round trips (weight 0 -> held -> 0); a monthly rotation sleeve is low-frequency by design."]}
    if spy is not None:
        bh = M.summary(spy["close"].reindex(r.index).pct_change().fillna(0))
        extra["benchmark_spy"] = f"CAGR {bh.get('cagr_pct')}%  Sharpe {bh.get('sharpe')}  maxDD {bh.get('max_drawdown_pct')}%"
    report.save_and_print(NAME, r, legs if legs is not None and len(legs) else None, eq, params, extra, synthetic=a.synthetic)
    if a.grid:
        grid = {"top_n": [2, 3, 4], "sma_filter": [True, False]}
        tab, best, dsr, win = engine.grid_search(
            lambda top_n, sma_filter: run(frames, top_n, tuple(a.lookbacks), a.skip, sma_filter, cost_bps, a.capital,
                                          freq=a.rebalance, weighting=a.weighting, vol_target=a.vol_target, start=a.start,
                                          score=a.score, cash_yield=cash, universe=universe, defensive=defensive,
                                          abs_mom=a.abs_mom)[0],
            grid, r.index)
        report.print_grid(tab, best, dsr, win)


if __name__ == "__main__":
    main()
