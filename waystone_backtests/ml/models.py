"""Model wrappers.  LightGBM when it is installed, sklearn HistGradientBoosting otherwise — identical
interface, similar inductive bias.  Deliberately conservative defaults (few leaves, strong minimum leaf
size, L2, subsampling): the datasets here are hundreds to low thousands of events, not millions, and
the goal is a probability that ranks trades, not a leaderboard AUC."""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

try:
    import lightgbm as lgb
    HAVE_LGB = True
except Exception:                                    # pragma: no cover
    HAVE_LGB = False
from sklearn.ensemble import HistGradientBoostingClassifier

DEFAULTS = dict(n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=50,
                subsample=0.8, colsample_bytree=0.8, reg_lambda=5.0)


def make_classifier(kind: str = "auto", seed: int = 0, **overrides):
    p = {**DEFAULTS, **overrides}
    use_lgb = HAVE_LGB and kind in ("auto", "lgb", "lightgbm")
    if use_lgb:
        return lgb.LGBMClassifier(n_estimators=int(p["n_estimators"]), learning_rate=p["learning_rate"],
                                  num_leaves=int(p["num_leaves"]), min_child_samples=int(p["min_child_samples"]),
                                  subsample=p["subsample"], subsample_freq=1, colsample_bytree=p["colsample_bytree"],
                                  reg_lambda=p["reg_lambda"], random_state=seed, verbose=-1, n_jobs=2)
    return HistGradientBoostingClassifier(max_iter=int(p["n_estimators"]), learning_rate=p["learning_rate"],
                                          max_leaf_nodes=int(p["num_leaves"]), min_samples_leaf=int(p["min_child_samples"]),
                                          l2_regularization=p["reg_lambda"], random_state=seed)


def model_name() -> str:
    return "lightgbm" if HAVE_LGB else "sklearn_histgb"


def _importance(model, cols: list[str], X: pd.DataFrame | None = None, y=None) -> pd.Series:
    if hasattr(model, "booster_"):
        imp = model.booster_.feature_importance(importance_type="gain")
        return pd.Series(imp, index=cols, dtype=float)
    # HistGB has no native importance -> permutation importance on the given (test) set
    from sklearn.inspection import permutation_importance
    if X is None or y is None or len(X) < 20:
        return pd.Series(0.0, index=cols)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = permutation_importance(model, X, y, n_repeats=3, random_state=0, scoring="roc_auc")
    return pd.Series(r.importances_mean, index=cols, dtype=float)


def fit_predict_folds(X: pd.DataFrame, y: pd.Series, folds, weights: pd.Series | None = None,
                      kind: str = "auto", seed: int = 0, **params) -> dict:
    """folds: iterable of (train_pos, test_pos, (test_start, test_end)).  Returns out-of-fold probabilities
    (NaN where an event was never in a test fold), per-fold metrics, per-fold feature importances and the
    importance-stability score (mean pairwise Spearman rho of the importance vectors)."""
    cols = list(X.columns)
    Xv = X.to_numpy(dtype=float)
    yv = y.to_numpy(dtype=int)
    wv = weights.to_numpy(dtype=float) if weights is not None else None
    oof = np.full(len(X), np.nan)
    p_is_sum, p_is_cnt = np.zeros(len(X)), np.zeros(len(X))
    rows, imps, fold_preds = [], [], []
    for k, (tr, te, (ts, te_end)) in enumerate(folds):
        if len(np.unique(yv[tr])) < 2 or len(te) == 0:
            continue
        m = make_classifier(kind, seed=seed + k, **params)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if wv is not None:
                m.fit(Xv[tr], yv[tr], sample_weight=wv[tr])
            else:
                m.fit(Xv[tr], yv[tr])
        p = m.predict_proba(Xv[te])[:, 1]
        oof[te] = p
        p_tr = m.predict_proba(Xv[tr])[:, 1]
        p_is_sum[tr] += p_tr; p_is_cnt[tr] += 1
        fold_preds.append({"fold": k, "train_pos": tr, "test_pos": te, "p_train": p_tr, "p_test": p})
        yt = yv[te]
        auc = roc_auc_score(yt, p) if len(np.unique(yt)) > 1 else float("nan")
        rows.append({"fold": k, "test_start": ts, "test_end": te_end, "n_train": len(tr), "n_test": len(te),
                     "base_rate_train": round(float(yv[tr].mean()), 3), "base_rate_test": round(float(yt.mean()), 3),
                     "auc": round(float(auc), 3) if auc == auc else None,
                     "brier": round(float(brier_score_loss(yt, p)), 4),
                     "logloss": round(float(log_loss(yt, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])), 4)})
        imps.append(_importance(m, cols, pd.DataFrame(Xv[te], columns=cols), yt))
    imp_df = pd.DataFrame(imps) if imps else pd.DataFrame(columns=cols)
    stability = None
    if len(imps) >= 2:
        rhos = []
        for i in range(len(imps)):
            for j in range(i + 1, len(imps)):
                a, b = imps[i].to_numpy(), imps[j].to_numpy()
                if a.std() > 0 and b.std() > 0:
                    rhos.append(spearmanr(a, b).correlation)
        stability = round(float(np.nanmean(rhos)), 3) if rhos else None
    p_is = np.where(p_is_cnt > 0, p_is_sum / np.maximum(p_is_cnt, 1), np.nan)
    return {"oof": pd.Series(oof, index=X.index, name="p"), "p_is": pd.Series(p_is, index=X.index, name="p_is"),
            "folds": pd.DataFrame(rows), "fold_preds": fold_preds,
            "importance": imp_df, "importance_stability": stability, "model": model_name()}


def fit_final(X: pd.DataFrame, y: pd.Series, weights: pd.Series | None = None, kind: str = "auto", seed: int = 0, **params):
    m = make_classifier(kind, seed=seed, **params)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if weights is not None:
            m.fit(X.to_numpy(dtype=float), y.to_numpy(dtype=int), sample_weight=weights.to_numpy(dtype=float))
        else:
            m.fit(X.to_numpy(dtype=float), y.to_numpy(dtype=int))
    return m
