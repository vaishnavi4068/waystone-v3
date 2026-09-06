#!/usr/bin/env python3
"""Regime detection on daily features — a Gaussian HMM (hmmlearn) or k-means, decoded WITHOUT look-ahead.

The state on day t is `model.predict(X[:t+1])[-1]`: the Viterbi path through the data up to and including
t only.  A full-sample decode would let the state on day t depend on days t+1.. (smoothing) — that is the
usual regime-backtest leak and it is not done here.  The model itself is refit every `--refit-every`
sessions on an expanding window that ends at t, and states are relabelled after every refit by their mean
return / volatility so "state 0" means the same thing over time (0 = calm-up, 1 = choppy, 2 = stress).

What it gives you:
  data/regime/<name>_states.csv      date,state  — a feature for meta_label.py (--regime <name>) and a gate for any sleeve
  results/ml_regime_<name>/          per-state statistics, state-conditioned P&L of a trade list (--trades-csv), and
                                     the walk-forward "state gate" backtest: trade only in states that had positive
                                     expectancy on the trailing training window.
  --sleeves a.csv b.csv ...          allocate across sleeve equity curves by state (per-state mean return on the training
                                     window vs equal weight), reported OOS.

  python ml/regime_hmm.py --synthetic
  python ml/regime_hmm.py --symbol I:SPX --vol-symbol I:VIX --name spx --export-state
  python ml/regime_hmm.py --symbol MNQ --vol-symbol I:VXN --name mnq --trades-csv results/ml_meta_v221/primary_trades.csv
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import data as D, metrics as M  # noqa: E402
from wsbt.report import RESULTS  # noqa: E402
from ml import common as C  # noqa: E402
from ml.features import daily_features  # noqa: E402
from ml.evaluate import daily_pnl, sharpe_of, log_trial, to_et_naive, trial_count, trial_sr_variance  # noqa: E402
from ml.kpi_export import compute_kpis  # noqa: E402

try:
    from hmmlearn.hmm import GaussianHMM
    HAVE_HMM = True
except Exception:                                    # pragma: no cover
    HAVE_HMM = False
from sklearn.cluster import KMeans

DEFAULT_FEATURES = ["ret_20", "rv_20", "rv_ratio", "er_20", "vix_chg_5", "vix_vs_rv", "pct_above_50"]


def _fit(X: np.ndarray, k: int, seed: int, kind: str):
    if kind == "hmm" and HAVE_HMM:
        m = GaussianHMM(n_components=k, covariance_type="diag", n_iter=200, random_state=seed, tol=1e-3)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m.fit(X)
        return m
    return KMeans(n_clusters=k, n_init=5, random_state=seed).fit(X)


def _decode_prefix(m, X: np.ndarray, kind: str) -> int:
    if kind == "hmm" and HAVE_HMM:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return int(m.predict(X)[-1])
    return int(m.predict(X[-1:])[0])


def _relabel(m, X_train: np.ndarray, ret_train: np.ndarray, k: int, kind: str) -> dict:
    """Map raw state ids -> ordered ids by (mean return desc, vol asc): 0 = best/calm ... k-1 = stress."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = m.predict(X_train)
    stats = []
    for s in range(k):
        r = ret_train[raw == s]
        stats.append((s, float(r.mean()) if len(r) else 0.0, float(r.std()) if len(r) > 1 else 1e9))
    order = sorted(stats, key=lambda t: (-t[1] / (t[2] + 1e-9)))
    return {s: i for i, (s, _, _) in enumerate(order)}


def walk_forward_states(F: pd.DataFrame, ret: pd.Series, k: int = 3, warmup: int = 252, refit_every: int = 63,
                        kind: str = "hmm", seed: int = 0, max_history: int | None = None, min_dwell: int = 3) -> pd.Series:
    """Standardise with expanding stats (no look-ahead), refit periodically, decode by prefix.
    min_dwell: a new state is adopted only after the decoder has proposed it on `min_dwell` consecutive days
    (a causal debounce — it delays regime changes by min_dwell-1 days and removes most one-day flickers)."""
    cols = list(F.columns)
    Xraw = F.to_numpy(dtype=float)
    n = len(F)
    states = np.full(n, -1)
    m, label_map, next_fit = None, None, warmup
    mu = pd.DataFrame(Xraw).expanding(60).mean().to_numpy()
    sd = pd.DataFrame(Xraw).expanding(60).std().to_numpy()
    Z = np.nan_to_num((Xraw - mu) / np.where(sd > 0, sd, 1.0), nan=0.0, posinf=0.0, neginf=0.0)
    Z = np.clip(Z, -4, 4)
    r = ret.to_numpy(dtype=float)
    proposals = []
    cur = -1
    for t in range(warmup, n):
        lo = 0 if max_history is None else max(0, t - max_history)
        if m is None or t >= next_fit:
            m = _fit(Z[lo:t], k, seed, kind)
            label_map = _relabel(m, Z[lo:t], r[lo:t], k, kind)
            next_fit = t + refit_every
        prop = label_map[_decode_prefix(m, Z[lo:t + 1], kind)]
        proposals.append(prop)
        if cur < 0 or (len(proposals) >= min_dwell and len(set(proposals[-min_dwell:])) == 1):
            cur = prop
        states[t] = cur
    return pd.Series(states, index=F.index, name="state")


def state_gate_backtest(trades: pd.DataFrame, states: pd.Series, nav: float, train_days: int = 252, min_trades: int = 15):
    """train_days in TRADING days (converted to calendar days x1.45 for the window)."""
    """Walk-forward: on each day, allow trading only in states whose trailing `train_days` expectancy (per trade)
    was positive with >= min_trades.  Returns the gated trade list."""
    st = states.copy()
    st.index = pd.DatetimeIndex(st.index).normalize()
    t = trades.copy()
    # strategies/*/trades.csv write entry_date/exit_date (or entry_ts); the bot exports entry_time
    for src, dst in (("entry_ts", "entry_time"), ("exit_ts", "exit_time"), ("entry_date", "entry_time"), ("exit_date", "exit_time")):
        if dst not in t and src in t:
            t[dst] = t[src]
    if "entry_time" not in t:
        raise SystemExit("trades csv needs entry_time (or entry_ts / entry_date)")
    t["date"] = to_et_naive(t["entry_time"]).dt.normalize().to_numpy()
    t["state"] = st.reindex(t["date"]).to_numpy()
    t = t[t["state"] >= 0]
    keep = np.zeros(len(t), dtype=bool)
    tv = t.reset_index(drop=True)
    for i, row in tv.iterrows():
        d = row["date"]
        hist = tv[(tv["date"] < d) & (tv["date"] >= d - pd.Timedelta(days=train_days * 1.45))]
        h = hist[hist["state"] == row["state"]]
        keep[i] = len(h) >= min_trades and h["pnl"].mean() > 0
    tv["allowed"] = keep
    return tv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--vol-symbol", default=None)
    ap.add_argument("--panel-symbols", nargs="*", help="symbols for breadth (data/daily); default: none")
    ap.add_argument("--name", default=None)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--kind", choices=["hmm", "kmeans"], default="hmm" if HAVE_HMM else "kmeans")
    ap.add_argument("--features", nargs="*", default=DEFAULT_FEATURES)
    ap.add_argument("--warmup", type=int, default=252)
    ap.add_argument("--refit-every", type=int, default=63)
    ap.add_argument("--min-dwell", type=int, default=3, help="days a new state must be proposed before it is adopted")
    ap.add_argument("--export-state", action="store_true")
    ap.add_argument("--trades-csv", help="trade list to condition on the state (entry_time, exit_time, pnl)")
    ap.add_argument("--sleeves", nargs="*", help="equity.csv files (daily_ret) to allocate by state")
    ap.add_argument("--gate-train-days", type=int, default=252, help="trailing window (sessions) for the per-state expectancy")
    ap.add_argument("--gate-min-trades", type=int, default=15)
    ap.add_argument("--nav", type=float, default=100_000.0)
    ap.add_argument("--n", type=int, default=2500, help="synthetic days")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    name = a.name or (D._safe_name(a.symbol).lower() if not a.synthetic else "syn")
    kind = a.kind if (a.kind == "kmeans" or HAVE_HMM) else "kmeans"

    if a.synthetic:
        start, n = "2016-01-04", a.n
        if a.trades_csv:                                     # cover the trade list's dates with the synthetic history
            tt = to_et_naive(pd.read_csv(a.trades_csv)["entry_time"])
            start = (tt.min() - pd.Timedelta(days=int((a.warmup + 80) * 1.45))).strftime("%Y-%m-%d")
            n = int((tt.max() - pd.Timestamp(start)).days / 1.45) + 10
        bars = D.synthetic_daily(n=n, seed=a.seed, regime=True, start=start)
        vix = C.vol_index(None, bars, synthetic=True, seed=a.seed)
        panel = D.closes_panel(D.synthetic_panel([f"S{i}" for i in range(12)], n=n, seed=a.seed, start=start))
        macro = C.load_macro(True, bars.index, a.seed)
    else:
        bars = D.load_daily(a.symbol)
        vix = C.vol_index(a.vol_symbol, bars, synthetic=False) if a.vol_symbol else None
        panel = None
        if a.panel_symbols:
            frames = D.load_many(a.panel_symbols)
            panel = D.closes_panel(frames)
        macro = C.load_macro()
    F_all = daily_features(bars, vix=vix, panel=panel, macro=macro)
    feats = [f for f in a.features if f in F_all.columns]
    F = F_all[feats].dropna(how="all")
    F = F.loc[F.notna().sum(axis=1) >= max(2, len(feats) // 2)]
    F = F.ffill().fillna(0.0)
    ret = np.log(bars["close"]).diff().reindex(F.index).fillna(0.0)
    if len(F) < a.warmup + 100:
        raise SystemExit(f"need > {a.warmup + 100} days of features; have {len(F)} (lower --warmup)")
    print(f"[regime {name}] {kind}, k={a.k}, features={feats}, {len(F)} days, warmup {a.warmup}, refit every {a.refit_every}")
    states = walk_forward_states(F, ret, a.k, a.warmup, a.refit_every, kind, a.seed, min_dwell=a.min_dwell)
    st = states[states >= 0]
    fwd = np.log(bars["close"]).diff().shift(-1).reindex(st.index)          # NEXT day's return, given today's state
    tab = pd.DataFrame({"state": st, "fwd_ret": fwd, "vix": F_all["vix"].reindex(st.index) if "vix" in F_all else np.nan})
    g = tab.groupby("state")
    summary = pd.DataFrame({"days": g.size(), "share": (g.size() / len(tab)).round(3),
                            "next_day_mean_bp": (g["fwd_ret"].mean() * 1e4).round(1),
                            "next_day_vol_ann_pct": (g["fwd_ret"].std() * np.sqrt(252) * 100).round(1),
                            "next_day_sharpe": (g["fwd_ret"].mean() / g["fwd_ret"].std() * np.sqrt(252)).round(2),
                            "avg_vix": g["vix"].mean().round(1) if "vix" in F_all else None,
                            "avg_run_days": st.groupby((st != st.shift()).cumsum()).size().groupby(st.groupby((st != st.shift()).cumsum()).first()).mean().round(1)})
    print(summary.to_string())
    n_switch = int((st != st.shift()).sum())
    print(f"  state switches: {n_switch}  ({n_switch / (len(st) / 252):.1f} per year)")

    out_dir = RESULTS / f"ml_regime_{name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = {"kind": kind, "k": a.k, "features": feats, "state_summary": json.loads(summary.to_json()), "switches": n_switch}
    if True:                                              # states are always exported (--export-state kept for scripts)
        p = C.DATA / "regime" / f"{name}_states.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        st.rename("state").to_frame().rename_axis("date").to_csv(p)
        print(f"  states -> {p}")

    # ---- state-conditioned trade list + walk-forward state gate ---------------------------------
    if a.trades_csv:
        tr = pd.read_csv(a.trades_csv)
        gated = state_gate_backtest(tr, st, a.nav, a.gate_train_days, a.gate_min_trades)
        by = gated.groupby("state")["pnl"].agg(["count", "mean", "sum"]).round(2)
        by["hit"] = gated.groupby("state")["pnl"].apply(lambda s: round(float((s > 0).mean()), 3))
        print("  trade expectancy by state (all trades):")
        print(by.to_string())
        allowed = gated[gated["allowed"]]
        first_decision = gated["date"].min() + pd.Timedelta(days=int(a.gate_train_days * 1.45))
        base = gated[gated["date"] >= first_decision]
        allowed_oos = allowed[allowed["date"] >= first_decision]
        # full trading-day calendar (flat days = 0): a series of exit days only would overstate Sharpe
        last_exit = to_et_naive(gated["exit_time"]).max() if "exit_time" in gated else gated["date"].max()
        cal = pd.DatetimeIndex(st.index).normalize()
        cal = cal[(cal >= first_decision) & (cal <= max(pd.Timestamp(last_exit), first_decision))]
        d_base, d_gate = daily_pnl(base, index=cal), daily_pnl(allowed_oos, index=cal)
        cmpd = {"base": {"trades": len(base), "net": round(float(base["pnl"].sum()), 0), "sharpe": round(sharpe_of(d_base / a.nav), 2)},
                "state_gate": {"trades": len(allowed_oos), "net": round(float(allowed_oos["pnl"].sum()), 0), "sharpe": round(sharpe_of(d_gate / a.nav), 2)}}
        print(f"  walk-forward state gate (after {a.gate_train_days} sessions of history): {cmpd}")
        family = f"ml_regime_gate_{name}"
        log_trial(family, {"k": a.k, "kind": kind, "features": feats, "refit": a.refit_every}, {"sharpe": cmpd["state_gate"]["sharpe"],
                  "trades": len(allowed_oos), "net_pnl": cmpd["state_gate"]["net"], "sr_per_period": M.sharpe_per_period(d_gate / a.nav)})
        allowed_oos.to_csv(out_dir / "trades.csv", index=False)
        pd.DataFrame({"equity": M.equity_from_returns(d_gate / a.nav, a.nav), "daily_ret": d_gate / a.nav, "daily_pnl": d_gate}).to_csv(out_dir / "equity.csv")
        stats = M.summary(d_gate / a.nav, allowed_oos)
        extra.update({"by_state": json.loads(by.to_json()), "gate_vs_base": cmpd, "n_trials": trial_count(family), "family": family,
                      "trial_sr_var": trial_sr_variance(family), "attrib": 100.0})
        kp = compute_kpis(allowed_oos, d_gate, a.nav, name=f"ml_regime_gate_{name}", family=family)
        (out_dir / "kpi.json").write_text(json.dumps(kp, indent=1, default=str))
        (out_dir / "metrics.json").write_text(json.dumps({"strategy": f"ml_regime_{name}", "synthetic": a.synthetic, "params": {**vars(a), "nav": a.nav},
                                                          "stats": stats, "extra": extra}, indent=1, default=str))
        print(M.format_report(f"ml_regime_gate_{name}  {'[SYNTHETIC]' if a.synthetic else ''}", stats,
                              {"kpi": {k: kp[k] for k in ("sharpe", "maxdd", "ntrades", "dsr", "boot")}}))
    else:
        (out_dir / "metrics.json").write_text(json.dumps({"strategy": f"ml_regime_{name}", "synthetic": a.synthetic, "params": vars(a),
                                                          "stats": {}, "extra": extra}, indent=1, default=str))

    # ---- sleeve allocation by state -------------------------------------------------------------------
    if a.sleeves:
        R = {}
        for f in a.sleeves:
            e = pd.read_csv(f, index_col=0, parse_dates=True)
            R[Path(f).parent.name] = e["daily_ret"]
        R = pd.DataFrame(R).fillna(0.0)
        R.index = pd.DatetimeIndex(R.index).normalize()
        R = R.reindex(st.index).fillna(0.0)
        eq_w = R.mean(axis=1)
        alloc = pd.Series(0.0, index=R.index)
        for i, d in enumerate(R.index):
            if i < 252:
                continue
            hist = R.iloc[max(0, i - 504):i]
            hs = st.iloc[max(0, i - 504):i]
            m = hist[hs == st.iloc[i]].mean()
            w = (m > 0).astype(float)
            w = w / w.sum() if w.sum() > 0 else pd.Series(0.0, index=w.index)
            alloc.iloc[i] = float((R.iloc[i] * w).sum())
        both = pd.DataFrame({"equal_weight": eq_w, "state_alloc": alloc}).iloc[252:]
        res = {k: {"sharpe": M.summary(v).get("sharpe"), "maxdd": M.summary(v).get("max_drawdown_pct")} for k, v in both.items()}
        print(f"  sleeve allocation by state vs equal weight (OOS after 1y): {res}")
        extra["sleeve_alloc"] = res
        both.to_csv(out_dir / "sleeve_alloc.csv")
    print(f"  written -> {out_dir}/")


if __name__ == "__main__":
    main()
