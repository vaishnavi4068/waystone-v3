"""Labels for the ML sleeves.

  triple_barrier()        López de Prado's three barriers on a bar path: profit-take, stop, time.
                          Returns per-event side-adjusted return, the barrier hit and the exit time.
  meta_labels()           Turn a primary strategy's trade list into {0,1}: did the trade make money net
                          of costs (optionally by more than `min_pnl`)?  This is what the meta model learns.
  fwd_return_labels()     Direction labels on daily bars: forward N-day return beyond a cost hurdle.
  uniqueness_weights()    Sample weights for overlapping labels (average uniqueness) so a cluster of
                          simultaneous trades does not count as many independent observations.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def triple_barrier(bars: pd.DataFrame, events: pd.DataFrame, pt: float, sl: float, max_hold: int,
                   vol: pd.Series | None = None, price_col: str = "close") -> pd.DataFrame:
    """bars: OHLC path (daily or intraday).  events: DataFrame indexed by ENTRY bar timestamp with columns
    `side` (+1/-1) and optionally `entry` (fill price; defaults to that bar's open).
    pt / sl: barrier widths as a MULTIPLE of `vol` at entry (if given) else as fractions of price.
    max_hold: vertical barrier in bars (entry bar = 1).
    Returns DataFrame indexed like events: ret (signed, gross), label (+1 pt / -1 sl / 0 time), exit_ts, bars."""
    b = bars.sort_index()
    idx = b.index
    hi, lo, cl, op = (b[k].to_numpy(dtype=float) for k in ("high", "low", "close", "open"))
    pos = {t: i for i, t in enumerate(idx)}
    out = []
    for t, ev in events.iterrows():
        i0 = pos.get(t)
        if i0 is None:
            out.append((np.nan, 0, pd.NaT, 0)); continue
        side = int(ev["side"])
        entry = float(ev["entry"]) if "entry" in ev and not pd.isna(ev["entry"]) else op[i0]
        scale = float(vol.loc[t]) if vol is not None and t in vol.index and not pd.isna(vol.loc[t]) else entry
        up = entry + side * pt * scale if side > 0 else entry - pt * scale     # profit level
        dn = entry - sl * scale if side > 0 else entry + sl * scale             # stop level
        exit_px, label, exit_i = None, 0, min(i0 + max_hold - 1, len(idx) - 1)
        for i in range(i0, min(i0 + max_hold, len(idx))):
            if side > 0:
                if lo[i] <= dn:
                    exit_px, label, exit_i = dn, -1, i; break
                if hi[i] >= up:
                    exit_px, label, exit_i = up, 1, i; break
            else:
                if hi[i] >= dn:
                    exit_px, label, exit_i = dn, -1, i; break
                if lo[i] <= up:
                    exit_px, label, exit_i = up, 1, i; break
        if exit_px is None:
            exit_px = cl[exit_i]
        ret = (exit_px / entry - 1.0) * side
        out.append((ret, label, idx[exit_i], exit_i - i0 + 1))
    return pd.DataFrame(out, columns=["ret", "label", "exit_ts", "bars"], index=events.index)


def meta_labels(trades: pd.DataFrame, min_pnl: float = 0.0, pnl_col: str = "pnl") -> pd.Series:
    """1 if the primary trade's net P&L exceeded `min_pnl`, else 0."""
    return (trades[pnl_col].astype(float) > min_pnl).astype(int).rename("y")


def fwd_return_labels(bars: pd.DataFrame, horizon: int, cost_hurdle: float = 0.0) -> pd.DataFrame:
    """Forward `horizon`-day log return from the NEXT open (fill) to the close `horizon` days later, so the
    label matches what simulate_positions() would realise.  y = 1 if fwd_ret > cost_hurdle."""
    b = bars.sort_index()
    nxt_open = b["open"].shift(-1)
    exit_close = b["close"].shift(-horizon)
    fwd = np.log(exit_close / nxt_open)
    lab = pd.DataFrame({"fwd_ret": fwd, "y": (fwd > cost_hurdle).astype(int)}, index=b.index)
    lab.loc[fwd.isna(), "y"] = np.nan
    lab["t1"] = pd.Series(b.index, index=b.index).shift(-horizon)
    return lab


def uniqueness_weights(t0: pd.Series, t1: pd.Series, bar_index: pd.DatetimeIndex) -> pd.Series:
    """Average uniqueness of each label over its lifetime [t0, t1] (López de Prado ch. 4).
    A label that overlaps with k others on every bar gets weight ~1/(k+1)."""
    t0 = pd.DatetimeIndex(t0)
    t1 = pd.DatetimeIndex(t1)
    tz = bar_index.tz
    if tz is not None:
        t0 = t0.tz_localize(tz) if t0.tz is None else t0.tz_convert(tz)
        t1 = t1.tz_localize(tz) if t1.tz is None else t1.tz_convert(tz)
    else:
        t0 = t0.tz_localize(None) if t0.tz is not None else t0
        t1 = t1.tz_localize(None) if t1.tz is not None else t1
    n = len(bar_index)
    conc = np.zeros(n + 1)
    a = np.searchsorted(bar_index.values, t0.values, side="left")
    z = np.searchsorted(bar_index.values, t1.values, side="right")
    z = np.maximum(z, a + 1)
    for i, j in zip(a, z):
        conc[i] += 1
        conc[min(j, n)] -= 1
    conc = np.cumsum(conc)[:n]
    w = np.empty(len(t0))
    for k, (i, j) in enumerate(zip(a, z)):
        span = conc[i:min(j, n)]
        w[k] = float(np.mean(1.0 / np.maximum(span, 1))) if len(span) else 1.0
    return pd.Series(w, index=range(len(w)), name="w")
