#!/usr/bin/env python3
"""Free sentiment / news sources -> the CSV contracts the ML sleeves read.

  fng          CNN Fear & Greed history (same endpoint and headers the futures bot uses) -> data/macro/fng.csv
  pcr          CBOE total put/call ratio -> data/macro/pcr.csv (tries the CBOE CSV; --from-csv for a downloaded file)
  aaii         AAII sentiment survey -> data/macro/aaii.csv (--from-file sentiment.xls downloaded from aaii.com; needs xlrd)
  gdelt        GDELT DOC 2.0 API "timelinetone" per symbol/company -> data/macro/gdelt_<SYM>.csv (daily tone, 0-... free, no key)
  polygon-news Massive/Polygon GET /v2/reference/news per ticker -> data/news/<SYM>.csv (needs MASSIVE_API_KEY or
               POLYGON_API_KEY; Stocks plan required — options/futures-only keys return 403). Stores insights[].sentiment
               and sentiment_reasoning alongside title/text for dual scoring vs FinBERT.
  probe-news     One-shot subscription check: GET /v2/reference/news?ticker=AAPL&limit=3 — exit 0 if 200 + insights
  yahoo-rss    Yahoo Finance headline RSS per ticker -> appended to data/news/<SYM>.csv (recent items only; run daily via cron)
  sec-8k       SEC EDGAR full-text search for 8-K filings per company -> data/events/<SYM>_8k.csv (item codes = free event feed)

All news rows share one schema:  date,ts,symbol,source,title,text,url,massive_sentiment,massive_reasoning
Every fetcher is idempotent — it merges into the existing file on (symbol, url) or (date) and never drops rows.

  export MASSIVE_API_KEY=...   # or POLYGON_API_KEY
  python ml/sentiment/fetch_free_sentiment.py probe-news
  python ml/sentiment/fetch_free_sentiment.py polygon-news --symbols AAPL NVDA --start 2024-01-01
  python ml/sentiment/fetch_free_sentiment.py yahoo-rss --symbols AAPL NVDA
  python ml/sentiment/fetch_free_sentiment.py sec-8k --symbols AAPL --cik 320193 --start 2024-01-01
  python ml/sentiment/fetch_free_sentiment.py aaii --from-file ~/Downloads/sentiment.xls
  python ml/sentiment/fetch_free_sentiment.py pcr --from-csv ~/Downloads/totalpc.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wsbt.data import DATA_DIR, _safe_name  # noqa: E402

try:
    import requests
except Exception:                                    # pragma: no cover
    requests = None

ET = "America/New_York"
NEWS_COLS = [
    "date",
    "ts",
    "symbol",
    "source",
    "title",
    "text",
    "url",
    "massive_sentiment",
    "massive_reasoning",
]
CNN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed", "Origin": "https://edition.cnn.com",
}
SEC_HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "waystone-research contact@example.com")}


def _need_requests():
    if requests is None:
        raise SystemExit("pip install requests")


def _massive_key() -> str:
    return (os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY") or "").strip()


def _massive_base() -> str:
    return os.environ.get("POLYGON_BASE_URL", "https://api.massive.com").rstrip("/")


def _insight_for_ticker(insights: list | None, symbol: str) -> tuple[str, str]:
    """Pick Massive's per-ticker LLM sentiment for `symbol` from results[].insights."""
    sym = symbol.upper()
    for row in insights or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("ticker", "")).upper() == sym:
            return str(row.get("sentiment") or ""), str(row.get("sentiment_reasoning") or "")
    if insights and isinstance(insights[0], dict):
        first = insights[0]
        return str(first.get("sentiment") or ""), str(first.get("sentiment_reasoning") or "")
    return "", ""


def _news_row(
    *,
    date: str,
    ts: str,
    symbol: str,
    source: str,
    title: str,
    text: str,
    url: str,
    massive_sentiment: str = "",
    massive_reasoning: str = "",
) -> dict:
    return {
        "date": date,
        "ts": ts,
        "symbol": symbol,
        "source": source,
        "title": title,
        "text": text,
        "url": url,
        "massive_sentiment": massive_sentiment,
        "massive_reasoning": massive_reasoning,
    }


def _merge_news(path: Path, rows: list[dict]) -> int:
    new = pd.DataFrame(rows)
    for col in NEWS_COLS:
        if col not in new.columns:
            new[col] = ""
    new = new[NEWS_COLS]
    if path.exists():
        old = pd.read_csv(path)
        for col in NEWS_COLS:
            if col not in old.columns:
                old[col] = ""
        both = pd.concat([old[NEWS_COLS], new])
    else:
        both = new
    both = both.drop_duplicates(subset=["symbol", "url"]).sort_values("ts")
    path.parent.mkdir(parents=True, exist_ok=True)
    both.to_csv(path, index=False)
    return len(both)


def _merge_series(path: Path, new: pd.DataFrame, key: str = "date") -> int:
    if path.exists():
        old = pd.read_csv(path)
        both = pd.concat([old, new]).drop_duplicates(subset=[key], keep="last")
    else:
        both = new
    both = both.sort_values(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    both.to_csv(path, index=False)
    return len(both)


# ─────────────────────────────────────────────────────────────────────────────
def cmd_fng(a):
    _need_requests()
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata" + (f"/{a.start}" if a.start else "")
    r = requests.get(url, timeout=30, headers=CNN_HEADERS)
    if r.status_code != 200:
        raise SystemExit(f"CNN FnG HTTP {r.status_code}: {r.text[:200]}")
    data = r.json().get("fear_and_greed_historical", {}).get("data", [])
    rows = []
    for row in data:
        ts, v = row.get("x"), row.get("y")
        if ts is None or v is None:
            continue
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(ET))
        rows.append({"date": dt.date().isoformat(), "value": round(float(v), 2)})
    df = pd.DataFrame(rows)
    n = _merge_series(DATA_DIR / "macro" / "fng.csv", df)
    print(f"fng: {len(df)} rows fetched, file now {n} days -> data/macro/fng.csv")


def cmd_pcr(a):
    p = DATA_DIR / "macro" / "pcr.csv"
    if a.from_csv:
        raw = pd.read_csv(a.from_csv, skiprows=lambda i: i < a.skiprows)
        cols = {c.lower().strip(): c for c in raw.columns}
        dcol = next((cols[c] for c in cols if "date" in c), raw.columns[0])
        vcol = next((cols[c] for c in cols if "p/c" in c or "ratio" in c or "put/call" in c), raw.columns[-1])
        df = pd.DataFrame({"date": pd.to_datetime(raw[dcol]).dt.strftime("%Y-%m-%d"), "value": pd.to_numeric(raw[vcol], errors="coerce")}).dropna()
    else:
        _need_requests()
        url = "https://cdn.cboe.com/resources/options/volume_and_price_stats/totalpc.csv"
        r = requests.get(url, timeout=30, headers={"User-Agent": CNN_HEADERS["User-Agent"]})
        if r.status_code != 200:
            raise SystemExit(f"CBOE HTTP {r.status_code} — download the total put/call CSV from cboe.com/us/options/market_statistics/daily/ "
                             f"and pass --from-csv")
        from io import StringIO
        raw = pd.read_csv(StringIO(r.text), skiprows=2)
        raw.columns = [c.strip() for c in raw.columns]
        dcol = [c for c in raw.columns if "DATE" in c.upper()][0]
        vcol = [c for c in raw.columns if "P/C" in c.upper() or "RATIO" in c.upper()][-1]
        df = pd.DataFrame({"date": pd.to_datetime(raw[dcol]).dt.strftime("%Y-%m-%d"), "value": pd.to_numeric(raw[vcol], errors="coerce")}).dropna()
    n = _merge_series(p, df)
    print(f"pcr: {len(df)} rows, file now {n} -> {p}")


def cmd_aaii(a):
    if not a.from_file:
        raise SystemExit("AAII: download https://www.aaii.com/files/surveys/sentiment.xls (free with registration) and pass --from-file")
    raw = pd.read_excel(a.from_file, header=None)
    # find the header row: the one containing 'Bullish'
    hdr = next(i for i in range(min(10, len(raw))) if raw.iloc[i].astype(str).str.contains("Bullish", case=False).any())
    df = pd.read_excel(a.from_file, header=hdr)
    df.columns = [str(c).strip().lower() for c in df.columns]
    dcol = next(c for c in df.columns if "date" in c)
    out = pd.DataFrame({"date": pd.to_datetime(df[dcol], errors="coerce"),
                        "bull": pd.to_numeric(df[[c for c in df.columns if "bullish" in c][0]], errors="coerce"),
                        "neutral": pd.to_numeric(df[[c for c in df.columns if "neutral" in c][0]], errors="coerce"),
                        "bear": pd.to_numeric(df[[c for c in df.columns if "bearish" in c][0]], errors="coerce")}).dropna()
    if out["bull"].max() <= 1.0:
        for c in ("bull", "neutral", "bear"):
            out[c] = out[c] * 100
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    n = _merge_series(DATA_DIR / "macro" / "aaii.csv", out.round(1))
    print(f"aaii: {len(out)} weeks, file now {n} -> data/macro/aaii.csv  (survey published Thursday for the week — the feature "
          f"store forward-fills, so a Thursday value applies from Thursday's close on)")


GDELT_PAUSE = float(os.environ.get("GDELT_PAUSE", "6"))   # GDELT's DOC API throttles at roughly one call per 5 s per IP


def _gdelt_get(url: str, tries: int = 4):
    """GET with backoff on 429; returns the Response or None."""
    r = None
    for k in range(tries):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": CNN_HEADERS["User-Agent"]})
        except requests.RequestException:
            r = None
        if r is not None and r.status_code != 429:
            return r
        time.sleep(GDELT_PAUSE * (2 ** k))
    return r


def cmd_gdelt(a):
    _need_requests()
    companies = a.company or a.symbols
    for sym, comp in zip(a.symbols, companies):
        if len(comp) < 4:
            print(f"gdelt {sym}: query '{comp}' too short for GDELT — pass --company names"); continue
        q = f'"{comp}" sourcelang:english'
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=" + requests.utils.quote(q) +
               f"&mode=timelinetone&format=json&startdatetime={a.start.replace('-', '')}000000"
               f"&enddatetime={(a.end or datetime.now(timezone.utc).strftime('%Y-%m-%d')).replace('-', '')}235959")
        r = _gdelt_get(url)
        if r is None or r.status_code != 200:
            print(f"gdelt {sym}: HTTP {getattr(r, 'status_code', '?')}"); continue
        try:
            series = r.json()["timeline"][0]["data"]
        except Exception:
            print(f"gdelt {sym}: unexpected response {r.text[:120]}"); continue
        df = pd.DataFrame({"date": [d["date"][:8] for d in series], "tone": [d["value"] for d in series]})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
        n = _merge_series(DATA_DIR / "macro" / f"gdelt_{_safe_name(sym)}.csv", df)
        print(f"gdelt {sym} ({comp}): {len(df)} days, file now {n}")
        time.sleep(GDELT_PAUSE)
        # also a volume timeline (article count) — the "attention" signal
        url_v = url.replace("mode=timelinetone", "mode=timelinevolraw")
        r = _gdelt_get(url_v)
        if r is not None and r.status_code == 200:
            try:
                series = r.json()["timeline"][0]["data"]
                dv = pd.DataFrame({"date": pd.to_datetime([d["date"][:8] for d in series], format="%Y%m%d").strftime("%Y-%m-%d"),
                                   "articles": [d["value"] for d in series]})
                _merge_series(DATA_DIR / "macro" / f"gdelt_{_safe_name(sym)}_volume.csv", dv)
            except Exception:
                pass
        time.sleep(GDELT_PAUSE)


def cmd_probe_news(a):
    """Verify Stocks news access and Massive insights on the current API key."""
    _need_requests()
    key = _massive_key()
    if not key:
        raise SystemExit("set MASSIVE_API_KEY or POLYGON_API_KEY")
    sym = (a.symbol or "AAPL").upper()
    base = _massive_base()
    url = f"{base}/v2/reference/news?ticker={sym}&limit={a.limit}&apiKey={key}"
    r = requests.get(url, timeout=60)
    out = {
        "endpoint": "/v2/reference/news",
        "base": base,
        "ticker": sym,
        "http_status": r.status_code,
        "ok": r.status_code == 200,
        "stocks_news_access": r.status_code == 200,
    }
    if r.status_code == 403:
        out["verdict"] = "FAIL — Stocks news not in plan (403). Add a Stocks tier or use gdelt/yahoo-rss."
        print(json.dumps(out, indent=2))
        raise SystemExit(2)
    if r.status_code != 200:
        out["verdict"] = f"FAIL — HTTP {r.status_code}"
        out["body"] = r.text[:300]
        print(json.dumps(out, indent=2))
        raise SystemExit(1)
    j = r.json()
    results = j.get("results") or []
    sample = results[0] if results else {}
    insights = sample.get("insights") or []
    insight0 = insights[0] if insights else {}
    out.update(
        {
            "status": j.get("status"),
            "count": j.get("count"),
            "results_returned": len(results),
            "has_insights": bool(insights),
            "sample_title": (sample.get("title") or "")[:120],
            "sample_insight": {
                "ticker": insight0.get("ticker"),
                "sentiment": insight0.get("sentiment"),
                "sentiment_reasoning": (insight0.get("sentiment_reasoning") or "")[:200],
            }
            if insight0
            else None,
        }
    )
    if insights:
        out["verdict"] = "PASS — Stocks news + LLM insights available. Run polygon-news for historical backfill."
        print(json.dumps(out, indent=2))
        return
    out["verdict"] = "WARN — 200 OK but no insights[] on sample article (unexpected)."
    print(json.dumps(out, indent=2))
    raise SystemExit(1)


def cmd_polygon_news(a):
    _need_requests()
    key = _massive_key()
    if not key:
        raise SystemExit("set MASSIVE_API_KEY or POLYGON_API_KEY")
    base = _massive_base()
    for sym in a.symbols:
        url = f"{base}/v2/reference/news?ticker={sym}&published_utc.gte={a.start}&limit=1000&order=asc&apiKey={key}"
        rows, pages = [], 0
        while url and pages < 50:
            r = requests.get(url, timeout=60)
            if r.status_code == 403:
                raise SystemExit(
                    "Massive/Polygon news: 403 — /v2/reference/news requires a Stocks plan "
                    "(options/indices/futures-only keys cannot backfill news). "
                    "Run: python ml/sentiment/fetch_free_sentiment.py probe-news"
                )
            if r.status_code == 429:
                time.sleep(15)
                continue
            if r.status_code != 200:
                print(f"polygon-news {sym}: HTTP {r.status_code} {r.text[:120]}")
                break
            j = r.json()
            for it in j.get("results", []):
                ts = pd.Timestamp(it["published_utc"]).tz_convert(ET)
                sent, reasoning = _insight_for_ticker(it.get("insights"), sym)
                pub = it.get("publisher") or {}
                rows.append(
                    _news_row(
                        date=ts.strftime("%Y-%m-%d"),
                        ts=ts.isoformat(),
                        symbol=sym,
                        source=pub.get("name") or "massive",
                        title=it.get("title", ""),
                        text=it.get("description", "") or "",
                        url=it.get("article_url", ""),
                        massive_sentiment=sent,
                        massive_reasoning=reasoning,
                    )
                )
            url = j.get("next_url")
            url = f"{url}&apiKey={key}" if url else None
            pages += 1
            time.sleep(0.25)
        n = _merge_news(DATA_DIR / "news" / f"{_safe_name(sym)}.csv", rows)
        with_insights = sum(1 for row in rows if row.get("massive_sentiment"))
        print(f"polygon-news {sym}: {len(rows)} items ({with_insights} with Massive insights), file now {n}")


def cmd_yahoo_rss(a):
    _need_requests()
    for sym in a.symbols:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
        r = requests.get(url, timeout=30, headers={"User-Agent": CNN_HEADERS["User-Agent"]})
        if r.status_code != 200:
            print(f"yahoo-rss {sym}: HTTP {r.status_code}"); continue
        rows = []
        try:
            root = ElementTree.fromstring(r.content)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = item.findtext("pubDate")
                ts = pd.Timestamp(pub).tz_convert(ET) if pub else pd.Timestamp.now(tz=ET)
                rows.append(
                    _news_row(
                        date=ts.strftime("%Y-%m-%d"),
                        ts=ts.isoformat(),
                        symbol=sym,
                        source="yahoo_rss",
                        title=title,
                        text=(item.findtext("description") or "").strip(),
                        url=link,
                    )
                )
        except ElementTree.ParseError as exc:
            print(f"yahoo-rss {sym}: parse error {exc}"); continue
        n = _merge_news(DATA_DIR / "news" / f"{_safe_name(sym)}.csv", rows)
        print(f"yahoo-rss {sym}: {len(rows)} items, file now {n}")
        time.sleep(0.5)


def _sec_cik_map() -> dict[str, str]:
    """ticker -> 10-digit CIK from the SEC's public company_tickers.json (no key, cached per run)."""
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", timeout=60, headers=SEC_HEADERS)
        r.raise_for_status()
        return {str(v["ticker"]).upper().replace(".", "-"): str(v["cik_str"]).zfill(10) for v in r.json().values()}
    except Exception as e:  # noqa: BLE001
        print(f"sec-8k: could not load company_tickers.json ({e}); pass --cik")
        return {}


def _sec_8k_submissions(sym: str, cik: str, start: str, end: str) -> list[dict] | None:
    """8-K filings from data.sec.gov/submissions (works from cloud IPs where efts full-text search returns 403)."""
    r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=60, headers=SEC_HEADERS)
    if r.status_code != 200:
        return None
    rec = r.json().get("filings", {}).get("recent", {})
    rows = []
    for form, fdate, acc_ts, items, accession, doc in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("acceptanceDateTime", []),
                                                         rec.get("items", []), rec.get("accessionNumber", []), rec.get("primaryDocument", [])):
        if not str(form).startswith("8-K") or not (start <= fdate <= end):
            continue
        ts = pd.Timestamp(acc_ts).tz_convert(ET) if acc_ts else pd.Timestamp(fdate).tz_localize(ET)
        # filings accepted after the 5:30pm ET cut-off are disseminated the next business day
        eff = ts if ts.time() <= pd.Timestamp("17:30").time() else (ts + pd.tseries.offsets.BDay(1)).normalize()
        rows.append({"date": eff.strftime("%Y-%m-%d"), "ts": ts.isoformat(), "symbol": sym, "source": "sec_8k",
                     "title": f"{form} items {items}", "text": "",
                     "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{doc}"})
    return rows


def cmd_sec_8k(a):
    _need_requests()
    ciks = a.cik or []
    end = a.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cik_map = _sec_cik_map() if len(ciks) < len(a.symbols) else {}
    for i, sym in enumerate(a.symbols):
        cik = ciks[i] if i < len(ciks) else cik_map.get(sym.upper())
        if cik:
            rows = _sec_8k_submissions(sym, str(cik).zfill(10), a.start, end)
            if rows is not None:
                n = _merge_news(DATA_DIR / "events" / f"{_safe_name(sym)}_8k.csv", rows)
                print(f"sec-8k {sym}: {len(rows)} filings via submissions API, file now {n}")
                time.sleep(0.15)  # SEC fair-access limit is 10 req/s
                continue
        q = {"q": '"8-K"', "forms": "8-K", "dateRange": "custom", "startdt": a.start, "enddt": end}
        if cik:
            q["ciks"] = str(cik).zfill(10)
        else:
            q["entityName"] = sym
        url = "https://efts.sec.gov/LatestSearch/search-index?" + "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in q.items())
        r = requests.get(url, timeout=60, headers=SEC_HEADERS)
        if r.status_code != 200:
            print(f"sec-8k {sym}: HTTP {r.status_code} (set SEC_USER_AGENT='name email' — EDGAR requires it)"); continue
        hits = r.json().get("hits", {}).get("hits", [])
        rows = []
        for h in hits:
            src = h.get("_source", {})
            items = src.get("items", []) or []
            ts = pd.Timestamp(src.get("file_date")).tz_localize(ET) if src.get("file_date") else None
            if ts is None:
                continue
            rows.append(
                _news_row(
                    date=ts.strftime("%Y-%m-%d"),
                    ts=ts.isoformat(),
                    symbol=sym,
                    source="sec_8k",
                    title="8-K items " + ",".join(items),
                    text=src.get("display_names", [""])[0] if src.get("display_names") else "",
                    url="https://www.sec.gov/Archives/edgar/data/" + h.get("_id", "").replace(":", "/"),
                )
            )
        n = _merge_news(DATA_DIR / "events" / f"{_safe_name(sym)}_8k.csv", rows)
        print(f"sec-8k {sym}: {len(rows)} filings, file now {n}  (item 2.02 = results, 5.02 = officer change, 1.01 = material agreement, "
              f"8.01 = other events, 2.05 = exit costs, 4.02 = non-reliance on prior financials)")
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fng"); p.add_argument("--start", help="YYYY-MM-DD (the endpoint accepts a start date path)"); p.set_defaults(fn=cmd_fng)
    p = sub.add_parser("pcr"); p.add_argument("--from-csv"); p.add_argument("--skiprows", type=int, default=0); p.set_defaults(fn=cmd_pcr)
    p = sub.add_parser("aaii"); p.add_argument("--from-file"); p.set_defaults(fn=cmd_aaii)
    p = sub.add_parser("gdelt"); p.add_argument("--symbols", nargs="+", required=True); p.add_argument("--company", nargs="*")
    p.add_argument("--start", default="2023-01-01"); p.add_argument("--end"); p.set_defaults(fn=cmd_gdelt)
    p = sub.add_parser("probe-news")
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--limit", type=int, default=3)
    p.set_defaults(fn=cmd_probe_news)
    p = sub.add_parser("polygon-news"); p.add_argument("--symbols", nargs="+", required=True); p.add_argument("--start", default="2023-01-01"); p.set_defaults(fn=cmd_polygon_news)
    p = sub.add_parser("yahoo-rss"); p.add_argument("--symbols", nargs="+", required=True); p.set_defaults(fn=cmd_yahoo_rss)
    p = sub.add_parser("sec-8k"); p.add_argument("--symbols", nargs="+", required=True); p.add_argument("--cik", nargs="*")
    p.add_argument("--start", default="2023-01-01"); p.add_argument("--end"); p.set_defaults(fn=cmd_sec_8k)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
