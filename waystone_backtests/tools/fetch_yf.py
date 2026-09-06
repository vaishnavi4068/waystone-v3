#!/usr/bin/env python3
"""Fill data/daily and data/earnings from Yahoo Finance (free, delayed, good enough for daily research).

    python tools/fetch_yf.py --symbols SPY QQQ IWM ^VIX ^VIX3M ^GSPC --start 2010-01-01
    python tools/fetch_yf.py --sectors                       # 11 SPDR sector ETFs + SPY
    python tools/fetch_yf.py --sp500 --max-symbols 120       # constituents from data/sp500.csv
    python tools/fetch_yf.py --earnings --sp500 --max-symbols 120
    python tools/fetch_yf.py --list nsdq250.csv --extend --start 2017-01-01   # prepend warm-up history to GCS files

Re-running only refreshes; existing CSVs are overwritten with the full history.
Yahoo occasionally rate-limits: the script sleeps between symbols and retries once.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsbt import data as D  # noqa: E402

SECTORS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


def fetch_daily(symbols: list[str], start: str, end: str | None, extend_only: bool = False) -> None:
    """extend_only: keep the existing CSV (e.g. synced from GCS NSDQ250) and only PREPEND Yahoo rows dated
    before its first row, so indicators can warm up.  The primary source stays primary for its own range."""
    import yfinance as yf
    out_dir = D.DATA_DIR / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(symbols):
        path = D.daily_path(s)
        existing = None
        if extend_only and path.exists():
            existing = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
            first = existing.index[0]
            if first <= pd.Timestamp(start) + pd.Timedelta(days=7):
                print(f"  {s}: already starts {first.date()}, skip")
                continue
            end = (first - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        for attempt in (1, 2):
            try:
                df = yf.download(s, start=start, end=end, progress=False, auto_adjust=False, threads=False)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  {s}: {exc} (attempt {attempt})")
                time.sleep(5)
                df = pd.DataFrame()
        if df is None or df.empty:
            print(f"  {s}: no data")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close",
                                "Adj Close": "adj_close", "Volume": "volume"})
        df.index.name = "date"
        df = df[["open", "high", "low", "close", "adj_close", "volume"]].dropna(subset=["close"])
        if existing is not None:
            df = df[df.index < existing.index[0]]
            # guard against a split between the two sources: the join must be continuous within ~25%
            if len(df) and abs(float(df["close"].iloc[-1]) / float(existing["close"].iloc[0]) - 1.0) > 0.25:
                print(f"  {s}: price jump at the join ({df['close'].iloc[-1]:.2f} -> {existing['close'].iloc[0]:.2f}), not extended")
                continue
            cols = [c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in existing.columns]
            df = pd.concat([df.reindex(columns=cols), existing[cols]]).sort_index()
        df.to_csv(path)
        print(f"  {s}: {len(df)} rows -> {path.name}{' (extended)' if existing is not None else ''}")
        if i % 10 == 9:
            time.sleep(1.5)


def fetch_earnings(symbols: list[str]) -> None:
    import yfinance as yf
    out_dir = D.DATA_DIR / "earnings"
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in symbols:
        try:
            ed = yf.Ticker(s).get_earnings_dates(limit=40)
        except Exception as exc:  # noqa: BLE001
            print(f"  {s}: earnings fetch failed: {exc}")
            continue
        if ed is None or ed.empty:
            print(f"  {s}: no earnings dates")
            continue
        ed = ed.reset_index()
        ts = pd.to_datetime(ed.iloc[:, 0], utc=True).dt.tz_convert("America/New_York")
        df = pd.DataFrame({
            "date": ts.dt.normalize().dt.tz_localize(None),
            # Yahoo timestamps carry the session: before 09:30 ET = bmo, after 16:00 = amc
            "time": ["bmo" if t.hour < 9 or (t.hour == 9 and t.minute < 30) else ("amc" if t.hour >= 16 else "unknown") for t in ts],
            "eps_est": ed.get("EPS Estimate"), "eps_act": ed.get("Reported EPS"),
        })
        df = df.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")
        df.to_csv(out_dir / f"{D._safe_name(s)}.csv", index=False)
        print(f"  {s}: {len(df)} earnings dates")
        time.sleep(0.8)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--sectors", action="store_true")
    ap.add_argument("--sp500", action="store_true")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--earnings", action="store_true")
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--list", default=None, help="symbol list CSV in data/ (e.g. nsdq250.csv)")
    ap.add_argument("--extend", action="store_true", help="only prepend history before each existing CSV's first row")
    a = ap.parse_args()
    syms = list(a.symbols)
    if a.sectors:
        syms += SECTORS + ["SPY"]
    if a.sp500:
        syms += D.load_symbol_list(max_symbols=a.max_symbols)
    if a.list:
        syms += D.load_symbol_list(a.list, a.max_symbols)
    syms = list(dict.fromkeys(syms))
    if not syms:
        ap.error("nothing to fetch — pass --symbols, --sectors or --sp500")
    if a.earnings:
        fetch_earnings([s for s in syms if not s.startswith("^")])
    else:
        fetch_daily(syms, a.start, a.end, extend_only=a.extend)


if __name__ == "__main__":
    main()
