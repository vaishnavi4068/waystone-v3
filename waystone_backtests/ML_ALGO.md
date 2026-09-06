# ML & sentiment backtests — the algorithm, end to end

This document is the specification behind `ml/`.  Everything here is implemented; every number the code
prints maps to a row of the Stage-Gate KPI dashboard, and every configuration the code tries is written to
`results/trial_log.csv` so the Stage-2 gates (deflated Sharpe, PBO) are computed against the real search
history, not the winner.

The one-sentence version: **the ML never generates a trade.**  V221 and the VWAP pullback generate trades.
The models decide which of those trades to skip, which to size up, and which regime to stand aside in; a
direction model is allowed only as a small tilt.  Each of those three uses is a separate backtest with its own
gate card, and "earning its keep" has a precise meaning — on the *same out-of-fold trades*, the overlay must
beat the base book on Sharpe and drawdown **and** clear the critical gates (Sharpe ≥ 1.5, DSR ≥ 0.95, OOS/IS
≥ 0.6, cost-stress ≥ 1.0, param-sensitivity ≤ 30 %, ≥ 200 OOS trades).  A model that improves the base book but
fails DSR is noise that happened to help; it is not promoted.

---

## 0. Run order (what to type)

```bash
cd waystone_backtests
pip install -r requirements.txt          # lightgbm + hmmlearn are optional; sklearn fallbacks are automatic
python -m pytest tests -q                # 22 tests: look-ahead, purging, PBO, positive/negative controls

# mechanics on synthetic data (no network, ~2 min total)
./run_ml.sh --synthetic

# real data, in this order
python tools/fetch_polygon.py futures --root MNQ --start 2024-01-01 --resolution 1min      # data/intraday/MNQ_1min.csv
python tools/fetch_polygon.py indices --symbols VXN VIX SPX --start 2015-01-01             # data/daily/I_VXN.csv ...
python tools/fetch_yf.py --symbols SPY --start 2010-01-01                                  # or Polygon I:SPX
python ml/sentiment/fetch_free_sentiment.py fng                                            # data/macro/fng.csv
python ml/sentiment/fetch_free_sentiment.py aaii --from-file ~/Downloads/sentiment.xls     # data/macro/aaii.csv
python ml/sentiment/fetch_free_sentiment.py pcr  --from-csv  ~/Downloads/totalpc.csv       # data/macro/pcr.csv

python ml/regime_hmm.py --symbol I:SPX --vol-symbol I:VIX --name spx                       # data/regime/spx_states.csv
python ml/meta_label.py --primary v221 --symbol MNQ --vol-symbol I:VXN --regime spx --tune --dashboard "<kpi html>"
python ml/meta_label.py --primary vwap --n-symbols 45 --vol-symbol I:VIX --regime spx --tune --dashboard "<kpi html>"
python ml/direction_gbdt.py --symbol SPY --vol-symbol I:VIX --regime spx --tune --dashboard "<kpi html>"

# sentiment (free sources), per name, then the three sentiment backtests
python ml/sentiment/fetch_free_sentiment.py gdelt --symbols AAPL NVDA --company "Apple" "Nvidia" --start 2023-01-01
python ml/sentiment/fetch_free_sentiment.py yahoo-rss --symbols AAPL NVDA          # cron it daily; RSS is recent-only
python ml/sentiment/fetch_free_sentiment.py sec-8k --symbols AAPL --cik 320193 --start 2023-01-01
python ml/sentiment/finbert_score.py --symbols AAPL NVDA                            # FinBERT if installed, lexicon otherwise
python ml/sentiment/event_classifier.py --symbols AAPL NVDA
python ml/sentiment/sentiment_backtest.py --mode shock --symbols AAPL NVDA --grid --dashboard "<kpi html>"
python ml/sentiment/sentiment_backtest.py --mode event-filter --trades-csv results/ml_meta_vwap/primary_trades.csv
python ml/sentiment/sentiment_backtest.py --mode macro --symbol SPY
```

Each run writes `results/<name>/{metrics.json, trades.csv, equity.csv, kpi.json, folds.csv, importance.csv}` and,
with `--dashboard`, a `dashboard.html` — a copy of your Stage-Gate scorecard with `window.__WEEKLY_KPI_PREFILL__`
replaced, the banner rebuilt from this run's dollars, and the old weekly trade tables removed.  Open it in a
browser; the stage verdicts are computed by the dashboard's own JavaScript, untouched.

> Note on the uploaded dashboard: its main script contains the `let state = {}` bootstrap block five times (the
> weekly generator appended it on every regeneration).  A second `let state` is a JavaScript SyntaxError, so the
> file as uploaded renders **no KPI rows at all** — the inputs and verdicts are simply absent.  `ml/kpi_export.py`
> de-duplicates that block when it writes a copy; fix the generator so the next weekly file does not carry it.

---

## 1. Data

| Layer | Source | File | Used by |
|---|---|---|---|
| MNQ 1-min bars (Globex) | `tools/fetch_polygon.py futures` (quarterly contracts stitched by volume) or `tools/ib_fetch_bars.py` on the VM | `data/intraday/MNQ_1min.csv` | V221 primary, intraday features |
| Stock 1-min / 10-min bars | Polygon (you have options/indices/futures; stock bars need the stocks plan or IB on the VM) | `data/intraday/<SYM>_1min.csv` / `_10min.csv` | VWAP primary |
| Daily bars | Polygon indices (`I:SPX`, `I:VIX`, `I:VXN`), Yahoo (`SPY`, stocks) | `data/daily/*.csv` | daily features, regime, direction |
| GEX regime | `strategies/02_gex_dealer_gamma` output | `results/02_.../gex_daily.csv` | daily features (`gex_z`, `gex_sign`, `dist_flip`) |
| Fear & Greed | CNN graphdata endpoint (same as the futures bot) | `data/macro/fng.csv` | V221 gate, macro features, macro sentiment sleeve |
| AAII bull/bear, CBOE put/call | AAII xls, CBOE csv | `data/macro/aaii.csv`, `pcr.csv` | macro features, macro sleeve |
| News per name | GDELT DOC API (tone + article volume), Polygon news (if in plan), Yahoo RSS, SEC EDGAR 8-K items | `data/news/<SYM>.csv`, `data/macro/gdelt_<SYM>.csv`, `data/events/<SYM>_8k.csv` | sentiment scoring, events |
| Sentiment features | `ml/sentiment/finbert_score.py` | `data/sentiment/<SYM>_daily.csv` | daily features, shock sleeve |
| Events | `ml/sentiment/event_classifier.py` | `data/events/<SYM>.csv` | event-filter overlay |
| Regime states | `ml/regime_hmm.py` | `data/regime/<name>_states.csv` | daily features (`regime`), state gate |

Timestamp discipline: every intraday file is ET (tz-aware); daily files are dated by session; news items after
16:00 ET roll to the next session's date; AAII (published Thursday) is forward-filled from its publication date.

---

## 2. Feature store (`ml/features.py`)

**Daily** (one row per session, measured at that session's close):
returns 1/5/20 d, realised vol 5/20 d and their ratio, ATR14 %, day range %, Kaufman efficiency ratio (20 d),
distance to SMA50/200 and SMA50 slope, RSI14, volume z-score, gap %, close position in range, day-of-week,
position within the month; from the vol index: level, 5-day change, 1-year percentile, implied/realised
ratio, VIX/VIX3M when available; from the breadth panel: % above 50/200-day, McClellan oscillator, 10-day
breadth thrust; from GEX: z-score, sign, distance to the flip strike; macro: FnG, put/call, AAII spread and
their 5-day changes; sentiment: daily score, count, shock z, 5-day mean; regime state.

**Intraday** (one row per bar, measured at that bar's close): 1/5/15-bar returns, 30-bar realised vol,
ATR14 and ATR %, session VWAP and distance from it in ATRs, day range position and day range in ATRs, session
return, bars since open, minutes since open, in-RTH flag, relative volume vs the same minute-of-day over the
last 20 sessions, 60-bar volume z-score.

**Alignment rules (tested in `tests/test_ml.py`):**
`align_prior_close()` maps a signal timestamp to the *last daily row strictly before that date*, so a 10:15
signal never sees its own day's close.  `features_at(..., strict_before=True)` maps a fill timestamp to the
*previous* bar — the signal bar — because every primary fills at the open of the bar after the signal.  The
truncation test confirms the intraday store is causal (recomputing on a prefix reproduces the same rows).

---

## 3. Primary signals (`ml/engines/`)

* **V221** — `v221_engine.py` is a byte-for-byte copy of the live engine (`FUTURE_IBKR/v221_engine.py`),
  run with `backtest_fill=True` (fill at next-bar open ± 0.125 pt, exits at close ∓ 0.125).  VXN is fed as
  the **prior-day close**, exactly as the live bot does (the reference backtest's same-day VXN look-ahead is
  deliberately not reproduced).  FnG is the last value strictly before the bar's date.  P&L = pts × $2 ×
  contracts − $1.24 × contracts (MNQ); `--contracts 2` matches `env.sh`.
* **VWAP pullback** — `vwap_pullback.py` ports `ibkr_month/app.py` rule for rule: universe scoring
  (ATR %14 and RVOL20, min-max scaled across the universe at every bar, score > 5, skip band [8, 9)), the
  `check_vwap_pullback` condition on completed 10-min bars, no entries once the bar-end clock passes 15:00,
  the 15:45 sweep, and the −30 % premium stop.  The option leg is a **delta proxy** (P&L = 0.65 × 100 × qty ×
  underlying move; stop = 30 % × premium / delta ≈ 1.0 % of spot).  The trade list carries the underlying
  timestamps and prices so each trade can be re-priced from Polygon option bars later
  (`tools/fetch_polygon.py option-bars`); until then the proxy is labelled as such in every report.
* **Any trade list** — `--primary trades --trades-csv` accepts the `trades.csv` of any strategy in
  `strategies/` (columns entry/exit time or date, pnl, optional symbol/side/units/cost).

---

## 4. Meta-labeling (`ml/meta_label.py`)

**Question:** given that the primary fired, will this trade make money?

1. **Events.** One row per primary trade: `t0` = fill time, `t1` = exit time.
2. **Label.** `y = 1[pnl > min_pnl]` (net of costs; `--min-pnl` lets you demand a cushion).
3. **Features.** Intraday store at the signal bar (`i_*`), the name's daily store at the prior close (`d_*`),
   the market's daily store at the prior close (`m_*` — SPY/SPX + VIX/VXN + breadth + GEX + macro + regime),
   plus side, hour, day-of-week and the universe score.  Columns with < 20 % coverage are dropped.
4. **Sample weights.** Average uniqueness over the label's lifetime (López de Prado ch. 4): five VWAP trades
   open at the same time share one weight, not five.
5. **Cross-validation.** `PurgedWalkForward(n_splits=5, embargo=2D, min_train_frac=0.3)`: test blocks are
   contiguous and later than every training event; training events whose `t1` falls inside the test window or
   the embargo are purged.  Nothing after the test window is ever in the training set.
6. **Model.** LightGBM (`n_estimators 300, lr 0.03, 15 leaves, min_child = n/15 clipped to [10, 50], subsample
   0.8, colsample 0.8, λ₂ = 5`) or the sklearn HistGradientBoosting equivalent.  Per fold: AUC, Brier, log-loss,
   base rates, gain importance.  **Importance stability** = mean pairwise Spearman ρ of the importance vectors
   across folds; below ~0.5 the model is fitting a different story in every fold and should not be trusted
   whatever the AUC says.
7. **Sizing.** Thresholds are multiples of the fold's **breakeven probability**
   `p_be = |avg loss| / (avg win + |avg loss|)` measured on the training trades, so the rule is portable between
   a 30 %-hit trend system and a 60 %-hit mean-reversion system:
   `p < m_skip·p_be → skip;  p ≥ m_boost·p_be → 2×;  else 1×`  (defaults 1.0 / 1.4).
8. **Report.** Base book vs meta book on the **same OOF trades**: net P&L, Sharpe, max DD, trades kept /
   skipped / boosted, the P&L of the skipped trades (the overlay's whole contribution is here — if the skipped
   trades were profitable the model is destroying value), hit rate kept vs skipped.
9. **`--tune`.** Nested walk-forward threshold choice: for fold *k* the `(m_skip, m_boost)` pair is picked on
   the OOF trades of folds < *k* (fold 0 uses the defaults).  Every grid point on the full OOF period is also
   evaluated and logged as a trial (19 trials), the CSCV **PBO** is computed over that grid's daily-return
   matrix, and the "full-OOF grid" table is printed *as a search log* — the reported number is the nested one.
10. **Parameter sensitivity.** ±20 % on `m_skip`, `m_boost`, `n_estimators`, `num_leaves` (the last two are
    full walk-forward refits); the KPI is the worst Sharpe drop in %.
11. **OOS/IS and WFE.** In-sample = each fold's model applied to its own training trades (what the model
    "thinks" it can do); WFE = mean per-fold OOS daily P&L / mean per-fold IS daily P&L.

Outputs: `trades.csv` (OOF trades with `p`, `p_be`, `size`, `pnl_base`, fold), `primary_trades.csv` (every
primary trade with `p` and `y`), `features.csv`, `folds.csv`, `importance.csv`, `threshold_grid.csv`, `kpi.json`.

---

## 5. Regime detection (`ml/regime_hmm.py`)

Gaussian HMM (3 states, diagonal covariance) on standardised daily features (`ret_20, rv_20, rv_ratio, er_20,
vix_chg_5, vix_vs_rv, pct_above_50`); k-means fallback.  Three things make it honest:

* features are standardised with **expanding** mean/std (no full-sample scaling);
* the state on day *t* is `predict(X[:t+1])[-1]` — Viterbi on the prefix — never the smoothed full-sample
  path; the model is refit every 63 sessions on data up to *t* and its states are relabelled after each refit
  by return/vol ranking so state 0 always means "calm-up" and state 2 "stress";
* a causal debounce (`--min-dwell 3`) adopts a new state only after three consecutive proposals — it delays
  changes by two days and removes the one-day flicker that makes prefix-decoded HMMs unusable.

Uses: (a) `data/regime/<name>_states.csv` is a feature for the meta model (`--regime <name>`) and the
direction model; (b) `--trades-csv` reports a primary's expectancy by state and runs the **walk-forward state
gate** — trade only in states whose trailing-252-session expectancy was positive with ≥ 15 trades — reported
after the first year; (c) `--sleeves` allocates across sleeve equity curves by state vs equal weight.

---

## 6. Direction GBDT (`ml/direction_gbdt.py`)

Label: next-open → close-*H*-days-later log return beyond a 5 bp hurdle.  Same feature store, same purged
walk-forward, embargo = 1.5 × horizon.  Position at the close = `clip((p − 0.5)/margin, −1, 1) × max_tilt`
(dead band ±margin), filled at the next open through `wsbt.engine.simulate_positions` with IBKR costs.
Reported: per-fold AUC, information coefficient (Spearman of *p* against realised return), tilt Sharpe/DD
versus buy-and-hold, and the KPI set.  `--tune` grids horizon × margin (12 trials, logged), computes PBO,
chooses on the first half of the OOF window and reports the second half.  The synthetic run shows what
"nothing there" looks like: AUC 0.46, IC −0.04, DSR 0.21 — this is the result to expect from most real
attempts, and the gates are what stop it from being traded.

---

## 7. Sentiment (`ml/sentiment/`)

**Scoring.** FinBERT (`ProsusAI/finbert`) when `transformers`+`torch` are installed (CPU is fine, ~20 items/s);
otherwise an embedded Loughran-McDonald-style lexicon with negation and intensifier handling.  Both give a
score in [−1, 1] per item.  Daily per name: mean score, item count, positive/negative share, **shock_z**
(today's score vs the previous 20 news days' mean/std) and **count_z** (attention shock).

**Events.** Rule-based classifier with priority order (guidance cut/raise, down/upgrade, regulatory,
litigation, M&A, management change, product/recall, capital actions, earnings, macro), polarity and a
confidence from cue-word count; SEC 8-K item codes (2.02 results, 5.02 officer change, 4.02 non-reliance …)
are a second, structured event feed.  `EVENT_LLM_CMD` lets a model override the rules line by line without
touching the backtest.

**Three backtests, same KPI card:**
* `shock` — long at the next open when `shock_z ≥ z` and RVOL ≥ 1.5 (the tape confirmed the tone), stop
  1.5 ATR, exit after `hold` sessions; `--grid` sweeps z × hold × rvol (18 trials, logged, PBO), chooses on
  the first half, reports the second.  `--synthetic --plant 0.6` is the positive control: a planted tone →
  return relation is recovered (Sharpe ≈ 2.7, PBO 0); without `--plant` the gates fail, as they should.
* `event-filter` — stand aside for *quiet* sessions after a negative binary event; reports base vs filtered
  on otherwise identical trades and the P&L of the blocked trades (the overlay earns its keep only if that is
  negative).
* `macro` — long the index when FnG / AAII / put-call are in their **trailing** 252-day fear tails (15th
  percentile, computed on prior data only), flat (or half-short) in the greed tails; compared with buy-and-hold.

---

## 8. KPI mapping (`ml/kpi_export.py`)

| Dashboard id | Computed as |
|---|---|
| sharpe / sortino | daily P&L / NAV over the OOS window, flat days = 0, × √252; Sortino over downside deviation |
| maxdd / calmar | |min(cum P&L − peak)| / NAV (constant NAV, no compounding — same convention as your weekly banner); annualised return % / maxdd % |
| pf / payoff / expect / ntrades | on closed OOS trades |
| worstmo / cvar / skew | worst calendar month % NAV; mean of the worst 5 % daily returns; skew of monthly returns |
| coststress | Sharpe after **doubling** every trade's recorded cost (or −$15/unit RT when no cost column) |
| oosis / wfe | OOS Sharpe / in-fold-training Sharpe; mean per-fold OOS daily P&L / mean per-fold IS daily P&L |
| dsr | Bailey–López de Prado DSR with `n_trials` = rows in `results/trial_log.csv` for this family and the variance of their per-period Sharpes |
| pbo | CSCV over the tuning grid's daily-return matrix (8 or 16 blocks, all C(n, n/2) combinations up to 3 000) |
| paramsens | worst Sharpe drop, %, over ±20 % shifts of every free parameter |
| boot | 5th percentile of 500 five-day block bootstraps of the OOS daily P&L |
| regimes | distinct calendar years with trades (the dashboard's definition) |
| margin / stress | peak concurrent units × margin per unit / NAV (MNQ default $2 500); worst rolling 5-day loss % NAV |
| corr | |ρ| of daily P&L with `--book equity.csv` |
| triallog | true — `results/trial_log.csv` is appended by every run |
| attrib / gammatheta / netvega / netdelta | 100 % for delta-only sleeves (MNQ, stock proxy); null for the VWAP option proxy until it is re-priced from option bars |
| incmonths / inctrades / sliprat / livebt / opserr | null until paper/live logs exist (`v221_logs/v221_futures.db`, the options bot's SQLite) |
| killswitch / ddstop / runbook | `--flags killswitch,ddstop,runbook` |

---

## 9. The tuning protocol (how to not fool yourself)

1. Decide the family name before you start (`--name`); every run of that family appends to the trial log.
   DSR is computed against the count for that family — renaming a family to reset the count is the one
   thing this framework cannot stop you from doing.
2. Tune only with `--tune` (nested) or by reading the printed grid *as a search log*.  Never pick a grid
   point by its full-OOF number and report it.
3. A change to the feature set, the label threshold, the primary's parameters or the universe is a new
   trial — run it under the same family so it counts.
4. Promotion rule for an overlay (meta / regime gate / event filter): on the same OOF trades it must (a)
   improve Sharpe **and** max DD over the base book, (b) skip trades whose summed P&L is negative, (c) pass the
   critical gates on its own card, (d) have importance stability ≥ 0.5 and per-fold AUC that is never below
   0.5 by more than noise.  Then it goes to paper for `incmonths ≥ 3` with the live sizing rule
   (`p_skip_live`, `p_boost_live` in `metrics.json → extra.thresholds`) before any size.
5. Direction models are a tilt capped at `--max-tilt` and are never promoted without DSR ≥ 0.95 **and** IC > 0
   in every fold.

---

## 10. Deploying a promoted overlay (sketch)

The live bots already log every signal with its context.  A meta overlay is a ~40-line addition: build the
same feature row at signal time (`intraday_features` on the bot's bar buffer, `align_prior_close` on a daily
store refreshed at 16:30), load the last fold's model (`ml/models.fit_final` on all events, pickled), compute
`p`, and pass `size ∈ {0, 1, 2}` to the order layer.  Log `p`, `p_be` and the size with every signal so the
incubation KPIs (`livebt`, `sliprat`) can be filled from the bot's SQLite after three months.  Regime states
come from a nightly `regime_hmm.py` run writing `data/regime/<name>_states.csv`; the bot reads today's row.
