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
  _manifest.json
  _SUCCESS
```

`dt=` is the last equity date (NY). `run_id` is recorded in `_manifest.json`. HQ **Strategies** lists sleeves by book and shows Sharpe / CAGR / DD for that date.

Read APIs (bearer): `GET /api/strategies`, `GET /api/strategies/{id}`, `GET /api/strategies/{id}/runs`.

Preview without a published run: staged fixture date `2026-08-14` (also used when `IBKR_STAGED=1` / no bucket).

## Mac worker + Grok Bot

This Linux cloud VM cannot run the 5-year jobs. Start a worker on the Mac
([docs/MAC_STUDIO_WORKER.md](MAC_STUDIO_WORKER.md)) and wire Grok Bot
([docs/GROK_BOT.md](GROK_BOT.md)) so fetch/run/publish post status and can
pick up `approve-*` instructions from the GCS inbox.
