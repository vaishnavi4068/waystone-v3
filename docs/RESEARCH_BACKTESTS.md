# Research backtests (Mac Studio → GCS → HQ)

Eight research sleeves live in `waystone_backtests/`. **Compute and keys stay on the Mac Studio.**
HQCapital only reads dated objects from GCS.

## GCP auth (Mac Studio only)

Identity:

- Project: `microdrive-dev`
- Bucket: `gs://waystone-data`
- Service account: `waystone-data@microdrive-dev.iam.gserviceaccount.com`

Copy the SA JSON onto the Mac (not into git, not into `waystone_backtests/`, not into the GKE image):

```sh
mkdir -p "$HOME/.config/gcloud"
# save the downloaded key as:
#   $HOME/.config/gcloud/waystone-data.json
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/waystone-data.json"
export GOOGLE_CLOUD_PROJECT=microdrive-dev
export IBKR_REPORTS_BUCKET=waystone-data
```

`GcsStore` uses Application Default Credentials (`storage.Client()`). Fetch and publish pick up that env.

**Never commit** `microdrive-dev-*.json`, `*iam.gserviceaccount.com*.json`, or any SA key. Root `.gitignore` already blocks those patterns. GKE dashboard reads stay Workload Identity / `objectViewer` later — not this JSON key. Local/GKE preview without ADC uses the staged fixture (`2026-08-14`).

## Data order (do not invert)

1. `gs://waystone-data/NSDQ250` (Polygon dumps already on GCS: daily OHLC + MNQ/NQ 1-min)
2. Massive flat-file S3 (`https://files.massive.com`, bucket `flatfiles`) — optional, `--flatfiles`
3. Massive REST API (`MASSIVE_API_KEY` or `POLYGON_API_KEY`), then Yahoo

```sh
# Mac Studio — local files, not committed
export MASSIVE_API_KEY=...                 # REST (also accepted as POLYGON_API_KEY)
export MASSIVE_S3_ACCESS_KEY_ID=...        # S3
export MASSIVE_S3_SECRET_ACCESS_KEY=...    # S3 (often the same as the REST key)
export MASSIVE_S3_ENDPOINT=https://files.massive.com
export MASSIVE_S3_BUCKET=flatfiles
```

## Window: 2–5 years from the data

Do not force five years. Fetch/run look at NSDQ250 (and then local CSVs), take the **overlap** across a sleeve's symbols, and clamp:

- **≥ 5 years on GCS** → run the most recent **5** years
- **4 years on GCS** → run **4** years
- **2–5 years** → run that span
- **< 2 years** → skip that sleeve (too short for a research backtest)

`--years` is the **maximum** (default 5). `--min-years` is the floor (default 2).

```sh
uv sync --extra research
uv run waystone3 research-fetch --years 5          # uses 4y if that is what NSDQ250 has
uv run waystone3 research-run --years 5            # same clamp
uv run waystone3 research-publish                  # dated GCS objects
```

`research-run` invokes each `strategies/*/backtest.py` from `catalog.json` (same jobs as `waystone_backtests/run_all.sh`).

## GCS layout (dashboard reads this)

```
gs://waystone-data/research/v1/catalog.json
gs://waystone-data/research/v1/<id>/latest.json
gs://waystone-data/research/v1/<id>/dt=YYYY-MM-DD/<variant>/
  metrics.json
  equity.csv
  trades.csv
  scorecard.json
  scorecard.html
  _manifest.json
  _SUCCESS
gs://waystone-data/research/v1/scorecards/dt=YYYY-MM-DD/index.html
gs://waystone-data/research/v1/scorecards/latest.html
gs://waystone-data/research/v1/sentiment/
  macro/              fng.csv, aaii.csv, pcr.csv, gdelt_<SYM>.csv
  news/               <SYM>.csv  (Massive polygon-news + LLM insights)
  events/             <SYM>.csv, <SYM>_8k.csv
  sentiment/          <SYM>_daily.csv  (Massive-scored shock_z for backtests)
  lists/              sp500.csv  (503 S&P 500 constituents)
```

Sync commands:

```sh
uv run waystone3 research-sentiment-sync --pull   # new box: merge GCS -> local data/
uv run waystone3 research-sentiment-sync --push   # after fetch/score: merge local -> GCS
./scripts/backfill-massive-sp500.sh               # pull, backfill 503 names, score, push
```

`dt=` is the last equity date (NY). `run_id` is recorded in `_manifest.json`. HQ **Strategies** lists sleeves by book and shows Sharpe / CAGR / DD plus the stage-gate verdict for that date. Each sleeve detail page renders the full KPI scorecard (same gates as the options dashboard HTML).

```sh
uv run waystone3 research-scorecard          # local HTML next to results/
uv run waystone3 research-publish            # dated GCS objects + scorecards
```

Read APIs (bearer): `GET /api/strategies`, `GET /api/strategies/{id}`, `GET /api/strategies/{id}/runs`, `GET /api/strategies/{id}/scorecard`, `GET /api/strategies/{id}/scorecard.html`.

Preview without a published run: staged fixture date `2026-08-14` (also used when `IBKR_STAGED=1` / no bucket).

## Tuning (research-tune)

```sh
uv run waystone3 research-tune --strategy 08_pead_implied_move --workers 4 --apply
```

Grid-tunes one sleeve from `catalog.json` (`scripts[].grid` + `constraints`) and writes
`results/<variant>/tuning.json` + `trials.csv`, which the scorecard reads for the Stage 2 KPIs:

- every grid point is logged (the trial log is cumulative across runs; the DSR uses the total count);
- selection is on the in-sample 60 % by *plateau* score (a point's Sharpe averaged with its grid
  neighbours), so a lone spike never wins; the OOS 40 % is untouched by selection;
- anchored walk-forward efficiency, CSCV probability of backtest overfitting, ±20 % parameter shifts and
  a 2× cost run (`--cost-mult 2`) are computed on the chosen point;
- `--apply` writes the chosen args back into the catalog and re-runs the sleeve.

`gate_profile: regime_filter` (07) marks trade-count / PF / payoff / expectancy N/A and reports the
filtered-vs-base Sharpe uplift instead: a switch is judged by what it does to the sleeves it gates.

### Equity sleeves — what tuning found (2021-09 → 2026-07, NSDQ250 universe)

| sleeve | before | after | what changed | still short of the gate |
|---|---|---|---|---|
| 01 mean reversion | single-name, ~20 trades | pullback portfolio, Sharpe 1.39, 1.7k trades, DD −13 % | RSI-2 dips above SMA200 across ~500 names, passive ATR-limit entries, earnings avoidance, 10 slots | Sharpe 1.5 |
| 06 sector rotation | Sharpe 0.67, DD −18 % | Sharpe 1.04, DD −9 %, 2× cost 1.02 | sector **+ industry** ETFs, ex-ante vol target 8–12 %, T-bill yield on cash; defensive/GLD fallback and inverse-vol tested, no uplift | Sharpe 1.5; 96 legs < 100 |
| 07 breadth regime | on/off switch, Sharpe 0.57 | best variant ≈ SMA200 base (0.65); PBO 83 % | hysteresis, sizing and thrust variants; gating 01 on breadth *lowers* return (a dip-buyer wants washed-out breadth) | no standalone timing value — diagnostic input only |
| 08 PEAD | Sharpe 0.73, PF 1.3 | Sharpe ~1.3, PF ≥ 2.1, DD −9 %, 400 trades | event study on 9.6k events: unconditioned drift = SPY beta, shorts after misses drift *up*; require EPS beat ≥ 10 %, skip top-of-range closes, **no stop** (stops forfeit the drift), hold 20 | Sharpe 1.5 |

Things that were tried and rejected because they only fit the sample: market-regime gating and beta
hedging on 01 and 08 (the dollar alpha is a few bp per trade, the hedge costs more than it saves),
weekly rebalancing on 06, risk-adjusted momentum scores, gap-fill and ATR stops on PEAD.

Re-tuned on the full 2021-09-04 → 2026-09-04 window (after `fetch_yf.py --append` brought every
NSDQ250 file up to date): 01 pullback Sharpe 1.42, CAGR 19 %, DD −12.6 %, 1,489 trades, 2× cost 1.31,
PBO 15.7 %, OOS Sharpe 2.13 > IS 0.85; 08 PEAD Sharpe 1.31, DD −11.3 %, 216 trades, but OOS 0.66 and
PBO 50 % — the weaker of the two.

## ML layer (`waystone_backtests/ml/`, see `ML_ALGO.md`)

The ML never generates a trade. It is an overlay on trades that already exist: a meta-model
(`ml/meta_label.py`) scores each fill of a primary sleeve and either skips it or sizes it up; a
prefix-decoded HMM (`ml/regime_hmm.py`) gates a trade list by market state. Overlays only make sense
on a sleeve that already passes its own grid/DSR check, so the order is: prove the primary, then ask
the ML whether it improves it.

Run order (all self-contained, nothing here touches a live bot):

```sh
uv sync --extra research                         # scikit-learn, lightgbm, hmmlearn, yfinance
cd waystone_backtests && python -m pytest tests -q && ./run_ml.sh --synthetic
python tools/fetch_yf.py --symbols ^VIX ^VXN ^GSPC SPY --start 2010-01-01
python ml/sentiment/fetch_free_sentiment.py fng --start 2021-01-01
python ml/regime_hmm.py --symbol ^GSPC --vol-symbol ^VIX --name spx   # -> data/regime/spx_states.csv
waystone3 research-run --strategy ml_meta_08_pead      # catalog rows with kind: overlay
waystone3 research-run --strategy ml_meta_01_pullback
waystone3 research-run --strategy ml_meta_v221         # needs data/intraday/MNQ_1min.csv
waystone3 research-run --strategy ml_regime_gate_08
waystone3 research-scorecard && waystone3 research-publish
```

Overlay rows in `catalog.json` carry `kind: overlay`, their own `script`, and `inputs` (the primary's
`trades.csv`); `research-run` skips them until the primary output exists. `ml/kpi_export.py` writes
`kpi.json` next to `metrics.json` with the Stage 1–2 KPIs computed from purged walk-forward
out-of-fold trades; the scorecard takes those as authoritative and publishes `kpi.json` alongside.
The card's decisive block is **base vs meta on the same OOF trades**: an overlay passes only if the
meta Sharpe is positive, the uplift is ≥ 0.1 and the model's AUC is above 0.52 — otherwise the
research gate is FAIL no matter how the absolute KPIs look (the primary already delivers those).
A passing overlay goes to paper for three months with `p_skip_live` / `p_boost_live` from
`metrics.json` before it gets size. `results/trial_log.csv` family names must stay stable.

### What the overlays found (first pass, 2026-09-06)

| overlay | primary (same OOF trades) | overlay | verdict |
|---|---|---|---|
| `ml_meta_01_pullback` | Sharpe 2.66, $80.4k, 1,043 trades | Sharpe 2.32, $71.5k, skipped 324 trades worth **+$20.4k** (61 % hit) | AUC 0.54 — does not earn its keep; run the primary ungated |
| `ml_meta_08_pead` | Sharpe 1.59, $38.1k | Sharpe 1.60, $76.5k, boosted 135 of 152 | AUC 0.497 — pure leverage, not skill |
| `ml_meta_v221` (MNQ 1-min re-sim) | Sharpe 0.38, $16.0k, 1,414 trades | Sharpe 0.85, $34.2k, DD −18 % → −11 %, skipped 559 (−$5.2k), boosted 262 (+$13.0k) | the only overlay with real uplift (+0.47, AUC 0.54) — but PBO 88 %, WFE 0.08, DSR 0.33: threshold choice is overfit and the sleeve stays below Sharpe 1.5. Paper at size 1 (skip leg only) is the most it has earned. Variant `skiponly` (`--boost-size 1`): 0.38 → 0.71, DD −18 % → −9 %, 508 skipped trades worth −$5.3k, PBO 93 % |
| `ml_regime_gate_08` (SPX/VIX HMM) | Sharpe 1.14, $26.0k, 119 trades | Sharpe 0.73, $8.7k, 61 trades | the state gate throws away good PEAD trades |

Bundle fix that changed a verdict: `ml/evaluate.daily_pnl` now rolls exits on non-session dates
(Globex Sunday-evening fills, holidays) to the next session instead of dropping them on reindex —
before that the MNQ card was missing +$37k of P&L on 77 Sundays and read as a losing sleeve.

Sentiment: `scripts/sentiment-daily.sh` collects CNN Fear & Greed, Massive/Polygon news (full
S&P 500 via `data/sp500.csv`), SEC 8-K (via `data.sec.gov/submissions`), and GDELT tone/volume,
scores Massive LLM insights into `data/sentiment/<SYM>_daily.csv`, then
`waystone3 research-sentiment-sync --pull --push` merges into
`gs://…/research/v1/sentiment/{macro,news,events,sentiment,lists}/`. Install daily on the Mac with
`scripts/com.waystone.sentiment.plist`; a new box runs `research-sentiment-sync --pull` first. One-time
historical backfill: `./scripts/backfill-massive-sp500.sh` (503 names from 2021, then push to GCS).

## Mac worker + Grok Bot

This Linux cloud VM cannot run the 5-year jobs. Start a worker on the Mac
([docs/MAC_STUDIO_WORKER.md](MAC_STUDIO_WORKER.md)) and wire Grok Bot
([docs/GROK_BOT.md](GROK_BOT.md)) so fetch/run/publish post status and can
pick up `approve-*` instructions from the GCS inbox.
