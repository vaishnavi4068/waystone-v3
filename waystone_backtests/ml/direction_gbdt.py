#!/usr/bin/env python3
"""Direction GBDT — the weakest of the three ML uses, run last and sized as a TILT, never as a signal.

Label: next-`horizon`-day return (next open -> close H days later) beyond a cost hurdle, binary.
Features: the daily store (price/vol/breadth/vol-index/macro/sentiment/regime) as of the decision close.
Model: same conservative GBDT, purged walk-forward folds, expanding training window.
Position rule on the OOF probability p (decided at the close, filled at the next open):
    tilt = clip((p - 0.5) / margin, -1, 1) x max_tilt   (long when p > 0.5, short when p < 0.5, flat inside the dead band)
    or, with --mode gate, +1 only when p > 0.5 + margin (long-only overlay for a long sleeve).
Reported: OOF AUC per fold, information coefficient (Spearman p vs realised return), the tilt's Sharpe /
drawdown vs buy-and-hold, deflated Sharpe with the trial count, and the KPI set.  A direction model that
does not clear DSR here should be discarded, not tuned — that is the point of running it inside the gates.

  python ml/direction_gbdt.py --synthetic
  python ml/direction_gbdt.py --symbol SPY --vol-symbol I:VIX --horizon 5 --tune
  python ml/direction_gbdt.py --symbol MES --futures --vol-symbol I:VIX --horizon 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import costs, data as D, metrics as M  # noqa: E402
from wsbt.engine import simulate_positions  # noqa: E402
from wsbt.report import RESULTS  # noqa: E402
from ml import common as C  # noqa: E402
from ml.features import daily_features, feature_columns  # noqa: E402
from ml.labels import fwd_return_labels  # noqa: E402
from ml.cv import PurgedWalkForward, cscv_pbo  # noqa: E402
from ml.models import fit_predict_folds  # noqa: E402
from ml.evaluate import log_trial, sharpe_of, trial_count, trial_sr_variance  # noqa: E402
from ml.kpi_export import build_banner, build_sections, compute_kpis, inject  # noqa: E402


def tilt_positions(p: pd.Series, margin: float, max_tilt: float = 1.0, mode: str = "tilt") -> pd.Series:
    if mode == "gate":
        return ((p > 0.5 + margin).astype(float) * max_tilt).fillna(0.0)
    return (((p - 0.5) / max(margin, 1e-6)).clip(-1, 1) * max_tilt).where(p.notna(), 0.0)


def run_tilt(bars, p, margin, max_tilt, mode, cost, size, nav):
    tgt = tilt_positions(p, margin, max_tilt, mode)
    tgt[(tgt.abs() < 0.05)] = 0.0
    dr, tr, eq = simulate_positions(bars, tgt, cost, size, nav)
    return dr, tr, eq


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--vol-symbol", default=None)
    ap.add_argument("--panel-symbols", nargs="*")
    ap.add_argument("--regime", default=None)
    ap.add_argument("--futures", action="store_true", help="MES/MNQ cost model and 1-contract sizing")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--cost-hurdle-bps", type=float, default=5.0)
    ap.add_argument("--margin", type=float, default=0.05, help="dead band around 0.5 (tilt reaches max at 0.5±margin)")
    ap.add_argument("--max-tilt", type=float, default=1.0)
    ap.add_argument("--mode", choices=["tilt", "gate"], default="tilt")
    ap.add_argument("--n-splits", type=int, default=6)
    ap.add_argument("--min-train-frac", type=float, default=0.4)
    ap.add_argument("--model", default="auto", choices=["auto", "lgb", "histgb"])
    ap.add_argument("--n-estimators", type=int, default=200)
    ap.add_argument("--num-leaves", type=int, default=7)
    ap.add_argument("--min-child", type=int, default=60)
    ap.add_argument("--tune", action="store_true", help="grid over margin x horizon with trial logging + PBO")
    ap.add_argument("--nav", type=float, default=100_000.0)
    ap.add_argument("--notional", type=float, default=100_000.0, help="$ per unit tilt (stocks/ETF)")
    ap.add_argument("--n", type=int, default=2500)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dashboard")
    ap.add_argument("--flags", default="")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    name = a.name or f"ml_direction_{D._safe_name(a.symbol).lower()}" + ("_syn" if a.synthetic else "")
    family = name

    if a.synthetic:
        bars = D.synthetic_daily(n=a.n, seed=a.seed, regime=True)
        vix = C.vol_index(None, bars, synthetic=True, seed=a.seed)
        panel = D.closes_panel(D.synthetic_panel([f"S{i}" for i in range(12)], n=a.n, seed=a.seed))
        macro = C.load_macro(True, bars.index, a.seed)
        sent = None
    else:
        bars = D.load_daily(a.symbol)
        vix = C.vol_index(a.vol_symbol, bars, synthetic=False) if a.vol_symbol else None
        panel = D.closes_panel(D.load_many(a.panel_symbols)) if a.panel_symbols else None
        macro = C.load_macro()
        sent = C.load_sentiment(a.symbol)
    regime = C.load_regime(a.regime) if a.regime else None
    F = daily_features(bars, vix=vix, panel=panel, macro=macro, sentiment=sent, regime=regime)
    F = F[feature_columns(F)]
    F = F.loc[:, F.notna().mean() > 0.5]
    cost = costs.MES if a.futures else costs.US_ETF
    size = {"units": 1} if a.futures else {"notional": a.notional}
    hurdle = a.cost_hurdle_bps / 1e4

    def fit(horizon: int, **mp):
        lab = fwd_return_labels(bars, horizon, hurdle)
        ok = lab["y"].notna() & F.notna().any(axis=1)
        X, y = F[ok], lab.loc[ok, "y"].astype(int)
        t0 = pd.Series(X.index)
        t1 = pd.Series(pd.DatetimeIndex(lab.loc[ok, "t1"]).fillna(X.index[-1]))
        cv = PurgedWalkForward(n_splits=a.n_splits, embargo=pd.Timedelta(days=int(horizon * 1.5) + 1), min_train_frac=a.min_train_frac)
        folds = list(cv.split(t0, t1))
        res = fit_predict_folds(X, y, folds, kind=a.model, seed=a.seed, **mp)
        res["fwd"] = lab.loc[ok, "fwd_ret"]
        return res

    model_params = dict(n_estimators=a.n_estimators, num_leaves=a.num_leaves, min_child_samples=a.min_child)
    res = fit(a.horizon, **model_params)
    p = res["oof"]
    ok = p.notna()
    ic = spearmanr(p[ok], res["fwd"][ok]).correlation if ok.sum() > 20 else float("nan")
    print(f"[{name}] {res['model']}  features {F.shape[1]}  folds {len(res['folds'])}  OOF days {int(ok.sum())}  "
          f"AUC mean {res['folds']['auc'].mean():.3f}  IC (Spearman p vs fwd ret) {ic:.3f}  importance stability {res['importance_stability']}")
    with pd.option_context("display.width", 160):
        print(res["folds"].to_string(index=False))

    # ---- tilt backtest on the OOF window --------------------------------------------------------
    oos_bars = bars[bars.index >= p[ok].index.min()]
    trials = []
    pbo = None
    margin, horizon = a.margin, a.horizon
    if a.tune:
        mat = {}
        for h in (3, 5, 10):
            r_h = res if h == a.horizon else fit(h, **model_params)
            for mg in (0.03, 0.05, 0.08, 0.12):
                dr, tr, _ = run_tilt(oos_bars, r_h["oof"], mg, a.max_tilt, a.mode, cost, size, a.nav)
                mat[(h, mg)] = dr
                sr = sharpe_of(dr)
                log_trial(family, {"horizon": h, "margin": mg, **model_params}, {"sharpe": round(sr, 3), "trades": len(tr),
                          "net_pnl": round(float(dr.sum() * a.nav), 2), "sr_per_period": M.sharpe_per_period(dr)})
                trials.append({"horizon": h, "margin": mg, "sharpe": round(sr, 3), "trades": len(tr), "maxdd": M.summary(dr).get("max_drawdown_pct")})
        R = pd.DataFrame({f"{k[0]}/{k[1]}": v for k, v in mat.items()}).fillna(0.0)
        pbo = cscv_pbo(R.to_numpy(), n_blocks=16 if len(R) >= 400 else 8)
        tab = pd.DataFrame(trials).sort_values("sharpe", ascending=False)
        print("  grid (search log):"); print(tab.to_string(index=False))
        # nested choice: pick on the first half of the OOF window, report on the second half
        half = R.index[len(R) // 2]
        sel = R[R.index < half].apply(sharpe_of).idxmax()
        horizon, margin = int(sel.split("/")[0]), float(sel.split("/")[1])
        print(f"  chosen on first half of OOF: horizon={horizon} margin={margin}  ->  reported on the second half;  CSCV PBO={pbo['pbo']}")
        res_sel = res if horizon == a.horizon else fit(horizon, **model_params)
        p_sel = res_sel["oof"]
        oos_bars = bars[bars.index >= half]
        p_sel = p_sel[p_sel.index >= half]
        dr, tr, eq = run_tilt(oos_bars, p_sel, margin, a.max_tilt, a.mode, cost, size, a.nav)
    else:
        dr, tr, eq = run_tilt(oos_bars, p, margin, a.max_tilt, a.mode, cost, size, a.nav)
        log_trial(family, {"horizon": horizon, "margin": margin, **model_params}, {"sharpe": round(sharpe_of(dr), 3), "trades": len(tr),
                  "net_pnl": round(float(dr.sum() * a.nav), 2), "sr_per_period": M.sharpe_per_period(dr)})
    bh = np.log(oos_bars["close"]).diff().fillna(0.0) * (size.get("notional", oos_bars["close"].iloc[0] * cost.multiplier) / a.nav)
    stats = M.summary(dr, tr)
    print(M.format_report(f"{name}  {'[SYNTHETIC — mechanics only]' if a.synthetic else ''}", stats,
                          {"buy_and_hold_sharpe": M.summary(bh).get("sharpe"), "buy_and_hold_maxdd": M.summary(bh).get("max_drawdown_pct"),
                           "exposure_mean_abs": round(float(tilt_positions(p[ok], margin, a.max_tilt, a.mode).abs().mean()), 3)}))

    # parameter sensitivity ±20% on margin, horizon
    base_sr = sharpe_of(dr)
    sens = []
    if base_sr > 0:
        for mg in (margin * 0.8, margin * 1.2):
            d2, _, _ = run_tilt(oos_bars, p, mg, a.max_tilt, a.mode, cost, size, a.nav)
            sens.append((sharpe_of(d2) - base_sr) / base_sr * 100)
        for h in {max(1, int(round(horizon * 0.8))), int(round(horizon * 1.2))} - {horizon}:
            r2 = fit(h, **model_params)
            d2, _, _ = run_tilt(oos_bars, r2["oof"], margin, a.max_tilt, a.mode, cost, size, a.nav)
            sens.append((sharpe_of(d2) - base_sr) / base_sr * 100)
    paramsens = float(max(0.0, -min(sens))) if sens else None

    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    tr = tr.rename(columns={"entry_date": "entry_time", "exit_date": "exit_time"}) if len(tr) else tr
    if len(tr):
        tr["cost"] = 2 * cost.commission * tr["units"] + 2 * (tr["entry"] * cost.slippage_bps / 1e4 + cost.slippage_abs) * tr["units"] * cost.multiplier
    n_trials = trial_count(family)
    extra = {"model": res["model"], "auc_mean": round(float(res["folds"]["auc"].mean()), 3), "ic": round(float(ic), 3) if ic == ic else None,
             "importance_stability": res["importance_stability"], "horizon": horizon, "margin": margin, "mode": a.mode,
             "n_trials": n_trials, "trial_sr_var": trial_sr_variance(family), "family": family, "pbo": pbo["pbo"] if pbo else None,
             "paramsens": paramsens, "attrib": 100.0, "synthetic": a.synthetic,
             "notes": [f"Direction GBDT on {a.symbol}: horizon {horizon}d, dead band ±{margin}, mode {a.mode}; AUC {res['folds']['auc'].mean():.3f}, IC {ic:.3f}.",
                       f"{n_trials} trials logged for the deflated Sharpe; PBO {pbo['pbo'] if pbo else 'n/a'}.",
                       "Position decided at the close, filled at the next open; costs from wsbt.costs."]}
    (out_dir / "metrics.json").write_text(json.dumps({"strategy": name, "synthetic": a.synthetic, "params": {**vars(a), "nav": a.nav},
                                                      "stats": stats, "extra": extra}, indent=1, default=str))
    tr.to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame({"equity": eq, "daily_ret": dr, "daily_pnl": dr * a.nav}).to_csv(out_dir / "equity.csv")
    res["folds"].to_csv(out_dir / "folds.csv", index=False)
    if len(res["importance"]):
        res["importance"].T.assign(mean=res["importance"].mean()).sort_values("mean", ascending=False).to_csv(out_dir / "importance.csv")
    flags = {f.strip(): True for f in a.flags.split(",") if f.strip()}
    kp = compute_kpis(tr, dr * a.nav, a.nav, name=name, n_trials=n_trials, trial_sr_var=extra["trial_sr_var"], family=family,
                      pbo=extra["pbo"], paramsens=paramsens, flags=flags, attrib=100.0, extra={"model": res["model"]})
    (out_dir / "kpi.json").write_text(json.dumps(kp, indent=1, default=str))
    if a.dashboard:
        tpl = Path(a.dashboard).read_text(encoding="utf-8", errors="ignore")
        (out_dir / "dashboard.html").write_text(inject(tpl, kp, build_banner(kp, "SYNTHETIC" if a.synthetic else ""), build_sections(kp, extra["notes"], tr)), encoding="utf-8")
    print(f"  kpi: { {k: kp[k] for k in ('sharpe', 'maxdd', 'ntrades', 'oosis', 'dsr', 'pbo', 'paramsens', 'boot')} }")
    print(f"  written -> {out_dir}/")


if __name__ == "__main__":
    main()
