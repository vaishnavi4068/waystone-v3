#!/usr/bin/env python3
"""Nightly SPX option-chain snapshot from IB Gateway -> data/chains/SPX_chain.csv  (run on the VM after 16:15 ET).

    source /root/FUTURE_IBKR/env.sh
    python tools/ib_chain_snapshot.py --underlying SPX --max-dte 45 --width 0.10

Appends one row per (date, expiry, strike, right) with open interest and implied vol, which is all
the GEX strategy needs.  IB does NOT serve historical open interest, so this only builds history
going forward; for back history buy CBOE DataShop end-of-day option summaries or Polygon options
snapshots and convert them to the same columns.

Market-data lines: ±10% around spot at 25-pt strikes over four expiries is ~400 contracts; the script
requests them in batches of 90 (below the 100-line cap on your plan) with snapshot=True.
Untested here (no Gateway in the build sandbox).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wsbt import data as D  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="SPX")
    ap.add_argument("--max-dte", type=int, default=45)
    ap.add_argument("--width", type=float, default=0.10, help="strike band around spot, fraction")
    ap.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT", "4002")))
    ap.add_argument("--client-id", type=int, default=79)
    a = ap.parse_args()

    from ib_async import IB, Index, Option

    ib = IB()
    ib.connect(a.host, a.port, clientId=a.client_id, timeout=30)
    und = Index(a.underlying, "CBOE", "USD")
    ib.qualifyContracts(und)
    tk = ib.reqMktData(und, "", True, False)
    ib.sleep(3)
    spot = tk.last if tk.last == tk.last and tk.last else tk.close
    ib.cancelMktData(und)
    print(f"{a.underlying} spot {spot}")
    params = ib.reqSecDefOptParams(und.symbol, "", und.secType, und.conId)
    p = next(x for x in params if x.exchange in ("SMART", "CBOE"))
    today = datetime.now().date()
    exps = sorted(e for e in p.expirations if 0 < (datetime.strptime(e, "%Y%m%d").date() - today).days <= a.max_dte)
    strikes = sorted(k for k in p.strikes if spot * (1 - a.width) <= k <= spot * (1 + a.width) and k % 5 == 0)
    contracts = [Option(a.underlying, e, k, r, "SMART", tradingClass=p.tradingClass) for e in exps for k in strikes for r in ("C", "P")]
    contracts = [c for c in ib.qualifyContracts(*contracts) if c.conId]
    print(f"{len(exps)} expiries x {len(strikes)} strikes -> {len(contracts)} contracts")
    rows = []
    for i in range(0, len(contracts), 90):
        batch = contracts[i:i + 90]
        tks = [ib.reqMktData(c, "100,101,106", True, False) for c in batch]      # 101 = OI, 106 = IV
        ib.sleep(8)
        for c, t in zip(batch, tks):
            oi = t.callOpenInterest if c.right == "C" else t.putOpenInterest
            iv = getattr(t, "impliedVolatility", None)
            if iv is None or not (iv == iv):
                g = getattr(t, "modelGreeks", None)
                iv = g.impliedVol if g else float("nan")
            rows.append({"date": today.isoformat(), "spot": spot, "expiry": c.lastTradeDateOrContractMonth,
                         "strike": c.strike, "right": c.right, "oi": oi if oi == oi else 0, "iv": iv})
        time.sleep(1)
    ib.disconnect()
    df = pd.DataFrame(rows)
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.strftime("%Y-%m-%d")
    out = D.DATA_DIR / "chains" / f"{a.underlying}_chain.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        old = pd.read_csv(out)
        df = pd.concat([old[old["date"] != today.isoformat()], df])
    df.to_csv(out, index=False)
    print(f"{len(rows)} rows appended -> {out}")


if __name__ == "__main__":
    main()
