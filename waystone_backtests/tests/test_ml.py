"""Leakage and sanity tests for the ML layer.  Run:  python -m pytest tests/test_ml.py -q"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wsbt import data as D  # noqa: E402
from ml import common as C  # noqa: E402
from ml.features import align_prior_close, daily_features, features_at, intraday_features  # noqa: E402
from ml.labels import triple_barrier, uniqueness_weights  # noqa: E402
from ml.cv import PurgedKFold, PurgedWalkForward, cscv_pbo  # noqa: E402
from ml.models import fit_predict_folds  # noqa: E402
from ml.kpi_export import compute_kpis, inject  # noqa: E402
from ml.engines import vwap_pullback as W, v221_primary as V  # noqa: E402


# ── feature alignment ─────────────────────────────────────────────────────────
def test_daily_features_strictly_prior_close():
    bars = D.synthetic_daily(400, seed=2)
    F = daily_features(bars)
    when = pd.DatetimeIndex(["2016-03-01 10:15", "2016-03-01 15:59", "2016-03-02 09:31"]).tz_localize("America/New_York")
    out = align_prior_close(F, when)
    for w, fd in zip(when, out["feat_date"]):
        assert pd.Timestamp(fd) < w.tz_localize(None).normalize(), "daily feature row must be from a day strictly before the signal"
    # the two intraday timestamps on 03-01 must see the same (03-01 - 1 session) row
    assert out["feat_date"].iloc[0] == out["feat_date"].iloc[1]


def test_massive_insight_picker():
    from ml.sentiment.fetch_free_sentiment import _insight_for_ticker, _merge_news, _news_row, NEWS_COLS

    insights = [
        {"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "AI demand"},
        {"ticker": "AAPL", "sentiment": "neutral", "sentiment_reasoning": "mixed supply chain"},
    ]
    assert _insight_for_ticker(insights, "AAPL") == ("neutral", "mixed supply chain")
    row = _news_row(
        date="2024-01-02",
        ts="2024-01-02T10:00:00-05:00",
        symbol="AAPL",
        source="massive",
        title="t",
        text="d",
        url="https://x",
        massive_sentiment="positive",
        massive_reasoning="bullish",
    )
    path = D.DATA_DIR / "news" / "_probe_merge.csv"
    try:
        n = _merge_news(path, [row])
        assert n == 1
        df = pd.read_csv(path)
        assert list(df.columns) == NEWS_COLS
        assert df.loc[0, "massive_sentiment"] == "positive"
    finally:
        path.unlink(missing_ok=True)


def test_intraday_features_causal_truncation():
    bars = C.intraday("MNQ", 1, synthetic=True, days=12, seed=4)
    full = intraday_features(bars)
    cut = bars.index[len(bars) // 2]
    part = intraday_features(bars[bars.index <= cut])
    a, b = full.loc[:cut], part.loc[:cut]
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_features_at_strict_before_returns_previous_bar():
    bars = C.intraday("MNQ", 1, synthetic=True, days=3, seed=4)
    fi = intraday_features(bars)
    when = bars.index[[100, 500, 900]]
    out = features_at(fi, when, strict_before=True)
    for w, bt in zip(when, out["bar_ts"]):
        assert pd.Timestamp(bt) < w
    out2 = features_at(fi, when, strict_before=False)
    for w, bt in zip(when, out2["bar_ts"]):
        assert pd.Timestamp(bt) == w


def test_prior_day_vol_is_strictly_prior():
    bars = C.intraday("MNQ", 1, synthetic=True, days=6, seed=4)
    daily = C.daily_from_intraday(bars)
    vix = pd.Series(np.arange(len(daily), dtype=float) + 10, index=daily.index)     # value == day number + 10
    volp = V.prior_day_vol(vix, bars.index)
    et = bars.index.tz_localize(None)
    for ts, v in zip(et[::700], volp[::700]):
        if np.isnan(v):
            continue
        d = vix.index[int(v) - 10]
        assert d < ts.normalize()


# ── labels / weights ─────────────────────────────────────────────────────────
def test_triple_barrier_hits():
    idx = pd.date_range("2024-01-01", periods=10, freq="B")
    bars = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}, index=idx)
    bars.loc[idx[3], "high"] = 103.0            # profit barrier at +2 hit on day 3 (entry day = idx[1])
    ev = pd.DataFrame({"side": [1]}, index=[idx[1]])
    r = triple_barrier(bars, ev, pt=0.02, sl=0.01, max_hold=5)
    assert r["label"].iloc[0] == 1 and r["exit_ts"].iloc[0] == idx[3]
    bars.loc[idx[2], "low"] = 98.0              # stop before the target
    r = triple_barrier(bars, ev, pt=0.02, sl=0.01, max_hold=5)
    assert r["label"].iloc[0] == -1 and r["exit_ts"].iloc[0] == idx[2]


def test_uniqueness_weights_overlap():
    idx = pd.date_range("2024-01-01", periods=20, freq="D")
    t0 = pd.Series([idx[0], idx[0], idx[10]])
    t1 = pd.Series([idx[5], idx[5], idx[12]])
    w = uniqueness_weights(t0, t1, idx)
    assert abs(w.iloc[0] - 0.5) < 1e-9 and abs(w.iloc[2] - 1.0) < 1e-9


# ── cross-validation ─────────────────────────────────────────────────────────
def test_purged_walk_forward_no_leak():
    n = 300
    t0 = pd.Series(pd.date_range("2024-01-01", periods=n, freq="6h"))
    t1 = t0 + pd.Timedelta(hours=30)             # labels overlap the next 5 events
    cv = PurgedWalkForward(n_splits=4, embargo="12h", min_train_frac=0.3)
    seen = 0
    for tr, te, (ts, te_end) in cv.split(t0, t1):
        assert (t1.iloc[tr] < ts - pd.Timedelta(hours=12)).all(), "training label ends inside the test window / embargo"
        assert (t0.iloc[tr] < ts).all()
        assert len(set(tr) & set(te)) == 0
        seen += len(te)
    assert seen == n - int(n * 0.3)


def test_purged_kfold_purges_both_sides():
    n = 200
    t0 = pd.Series(pd.date_range("2024-01-01", periods=n, freq="D"))
    t1 = t0 + pd.Timedelta(days=5)
    for tr, te, (ts, te_end) in PurgedKFold(n_splits=5, embargo="2D").split(t0, t1):
        assert ((t1.iloc[tr] < ts - pd.Timedelta(days=2)) | (t0.iloc[tr] > te_end + pd.Timedelta(days=2))).all()


def test_cscv_pbo_random_vs_dominant():
    rng = np.random.default_rng(0)
    R = rng.normal(0, 1, (640, 12))
    pbo_rand = cscv_pbo(R, n_blocks=8)["pbo"]
    assert 0.25 <= pbo_rand <= 0.8, pbo_rand
    R2 = R.copy(); R2[:, 3] += 0.4               # one trial with a real edge
    assert cscv_pbo(R2, n_blocks=8)["pbo"] < 0.15


# ── model: positive control ──────────────────────────────────────────────────
def test_gbdt_detects_planted_signal_out_of_fold():
    rng = np.random.default_rng(1)
    n = 1500
    X = pd.DataFrame(rng.normal(size=(n, 6)), columns=list("abcdef"))
    logit = 1.5 * X["a"] - 1.0 * X["b"] + 0.3 * rng.normal(size=n)
    y = pd.Series((rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int))
    t0 = pd.Series(pd.date_range("2020-01-01", periods=n, freq="h"))
    t1 = t0 + pd.Timedelta(hours=2)
    folds = list(PurgedWalkForward(n_splits=4, embargo="3h").split(t0, t1))
    res = fit_predict_folds(X, y, folds, n_estimators=150, num_leaves=7, min_child_samples=30)
    assert res["folds"]["auc"].mean() > 0.75
    top = res["importance"].mean().sort_values(ascending=False).index[:2].tolist()
    assert set(top) == {"a", "b"}
    assert res["importance_stability"] > 0.5


def test_gbdt_no_signal_gives_chance_auc():
    rng = np.random.default_rng(2)
    n = 1200
    X = pd.DataFrame(rng.normal(size=(n, 6)), columns=list("abcdef"))
    y = pd.Series(rng.integers(0, 2, n))
    t0 = pd.Series(pd.date_range("2020-01-01", periods=n, freq="h"))
    folds = list(PurgedWalkForward(n_splits=4, embargo="3h").split(t0, t0 + pd.Timedelta(hours=2)))
    res = fit_predict_folds(X, y, folds, n_estimators=100, num_leaves=7, min_child_samples=30)
    assert abs(res["folds"]["auc"].mean() - 0.5) < 0.08


# ── primaries ────────────────────────────────────────────────────────────────
def test_vwap_primary_timing_rules():
    frames = C.synthetic_universe([f"S{i}" for i in range(6)], days=15, seed=3)
    tr = W.run_primary(frames)
    assert len(tr) > 0
    et = tr["entry_time"].dt.tz_convert("America/New_York")
    xt = tr["exit_time"].dt.tz_convert("America/New_York")
    assert (tr["entry_time"] > tr["signal_ts"]).all(), "fill must be after the signal bar"
    assert ((et - tr["signal_ts"]).dt.total_seconds() == 600).all(), "fill at the very next 10-min bar"
    assert (et.dt.time < pd.Timestamp("15:00").time()).all(), "no entries at/after 15:00 (bar end)"
    assert (xt.dt.time <= pd.Timestamp("15:40").time()).all(), "flat by the 15:45 sweep"
    expect = (tr["exit"] - tr["entry"]) * tr["side"] * 0.65 * 100 * tr["units"] - 2 * 0.65 * tr["units"]
    assert np.allclose(tr["pnl"].to_numpy(), expect.to_numpy(), atol=1e-6)


def test_vwap_primary_truncation_invariance():
    frames = C.synthetic_universe([f"S{i}" for i in range(6)], days=20, seed=5)
    full = W.run_primary(frames)
    cut = list(frames.values())[0].index[len(list(frames.values())[0]) * 3 // 4]
    part = W.run_primary({s: f[f.index <= cut] for s, f in frames.items()})
    safe = cut - pd.Timedelta(days=2)
    a = full[full["exit_time"] < safe].reset_index(drop=True)
    b = part[part["exit_time"] < safe].reset_index(drop=True)
    assert len(a) == len(b) > 0
    pd.testing.assert_frame_equal(a[["symbol", "entry_time", "exit_time", "pnl"]], b[["symbol", "entry_time", "exit_time", "pnl"]])


# ── KPI export ───────────────────────────────────────────────────────────────
def test_kpi_export_and_inject(tmp_path):
    idx = pd.date_range("2024-01-01", periods=260, freq="B")
    rng = np.random.default_rng(3)
    daily = pd.Series(rng.normal(50, 400, len(idx)), index=idx)
    trades = pd.DataFrame({"entry_time": idx[:200], "exit_time": idx[1:201], "pnl": rng.normal(60, 300, 200), "units": 1, "cost": 3.0, "reason": "x"})
    k = compute_kpis(trades, daily, 100_000.0, name="t", n_trials=5, trial_sr_var=0.001)
    for key in ("sharpe", "maxdd", "ntrades", "coststress", "dsr", "boot", "payoff", "expect", "regimes"):
        assert k[key] is not None
    assert k["ntrades"] == 200 and k["triallog"] is True
    tpl = ('<html><head><title>x</title></head><body><div class="sub">old</div><section id="run-summary">OLD</section>'
           '<section id="kpi-calc-ref">REF</section><section id="weekly-trades">T</section><script>\n'
           'window.__WEEKLY_KPI_PREFILL__ = {"sharpe": 1, "_meta": {"a": [1,2]}};\nconst x = window.__WEEKLY_KPI_PREFILL__ || {};</script></body></html>')
    out = inject(tpl, k, "<div>BANNER</div>", "<section>SECT</section>")
    i = out.find("window.__WEEKLY_KPI_PREFILL__ =") + len("window.__WEEKLY_KPI_PREFILL__ =")
    parsed, _ = json.JSONDecoder().raw_decode(out, i + 1)
    assert parsed["ntrades"] == 200 and parsed["_name"] == "t"
    assert "OLD" not in out and 'id="weekly-trades">T' not in out and "BANNER" in out and "REF" in out
