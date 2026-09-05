# IBKR daily dump + KPI dashboard

The dump CLI lives in this repo. You run it on the Google VM that already talks to
TWS/Gateway (same host/port as the futures and options algos, **unused** `clientId`).
The dashboard never opens IBKR; it only reads the GCS (or local) prefix after `_SUCCESS`
exists.

## Local KPI UI (no TWS)

From the repo root:

```sh
uv sync
uv run waystone3 ibkr-seed-demo --out ./reports/demo

export WAYSTONE_DB=./arena.db
export WAYSTONE_ADMIN_TOKEN=$(openssl rand -hex 16)
export IBKR_REPORTS_LOCAL_DIR=$PWD/reports/demo
uv run waystone3 arena-seed --players "Manoj"    # optional; API also creates the five users
uv run waystone3 api-serve                       # http://localhost:9200

# other terminal
cd frontend
cp .env.example .env.local     # NEXT_PUBLIC_API_BASE=http://localhost:9200
npm install
npm run dev                    # http://localhost:3001 — UI proxies /api to :9200
```

`ibkr-seed-demo` writes **staged** sample data for **10–14 Aug 2026** (banner on Daily,
Compare, Options KPIs, Futures KPIs). Source labels stay COMPUTED / DERIVED / MANUAL.
Set `IBKR_STAGED=0` to turn the staged overlay off.

Sign in with a team username and password. Open **Daily** (`/ibkr`) for NLV, cash, day P&L, commissions,
and futures vs options cards. Open **Compare** (`/compare`) for per-algo live paper vs
same-day replay. Open **Options KPIs** (`/options-kpis`) for the Strategy 5
weekly paper scorecard. Open **Futures KPIs** (`/futures-kpis`) for the NQ v5.1 tiers
(trade count, Sharpe, drawdown, cost drag, margin-to-equity).
After the VM has written a real dump, point the API at GCS
instead of the demo tree:

```sh
gcloud auth application-default login    # once, on your laptop
export IBKR_REPORTS_BUCKET=your-ibkr-daily-reports
unset IBKR_REPORTS_LOCAL_DIR
uv run waystone3 api-serve
```

`IBKR_REPORTS_LOCAL_DIR` wins over the bucket if both are set.

## VM dump (you run this; it populates GCS)

On `waystone`, with TWS/Gateway still connected (same `IB_HOST` / `IB_PORT` the algos use):

```sh
cd /root/waystone-v3          # or wherever this repo is cloned
uv sync --extra ibkr

export IB_HOST=127.0.0.1      # match the algos
export IB_PORT=4001           # or 7497 if you use TWS
export IB_CLIENT_ID=99        # must not collide with futures/options clientIds
export IBKR_REPORTS_BUCKET=your-ibkr-daily-reports
export IBKR_LEDGER_DIR=/root/IBKR_MONTH/reports/ledger
# optional: IBKR_CLIENT_BOOKS=1=futures,2=options

uv run waystone3 ibkr-collect   # optional, during the session
uv run waystone3 ibkr-export    # around 6pm ET; writes _SUCCESS last
```

Empty days still publish positions + account + `_SUCCESS`.

GCE VM: grant the instance service account `roles/storage.objectAdmin` on the bucket.
Dashboard (GKE or laptop): `roles/storage.objectViewer`.

## Cron on the VM (optional)

The scripts assume this repo is at `/root/waystone-v3`. Adjust paths. Put env in
`/root/IBKR_MONTH/reports/ibkr.env`:

```sh
IB_HOST=127.0.0.1
IB_PORT=4001
IB_CLIENT_ID=99
IBKR_REPORTS_BUCKET=your-ibkr-daily-reports
IBKR_LEDGER_DIR=/root/IBKR_MONTH/reports/ledger
```

Make the wrappers executable:

```sh
chmod +x /root/waystone-v3/scripts/ibkr-vm-collect.sh /root/waystone-v3/scripts/ibkr-vm-export.sh
```

`crontab -e` (runs in Eastern even if the VM is UTC):

```
CRON_TZ=America/New_York
SHELL=/bin/bash

# Merge TWS fills into the local ledger while the session is up
*/15 8-17 * * 1-5 . /root/IBKR_MONTH/reports/ibkr.env && /root/waystone-v3/scripts/ibkr-vm-collect.sh >> /var/log/ibkr-collect.log 2>&1

# 6:00pm ET publish to GCS (KPI dashboard picks up when _SUCCESS lands)
0 18 * * 1-5 . /root/IBKR_MONTH/reports/ibkr.env && /root/waystone-v3/scripts/ibkr-vm-export.sh >> /var/log/ibkr-export.log 2>&1
```

You can still run `scripts/ibkr-vm-export.sh` by hand any time. Weekends omitted (`1-5`);
drop that restriction if you want a dump every calendar day.

Install the log files once: `touch /var/log/ibkr-collect.log /var/log/ibkr-export.log`.

## GCS layout

```
gs://$IBKR_REPORTS_BUCKET/ibkr/v1/dt=YYYY-MM-DD/
  executions.jsonl
  positions.json
  account.json
  summary.json
  _manifest.json
  _SUCCESS
```

Date is the America/New_York calendar day. The API lists a day only when `_SUCCESS` exists.

## Live vs replay (per algo)

Each paper algo has its own live IBKR transaction folder and a **different** replay-backtest
folder. Defaults for the three built-in algos (`s5_options`, `nq_futures`, `es_futures`):

```
gs://$IBKR_REPORTS_BUCKET/algos/v1/registry.json
gs://$IBKR_REPORTS_BUCKET/algos/v1/<algo_id>/live/dt=YYYY-MM-DD/
  executions.jsonl
  summary.json
  _SUCCESS
gs://$IBKR_REPORTS_BUCKET/algos/v1/<algo_id>/replay/dt=YYYY-MM-DD/
  executions.jsonl
  summary.json
  _SUCCESS
```

If an algo has no live blotter yet, Compare falls back to the shared IBKR dump filtered by
that algo's book and optional `clientId`. Replay never falls back — missing replay shows as
empty / `replay_only` gaps.

Onboard another algo from the Compare page, or:

```sh
export IBKR_REPORTS_BUCKET=waystone-data   # or IBKR_REPORTS_LOCAL_DIR=...
uv run waystone3 algo-list
uv run waystone3 algo-register --id cl_futures --name "CL futures" --book futures --client-id 7
```

Custom prefixes are allowed (`--live-prefix`, `--replay-prefix`) when the IBKR log and the
replay writer already use different folders.

## Env reference

| Variable | Where | Purpose |
|---|---|---|
| `IB_HOST` / `IB_PORT` / `IB_CLIENT_ID` | VM dump | TWS/Gateway socket; default clientId **99** |
| `IBKR_CLIENT_BOOKS` | VM dump | optional `1=futures,2=options` overlay |
| `IBKR_LEDGER_DIR` | VM dump | local execId JSONL (TWS history is session-scoped) |
| `IBKR_REPORTS_BUCKET` | VM write, laptop/GKE read | GCS bucket |
| `IBKR_REPORTS_LOCAL_DIR` | laptop API | local tree with the same keys (demo / tests) |
| `IBKR_PAPER` | API | `true` only if this IBKR account is paper |
| `IBKR_KPI_NAV` | API | Assumed account size for the options scorecard (default 100000) |
| `IBKR_KPI_MULTIPLIER` | API | Option $/pt/contract (default 100) |
| `IBKR_KPI_SLIPPAGE` | API | Round-trip slippage as a fraction of premium (default 0.02) |
| `IBKR_KPI_CONTRACTS` | API | Contracts per trade assumption (default 1) |
| `IBKR_KPI_POINT_VALUE` | API | Futures $/point (default 20, NQ) |
| `IBKR_KPI_TRIALS` | API | Optional trial count for the futures Tier 0 row |
| `WAYSTONE_DB` / `WAYSTONE_ADMIN_TOKEN` | API | existing dashboard login tokens |
