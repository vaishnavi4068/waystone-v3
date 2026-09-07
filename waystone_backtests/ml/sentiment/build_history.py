#!/usr/bin/env python3
"""Build per-name sentiment HISTORY for real symbols over a backtest window — the step between "the code
reads data/sentiment/<SYM>_daily.csv" and "those files exist".

Sources, in order of preference (all merged; headline scores win on the days they exist, GDELT tone fills the rest):
  massive   Massive/Polygon Ticker News (/v2/reference/news) — headline + description per article, back to June 2016
            on paid Stocks plans, 2 years on the free Stocks Basic plan.  The response carries Massive's own
            per-ticker LLM sentiment ("insights"), kept as score_src next to our FinBERT/lexicon score.
            NOT part of the options/indices/futures products: add the (free) Stocks Basic plan to the account.
  gdelt     GDELT DOC 2.0 timelinetone + timelinevolraw per company — daily tone and article volume back to 2017,
            no key, no cost.  Coarser (one number a day) but it is real history you can backtest against today.
  sec       EDGAR full-text search 8-K filings per company -> item codes as a structured event feed.
  finbert   Score the headlines (FinBERT if torch+transformers are installed, else the lexicon) and classify events.

Output per symbol:  data/news/<SYM>.csv, data/macro/gdelt_<SYM>.csv (+_volume), data/events/<SYM>.csv,
                    data/sentiment/<SYM>_daily.csv   and   data/sentiment/coverage.csv (what you actually have).

  export POLYGON_API_KEY=...            # a key with a Stocks plan (Basic is free)
  export SEC_USER_AGENT="Your Name you@example.com"
  python ml/sentiment/build_history.py --symbols AAPL NVDA COIN HOOD --start 2024-01-01 --names data/company_names.csv
  python ml/sentiment/build_history.py --symbols-from data/sp500.csv --max-symbols 45 --start 2024-06-01 --sources gdelt,finbert
  python ml/sentiment/build_history.py --symbols AAPL --dry-run          # prints the request plan, no network

data/company_names.csv:  symbol,name[,cik]   (GDELT queries need the company name; CIK makes the EDGAR query exact).
Rate limits are respected by default: Massive free tier 5 req/min, GDELT 1 req / 5 s, EDGAR 1 req / 0.4 s.
Resumable: every fetcher merges into the existing files, so re-running only adds what is missing.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wsbt.data import DATA_DIR, _safe_name, load_symbol_list  # noqa: E402
from ml.sentiment import fetch_free_sentiment as F  # noqa: E402
from ml.sentiment.finbert_score import daily_from_timeline, daily_sentiment, get_scorer, merge_daily  # noqa: E402
from ml.sentiment.event_classifier import classify_file  # noqa: E402

HERE = Path(__file__).resolve().parent


def load_names(path: str | None, symbols: list[str]) -> dict[str, dict]:
    out = {s: {"name": s, "cik": None} for s in symbols}
    if path and Path(path).exists():
        df = pd.read_csv(path)
        for _, r in df.iterrows():
            sym = str(r["symbol"]).strip()
            if sym in out:
                out[sym] = {"name": str(r.get("name", sym)).strip() or sym,
                            "cik": None if pd.isna(r.get("cik", np.nan)) else int(r["cik"])}
    return out


def gdelt_daily(sym: str) -> pd.DataFrame | None:
    p = DATA_DIR / "macro" / f"gdelt_{_safe_name(sym)}.csv"
    if not p.exists():
        return None
    tone = pd.read_csv(p, parse_dates=["date"]).set_index("date")["tone"]
    pv = DATA_DIR / "macro" / f"gdelt_{_safe_name(sym)}_volume.csv"
    vol = pd.read_csv(pv, parse_dates=["date"]).set_index("date")["articles"] if pv.exists() else None
    return daily_from_timeline(tone, vol)


def headline_daily(sym: str, score_fn, window: int = 20) -> pd.DataFrame | None:
    p = DATA_DIR / "news" / f"{_safe_name(sym)}.csv"
    if not p.exists():
        return None
    news = pd.read_csv(p)
    if not len(news):
        return None
    texts = (news["title"].fillna("") + ". " + news["text"].fillna("")).tolist()
    scores = np.array(score_fn(texts))
    return daily_sentiment(news, scores, window)


def coverage_row(sym: str, daily: pd.DataFrame, start: str, end: str | None) -> dict:
    if daily is None or not len(daily):
        return {"symbol": sym, "days": 0, "first": None, "last": None, "headline_days": 0, "gdelt_days": 0,
                "window_coverage_pct": 0.0, "src_sentiment_days": 0, "shock_days_abs2": 0}
    idx = pd.DatetimeIndex(daily.index)
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) if end else idx.max()
    sessions = pd.bdate_range(lo, hi)
    in_win = daily[(idx >= lo) & (idx <= hi)]
    return {"symbol": sym, "days": int(len(daily)), "first": str(idx.min().date()), "last": str(idx.max().date()),
            "headline_days": int((daily["source"] == "headlines").sum()) if "source" in daily else 0,
            "gdelt_days": int((daily["source"] == "gdelt_timeline").sum()) if "source" in daily else 0,
            "window_coverage_pct": round(100 * len(in_win) / max(1, len(sessions)), 1),
            "src_sentiment_days": int(daily["score_src"].notna().sum()) if "score_src" in daily else 0,
            "shock_days_abs2": int((daily["shock_z"].abs() >= 2).sum())}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--symbols-from", help="CSV with a symbol column (e.g. data/sp500.csv)")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--names", default=str(DATA_DIR / "company_names.csv"), help="symbol,name[,cik] CSV for GDELT / EDGAR queries")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--sources", default="massive,gdelt,sec,finbert", help="comma list from massive,gdelt,sec,finbert")
    ap.add_argument("--scorer", choices=["auto", "finbert", "lexicon"], default="auto")
    ap.add_argument("--rpm", type=float, default=5.0, help="Massive requests per minute (free Stocks Basic = 5)")
    ap.add_argument("--gdelt-pause", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    symbols = list(a.symbols or [])
    if a.symbols_from:
        symbols += load_symbol_list(Path(a.symbols_from).name if Path(a.symbols_from).parent == DATA_DIR else a.symbols_from, a.max_symbols)
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    if a.max_symbols:
        symbols = symbols[: a.max_symbols]
    if not symbols:
        raise SystemExit("give --symbols or --symbols-from")
    sources = {s.strip() for s in a.sources.split(",") if s.strip()}
    names = load_names(a.names, symbols)
    have_key = bool(os.environ.get("POLYGON_API_KEY"))
    print(f"[build_history] {len(symbols)} symbols, window {a.start} -> {a.end or 'today'}, sources {sorted(sources)}, "
          f"POLYGON_API_KEY {'set' if have_key else 'MISSING (massive skipped)'}")

    if a.dry_run:
        for s in symbols:
            n = names[s]
            print(f"  {s:6s} massive: /v2/reference/news?ticker={s}&published_utc.gte={a.start} (paged, {a.rpm}/min)"
                  f" | gdelt: '{n['name']}' timelinetone+timelinevolraw | sec: 8-K {'cik ' + str(n['cik']) if n['cik'] else 'entityName ' + s}")
        return

    # ---- 1. fetch ------------------------------------------------------------------------------------
    if "massive" in sources and have_key:
        ns = argparse.Namespace(symbols=symbols, start=a.start, end=a.end, rpm=a.rpm, max_pages=60)
        try:
            F.cmd_polygon_news(ns)
        except SystemExit as exc:
            print(f"  massive news skipped: {exc}")
    if "gdelt" in sources:
        ns = argparse.Namespace(symbols=symbols, company=[names[s]["name"] for s in symbols], start=a.start, end=a.end)
        F.GDELT_PAUSE = a.gdelt_pause
        F.cmd_gdelt(ns)
    if "sec" in sources:
        ns = argparse.Namespace(symbols=symbols, cik=[names[s]["cik"] or "" for s in symbols], start=a.start, end=a.end)
        try:
            F.cmd_sec_8k(ns)
        except Exception as exc:
            print(f"  sec-8k skipped: {exc}")

    # ---- 2. score + merge -----------------------------------------------------------------------------
    kind, score_fn = get_scorer(a.scorer if "finbert" in sources else "lexicon")
    print(f"  scorer: {kind}")
    rows = []
    for s in symbols:
        h = headline_daily(s, score_fn)
        g = gdelt_daily(s)
        daily = merge_daily(h, g)
        if daily is None or not len(daily):
            rows.append(coverage_row(s, None, a.start, a.end)); print(f"  {s}: no sentiment data"); continue
        out = DATA_DIR / "sentiment" / f"{_safe_name(s)}_daily.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        daily.to_csv(out)
        p_news = DATA_DIR / "news" / f"{_safe_name(s)}.csv"
        if p_news.exists():
            ev = classify_file(pd.read_csv(p_news))
            (DATA_DIR / "events").mkdir(parents=True, exist_ok=True)
            ev.to_csv(DATA_DIR / "events" / f"{_safe_name(s)}.csv", index=False)
        r = coverage_row(s, daily, a.start, a.end)
        rows.append(r)
        print(f"  {s}: {r['days']} days ({r['headline_days']} headline, {r['gdelt_days']} gdelt), window coverage {r['window_coverage_pct']}%, "
              f"|shock|>=2 on {r['shock_days_abs2']} days")
    cov = pd.DataFrame(rows)
    cov.to_csv(DATA_DIR / "sentiment" / "coverage.csv", index=False)
    ok = cov[cov["window_coverage_pct"] >= 60]
    print(f"\n  coverage.csv written: {len(ok)}/{len(cov)} symbols cover >= 60% of the window "
          f"({int(cov['shock_days_abs2'].sum())} shock days in total — the shock sleeve needs ~200 to be judged).")


if __name__ == "__main__":
    main()
