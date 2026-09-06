#!/usr/bin/env python3
"""Fill data/daily and data/earnings from Yahoo Finance (free, delayed, good enough for daily research).

    python tools/fetch_yf.py --symbols SPY QQQ IWM ^VIX ^VIX3M ^GSPC --start 2010-01-01
    python tools/fetch_yf.py --sectors                       # 11 SPDR sector ETFs + SPY
    python tools/fetch_yf.py --sp500 --max-symbols 120       # constituents from data/sp500.csv
    python tools/fetch_yf.py --earnings --sp500 --max-symbols 120
    python tools/fetch_yf.py --list nsdq250.csv --extend --start 2017-01-01   # prepend warm-up history to GCS files
    python tools/fetch_yf.py --list nsdq250.csv --append                      # bring GCS files up to today

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


def append_daily(symbols: list[str], end: str | None = None, overlap_days: int = 5, tol: float = 0.02) -> None:
    """Bring GCS-synced CSVs up to date: download from (last row - overlap) in one batched Yahoo call, require the
    overlapping closes to agree within `tol` (a split or a different adjustment basis fails the check and the
    file is left alone), then append only the rows dated after the existing last row."""
    import yfinance as yf
    todo: dict[str, pd.DataFrame] = {}
    for s in symbols:
        path = D.daily_path(s)
        if not path.exists():
            continue
        todo[s] = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    if not todo:
        print("  nothing to append")
        return
    start = (min(df.index[-1] for df in todo.values()) - pd.Timedelta(days=overlap_days * 2)).strftime("%Y-%m-%d")
    names = list(todo)
    batch = None
    for chunk_start in range(0, len(names), 100):
        chunk = names[chunk_start:chunk_start + 100]
        for attempt in (1, 2, 3):
            try:
                raw = yf.download(chunk, start=start, end=end, progress=False, auto_adjust=False, threads=True, group_by="ticker")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  batch {chunk_start}: {exc} (attempt {attempt})")
                time.sleep(10 * attempt)
                raw = None
        if raw is None or raw.empty:
            continue
        batch = raw if batch is None else pd.concat([batch, raw], axis=1)
        time.sleep(2)
    if batch is None:
        print("  Yahoo returned nothing")
        return
    n_ok = n_skip = 0
    for s, existing in todo.items():
        try:
            df = batch[s] if isinstance(batch.columns, pd.MultiIndex) else batch
        except KeyError:
            continue
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close",
                                "Adj Close": "adj_close", "Volume": "volume"}).dropna(subset=["close"])
        if df.empty:
            continue
        df.index = pd.to_datetime(df.index).tz_localize(None) if getattr(df.index, "tz", None) is not None else pd.to_datetime(df.index)
        df.index.name = "date"
        last = existing.index[-1]
        overlap = df[(df.index <= last) & (df.index > last - pd.Timedelta(days=overlap_days * 2))]
        both = overlap.join(existing[["close"]], rsuffix="_have", how="inner")
        if len(both) == 0 or (both["close"] / both["close_have"] - 1.0).abs().max() > tol:
            worst = float((both["close"] / both["close_have"] - 1.0).abs().max()) if len(both) else float("nan")
            print(f"  {s}: overlap mismatch {worst:.1%} (split / adjustment basis) — not appended")
            n_skip += 1
            continue
        new = df[df.index > last]
        if new.empty:
            continue
        cols = [c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in existing.columns]
        out = pd.concat([existing[cols], new.reindex(columns=cols)]).sort_index()
        out.to_csv(D.daily_path(s))
        n_ok += 1
    print(f"  appended {n_ok} files, skipped {n_skip}, latest row now {max(pd.read_csv(D.daily_path(s), usecols=['date'])['date'].max() for s in todo)}")


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
    ap.add_argument("--append", action="store_true", help="bring existing CSVs up to date (batched; overlap must agree)")
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
    elif a.append:
        append_daily(syms, a.end)
    else:
        fetch_daily(syms, a.start, a.end, extend_only=a.extend)


if __name__ == "__main__":
    main()
