"""Shared evaluation helpers for the ML sleeves: trade list -> daily P&L, sized-vs-base comparison,
deflated Sharpe with the real trial count, and the trial log the KPI dashboard's Stage 2 insists on."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import metrics as M  # noqa: E402

TRIAL_LOG = Path(os.environ.get("WSBT_TRIAL_LOG", ROOT / "results" / "trial_log.csv"))


# ─────────────────────────────────────────────────────────────────────────────
# Trade list -> daily P&L
# ─────────────────────────────────────────────────────────────────────────────
ET = "America/New_York"


def to_et_naive(ts) -> pd.Series:
    """Parse timestamps of any flavour (tz-aware, naive, ISO strings with mixed DST offsets) to naive ET."""
    s = pd.Series(ts)
    if pd.api.types.is_datetime64_any_dtype(s):
        t = s
    else:
        try:
            t = pd.to_datetime(s, utc=True).dt.tz_convert(ET)
        except Exception:
            t = pd.to_datetime(s)
    if getattr(t.dt, "tz", None) is not None:
        t = t.dt.tz_convert(ET).dt.tz_localize(None)
    return t


def _to_naive_date(ts: pd.Series) -> pd.Series:
    return to_et_naive(ts).dt.normalize()


def daily_pnl(trades: pd.DataFrame, pnl_col: str = "pnl", when_col: str = "exit_time",
              index: pd.DatetimeIndex | None = None) -> pd.Series:
    """$ P&L per calendar day of EXIT (flat days = 0).  If `index` is given the series is reindexed to it."""
    if trades is None or len(trades) == 0:
        return pd.Series(dtype=float, index=index if index is not None else pd.DatetimeIndex([]))
    d = _to_naive_date(trades[when_col])
    s = trades[pnl_col].astype(float).groupby(d.values).sum()
    s.index = pd.DatetimeIndex(s.index)
    if index is not None:
        idx = pd.DatetimeIndex(index)
        idx = idx.tz_localize(None) if idx.tz is not None else idx
        s = s.reindex(idx.normalize().unique(), fill_value=0.0)
    return s.sort_index()


def sharpe_of(daily: pd.Series) -> float:
    r = daily.dropna().astype(float)
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if len(r) > 1 and r.std(ddof=1) > 0 else 0.0


def size_trades(trades: pd.DataFrame, p: pd.Series, p_skip: float, p_boost: float,
                boost_mult: float = 2.0) -> pd.DataFrame:
    """Apply the meta-model's sizing rule to a trade list with per-trade probability `p`.
    size = 0 (skip) below p_skip, 1 between, `boost_mult` at or above p_boost.  Costs scale with size.
    Trades with NaN p (never out-of-fold) are dropped — they have no honest prediction."""
    t = trades.copy()
    t["p"] = p.reindex(t.index).to_numpy()
    t = t[t["p"].notna()].copy()
    size = np.where(t["p"] >= p_boost, boost_mult, np.where(t["p"] >= p_skip, 1.0, 0.0))
    t["size"] = size
    t["pnl_base"] = t["pnl"].astype(float)
    t["pnl"] = t["pnl_base"] * t["size"]
    if "cost" in t:
        t["cost"] = t["cost"].astype(float) * t["size"]
    return t


def compare_base_meta(trades_oof: pd.DataFrame, sized: pd.DataFrame, nav: float, index=None) -> dict:
    """Head-to-head on the OUT-OF-FOLD trades only (same trades, base = every one at size 1)."""
    base = daily_pnl(trades_oof, index=index)
    meta = daily_pnl(sized[sized["size"] > 0], index=index)
    both = pd.concat([base.rename("base"), meta.rename("meta")], axis=1).fillna(0.0)
    out = {}
    for k in ("base", "meta"):
        d = both[k]
        stats = M.summary(d / nav, None)
        out[k] = {"net_pnl": round(float(d.sum()), 2), "sharpe": stats.get("sharpe"), "max_drawdown_pct": stats.get("max_drawdown_pct"),
                  "days": int(len(d))}
    out["base"]["trades"] = int(len(trades_oof))
    out["meta"]["trades"] = int((sized["size"] > 0).sum())
    out["meta"]["skipped"] = int((sized["size"] == 0).sum())
    out["meta"]["boosted"] = int((sized["size"] > 1).sum())
    skipped = sized[sized["size"] == 0]
    out["meta"]["pnl_of_skipped_trades"] = round(float(skipped["pnl_base"].sum()), 2)
    out["meta"]["hit_rate_kept"] = round(float((sized.loc[sized["size"] > 0, "pnl_base"] > 0).mean()), 3) if (sized["size"] > 0).any() else None
    out["meta"]["hit_rate_skipped"] = round(float((skipped["pnl_base"] > 0).mean()), 3) if len(skipped) else None
    return out


def dsr_for(daily: pd.Series, n_trials: int, trial_sr_var: float = 0.0) -> dict:
    r = daily.dropna().astype(float)
    sr = M.sharpe_per_period(r)
    return M.deflated_sharpe(sr, len(r), max(1, int(n_trials)), float(trial_sr_var),
                             skew=float(r.skew()) if len(r) > 2 else 0.0, kurt=float(r.kurt() + 3) if len(r) > 3 else 3.0)


# ─────────────────────────────────────────────────────────────────────────────
# Trial log
# ─────────────────────────────────────────────────────────────────────────────
def log_trial(family: str, params: dict, result: dict, note: str = "") -> int:
    """Append one configuration + result to results/trial_log.csv and return the number of trials
    logged so far for this family (use it as n_trials in the deflated Sharpe)."""
    TRIAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "family": family,
           "params": json.dumps(params, sort_keys=True, default=str),
           "sharpe": result.get("sharpe"), "n_trades": result.get("trades"), "net_pnl": result.get("net_pnl"),
           "sr_per_period": result.get("sr_per_period"), "note": note}
    df = pd.DataFrame([row])
    header = not TRIAL_LOG.exists()
    df.to_csv(TRIAL_LOG, mode="a", header=header, index=False)
    return trial_count(family)


def trial_count(family: str) -> int:
    if not TRIAL_LOG.exists():
        return 0
    df = pd.read_csv(TRIAL_LOG)
    return int((df["family"] == family).sum())


def trial_sr_variance(family: str) -> float:
    if not TRIAL_LOG.exists():
        return 0.0
    df = pd.read_csv(TRIAL_LOG)
    s = df.loc[df["family"] == family, "sr_per_period"].dropna()
    return float(s.var(ddof=1)) if len(s) > 1 else 0.0
