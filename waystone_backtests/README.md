# Waystone backtests — signals from other return drivers

Eight rules-based strategies, each drawing on a return driver **other than** the continuation/momentum
logic behind V221 and the VWAP pullback, each with its own `README.md` (book, data needed, rule sketch,
parameters) and a runnable `backtest.py`. One small shared toolkit (`wsbt/`) does the simulation, costs,
metrics and the deflated-Sharpe accounting so every strategy is measured the same way.

| # | Folder | Return driver | Book | Data | Runs without data? |
|---|---|---|---|---|---|
| 01 | `strategies/01_mean_reversion` | Short-horizon mean reversion / range expansion (Bollinger fade, NR7) | ETFs, large caps | Daily OHLC (Yahoo) | ✔ synthetic |
| 02 | `strategies/02_gex_dealer_gamma` | Dealer gamma positioning → intraday regime | MES/ES, SPX options for the signal | **Polygon**: nightly chain snapshot (OI, IV, greeks) + flat-file history (volume-gamma proxy) | ✔ synthetic |
| 03 | `strategies/03_vol_carry_put_spreads` | Variance risk premium, gated by VIX term structure | XSP/SPX | **Polygon** I:SPX I:VIX I:VIX3M + real daily option prices for the legs (`--pricing polygon`); model fallback | ✔ synthetic |
| 04 | `strategies/04_calendar_effects` | Turn-of-month, FOMC drift, opex week | MES/ES | **Polygon** MES daily (front by volume) or SPY (Yahoo) + `data/fomc_dates.csv` (verify) | ✔ synthetic |
| 05 | `strategies/05_orderflow_cvd_mnq` | Absorption: CVD divergence at VWAP bands | MNQ 1-min | **Polygon** futures 1-min bars (`fetch_polygon.py futures`); trades endpoint for true delta; IB script as fallback | ✔ synthetic |
| 06 | `strategies/06_sector_rotation` | Cross-sectional momentum | 11 sector ETFs | Daily OHLC (Yahoo) | ✔ synthetic |
| 07 | `strategies/07_breadth_regime` | Participation → regime switch for the other sleeves | Filter (any book) | 100–500 constituent closes + SPY (Yahoo) | ✔ synthetic |
| 08 | `strategies/08_pead_implied_move` | Under-reaction to earnings, sized by the implied move | S&P 500 stocks | daily OHLC + earnings dates (Yahoo) + **Polygon** ATM-straddle implied move (`fetch_polygon.py implied-move`) | ✔ synthetic |

## Quick start
```bash
pip install -r requirements.txt
./run_all.sh --synthetic                 # every strategy on generated data: proves the code runs (numbers are meaningless)
python -m pytest tests -q                # 22 look-ahead / purging / control tests

# stocks / ETFs (Yahoo, free — your Polygon plan is options+indices+futures)
python tools/fetch_yf.py --symbols SPY QQQ IWM --start 2005-01-01
python tools/fetch_yf.py --sectors --start 2005-01-01
cp /path/to/options-bot/sp500.csv data/ && python tools/fetch_yf.py --sp500 --max-symbols 150 --start 2010-01-01
python tools/fetch_yf.py --earnings --sp500 --max-symbols 150

# Polygon / Massive (export POLYGON_API_KEY=...)
python tools/fetch_polygon.py indices --symbols SPX VIX VIX3M NDX --start 2010-01-01     # -> data/daily/I_SPX.csv ...
python tools/fetch_polygon.py futures --root MES --start 2010-01-01 --resolution 1session  # -> data/daily/MES.csv
python tools/fetch_polygon.py futures --root MNQ --start 2025-01-01 --resolution 1min      # -> data/intraday/MNQ_1min.csv
python tools/fetch_polygon.py chain-snapshot --underlying SPX                              # nightly cron 16:30 ET -> data/chains/SPX_chain.csv (OI+IV+greeks)
python tools/fetch_polygon.py implied-move --symbols AAPL MSFT NVDA                        # fills implied_move_pct in data/earnings/
python tools/polygon_flatfile_gex.py --flatfile-dir data/flatfiles --start 2025-01-01      # GEX history from S3 flat files (volume-gamma proxy)

./run_all.sh                             # runs everything that has data
python strategies/04_calendar_effects/backtest.py --symbol MES --futures --mode tom --grid
python strategies/03_vol_carry_put_spreads/backtest.py --spx I:SPX --vix I:VIX --vix3m I:VIX3M --product XSP --pricing polygon
```

## Conventions every backtest follows
* A signal computed from the bar that closes on day *t* is filled no earlier than the **open of day t+1**.
  `tests/test_no_lookahead.py` truncates the history and checks that earlier trades don't change.
* Costs are always on: IBKR-style commissions plus slippage (`wsbt/costs.py` has the presets — edit them to your schedule).
* Same-day stop **and** target → the stop wins.
* Returns are on a **constant capital base** (no compounding) so sleeves are additive and comparable; the
  equity curve is compounded for drawdown.
* `--grid` runs a small parameter sweep with a 60/40 in-sample/out-of-sample split and prints the **deflated
  Sharpe ratio** (Bailey & López de Prado) for the number of trials — the same gate as the KPI workbook. A
  `dsr_probability` below 0.95 means the in-sample winner is not distinguishable from the best of N random tries.
* Every run writes `results/<strategy>/metrics.json`, `trades.csv`, `equity.csv`.

## Reading a result
1. `--synthetic` first: it must run and produce trades. Its Sharpe is noise by design (the generators are
   random walks, with two exceptions noted in the READMEs).
2. Real data, default parameters, full sample: is the sign right, is the trade count enough to mean anything
   (rule of thumb: 100+ trades, 3+ years)?
3. `--grid`: does the out-of-sample column hold up, and is `dsr_probability` ≥ 0.95?
4. Only then read the README's "known weaknesses" and decide whether the data upgrade (chain history, tick
   delta, implied moves, point-in-time constituents) is worth paying for.

## ML & sentiment layer (`ml/`) — see `ML_ALGO.md`
The models never generate a trade; they decide which of V221's / the VWAP scan's trades to skip or size up,
which regime to stand aside in, and (as a capped tilt only) direction.  Every run is scored on the Stage-Gate
KPI dashboard's own ids and every configuration tried is logged for the deflated Sharpe and PBO.

| Script | What it tests | Output |
|---|---|---|
| `ml/meta_label.py --primary v221|vwap|trades` | GBDT meta-labeling on purged walk-forward folds; base vs meta on the same OOF trades; nested threshold tuning, PBO, ±20 % sensitivity | `results/ml_meta_*/` + `dashboard.html` |
| `ml/regime_hmm.py` | prefix-decoded HMM / k-means regimes, per-state expectancy, walk-forward state gate, sleeve allocation | `data/regime/<name>_states.csv`, `results/ml_regime_*/` |
| `ml/direction_gbdt.py` | next-N-day direction as a dead-banded tilt vs buy-and-hold; AUC, IC, DSR | `results/ml_direction_*/` |
| `ml/sentiment/fetch_free_sentiment.py` | CNN Fear & Greed, CBOE put/call, AAII, GDELT tone + volume, Polygon news, Yahoo RSS, SEC 8-K items | `data/macro/`, `data/news/`, `data/events/` |
| `ml/sentiment/finbert_score.py` · `event_classifier.py` | FinBERT (or lexicon) daily score / shock z; rule-based event types with an LLM hook | `data/sentiment/`, `data/events/` |
| `ml/sentiment/sentiment_backtest.py --mode shock|event-filter|macro` | tone-shock sleeve (with a planted-signal positive control), event stand-aside overlay, contrarian macro tilt | `results/ml_sent_*/` |
| `ml/kpi_export.py` | KPI ids from any results dir → `kpi.json`, and a prefilled copy of the dashboard | `results/<name>/dashboard.html` |

```bash
./run_ml.sh --synthetic                                   # mechanics + positive/negative controls, ~1 min
export KPI_DASHBOARD="/path/to/Options Strategy KPI Dashboard — StageGate Scorecard.html"
./run_ml.sh                                               # real data (see ML_ALGO.md §0 for the fetch order)
```

## Where each one plugs into the live stack
02 · 04 · 05 are MES/MNQ strategies: they become `on_bar` engines in the v3 futures loop (`v221_engine.py`
shape), inheriting brackets, stop verification, reconciliation and telemetry. 03 uses the options bot's
chain/quote/order layer on XSP. 01 · 06 · 08 are share trades. 07 is a JSON regime file both bots read at
startup, exactly like the Fear & Greed cache.

## Layout
```
wsbt/              toolkit: data.py (CSV contracts + loaders + synthetic), engine.py (3 simulators + grid),
                   metrics.py (stats + deflated Sharpe), costs.py, report.py, calendar_utils.py
tools/             fetch_yf.py · fetch_polygon.py · polygon_flatfile_gex.py · ib_fetch_bars.py · ib_chain_snapshot.py
strategies/NN_*/   README.md + backtest.py
ml/                features.py · labels.py · cv.py · models.py · evaluate.py · kpi_export.py · meta_label.py ·
                   regime_hmm.py · direction_gbdt.py · engines/ (v221 copy, vwap port) · sentiment/
tests/             test_no_lookahead.py · test_ml.py
data/              your CSVs (contracts documented in wsbt/data.py); fomc_dates.csv included
results/           written by the backtests
```
Python 3.10+, pandas ≥ 2.2 (tested on 3.0), numpy, scipy, scikit-learn; lightgbm and hmmlearn optional (sklearn fallbacks); yfinance only for `tools/fetch_yf.py`; requests for `tools/fetch_polygon.py` and the sentiment fetchers; torch+transformers only if you want FinBERT; ib_async only for the IB tools.

*Research tooling, not investment advice. Every strategy here is public; the work is in regime selection,
sizing and execution, and in not fooling yourself — which is what the deflated Sharpe and the truncation
tests are for.*
