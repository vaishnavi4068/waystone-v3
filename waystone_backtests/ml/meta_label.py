#!/usr/bin/env python3
"""Meta-labeling: a GBDT that learns WHICH of a primary strategy's trades to take (and which to size up).

The primary strategy is untouched — V221 on MNQ 1-minute bars, the VWAP pullback scan on 10-minute stock
bars, or any trade list you hand in.  For every trade the primary would have taken, the model is shown
what was knowable at the SIGNAL bar (intraday state, prior-day daily features, vol index, breadth, GEX,
macro sentiment, per-name news sentiment, regime state) and asked: did this one make money?

Out-of-fold probabilities come from purged, embargoed walk-forward folds (never trained on the future).
Sizing rule on the OOF probability p, relative to the fold's BREAKEVEN probability p_be = |avg loss| / (avg win + |avg loss|)
measured on the training trades:   p < m_skip*p_be -> skip;   otherwise 1x;   p >= m_boost*p_be -> boost (2x).
(Multiples of p_be rather than absolute probabilities, so the same rule works for a 30 %-hit trend system
and a 60 %-hit mean-reversion system.)
The report compares the base book (every trade, 1x) with the meta-sized book on the SAME out-of-fold
trades, and writes the Stage-Gate KPI set (ml/kpi_export.py) so the dashboard can grade it.

  python ml/meta_label.py --primary v221 --synthetic                      # MNQ, 1-min, synthetic VXN/FnG
  python ml/meta_label.py --primary vwap --synthetic --n-symbols 20        # 10-min universe scan
  python ml/meta_label.py --primary trades --trades-csv results/05_orderflow_cvd_mnq/trades.csv --symbol MNQ
  python ml/meta_label.py --primary v221 --symbol MNQ --vol-symbol I:VXN --tune --dashboard "<kpi html>"

Real data: data/intraday/MNQ_1min.csv (fetch_polygon.py futures --root MNQ --resolution 1min),
data/daily/I_VXN.csv (fetch_polygon.py indices --symbols VXN), data/macro/fng.csv (ml/sentiment/fetch_free_sentiment.py fng),
and for --primary vwap data/intraday/<SYM>_1min.csv or _10min.csv per name plus data/daily/SPY.csv, data/daily/I_VIX.csv.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import data as D, metrics as M  # noqa: E402
from wsbt.report import RESULTS  # noqa: E402
from ml import common as C  # noqa: E402
from ml.features import align_prior_close, daily_features, feature_columns, features_at, intraday_features  # noqa: E402
from ml.labels import meta_labels, uniqueness_weights  # noqa: E402
from ml.cv import PurgedWalkForward, cscv_pbo  # noqa: E402
from ml.models import fit_predict_folds  # noqa: E402
from ml.evaluate import compare_base_meta, daily_pnl, log_trial, sharpe_of, trial_count, trial_sr_variance  # noqa: E402
from ml.kpi_export import build_banner, build_sections, compute_kpis, inject  # noqa: E402

FAMILY_PREFIX = "ml_meta_"


# ─────────────────────────────────────────────────────────────────────────────
# 1. primary trades + bars
# ─────────────────────────────────────────────────────────────────────────────
def primary_v221(a):
    from ml.engines import v221_primary as V
    bars = C.intraday(a.symbol, 1, a.synthetic, days=a.days, seed=a.seed)
    if a.synthetic and a.syn_vol_scale != 1.0:
        bars = C.scale_vol(bars, a.syn_vol_scale)          # the plain generator is too smooth for Renko bricks to form
    daily = C.daily_from_intraday(bars)
    vix = C.vol_index(a.vol_symbol, daily, a.synthetic)
    if a.synthetic:
        fng = C.synthetic_fng(daily.index, seed=a.seed)
    else:
        fng = C.load_macro().get("fng")
    trades = V.run_primary(bars, vix["close"], fng, contracts=a.contracts)
    return trades, {a.symbol: bars}, {a.symbol: daily}, vix, daily, fng


def primary_vwap(a):
    from ml.engines import vwap_pullback as W
    if a.synthetic:
        syms = [f"S{i:02d}" for i in range(a.n_symbols)]
        frames = C.synthetic_universe(syms, days=a.days, seed=a.seed, bar_min=10)
    else:
        syms = a.symbols or D.load_symbol_list(max_symbols=a.n_symbols)
        frames = {}
        for s in syms:
            try:
                frames[s] = C.intraday(s, 10)
            except D.DataMissing:
                pass
        if len(frames) < 2:
            raise SystemExit("need >= 2 names with data/intraday/<SYM>_1min.csv or _10min.csv (or --synthetic)")
    proxy = W.Proxy(delta=a.delta, premium_pct=a.premium_pct, qty=a.qty)
    trades = W.run_primary(frames, proxy, max_positions=a.max_positions)
    dailies = {s: C.daily_from_intraday(f) for s, f in frames.items()}
    # market context: SPY (or the universe mean) + VIX
    mkt = C.try_daily(a.market_symbol) if not a.synthetic else None
    if mkt is None:
        panel = pd.concat({s: d["close"] for s, d in dailies.items()}, axis=1)
        eq = panel.pct_change().mean(axis=1).fillna(0)
        lvl = 100 * (1 + eq).cumprod()
        mkt = pd.DataFrame({"open": lvl.shift(1).fillna(100), "high": lvl * 1.003, "low": lvl * 0.997, "close": lvl,
                            "volume": pd.concat({s: d["volume"] for s, d in dailies.items()}, axis=1).sum(axis=1)})
    vix = C.vol_index(a.vol_symbol, mkt, a.synthetic)
    fng = C.synthetic_fng(mkt.index, seed=a.seed) if a.synthetic else C.load_macro().get("fng")
    return trades, frames, dailies, vix, mkt, fng


def primary_trades_csv(a):
    """Any strategies/*/trades.csv: entry/exit columns may be entry_time/exit_time, entry_ts/exit_ts or entry_date/exit_date."""
    t = pd.read_csv(a.trades_csv)
    for src, dst in (("entry_ts", "entry_time"), ("exit_ts", "exit_time"), ("entry_date", "entry_time"), ("exit_date", "exit_time")):
        if dst not in t and src in t:
            t[dst] = t[src]
    for c in ("entry_time", "exit_time"):
        if c not in t:
            raise SystemExit(f"trades csv needs {c} (or entry_ts/exit_ts, entry_date/exit_date)")
        t[c] = pd.to_datetime(t[c], utc=True).dt.tz_convert(C.ET) if t[c].astype(str).str.contains(r"[+-]\d\d:\d\d|Z").any() \
            else pd.to_datetime(t[c])
    if "symbol" not in t:
        t["symbol"] = a.symbol
    if "side" not in t:
        t["side"] = 1
    if "cost" not in t:
        t["cost"] = 0.0
    if "units" not in t:
        t["units"] = 1
    t["signal_ts"] = t["entry_time"]
    frames, dailies = {}, {}
    for s in t["symbol"].unique():
        try:
            frames[s] = C.intraday(s, 1)
        except D.DataMissing:
            pass
        try:
            dailies[s] = D.load_daily(s)
        except Exception:
            if s in frames:
                dailies[s] = C.daily_from_intraday(frames[s])
    if not dailies:
        raise SystemExit("no daily bars for the symbols in the trade list (data/daily/<SYM>.csv)")
    mkt = C.try_daily(a.market_symbol)
    if mkt is None:
        mkt = next(iter(dailies.values()))
    vix = C.vol_index(a.vol_symbol, mkt, synthetic=False) if a.vol_symbol else None
    fng = C.load_macro().get("fng")
    return t, frames, dailies, vix, mkt, fng


# ─────────────────────────────────────────────────────────────────────────────
# 2. features for every trade
# ─────────────────────────────────────────────────────────────────────────────
def build_dataset(trades: pd.DataFrame, frames: dict, dailies: dict, vix, mkt: pd.DataFrame, fng, synthetic: bool,
                  regime_name: str | None = None) -> pd.DataFrame:
    macro = C.load_macro(synthetic, mkt.index) if synthetic else C.load_macro()
    if fng is not None and "fng" not in macro:
        macro["fng"] = fng
    panel = pd.concat({s: d["close"] for s, d in dailies.items()}, axis=1) if len(dailies) >= 5 else None
    regime = C.load_regime(regime_name) if regime_name else None
    mkt_feats = daily_features(mkt, vix=vix, panel=panel, macro=macro, regime=regime)
    parts = []
    intraday_cache = {}
    for sym, g in trades.groupby("symbol"):
        blocks = [g[["side"]].astype(float)]
        if "score" in g:
            blocks.append(g[["score"]].astype(float))
        when = pd.DatetimeIndex(g["entry_time"])
        if sym in frames:
            if sym not in intraday_cache:
                intraday_cache[sym] = intraday_features(frames[sym])
            fi = features_at(intraday_cache[sym], when, strict_before=True)
            fi = fi[feature_columns(fi)].add_prefix("i_")
            fi.index = g.index
            blocks.append(fi)
        if sym in dailies:
            sent = C.load_sentiment(sym) if not synthetic else None
            fd = daily_features(dailies[sym], vix=None if sym == "MNQ" else vix, sentiment=sent)
            fdd = align_prior_close(fd, when)
            fdd = fdd[feature_columns(fdd)].add_prefix("d_")
            fdd.index = g.index
            blocks.append(fdd)
        fm = align_prior_close(mkt_feats, when)
        fm = fm[feature_columns(fm)].add_prefix("m_")
        fm.index = g.index
        blocks.append(fm)
        parts.append(pd.concat(blocks, axis=1))
    X = pd.concat(parts).reindex(trades.index)
    et = pd.DatetimeIndex(trades["entry_time"])
    et = et.tz_convert(C.ET) if et.tz is not None else et
    X["hour"] = et.hour + et.minute / 60.0
    X["dow"] = et.dayofweek
    return X


# ─────────────────────────────────────────────────────────────────────────────
# 3. walk-forward meta model + sizing
# ─────────────────────────────────────────────────────────────────────────────
def walk_forward(X, y, w, t0, t1, a, **model_params):
    cv = PurgedWalkForward(n_splits=a.n_splits, embargo=a.embargo, min_train_frac=a.min_train_frac)
    folds = list(cv.split(t0, t1))
    if not folds:
        raise SystemExit("not enough trades for the requested folds — lower --n-splits or --min-train-frac")
    return fit_predict_folds(X, y, folds, weights=w, kind=a.model, seed=a.seed, **model_params), folds


def breakeven_prob(pnl: pd.Series) -> float:
    """p at which E[pnl] = 0 given the training fold's average win and average loss."""
    w, l = pnl[pnl > 0], pnl[pnl <= 0]
    if not len(w) or not len(l):
        return 0.5
    aw, al = float(w.mean()), float(-l.mean())
    return al / (aw + al) if aw + al > 0 else 0.5


def attach_breakeven(res: dict, trades: pd.DataFrame) -> None:
    for fp in res["fold_preds"]:
        fp["p_be"] = breakeven_prob(trades.iloc[fp["train_pos"]]["pnl"].astype(float))


def evaluate_thresholds(trades, p, m_skip, m_boost, nav, index, boost_size=2.0, p_be=None):
    """Thresholds are MULTIPLES of the breakeven probability p_be (per trade, from its fold's training set):
    skip if p < m_skip*p_be, boost if p >= m_boost*p_be.  p_be: Series aligned with trades, or a float."""
    pb = p_be.reindex(trades.index) if isinstance(p_be, pd.Series) else pd.Series(p_be if p_be is not None else 0.5, index=trades.index)
    sized = size_trades_rel(trades, p, pb, m_skip, m_boost, boost_size)
    d = daily_pnl(sized[sized["size"] > 0], index=index)
    return sized, d


def size_trades_rel(trades, p, p_be, m_skip, m_boost, boost_size=2.0):
    t = trades.copy()
    t["p"] = p.reindex(t.index).to_numpy()
    t["p_be"] = p_be.reindex(t.index).to_numpy()
    t = t[t["p"].notna()].copy()
    size = np.where(t["p"] >= m_boost * t["p_be"], boost_size, np.where(t["p"] >= m_skip * t["p_be"], 1.0, 0.0))
    t["size"] = size
    t["pnl_base"] = t["pnl"].astype(float)
    t["pnl"] = t["pnl_base"] * t["size"]
    if "cost" in t:
        t["cost"] = t["cost"].astype(float) * t["size"]
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--primary", choices=["v221", "vwap", "trades"], required=True)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--n-symbols", type=int, default=20)
    ap.add_argument("--trades-csv")
    ap.add_argument("--vol-symbol", default=None, help="I:VXN for MNQ, I:VIX for stocks (real data)")
    ap.add_argument("--market-symbol", default="SPY")
    ap.add_argument("--regime", default=None, help="name of a data/regime/<name>_states.csv from ml/regime_hmm.py")
    ap.add_argument("--days", type=int, default=250, help="synthetic sessions")
    ap.add_argument("--syn-vol-scale", type=float, default=3.0, help="synthetic MNQ: multiply the 1-min return path (v221 needs bricks)")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--contracts", type=int, default=2)
    ap.add_argument("--delta", type=float, default=0.65)
    ap.add_argument("--premium-pct", type=float, default=2.2)
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--nav", type=float, default=100_000.0)
    ap.add_argument("--n-splits", type=int, default=5)
    ap.add_argument("--embargo", default="2D")
    ap.add_argument("--min-train-frac", type=float, default=0.3)
    ap.add_argument("--min-pnl", type=float, default=0.0, help="label threshold: pnl > this = 1")
    ap.add_argument("--model", default="auto", choices=["auto", "lgb", "histgb"])
    ap.add_argument("--n-estimators", type=int, default=300)
    ap.add_argument("--num-leaves", type=int, default=15)
    ap.add_argument("--min-child", type=int, default=0, help="min samples per leaf (0 = auto: n_trades/15, 10..50)")
    ap.add_argument("--m-skip", type=float, default=1.0, help="skip when p < m_skip x breakeven probability")
    ap.add_argument("--m-boost", type=float, default=1.4, help="size up when p >= m_boost x breakeven probability")
    ap.add_argument("--boost-size", type=float, default=2.0, help="size multiple for boosted trades")
    ap.add_argument("--tune", action="store_true", help="nested walk-forward grid over (m_skip, m_boost) with CSCV/PBO and trial logging")
    ap.add_argument("--no-weights", action="store_true", help="disable uniqueness sample weights")
    ap.add_argument("--dashboard", help="KPI dashboard HTML to prefill")
    ap.add_argument("--flags", default="", help="killswitch,ddstop,runbook")
    ap.add_argument("--margin-per-unit", type=float, default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--no-sens", action="store_true", help="skip the ±20%% parameter-sensitivity refits")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    name = a.name or f"{FAMILY_PREFIX}{a.primary}" + ("_syn" if a.synthetic else "")
    family = name
    if a.primary == "v221":
        trades, frames, dailies, vix, mkt, fng = primary_v221(a)
        margin_per_unit = a.margin_per_unit or 2_500.0
    elif a.primary == "vwap":
        trades, frames, dailies, vix, mkt, fng = primary_vwap(a)
        margin_per_unit = a.margin_per_unit
    else:
        trades, frames, dailies, vix, mkt, fng = primary_trades_csv(a)
        margin_per_unit = a.margin_per_unit
    if len(trades) < 60:
        raise SystemExit(f"primary produced only {len(trades)} trades — too few to meta-label (need >= 60; use more history/--days)")
    trades = trades.reset_index(drop=True)
    print(f"[{name}] primary {a.primary}: {len(trades)} trades, net ${trades['pnl'].sum():,.0f}, "
          f"hit {100 * (trades['pnl'] > 0).mean():.1f}%  ({'SYNTHETIC' if a.synthetic else 'real data'})")

    X = build_dataset(trades, frames, dailies, vix, mkt, fng, a.synthetic, a.regime)
    X = X.loc[:, X.notna().mean() > 0.2]                      # drop features that are almost never available
    y = meta_labels(trades, a.min_pnl)
    t0, t1 = pd.Series(pd.DatetimeIndex(trades["entry_time"])), pd.Series(pd.DatetimeIndex(trades["exit_time"]))
    ref_index = next(iter(frames.values())).index if frames else pd.DatetimeIndex(mkt.index)
    w = None if a.no_weights else uniqueness_weights(t0, t1, ref_index)
    print(f"  features: {X.shape[1]}  label base rate: {y.mean():.3f}  uniqueness weight mean: {w.mean() if w is not None else 1:.3f}")

    min_child = a.min_child or int(np.clip(len(trades) // 15, 10, 50))
    model_params = dict(n_estimators=a.n_estimators, num_leaves=a.num_leaves, min_child_samples=min_child)
    res, folds = walk_forward(X, y, w, t0, t1, a, **model_params)
    attach_breakeven(res, trades)
    p = res["oof"]
    p_be_oof = pd.Series(np.nan, index=trades.index)
    for fp in res["fold_preds"]:
        p_be_oof.iloc[fp["test_pos"]] = fp["p_be"]
    oof_mask = p.notna()
    trades_oof = trades[oof_mask]
    print(f"  model: {res['model']}   folds: {len(res['folds'])}   OOF trades: {int(oof_mask.sum())}")
    with pd.option_context("display.width", 160):
        print(res["folds"].to_string(index=False))
    print(f"  importance stability (Spearman across folds): {res['importance_stability']}")
    if len(res["importance"]):
        top = res["importance"].mean().sort_values(ascending=False).head(12)
        print("  top features: " + ", ".join(f"{k} {v:.0f}" for k, v in top.items()))

    # daily index for the OOF period only
    oos_start = pd.Timestamp(trades_oof["entry_time"].min())
    day_index = pd.DatetimeIndex(pd.Series(pd.DatetimeIndex(daily_pnl(trades).index)))
    day_index = day_index[day_index >= (oos_start.tz_localize(None) if oos_start.tz is not None else oos_start).normalize()]
    # keep every session in the window (flat days count), from the bars if we have them
    if frames:
        sess = pd.DatetimeIndex(sorted(set(C.daily_from_intraday(next(iter(frames.values()))).index)))
        day_index = sess[sess >= day_index.min()] if len(day_index) else sess
    else:                                                   # daily-bar primaries: every session of the market series
        sess = pd.DatetimeIndex(mkt.index).normalize()
        day_index = sess[(sess >= day_index.min()) & (sess <= day_index.max())]

    # ---- threshold selection --------------------------------------------------------------
    # --tune: nested walk-forward.  For fold k the (m_skip, m_boost) pair is chosen on the OOF trades of folds
    # < k (fold 0 uses the defaults), so every OOF trade is sized with thresholds picked before its time.
    grid = [(s, b) for s in (0.8, 0.9, 1.0, 1.1, 1.2) for b in (1.2, 1.4, 1.6, 1.8) if b > s]
    trials, pbo = [], None
    thresholds_by_fold = {}
    if a.tune:
        mat = {}
        for s, b in grid:
            sized_g, d_g = evaluate_thresholds(trades_oof, p, s, b, a.nav, day_index, a.boost_size, p_be_oof)
            mat[(s, b)] = d_g
            sr = sharpe_of(d_g / a.nav)
            log_trial(family, {"m_skip": s, "m_boost": b, **model_params},
                      {"sharpe": round(sr, 3), "trades": int((sized_g["size"] > 0).sum()), "net_pnl": round(float(d_g.sum()), 2),
                       "sr_per_period": M.sharpe_per_period(d_g / a.nav)})
            trials.append({"m_skip": s, "m_boost": b, "sharpe": round(sr, 3), "net_pnl": round(float(d_g.sum()), 0),
                           "trades": int((sized_g["size"] > 0).sum())})
        R = pd.DataFrame({f"{k[0]}/{k[1]}": v for k, v in mat.items()}).fillna(0.0)
        pbo = cscv_pbo(R.to_numpy(), n_blocks=8 if len(R) < 400 else 16)
        for fp in res["fold_preds"]:
            k = fp["fold"]
            prior = [q for q in res["fold_preds"] if q["fold"] < k]
            if not prior:
                thresholds_by_fold[k] = (a.m_skip, a.m_boost)
                continue
            prior_pos = np.concatenate([q["test_pos"] for q in prior])
            prior_p = pd.Series(np.concatenate([q["p_test"] for q in prior]), index=trades.index[prior_pos])
            prior_be = pd.Series(np.concatenate([np.full(len(q["test_pos"]), q["p_be"]) for q in prior]), index=trades.index[prior_pos])
            best, best_sr = (a.m_skip, a.m_boost), -np.inf
            for s, b in grid:
                _, d_g = evaluate_thresholds(trades.iloc[prior_pos], prior_p, s, b, a.nav, None, a.boost_size, prior_be)
                sr = sharpe_of(d_g / a.nav) if len(d_g) > 5 else -np.inf
                if sr > best_sr:
                    best, best_sr = (s, b), sr
            thresholds_by_fold[k] = best
        print(f"  nested threshold choice per fold: " + ", ".join(f"f{k}:{v[0]}/{v[1]}" for k, v in thresholds_by_fold.items())
              + f"   CSCV PBO={pbo['pbo']} over {pbo['n_trials']} grid trials")
        tab = pd.DataFrame(trials).sort_values("sharpe", ascending=False)
        print("  full-OOF grid (search log, NOT the reported number):")
        print(tab.head(6).to_string(index=False))
    else:
        for fp in res["fold_preds"]:
            thresholds_by_fold[fp["fold"]] = (a.m_skip, a.m_boost)

    def size_by_fold(preds_key: str, pos_key: str, scale=(1.0, 1.0), preds=None) -> pd.DataFrame:
        parts = []
        for fp in (preds or res["fold_preds"]):
            s_, b_ = thresholds_by_fold.get(fp["fold"], (a.m_skip, a.m_boost))
            s_, b_ = s_ * scale[0], b_ * scale[1]
            if b_ <= s_:
                continue
            pos = fp[pos_key]
            pp = pd.Series(fp[preds_key], index=trades.index[pos])
            pb = pd.Series(fp["p_be"], index=trades.index[pos])
            parts.append(size_trades_rel(trades.iloc[pos], pp, pb, s_, b_, a.boost_size).assign(fold=fp["fold"]))
        return pd.concat(parts) if parts else pd.DataFrame()

    sized = size_by_fold("p_test", "test_pos").sort_values("entry_time")
    d_meta = daily_pnl(sized[sized["size"] > 0], index=day_index)
    m_skip, m_boost = thresholds_by_fold[max(thresholds_by_fold)]         # the multipliers a live deployment would use now
    p_be_live = res["fold_preds"][-1]["p_be"]
    print(f"  breakeven p (last fold train): {p_be_live:.3f}  ->  live thresholds skip<{m_skip * p_be_live:.3f}  boost>={m_boost * p_be_live:.3f}")
    if not a.tune:
        log_trial(family, {"m_skip": m_skip, "m_boost": m_boost, **model_params},
                  {"sharpe": round(sharpe_of(d_meta / a.nav), 3), "trades": int((sized["size"] > 0).sum()),
                   "net_pnl": round(float(d_meta.sum()), 2), "sr_per_period": M.sharpe_per_period(d_meta / a.nav)})
    cmp = compare_base_meta(trades_oof, sized, a.nav, day_index)
    print("  base vs meta (same OOF trades):")
    print(pd.DataFrame(cmp).to_string())

    # ---- in-sample view (for OOS/IS and WFE) ------------------------------------------------
    is_daily, wfe = None, None
    sized_is = size_by_fold("p_train", "train_pos")
    if len(sized_is):
        is_daily = daily_pnl(sized_is[sized_is["size"] > 0], index=pd.DatetimeIndex(daily_pnl(trades).index))
        is_means, oos_means = [], []
        for k, g_is in sized_is.groupby("fold"):
            g_oos = sized[sized["fold"] == k]
            d_tr, d_te = daily_pnl(g_is[g_is["size"] > 0]), daily_pnl(g_oos[g_oos["size"] > 0])
            span_is = max(1, (pd.Timestamp(g_is["exit_time"].max()) - pd.Timestamp(g_is["entry_time"].min())).days)
            span_oos = max(1, (pd.Timestamp(g_oos["exit_time"].max()) - pd.Timestamp(g_oos["entry_time"].min())).days)
            is_means.append(d_tr.sum() / span_is)
            oos_means.append(d_te.sum() / span_oos)
        if is_means and np.mean(is_means) > 0:
            wfe = float(np.mean(oos_means) / np.mean(is_means))
        elif is_means:
            wfe = 0.0

    # ---- parameter sensitivity: ±20% on the thresholds and the two main model knobs ---------------
    base_sr = sharpe_of(d_meta / a.nav)
    sens = []
    if base_sr > 0 and not a.no_sens:
        for sc in ((0.8, 1.0), (1.2, 1.0), (1.0, 0.8), (1.0, 1.2)):
            sz = size_by_fold("p_test", "test_pos", scale=sc)
            if len(sz):
                dd = daily_pnl(sz[sz["size"] > 0], index=day_index)
                sens.append((sharpe_of(dd / a.nav) - base_sr) / base_sr * 100)
        for mp in ({**model_params, "n_estimators": int(a.n_estimators * 0.8)}, {**model_params, "n_estimators": int(a.n_estimators * 1.2)},
                   {**model_params, "num_leaves": max(3, int(a.num_leaves * 0.8))}, {**model_params, "num_leaves": int(a.num_leaves * 1.2)}):
            r2, _ = walk_forward(X, y, w, t0, t1, a, **mp)
            attach_breakeven(r2, trades)
            sz = size_by_fold("p_test", "test_pos", preds=r2["fold_preds"])
            dd = daily_pnl(sz[sz["size"] > 0], index=day_index)
            sens.append((sharpe_of(dd / a.nav) - base_sr) / base_sr * 100)
    paramsens = float(max(0.0, -min(sens))) if sens else None

    # ---- outputs ---------------------------------------------------------------------------
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    daily_ret = d_meta / a.nav
    equity = M.equity_from_returns(daily_ret, a.nav)
    stats = M.summary(daily_ret, sized[sized["size"] > 0])
    n_trials = trial_count(family)
    extra = {"primary": a.primary, "model": res["model"], "n_features": int(X.shape[1]), "folds": len(res["folds"]),
             "auc_mean": round(float(res["folds"]["auc"].mean()), 3) if len(res["folds"]) else None,
             "importance_stability": res["importance_stability"],
             "thresholds": {"m_skip": m_skip, "m_boost": m_boost, "boost_size": a.boost_size, "p_be_live": round(p_be_live, 4),
                            "p_skip_live": round(m_skip * p_be_live, 4), "p_boost_live": round(m_boost * p_be_live, 4),
                            "by_fold": {int(k): list(v) for k, v in thresholds_by_fold.items()}},
             "base_vs_meta": cmp, "n_trials": n_trials, "trial_sr_var": trial_sr_variance(family), "family": family,
             "pbo": pbo["pbo"] if pbo else None, "paramsens": paramsens, "wfe": wfe, "margin_per_unit": margin_per_unit,
             "attrib": 100.0 if a.primary != "vwap" else None, "synthetic": a.synthetic,
             "notes": [f"Primary: {a.primary}; {len(trades)} primary trades, {int(oof_mask.sum())} out-of-fold.",
                       f"Model {res['model']}, {len(res['folds'])} purged walk-forward folds, embargo {a.embargo}, AUC mean "
                       f"{round(float(res['folds']['auc'].mean()), 3) if len(res['folds']) else None}, importance stability {res['importance_stability']}.",
                       f"Sizing: skip below {m_skip}x and boost above {m_boost}x the fold's breakeven probability "
                       f"(live: p_be={p_be_live:.3f}) — {'nested walk-forward choice per fold' if a.tune else 'fixed multipliers'}; "
                       f"{n_trials} trials logged for the deflated Sharpe.",
                       "VWAP option leg is a delta proxy on the underlying (see ml/engines/vwap_pullback.py)." if a.primary == "vwap" else
                       "MNQ P&L = pts x $2 x contracts - commission; VXN fed as prior-day close like the live bot."]}
    (out_dir / "metrics.json").write_text(json.dumps({"strategy": name, "synthetic": a.synthetic, "params": {**vars(a), "nav": a.nav},
                                                      "stats": stats, "extra": extra}, indent=1, default=str))
    sized_out = sized.copy()
    sized_out.to_csv(out_dir / "trades.csv", index=False)
    trades.assign(p=p, y=y).to_csv(out_dir / "primary_trades.csv", index=False)
    eq = pd.DataFrame({"equity": equity, "daily_ret": daily_ret, "daily_pnl": d_meta})
    if is_daily is not None:
        eq["is_daily_pnl"] = is_daily.reindex(eq.index)
    eq.to_csv(out_dir / "equity.csv")
    res["folds"].to_csv(out_dir / "folds.csv", index=False)
    if len(res["importance"]):
        res["importance"].T.assign(mean=res["importance"].mean()).sort_values("mean", ascending=False).to_csv(out_dir / "importance.csv")
    if trials:
        pd.DataFrame(trials).to_csv(out_dir / "threshold_grid.csv", index=False)
    X.assign(y=y, p=p).to_csv(out_dir / "features.csv")

    flags = {f.strip(): True for f in a.flags.split(",") if f.strip()}
    kpis = compute_kpis(sized[sized["size"] > 0], d_meta, a.nav, name=name, is_daily=is_daily, wfe=wfe, n_trials=n_trials,
                        trial_sr_var=extra["trial_sr_var"], family=family, pbo=extra["pbo"], paramsens=paramsens, flags=flags,
                        margin_per_unit=margin_per_unit, attrib=extra["attrib"], extra={"model": res["model"], "primary": a.primary})
    (out_dir / "kpi.json").write_text(json.dumps(kpis, indent=1, default=str))
    if a.dashboard:
        tpl = Path(a.dashboard).read_text(encoding="utf-8", errors="ignore")
        html = inject(tpl, kpis, build_banner(kpis, "SYNTHETIC DATA — mechanics only" if a.synthetic else ""),
                      build_sections(kpis, extra["notes"], sized_out[sized_out["size"] > 0]))
        (out_dir / "dashboard.html").write_text(html, encoding="utf-8")
    print(M.format_report(f"{name}  {'[SYNTHETIC — mechanics only]' if a.synthetic else ''}", stats,
                          {"kpi": {k: kpis[k] for k in ("sharpe", "maxdd", "ntrades", "coststress", "oosis", "wfe", "dsr", "pbo", "paramsens", "boot")}}))
    print(f"  written -> {out_dir}/ (metrics.json, trades.csv, primary_trades.csv, equity.csv, folds.csv, importance.csv, kpi.json"
          f"{', dashboard.html' if a.dashboard else ''})")


if __name__ == "__main__":
    main()
