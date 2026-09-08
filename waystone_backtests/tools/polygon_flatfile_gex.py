#!/usr/bin/env python3
"""Build a HISTORICAL SPX chain file for the GEX strategy from Polygon/Massive options flat files.

Polygon has no historical open interest, but its S3 flat files carry the DAILY BAR of every OPRA
contract.  From those we can reconstruct, per day and contract: close, volume, and (by inverting
Black-Scholes against the I:SPX close) implied vol and gamma.  Weighting gamma by VOLUME instead of
OI gives a "flow gamma" proxy — not the same thing as dealer inventory, but a backtestable stand-in
until the nightly snapshots (which do carry OI) have accumulated history.

    # 1. sync the day-aggregate flat files you need (Polygon S3, credentials in the dashboard):
    aws s3 sync s3://flatfiles/us_options_opra/day_aggs_v1/2025/ data/flatfiles/2025/ --endpoint-url https://files.polygon.io
    # 2. daily I:SPX from the API
    python tools/fetch_polygon.py indices --symbols SPX --start 2020-01-01
    # 3. build the chain history
    python tools/polygon_flatfile_gex.py --flatfile-dir data/flatfiles --roots SPX SPXW --start 2025-01-01 --max-dte 60

Writes data/chains/SPX_chain.csv with columns date,spot,expiry,strike,right,oi,iv,gamma,volume,close,oi_source
where oi = volume and oi_source = "volume" — the GEX backtest reads it unchanged.
"""
from __future__ import annotations

import argparse
import gzip
import re
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsbt import data as D  # noqa: E402

TICK_RE = re.compile(r"^O:([A-Z]+)(\d{6})([CP])(\d{8})$")
R = 0.04


def parse_ticker(t: str):
    m = TICK_RE.match(t)
    if not m:
        return None
    root, ymd, right, k = m.groups()
    return root, datetime.strptime(ymd, "%y%m%d").date(), right, int(k) / 1000.0


def bs_price(S, K, T, sig, right):
    d1 = (np.log(S / K) + (R + 0.5 * sig ** 2) * T) / (sig * np.sqrt(T))
    d2 = d1 - sig * np.sqrt(T)
    call = S * norm.cdf(d1) - K * np.exp(-R * T) * norm.cdf(d2)
    put = K * np.exp(-R * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return np.where(right == 1, call, put)


def implied_vol(price, S, K, T, right, iters: int = 40):
    """Vectorised bisection on [0.01, 3.0]."""
    lo, hi = np.full_like(price, 0.01), np.full_like(price, 3.0)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, T, mid, right)
        too_high = p > price
        hi = np.where(too_high, mid, hi)
        lo = np.where(too_high, lo, mid)
    iv = 0.5 * (lo + hi)
    intrinsic = np.where(right == 1, np.maximum(S - K, 0), np.maximum(K - S, 0))
    iv[price <= intrinsic + 1e-6] = np.nan            # no time value -> IV undefined
    return iv


def bs_gamma(S, K, T, sig):
    d1 = (np.log(S / K) + (R + 0.5 * sig ** 2) * T) / (sig * np.sqrt(T))
    return norm.pdf(d1) / (S * sig * np.sqrt(T))


def process_day(path: Path, spot_by_date: pd.Series, roots: set[str], max_dte: int, width: float) -> pd.DataFrame:
    day = date.fromisoformat(path.name[:10])
    if pd.Timestamp(day) not in spot_by_date.index:
        return pd.DataFrame()
    S = float(spot_by_date.loc[pd.Timestamp(day)])
    with gzip.open(path, "rt") as f:
        df = pd.read_csv(f)
    df = df[df["ticker"].str.startswith(tuple(f"O:{r}" for r in roots))]
    parsed = df["ticker"].map(parse_ticker)
    df = df[parsed.notna()].copy()
    parsed = parsed[parsed.notna()]
    df["root"] = [p[0] for p in parsed]
    df["expiry"] = [p[1] for p in parsed]
    df["right"] = [p[2] for p in parsed]
    df["strike"] = [p[3] for p in parsed]
    df = df[df["root"].isin(roots)]
    df["dte"] = [(e - day).days for e in df["expiry"]]
    df = df[(df["dte"] > 0) & (df["dte"] <= max_dte) & (df["strike"] >= S * (1 - width)) & (df["strike"] <= S * (1 + width))]
    if df.empty:
        return df
    T = df["dte"].to_numpy(float) / 365.0
    right = np.where(df["right"].to_numpy() == "C", 1, 0)
    iv = implied_vol(df["close"].to_numpy(float), S, df["strike"].to_numpy(float), T, right)
    gamma = bs_gamma(S, df["strike"].to_numpy(float), T, np.nan_to_num(iv, nan=0.2))
    out = pd.DataFrame({"date": day.isoformat(), "spot": S, "expiry": [e.isoformat() for e in df["expiry"]], "strike": df["strike"].to_numpy(),
                        "right": df["right"].to_numpy(), "oi": df["volume"].to_numpy(), "iv": np.round(iv, 4), "gamma": gamma,
                        "volume": df["volume"].to_numpy(), "close": df["close"].to_numpy(), "oi_source": "volume"})
    return out[out["iv"].notna()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flatfile-dir", required=True)
    ap.add_argument("--roots", nargs="+", default=["SPX", "SPXW"])
    ap.add_argument("--underlying", default="SPX")
    ap.add_argument("--spot-symbol", default="I:SPX")
    ap.add_argument("--start", default="2000-01-01")
    ap.add_argument("--end", default="2100-01-01")
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--width", type=float, default=0.15)
    a = ap.parse_args()
    spot = D.load_daily(a.spot_symbol)["close"]
    files = sorted(Path(a.flatfile_dir).rglob("*.csv.gz"))
    files = [f for f in files if a.start <= f.name[:10] <= a.end]
    if not files:
        raise SystemExit("no flat files found")
    parts = []
    for i, f in enumerate(files):
        d = process_day(f, spot, set(a.roots), a.max_dte, a.width)
        if len(d):
            parts.append(d)
        if i % 20 == 0:
            print(f"  {f.name}: {len(d)} contracts")
    out = D.DATA_DIR / "chains" / f"{a.underlying}_chain.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    chain = pd.concat(parts, ignore_index=True)
    chain.to_csv(out, index=False)
    print(f"{len(chain)} rows over {chain['date'].nunique()} days -> {out}   (oi = daily VOLUME proxy)")


if __name__ == "__main__":
    main()
