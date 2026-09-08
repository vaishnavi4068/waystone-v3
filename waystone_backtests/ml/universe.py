"""Universe filters — decide WHICH names a sleeve is allowed to trade, without peeking.

Three layers, applied in order; each returns a membership table (index = session, columns = symbol, bool)
that the sleeves consult AS OF the signal date:

  structural(bars_by, ...)      cheap, static-ish: price >= min_price, 20-day average dollar volume >= min_adv,
                                enough history.  Recomputed daily from trailing data, so a name enters/leaves
                                only on information available that day.
  coverage(cov, ...)            from data/sentiment/coverage.csv: enough news days in the window and enough
                                |shock| days to matter.  (Whole-window statistic -> use it to size the research
                                universe, not as a per-day trading rule.)
  reactive(bars_by, sent_by, …) the one that matters: WALK-FORWARD ranking of names by how their price has
                                reacted to their own tone shocks over the trailing `lookback` sessions
                                (signed mean return from the next open over `horizon` sessions, as a t-stat),
                                rebalanced every `rebalance` sessions, keeping the top `top_n` with >= `min_events`.
                                Membership on day D uses shocks whose outcome was known before D.
  from_file(path)               a fixed list (your options bot's turnover top-N, a sector, a watch list).

Combine with `intersect(*tables)`.  `select_symbols(table, when)` gives the allowed set on a date.

  python ml/universe.py --report                        # per-name structural + reactivity summary on what is in data/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import data as D  # noqa: E402


def _sessions(bars_by: dict) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(set().union(*[set(b.index) for b in bars_by.values()])))


def structural(bars_by: dict[str, pd.DataFrame], min_price: float = 10.0, min_adv_usd: float = 20e6,
               min_history: int = 60) -> pd.DataFrame:
    idx = _sessions(bars_by)
    out = pd.DataFrame(False, index=idx, columns=sorted(bars_by))
    for sym, b in bars_by.items():
        b = b.sort_index()
        adv = (b["close"] * b["volume"]).rolling(20).mean()
        ok = (b["close"] >= min_price) & (adv >= min_adv_usd) & (pd.Series(np.arange(len(b)), index=b.index) >= min_history)
        out.loc[ok[ok].index, sym] = True
    return out


def coverage(cov: pd.DataFrame, min_window_pct: float = 60.0, min_shock_days: int = 5) -> list[str]:
    c = cov[(cov["window_coverage_pct"] >= min_window_pct) & (cov["shock_days_abs2"] >= min_shock_days)]
    return sorted(c["symbol"].astype(str))


def shock_outcomes(bars: pd.DataFrame, sent: pd.DataFrame, z: float = 2.0, horizon: int = 3) -> pd.DataFrame:
    """One row per |shock_z| >= z event: signed return from the next open to the close `horizon` sessions later,
    and the session on which that outcome became KNOWN (used for walk-forward eligibility)."""
    b = bars.sort_index()
    s = sent["shock_z"].reindex(b.index)
    o, c, idx = b["open"].to_numpy(), b["close"].to_numpy(), b.index
    rows = []
    for i in np.where(s.abs() >= z)[0]:
        if i + horizon + 1 >= len(b):
            continue
        side = 1 if s.iloc[i] > 0 else -1
        rows.append({"date": idx[i], "known": idx[i + horizon + 1], "ret": (c[i + horizon] / o[i + 1] - 1) * side})
    return pd.DataFrame(rows)


def reactive(bars_by: dict, sent_by: dict, z: float = 2.0, horizon: int = 3, lookback: int = 252,
             rebalance: int = 21, top_n: int = 50, min_events: int = 8, min_t: float = 0.0) -> pd.DataFrame:
    """Walk-forward reactivity ranking.  At each rebalance session R: for every name, take the shock events whose
    outcome was known before R and whose date is within the last `lookback` sessions; t-stat of the signed
    returns; keep the top `top_n` with >= min_events and t >= min_t.  Membership holds until the next rebalance."""
    idx = _sessions(bars_by)
    ev = {sym: shock_outcomes(bars_by[sym], sent_by[sym], z, horizon) for sym in bars_by if sym in sent_by}
    ev = {k: v for k, v in ev.items() if len(v)}
    out = pd.DataFrame(False, index=idx, columns=sorted(bars_by))
    ranks = []
    for r in range(lookback, len(idx), rebalance):
        R = idx[r]
        lo = idx[r - lookback]
        scores = {}
        for sym, e in ev.items():
            w = e[(e["known"] < R) & (e["date"] >= lo)]
            if len(w) >= min_events and w["ret"].std(ddof=1) > 0:
                scores[sym] = float(w["ret"].mean() / w["ret"].std(ddof=1) * np.sqrt(len(w)))
        chosen = [s for s, t in sorted(scores.items(), key=lambda kv: -kv[1])[:top_n] if t >= min_t]
        until = idx[min(r + rebalance, len(idx)) - 1]
        out.loc[R:until, chosen] = True
        ranks.append({"rebalance": R, "eligible": len(scores), "chosen": len(chosen),
                      "t_median_chosen": round(float(np.median([scores[s] for s in chosen])), 2) if chosen else None})
    out.attrs["rebalances"] = pd.DataFrame(ranks)
    return out


def from_file(path: str | Path, idx: pd.DatetimeIndex, all_symbols: list[str]) -> pd.DataFrame:
    syms = set(D.load_symbol_list(Path(path).name if Path(path).parent == D.DATA_DIR else str(path)))
    out = pd.DataFrame(False, index=idx, columns=sorted(all_symbols))
    for s in syms & set(all_symbols):
        out[s] = True
    return out


def intersect(*tables: pd.DataFrame) -> pd.DataFrame:
    t = tables[0].copy()
    for other in tables[1:]:
        o = other.reindex(index=t.index, columns=t.columns).fillna(False)
        t = t & o
    return t


def select_symbols(table: pd.DataFrame, when: pd.Timestamp) -> set[str]:
    """Allowed names on `when` (the last membership row at or before that session)."""
    w = pd.Timestamp(when)
    w = w.tz_localize(None) if w.tzinfo is not None else w
    pos = table.index.searchsorted(w, side="right") - 1
    if pos < 0:
        return set()
    row = table.iloc[pos]
    return set(row[row].index)


def membership_series(table: pd.DataFrame, sym: str, bars_index: pd.DatetimeIndex) -> pd.Series:
    """Boolean per bar for one symbol, forward-filled from the membership table (no look-ahead)."""
    if sym not in table.columns:
        return pd.Series(False, index=bars_index)
    return table[sym].reindex(bars_index, method="ffill").fillna(False).astype(bool)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--z", type=float, default=2.0)
    ap.add_argument("--horizon", type=int, default=3)
    ap.add_argument("--top-n", type=int, default=50)
    a = ap.parse_args()
    cov_p = D.DATA_DIR / "sentiment" / "coverage.csv"
    if not cov_p.exists():
        raise SystemExit("run ml/sentiment/build_history.py first (needs data/sentiment/coverage.csv)")
    cov = pd.read_csv(cov_p)
    syms = coverage(cov)
    bars_by, sent_by = {}, {}
    for s in syms:
        try:
            bars_by[s] = D.load_daily(s)
            sent_by[s] = pd.read_csv(D.DATA_DIR / "sentiment" / f"{D._safe_name(s)}_daily.csv", parse_dates=["date"]).set_index("date")
        except Exception:
            pass
    st = structural(bars_by)
    rows = []
    for s in bars_by:
        e = shock_outcomes(bars_by[s], sent_by[s], a.z, a.horizon)
        t = float(e["ret"].mean() / e["ret"].std(ddof=1) * np.sqrt(len(e))) if len(e) > 2 and e["ret"].std(ddof=1) > 0 else np.nan
        rows.append({"symbol": s, "structural_pct": round(100 * st[s].mean(), 1), "events": len(e),
                     "mean_bps": round(float(e["ret"].mean() * 1e4), 1) if len(e) else None, "t_full_sample": round(t, 2) if t == t else None})
    rep = pd.DataFrame(rows).sort_values("t_full_sample", ascending=False)
    print(f"{len(cov)} names in coverage.csv -> {len(syms)} pass coverage -> {len(bars_by)} with bars")
    with pd.option_context("display.max_rows", 600, "display.width", 140):
        print(rep.to_string(index=False))
    print("\nt_full_sample is a whole-sample look — use it to understand the data, never to pick the trading universe; "
          "the sleeve uses universe.reactive() which ranks on trailing, already-known outcomes only.")
    rep.to_csv(D.DATA_DIR / "sentiment" / "universe_report.csv", index=False)


if __name__ == "__main__":
    main()
