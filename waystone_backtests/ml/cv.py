"""Cross-validation that respects time.

  PurgedWalkForward   expanding (or rolling) walk-forward folds on EVENTS that have a start (t0) and an end
                      (t1, the label's exit).  Training events whose label window overlaps the test window
                      are PURGED, and an EMBARGO gap after the test window is applied so that serial
                      correlation across the boundary cannot leak.  Nothing after the test window is ever
                      in the training set (walk-forward, unlike the combinatorial K-fold).
  PurgedKFold         K contiguous test blocks with purge + embargo, training on both sides — more data per
                      fold, but the "future" is in the training set; use for model selection, never for the
                      final number.
  cscv_pbo            Bailey, Borwein, López de Prado & Zhu (2017) Probability of Backtest Overfitting via
                      Combinatorially Symmetric Cross-Validation over a matrix of trial returns.
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd


class PurgedWalkForward:
    def __init__(self, n_splits: int = 5, embargo: pd.Timedelta | str = "2D", min_train_frac: float = 0.3,
                 rolling_train: pd.Timedelta | str | None = None):
        self.n_splits = int(n_splits)
        self.embargo = pd.Timedelta(embargo)
        self.min_train_frac = float(min_train_frac)
        self.rolling_train = pd.Timedelta(rolling_train) if rolling_train is not None else None

    def split(self, t0: pd.Series, t1: pd.Series):
        """t0/t1: per-event start and end timestamps (same tz).  Yields (train_idx, test_idx) as integer
        positions.  Test blocks partition the period after the initial training window."""
        t0 = pd.DatetimeIndex(t0)
        t1 = pd.DatetimeIndex(t1)
        order = np.argsort(t0.values)
        n = len(t0)
        start_i = int(n * self.min_train_frac)
        cuts = np.linspace(start_i, n, self.n_splits + 1).astype(int)
        for k in range(self.n_splits):
            test_pos = order[cuts[k]:cuts[k + 1]]
            if len(test_pos) == 0:
                continue
            test_start = t0[test_pos].min()
            test_end = t1[test_pos].max()
            # training: events that END before the test window starts (minus embargo)
            train_mask = (t1 < test_start - self.embargo)
            if self.rolling_train is not None:
                train_mask &= (t0 >= test_start - self.rolling_train)
            train_pos = np.where(train_mask)[0]
            if len(train_pos) == 0:
                continue
            yield train_pos, np.sort(test_pos), (test_start, test_end)


class PurgedKFold:
    def __init__(self, n_splits: int = 5, embargo: pd.Timedelta | str = "2D"):
        self.n_splits = int(n_splits)
        self.embargo = pd.Timedelta(embargo)

    def split(self, t0: pd.Series, t1: pd.Series):
        t0 = pd.DatetimeIndex(t0)
        t1 = pd.DatetimeIndex(t1)
        order = np.argsort(t0.values)
        n = len(t0)
        cuts = np.linspace(0, n, self.n_splits + 1).astype(int)
        for k in range(self.n_splits):
            test_pos = order[cuts[k]:cuts[k + 1]]
            if len(test_pos) == 0:
                continue
            ts, te = t0[test_pos].min(), t1[test_pos].max()
            keep = (t1 < ts - self.embargo) | (t0 > te + self.embargo)
            train_pos = np.where(keep)[0]
            yield train_pos, np.sort(test_pos), (ts, te)


def _sharpe(x: np.ndarray) -> float:
    s = x.std(ddof=1) if len(x) > 1 else 0.0
    return float(x.mean() / s) if s > 0 else 0.0


def cscv_pbo(returns: pd.DataFrame | np.ndarray, n_blocks: int = 16, max_combos: int = 3000, seed: int = 0) -> dict:
    """returns: T x N matrix of per-period returns, one column per TRIAL (parameter set / model variant).
    Splits T into n_blocks contiguous blocks; for every choice of n_blocks/2 blocks as in-sample, picks the
    best IS trial by Sharpe, ranks it out-of-sample among the N trials, and records logit(rank).
    PBO = share of combinations where the IS-best trial is below the OOS median.  Also returns the
    IS→OOS Sharpe degradation slope and the probability of OOS loss for the selected trial."""
    R = np.asarray(returns, dtype=float)
    if R.ndim == 1:
        R = R[:, None]
    T, N = R.shape
    if N < 2 or T < 2 * n_blocks:
        return {"pbo": None, "n_trials": N, "n_combos": 0, "note": "need >=2 trials and T >= 2*n_blocks"}
    R = R[: (T // n_blocks) * n_blocks]
    blocks = R.reshape(n_blocks, -1, N)                       # block, t, trial
    bs = blocks.sum(axis=1)                                   # sums per block
    bss = (blocks ** 2).sum(axis=1)
    L = blocks.shape[1]
    combos = list(itertools.combinations(range(n_blocks), n_blocks // 2))
    rng = np.random.default_rng(seed)
    if len(combos) > max_combos:
        combos = [combos[i] for i in rng.choice(len(combos), max_combos, replace=False)]
    all_blocks = set(range(n_blocks))
    logits, is_sr, oos_sr, oos_neg = [], [], [], 0
    for comb in combos:
        ins = list(comb)
        oos = sorted(all_blocks - set(ins))
        def sr(sel):
            n = L * len(sel)
            m = bs[sel].sum(axis=0) / n
            var = (bss[sel].sum(axis=0) / n - m ** 2) * n / max(n - 1, 1)
            sd = np.sqrt(np.maximum(var, 1e-18))
            return m / sd
        s_is, s_oos = sr(ins), sr(oos)
        best = int(np.argmax(s_is))
        rank = (s_oos < s_oos[best]).sum() + 0.5 * (s_oos == s_oos[best]).sum()   # 0..N
        w = (rank + 0.5) / (N + 1)
        logits.append(math.log(w / (1 - w)))
        is_sr.append(s_is[best]); oos_sr.append(s_oos[best])
        oos_neg += int(s_oos[best] <= 0)
    logits = np.array(logits)
    is_sr, oos_sr = np.array(is_sr), np.array(oos_sr)
    slope = float(np.polyfit(is_sr, oos_sr, 1)[0]) if len(is_sr) > 2 and is_sr.std() > 0 else None
    return {"pbo": round(float((logits <= 0).mean()), 3), "n_trials": N, "n_combos": len(combos),
            "prob_oos_loss": round(oos_neg / len(combos), 3), "is_oos_slope": round(slope, 3) if slope is not None else None,
            "mean_oos_sr_of_is_best": round(float(oos_sr.mean()), 4)}


def fold_table(folds: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(folds)
