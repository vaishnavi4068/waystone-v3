"""Run the V221 Hybrid engine (copied verbatim from FUTURE_IBKR/v221_engine.py) over a 1-minute bar
history and return its trades in the common trade-list format used by ml/meta_label.py.

Inputs the engine needs per bar: datetime_et, open, high, low, close, volume, vol_price (the VXN level).
Live feeds the PRIOR-DAY VXN close (vol_source="prior_day"); this wrapper does the same so the primary
signal here is exactly the live one, not the reference backtest's same-day look-ahead version.
Fear & Greed is looked up as the last value STRICTLY before the bar's date (verbatim helper)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v221_engine import V221Engine  # noqa: E402

MNQ_MULT, MNQ_COMMISSION_RT = 2.0, 1.24          # $/pt per contract, $ commission per contract round trip
NQ_MULT, NQ_COMMISSION_RT = 20.0, 4.50


def prior_day_vol(vol_daily: pd.Series, bar_index: pd.DatetimeIndex) -> np.ndarray:
    """vol_daily: VXN close indexed by date.  For each bar, the last close whose date is < bar date."""
    vd = pd.DatetimeIndex(vol_daily.index)
    vd = vd.tz_localize(None) if vd.tz is not None else vd
    vals = vol_daily.to_numpy(dtype=float)
    bi = bar_index.tz_localize(None) if bar_index.tz is not None else bar_index
    dates = bi.normalize()
    pos = np.searchsorted(vd.normalize().values, dates.values, side="left") - 1
    out = np.where(pos >= 0, vals[np.clip(pos, 0, len(vals) - 1)], np.nan)
    return out


def fng_lookup_from(fng: pd.Series | None):
    """fng: Series indexed by date (value = 0..100).  Returns (lookup dict, sorted dates) or (None, None)."""
    if fng is None or len(fng) == 0:
        return None, None
    idx = pd.DatetimeIndex(fng.index)
    idx = idx.tz_localize(None) if idx.tz is not None else idx
    lk = {d.date(): float(v) for d, v in zip(idx, fng.to_numpy())}
    return lk, sorted(lk)


def run_primary(bars: pd.DataFrame, vol_daily: pd.Series, fng: pd.Series | None = None, contracts: int = 2,
                mult: float = MNQ_MULT, commission_rt: float = MNQ_COMMISSION_RT, use_vxn_entries: bool = False,
                use_vxn_exits: bool = True, use_gate: bool = True) -> pd.DataFrame:
    """bars: 1-min OHLCV (index tz-aware ET or naive ET).  Returns the trade list."""
    b = bars.sort_index()
    idx = b.index
    et = idx.tz_convert("America/New_York").tz_localize(None) if idx.tz is not None else idx
    volp = prior_day_vol(vol_daily, idx)
    ok = ~np.isnan(volp)
    lk, dates = fng_lookup_from(fng)
    e = V221Engine(use_vxn_entries, use_vxn_exits, use_gate)
    o, h, l, c, v = (b[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    for i in range(len(b)):
        if not ok[i]:
            continue
        e.on_bar(et[i].to_pydatetime(), o[i], h[i], l[i], c[i], v[i], volp[i], lk, dates, backtest_fill=True)
    rows = []
    for t in e.trades:
        pts = float(t["pts"])
        gross = pts * mult * contracts
        rows.append({"symbol": "MNQ" if mult == MNQ_MULT else "NQ", "entry_time": pd.Timestamp(t["entry_time"]),
                     "exit_time": pd.Timestamp(t["exit_time"]), "side": 1 if t["direction"] == "LONG" else -1,
                     "entry": t["entry_price"], "exit": t["exit_price"], "pts": pts,
                     "pnl": gross - commission_rt * contracts, "cost": commission_rt * contracts + 2 * 0.125 * mult * contracts,
                     "units": contracts, "reason": t.get("reason")})
    out = pd.DataFrame(rows)
    if len(out):
        if idx.tz is not None:
            out["entry_time"] = out["entry_time"].dt.tz_localize("America/New_York")
            out["exit_time"] = out["exit_time"].dt.tz_localize("America/New_York")
        out["signal_ts"] = out["entry_time"]                      # signal bar = the bar before the fill bar; features_at(strict_before=True)
        out["bars_held"] = ((out["exit_time"] - out["entry_time"]).dt.total_seconds() / 60).round().astype(int)
        out = out.sort_values("entry_time").reset_index(drop=True)
    return out
