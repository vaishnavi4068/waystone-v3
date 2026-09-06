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

## Mac worker + Grok Bot

This Linux cloud VM cannot run the 5-year jobs. Start a worker on the Mac
([docs/MAC_STUDIO_WORKER.md](MAC_STUDIO_WORKER.md)) and wire Grok Bot
([docs/GROK_BOT.md](GROK_BOT.md)) so fetch/run/publish post status and can
pick up `approve-*` instructions from the GCS inbox.
