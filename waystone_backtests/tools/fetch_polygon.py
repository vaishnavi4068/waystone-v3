#!/usr/bin/env python3
"""Polygon.io / Massive fetchers — options, indices and futures (the plan you have).

    export POLYGON_API_KEY=...            # or MASSIVE_API_KEY; the options bot's "Massive" key works

    # indices (daily) -> data/daily/I_SPX.csv, I_VIX.csv, I_VIX3M.csv, I_NDX.csv
    python tools/fetch_polygon.py indices --symbols SPX VIX VIX3M NDX --start 2010-01-01
    # indices (1-min) -> data/intraday/I_SPX_1min.csv   (for the intraday GEX version)
    python tools/fetch_polygon.py indices --symbols SPX --start 2024-01-01 --minute

    # futures: front-month stitched by volume -> data/intraday/MNQ_1min.csv  or  data/daily/MES.csv
    python tools/fetch_polygon.py futures --root MNQ --start 2025-01-01 --resolution 1min
    python tools/fetch_polygon.py futures --root MES --start 2010-01-01 --resolution 1session

    # SPX option-chain snapshot with OPEN INTEREST, IV and greeks -> data/chains/SPX_chain.csv
    # (run nightly after 16:30 ET; Polygon has OI/IV only in snapshots, not historically)
    python tools/fetch_polygon.py chain-snapshot --underlying SPX --max-dte 60

    # implied move before each earnings print (ATM straddle / spot) -> data/earnings/<SYM>.csv column
    python tools/fetch_polygon.py implied-move --symbols AAPL MSFT NVDA

    # daily bars for one option contract (helper) -> data/option_bars/<ticker>.csv
    python tools/fetch_polygon.py option-bars --ticker O:SPX260918P05000000 --start 2026-07-01

Ticker formats (Massive docs): indices `I:SPX`; options `O:{root}{YYMMDD}{C|P}{strike*1000:08d}`
(SPX monthlies use root SPX, weeklies SPXW; XSP likewise); futures `{root}{month code}{year digit}`
e.g. MNQZ5.  Endpoints: /v2/aggs/ticker/{t}/range/..., /futures/v1/aggs/{t}, /v3/snapshot/options/{u},
/v3/reference/options/contracts.  All calls paginate through next_url and back off on HTTP 429.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsbt import data as D  # noqa: E402

BASE = os.environ.get("POLYGON_BASE_URL", "https://api.polygon.io")
MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}


class Client:
    def __init__(self, key: str | None = None, base: str = BASE, dry_run: bool = False):
        self.key = key or os.environ.get("POLYGON_API_KEY") or os.environ.get("MASSIVE_API_KEY")
        self.base = base.rstrip("/")
        self.dry_run = dry_run
        if not self.key and not dry_run:
            raise SystemExit("set POLYGON_API_KEY (or MASSIVE_API_KEY)")
        self.s = requests.Session()

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base}{path_or_url}"
        params = dict(params or {})
        params["apiKey"] = self.key
        if self.dry_run:
            print("GET", url, {k: v for k, v in params.items() if k != "apiKey"})
            return {"results": []}
        for attempt in range(6):
            r = self.s.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"gave up on {url}")

    def paginate(self, path: str, params: dict, max_pages: int = 1000) -> list[dict]:
        out = []
        js = self.get(path, params)
        out.extend(js.get("results") or [])
        pages = 1
        while js.get("next_url") and pages < max_pages:
            js = self.get(js["next_url"])
            out.extend(js.get("results") or [])
            pages += 1
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Indices
# ══════════════════════════════════════════════════════════════════════════════
def index_bars(c: Client, symbol: str, start: str, end: str, minute: bool = False) -> pd.DataFrame:
    span = "minute" if minute else "day"
    rows = c.paginate(f"/v2/aggs/ticker/I:{symbol}/range/1/{span}/{start}/{end}", {"adjusted": "true", "sort": "asc", "limit": 50000})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    if "volume" not in df:
        df["volume"] = 0.0
    return df[["ts", "open", "high", "low", "close", "volume"]]


def cmd_indices(a) -> None:
    c = Client(dry_run=a.dry_run)
    end = a.end or date.today().isoformat()
    for s in a.symbols:
        df = index_bars(c, s, a.start, end, a.minute)
        if df.empty:
            print(f"  {s}: no data"); continue
        if a.minute:
            out = D.DATA_DIR / "intraday" / f"I_{s}_1min.csv"
            df.to_csv(out, index=False)
        else:
            out = D.daily_path(f"I:{s}")
            d = df.copy()
            d["date"] = d["ts"].dt.tz_localize(None).dt.normalize()
            d = d.drop(columns=["ts"]).set_index("date")
            d["adj_close"] = d["close"]
            d.to_csv(out)
        print(f"  {s}: {len(df)} rows -> {out}")


# ══════════════════════════════════════════════════════════════════════════════
# Futures
# ══════════════════════════════════════════════════════════════════════════════
def quarterly_contracts(root: str, start: date, end: date) -> list[tuple[str, date]]:
    """All quarterly contracts whose expiry month is within [start - 4mo, end + 4mo]: (ticker, expiry_month_start)."""
    out = []
    y, m = start.year, start.month
    first = date(y, m, 1) - timedelta(days=120)
    last = end + timedelta(days=120)
    d = date(first.year, first.month, 1)
    while d <= last:
        if d.month in MONTH_CODES:
            out.append((f"{root}{MONTH_CODES[d.month]}{d.year % 10}", d))
        d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    return out


def futures_bars(c: Client, ticker: str, resolution: str, start: str, end: str) -> pd.DataFrame:
    rows = c.paginate(f"/futures/v1/aggs/{ticker}", {"resolution": resolution, "window_start.gte": start, "window_start.lte": end,
                                                     "limit": 50000, "sort": "window_start.asc"})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["window_start"].astype("int64"), unit="ns", utc=True).dt.tz_convert("America/New_York")
    df["contract"] = ticker
    keep = ["ts", "open", "high", "low", "close", "volume", "contract"] + (["session_end_date"] if "session_end_date" in df else [])
    return df[keep]


def stitch_front(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Per session date keep the contract with the most volume (front by liquidity); no back-adjustment."""
    if not frames:
        return pd.DataFrame()
    allb = pd.concat(frames, ignore_index=True)
    allb["session"] = allb["session_end_date"] if "session_end_date" in allb else allb["ts"].dt.tz_localize(None).dt.normalize()
    vol = allb.groupby(["session", "contract"])["volume"].sum().reset_index()
    front = vol.sort_values(["session", "volume"], ascending=[True, False]).drop_duplicates("session")[["session", "contract"]]
    out = allb.merge(front, on=["session", "contract"], how="inner").sort_values("ts")
    return out.drop(columns=["session"])


def cmd_futures(a) -> None:
    c = Client(dry_run=a.dry_run)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end) if a.end else date.today()
    frames = []
    for tk, _ in quarterly_contracts(a.root, start, end):
        df = futures_bars(c, tk, a.resolution, a.start, end.isoformat())
        print(f"  {tk}: {len(df)} bars")
        if len(df):
            frames.append(df)
        time.sleep(0.2)
    cont = stitch_front(frames)
    if cont.empty:
        print("nothing fetched"); return
    if a.resolution.endswith("session") or a.resolution.endswith("day"):
        d = cont.copy()
        d["date"] = pd.to_datetime(d["session_end_date"]) if "session_end_date" in d else d["ts"].dt.tz_localize(None).dt.normalize()
        d = d.set_index("date")[["open", "high", "low", "close", "volume", "contract"]]
        d["adj_close"] = d["close"]
        out = D.daily_path(a.root)
        d.to_csv(out)
    else:
        out = D.DATA_DIR / "intraday" / f"{a.root}_1min.csv"
        cont[["ts", "open", "high", "low", "close", "volume", "contract"]].to_csv(out, index=False)
    print(f"  {len(cont)} bars ({cont['contract'].nunique()} contracts, front by volume, NOT back-adjusted) -> {out}")


# ══════════════════════════════════════════════════════════════════════════════
# Options
# ══════════════════════════════════════════════════════════════════════════════
def option_ticker(root: str, expiry: date, right: str, strike: float) -> str:
    return f"O:{root}{expiry:%y%m%d}{right[0].upper()}{int(round(strike * 1000)):08d}"


def option_daily_bars(c: Client, ticker: str, start: str, end: str) -> pd.DataFrame:
    rows = c.paginate(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}", {"adjusted": "true", "sort": "asc", "limit": 50000})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "ts"})
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    return df[["date", "open", "high", "low", "close", "volume"]]


def option_bars_cached(c: Client, ticker: str, start: str, end: str) -> pd.DataFrame:
    """Daily bars for one contract, cached under data/option_bars/.  Used by strategy 03's --pricing polygon."""
    p = D.DATA_DIR / "option_bars" / f"{ticker.replace(':', '_')}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        df = pd.read_csv(p, parse_dates=["date"])
        if len(df) and df["date"].max() >= pd.Timestamp(end) - pd.Timedelta(days=3):
            return df
    df = option_daily_bars(c, ticker, start, end)
    if len(df):
        df.to_csv(p, index=False)
    return df


def cmd_option_bars(a) -> None:
    c = Client(dry_run=a.dry_run)
    df = option_bars_cached(c, a.ticker, a.start, a.end or date.today().isoformat())
    print(f"  {a.ticker}: {len(df)} daily bars -> data/option_bars/")


def cmd_chain_snapshot(a) -> None:
    c = Client(dry_run=a.dry_run)
    today = date.today()
    params = {"limit": 250, "expiration_date.lte": (today + timedelta(days=a.max_dte)).isoformat(),
              "expiration_date.gt": today.isoformat()}
    rows = c.paginate(f"/v3/snapshot/options/{a.underlying}", params)
    recs = []
    for r in rows:
        det = r.get("details", {})
        und = r.get("underlying_asset", {}) or {}
        gr = r.get("greeks", {}) or {}
        recs.append({"date": today.isoformat(), "spot": und.get("price"), "expiry": det.get("expiration_date"),
                     "strike": det.get("strike_price"), "right": (det.get("contract_type") or "")[:1].upper(),
                     "oi": r.get("open_interest") or 0, "iv": r.get("implied_volatility"), "gamma": gr.get("gamma"),
                     "delta": gr.get("delta"), "close": (r.get("day") or {}).get("close"), "volume": (r.get("day") or {}).get("volume"),
                     "bid": (r.get("last_quote") or {}).get("bid"), "ask": (r.get("last_quote") or {}).get("ask"), "ticker": det.get("ticker")})
    df = pd.DataFrame(recs)
    if df.empty:
        print("no contracts returned"); return
    spot = df["spot"].dropna().median() if df["spot"].notna().any() else None
    df["spot"] = df["spot"].fillna(spot)
    df = df[df["right"].isin(["C", "P"])]
    if a.width and spot:
        df = df[(df["strike"] >= spot * (1 - a.width)) & (df["strike"] <= spot * (1 + a.width))]
    out = D.DATA_DIR / "chains" / f"{a.underlying}_chain.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        old = pd.read_csv(out)
        df = pd.concat([old[old["date"] != today.isoformat()], df], ignore_index=True)
    df.to_csv(out, index=False)
    print(f"  {a.underlying}: {len(recs)} contracts, spot {spot} -> {out}")


def nearest_expiry_and_strikes(c: Client, symbol: str, on_or_after: date, spot: float) -> tuple[date | None, float | None]:
    """First listed expiration >= date and the listed strike nearest spot (via the contracts reference endpoint)."""
    rows = c.paginate("/v3/reference/options/contracts", {"underlying_ticker": symbol, "expiration_date.gte": on_or_after.isoformat(),
                                                           "contract_type": "call", "expired": "true", "limit": 1000,
                                                           "sort": "expiration_date", "order": "asc",
                                                           "strike_price.gte": spot * 0.9, "strike_price.lte": spot * 1.1}, max_pages=2)
    if not rows:
        return None, None
    exp = min(r["expiration_date"] for r in rows)
    strikes = [r["strike_price"] for r in rows if r["expiration_date"] == exp]
    k = min(strikes, key=lambda x: abs(x - spot))
    return date.fromisoformat(exp), float(k)


def cmd_implied_move(a) -> None:
    c = Client(dry_run=a.dry_run)
    for s in a.symbols:
        try:
            earn = D.load_earnings(s)
            bars = D.load_daily(s)
        except D.DataMissing as exc:
            print(f"  {s}: {exc}"); continue
        idx = bars.index
        vals = []
        for _, e in earn.iterrows():
            d = pd.Timestamp(e["date"]).normalize()
            t = str(e.get("time", "unknown")).lower()
            reaction_after = idx[idx >= d] if t == "bmo" else idx[idx > d]
            before = idx[idx < (reaction_after[0] if len(reaction_after) else d)]
            if not len(before) or not len(reaction_after):
                vals.append(e.get("implied_move_pct", float("nan"))); continue
            pre = before[-1]
            spot = float(bars.at[pre, "close"])
            exp, k = nearest_expiry_and_strikes(c, s, reaction_after[0].date(), spot)
            if exp is None:
                vals.append(float("nan")); continue
            call = option_bars_cached(c, option_ticker(s, exp, "C", k), pre.date().isoformat(), pre.date().isoformat())
            put = option_bars_cached(c, option_ticker(s, exp, "P", k), pre.date().isoformat(), pre.date().isoformat())
            if call.empty or put.empty:
                vals.append(float("nan")); continue
            straddle = float(call["close"].iloc[-1]) + float(put["close"].iloc[-1])
            vals.append(round(100 * straddle / spot, 2))
            time.sleep(0.1)
        earn["implied_move_pct"] = vals
        out = D.DATA_DIR / "earnings" / f"{D._safe_name(s)}.csv"
        earn.to_csv(out, index=False)
        print(f"  {s}: implied move filled for {sum(pd.notna(vals))}/{len(vals)} events -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the requests instead of calling")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("indices"); p.add_argument("--symbols", nargs="+", required=True); p.add_argument("--start", required=True)
    p.add_argument("--end"); p.add_argument("--minute", action="store_true"); p.set_defaults(fn=cmd_indices)
    p = sub.add_parser("futures"); p.add_argument("--root", required=True); p.add_argument("--start", required=True); p.add_argument("--end")
    p.add_argument("--resolution", default="1min"); p.set_defaults(fn=cmd_futures)
    p = sub.add_parser("chain-snapshot"); p.add_argument("--underlying", default="SPX"); p.add_argument("--max-dte", type=int, default=60)
    p.add_argument("--width", type=float, default=0.15, help="keep strikes within +/- this fraction of spot"); p.set_defaults(fn=cmd_chain_snapshot)
    p = sub.add_parser("implied-move"); p.add_argument("--symbols", nargs="+", required=True); p.set_defaults(fn=cmd_implied_move)
    p = sub.add_parser("option-bars"); p.add_argument("--ticker", required=True); p.add_argument("--start", required=True); p.add_argument("--end")
    p.set_defaults(fn=cmd_option_bars)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
