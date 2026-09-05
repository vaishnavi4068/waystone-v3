# Research backtests (Mac Studio → GCS → HQ)

Eight research sleeves live in `waystone_backtests/`. **Compute and keys stay on the Mac Studio.**
HQCapital only reads dated objects from GCS.

## Data order (do not invert)

1. `gs://waystone-data/NSDQ250` (Polygon dumps already on GCS: daily OHLC + MNQ/NQ 1-min)
2. Massive flat-file S3 (`https://files.massive.com`, bucket `flatfiles`) — optional, `--flatfiles`
3. Massive REST API, then Yahoo

GCP and Massive credentials are **Mac Studio only**. Never bake them into GKE, the dashboard image, or git.

```sh
# Mac Studio — local files, not committed
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/waystone-data.json"
export GOOGLE_CLOUD_PROJECT=microdrive-dev
export IBKR_REPORTS_BUCKET=waystone-data
export MASSIVE_API_KEY=...                 # REST
export MASSIVE_S3_ACCESS_KEY_ID=...        # S3
export MASSIVE_S3_SECRET_ACCESS_KEY=...    # S3 (often the same as the REST key)
export MASSIVE_S3_ENDPOINT=https://files.massive.com
export MASSIVE_S3_BUCKET=flatfiles
```

## Five-year window

Fetch and run default to **5 years** (or whatever NSDQ250 already holds — many names are ~2021–2026).

```sh
uv sync
uv run waystone3 research-fetch --years 5          # NSDQ250 first
uv run waystone3 research-run --years 5            # local CPU
uv run waystone3 research-publish                  # dated GCS objects
```

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

`dt=` is the last equity date (NY). HQ **Strategies** lists sleeves by book and shows Sharpe / CAGR / DD for that date.

Preview without a published run: `IBKR_STAGED=1` serves a dated fixture (`2026-08-14`).

## Mac worker + Grok Bot

This Linux cloud VM cannot run the 5-year jobs. Start a worker on the Mac
([docs/MAC_STUDIO_WORKER.md](MAC_STUDIO_WORKER.md)) and wire Grok Bot
([docs/GROK_BOT.md](GROK_BOT.md)) so fetch/run/publish post status and can
pick up `approve-*` instructions from the GCS inbox.
