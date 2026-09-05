#!/usr/bin/env python3
"""Pull 1-minute futures bars from IB Gateway into data/intraday/<ROOT>_1min.csv  (run on the VM).

    source /root/FUTURE_IBKR/env.sh
    python tools/ib_fetch_bars.py --root MNQ --days 60            # front contract, 24h bars
    python tools/ib_fetch_bars.py --root MNQ --days 60 --rth      # RTH only

IB serves at most ~1 day of 1-min bars per request and paces historical requests (~60 per
10 min), so this walks backwards one day per request with a small sleep.  It appends to the
CSV and de-duplicates on timestamp, so it can be re-run to extend the history.

NOTE  IB has no historical bid/ask volume for futures bars.  The CVD strategy therefore uses
a delta PROXY from OHLC (see strategies/05_orderflow_cvd_mnq/README.md).  If you want true
aggressor-side volume, record it live with reqTickByTickData("AllLast") into the same CSV
with bid_volume / ask_volume columns — the loader will pick them up automatically.

Untested here (no Gateway in the build sandbox); it uses the same ib_async calls as
FUTURE_IBKR/ibkr_futures.py, which are known to work on your Gateway.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsbt import data as D  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="MNQ")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--rth", action="store_true")
    ap.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT", "4002")))
    ap.add_argument("--client-id", type=int, default=int(os.environ.get("IBKR_CLIENT_ID", "78")) + 1)
    a = ap.parse_args()

    from ib_async import IB, Future, util

    ib = IB()
    ib.connect(a.host, a.port, clientId=a.client_id, timeout=30)
    cds = ib.reqContractDetails(Future(a.root, exchange="CME"))
    cds = sorted(cds, key=lambda c: c.contract.lastTradeDateOrContractMonth)
    today = datetime.now().strftime("%Y%m%d")
    front = next(c.contract for c in cds if c.contract.lastTradeDateOrContractMonth >= today)
    print(f"front contract {front.localSymbol}")
    out = D.DATA_DIR / "intraday" / f"{a.root}_1min.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    end = datetime.now()
    for i in range(a.days):
        end_str = end.strftime("%Y%m%d %H:%M:%S")
        bars = ib.reqHistoricalData(front, endDateTime=end_str, durationStr="1 D", barSizeSetting="1 min",
                                    whatToShow="TRADES", useRTH=a.rth, formatDate=2, keepUpToDate=False)
        df = util.df(bars)
        if df is None or df.empty:
            print(f"  {end_str}: no bars")
            end -= timedelta(days=1)
            continue
        df = df.rename(columns={"date": "ts"})[["ts", "open", "high", "low", "close", "volume"]]
        frames.append(df)
        print(f"  {end_str}: {len(df)} bars")
        end = pd.Timestamp(df["ts"].iloc[0]).to_pydatetime() - timedelta(minutes=1)
        time.sleep(10)          # stay under IB's historical pacing limit
    ib.disconnect()
    if not frames:
        print("nothing fetched")
        return
    new = pd.concat(frames)
    new["ts"] = pd.to_datetime(new["ts"], utc=True)
    if out.exists():
        old = pd.read_csv(out)
        old["ts"] = pd.to_datetime(old["ts"], utc=True)
        new = pd.concat([old, new])
    new = new.drop_duplicates("ts").sort_values("ts")
    new.to_csv(out, index=False)
    print(f"{len(new)} bars -> {out}")


if __name__ == "__main__":
    main()
