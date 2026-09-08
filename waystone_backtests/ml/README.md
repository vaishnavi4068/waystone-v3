# ml/ — see ../ML_ALGO.md for the full specification

```
features.py      daily + intraday feature stores, strict prior-close / signal-bar alignment
labels.py        triple barrier, meta labels, forward-return labels, uniqueness weights
cv.py            PurgedWalkForward, PurgedKFold, CSCV probability of backtest overfitting
models.py        LightGBM / HistGradientBoosting wrapper, per-fold metrics, importance stability
evaluate.py      trade list -> daily P&L, base-vs-meta comparison, deflated Sharpe, trial log
kpi_export.py    Stage-Gate KPI ids + dashboard injection (also repairs the duplicated `let state` block)
meta_label.py    the meta-labeling backtest (primary = v221 | vwap | any trades.csv)
regime_hmm.py    prefix-decoded HMM regimes, state gate, sleeve allocation
direction_gbdt.py direction tilt
engines/         v221_engine.py (verbatim copy of the live engine), v221_primary.py, vwap_pullback.py (port of app.py)
sentiment/       fetch_free_sentiment.py, finbert_score.py, event_classifier.py, sentiment_backtest.py
```
