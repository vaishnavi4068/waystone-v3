#!/usr/bin/env python3
"""Fill data/daily and data/earnings from Yahoo Finance (free, delayed, good enough for daily research).

    python tools/fetch_yf.py --symbols SPY QQQ IWM ^VIX ^VIX3M ^GSPC --start 2010-01-01
    python tools/fetch_yf.py --sectors                       # 11 SPDR sector ETFs + SPY
    python tools/fetch_yf.py --sp500 --max-symbols 120       # constituents from data/sp500.csv
    python tools/fetch_yf.py --earnings --sp500 --max-symbols 120

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


def fetch_daily(symbols: list[str], start: str, end: str | None) -> None:
    import yfinance as yf
    out_dir = D.DATA_DIR / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(symbols):
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
        df.to_csv(D.daily_path(s))
        print(f"  {s}: {len(df)} rows -> {D.daily_path(s).name}")
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
    a = ap.parse_args()
    syms = list(a.symbols)
    if a.sectors:
        syms += SECTORS + ["SPY"]
    if a.sp500:
        syms += D.load_symbol_list(max_symbols=a.max_symbols)
    syms = list(dict.fromkeys(syms))
    if not syms:
        ap.error("nothing to fetch — pass --symbols, --sectors or --sp500")
    if a.earnings:
        fetch_earnings([s for s in syms if not s.startswith("^")])
    else:
        fetch_daily(syms, a.start, a.end)


if __name__ == "__main__":
    main()
