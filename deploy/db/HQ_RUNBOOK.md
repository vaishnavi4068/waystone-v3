# HQ database: from VM logs to the dashboard

```text
VM v221 logs ──rsync :25 hourly / 16:20 + 17:20 ET──▶ gs://waystone-data/raw/...
   ──Cloud Run job waystone-load-* (:35 hourly / 16:35 + 17:35 ET)──▶ Cloud SQL waystone-hq (private IP)
   ──api.* views as waystone_read──▶ dashboard /hq pages + MCP hq_* tools
```

Run everything below from the Mac, in a checkout that contains the loader. That means
`main` once #14 → #15 → #16 → #17 are merged in that order. `gcloud auth login` first.
No step creates a Google service account: the loader runs as the VM's existing service
account, and the dashboard pod uses its k8s service account through Workload Identity.

```sh
cd waystone-v3 && git checkout main && git pull
export PROJECT_ID=microdrive-dev VM_ZONE=us-east4-c
```

## 1. VM sync and database (once)

1. **Let the VM write to the bucket.** This changes the access scope on the same service
   account. It needs a stop/start, so do it while the engines are flat. Futures reopen
   Sunday 18:00 ET.

   ```sh
   VM_SA=$(gcloud compute instances describe waystone --zone $VM_ZONE --format='value(serviceAccounts[0].email)')
   gcloud compute instances stop waystone --zone $VM_ZONE
   gcloud compute instances set-service-account waystone --zone $VM_ZONE \
     --service-account "$VM_SA" --scopes cloud-platform
   gcloud compute instances start waystone --zone $VM_ZONE
   ```

2. **Install the sync timers.** Then check that files arrive after the next :25 run.

   ```sh
   deploy/db/bootstrap_gcp.sh vm
   gcloud storage ls -l 'gs://waystone-data/raw/paper/*/' | tail -20
   gcloud storage ls -l gs://waystone-data/raw/backtest/ | tail -5   # after 16:20 ET
   ```

3. **Tools, then the tables.** The `sql` step briefly enables a public IP for the proxy
   and removes it again afterwards.

   ```sh
   brew install cloud-sql-proxy libpq && brew link --force libpq
   deploy/db/bootstrap_gcp.sh infra     # safe to re-run; also grants the VM SA its roles
   deploy/db/bootstrap_gcp.sh sql
   ```

## 2. Loader (Cloud Run jobs + schedules)

```sh
deploy/db/bootstrap_gcp.sh loader
```

This builds the image with Cloud Build (`.gcloudignore` keeps `.env` and keys out of the
upload) and deploys three Cloud Run jobs in us-east1:

| Job | What it runs | Schedule (America/New_York) |
|---|---|---|
| `waystone-load-paper` | `load-logs --job paper` | `35 * * * *` |
| `waystone-load-backtest` | `load-logs --job backtest` | `35 16,17 * * 1-5` |
| `waystone-load-backfill` | `load-logs --job backfill` | manual |

All three run as the VM's service account and connect through direct VPC egress to the
private IP as `waystone_load`. The password comes from `waystone-db-load-password`.

Run one now instead of waiting for the schedule, then read its logs:

```sh
gcloud run jobs execute waystone-load-paper --region us-east1 --wait
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=waystone-load-paper' \
  --project $PROJECT_ID --limit 20 --format='value(textPayload)'
```

## 3. Dashboard

Rebuild both images from `main` and roll them out, following [DASHBOARD_GKE.md](../DASHBOARD_GKE.md)
steps 1 and 7. Then wire the API to the database:

```sh
deploy/db/bootstrap_gcp.sh dash
```

This step:
- grants the `waystone-dash/waystone-dash` k8s service account access to
  `waystone-db-read-password`;
- sets `WAYSTONE_HQ_DB_HOST` to the private IP;
- restarts the API.

Open `https://<dash domain>/hq`. You should see a card for each of ES V221, NQ V221 and
R2 MNQ, plus the live-vs-backtest table.

## 4. MCP for Claude

The dashboard API serves the read-only `hq_*` tools at `https://<dash domain>/api/mcp`, with
the dashboard login token as the bearer. See [CLAUDE_CONNECTOR.md](../CLAUDE_CONNECTOR.md).

If the separate trading Arena (`waystone-arena`) is deployed, give it the same tools with:

```sh
DASH_NAMESPACE=waystone-arena DASH_KSA=waystone-arena DASH_DEPLOYMENT=waystone-arena \
  deploy/db/bootstrap_gcp.sh dash
```

Claude then sees the read-only tools `hq_strategies`, `hq_sync`, `hq_compare`, `hq_paper_day`, `hq_kpis`,
`hq_daily_pnl`, `hq_trades`, `hq_returns` and `hq_load_status`.

## 5. Backfill and check against the workbook

1. **Backfill.** This loads every file already in the bucket. R2 is from 2026-09-25 and
   ES/NQ from their paper start dates. It is idempotent: unchanged files are skipped on re-runs.

   ```sh
   deploy/db/bootstrap_gcp.sh backfill
   ```

2. **Open a read-only SQL session from the Mac.** The instance is private-IP only, so add
   a temporary public IP for the proxy and remove it afterwards.

   ```sh
   gcloud sql instances patch waystone-hq --assign-ip
   cloud-sql-proxy --port 6543 --gcloud-auth "$(gcloud sql instances describe waystone-hq --format='value(connectionName)')" &
   sleep 5
   export PGPASSWORD=$(gcloud secrets versions access latest --secret=waystone-db-read-password)
   psql "host=127.0.0.1 port=6543 dbname=waystone user=waystone_read sslmode=disable"
   ```

3. **Run the checks** at the `psql` prompt.

   ```sql
   -- Loader health: the last run per job should be OK or NOOP
   SELECT job, status, files_seen, files_loaded, started_at, error FROM api.v_load_health;

   -- Files that did not parse cleanly (should be empty)
   SELECT gcs_uri, parse_status, parse_message FROM raw.source_file
   WHERE is_current AND parse_status NOT IN ('PARSED', 'SKIPPED') ORDER BY gcs_uri;

   -- Parsed trades vs the engine's own DAILY SUMMARY (net_match/closed_match should be true)
   SELECT strategy_code, session_date, paper_status,
          checks->'paper'->>'trades_parsed' AS parsed, checks->'paper'->>'closed_reported' AS reported,
          checks->'paper'->>'net_parsed' AS net_parsed, checks->'paper'->>'net_reported' AS net_reported,
          checks->'paper'->>'net_match' AS net_ok, checks->'paper'->>'unparsed_lines' AS unparsed,
          checks->'backtest'->>'run_status' AS replay
   FROM api.v_day_status ORDER BY 1, 2;

   -- Same columns as the workbook's daily sync sheet: compare row by row for R2 and ES
   SELECT strategy_code, session_date, live_trades, live_net_pnl, bt_trades, bt_net_pnl,
          pnl_delta, round(pnl_delta_pct * 100, 1) AS delta_pct, sync_status, backtest_status
   FROM api.v_daily_sync WHERE strategy_code IN ('r2_mnq', 'es_v221') ORDER BY 1, 2;

   -- Daily P&L and equity (Daily Calc sheet)
   SELECT strategy_code, session_date, trades, net_pnl, equity_end, round(dd_itd * 100, 2) AS dd_pct
   FROM api.v_daily_pnl WHERE strategy_code IN ('r2_mnq', 'es_v221') ORDER BY 1, 2;

   -- Scorecard (Futures KPIs sheet), latest ITD
   SELECT strategy_code, kpi_code, label, num_value, text_value, status
   FROM api.v_kpi_latest WHERE kpi_window = 'ITD' AND strategy_code IN ('r2_mnq', 'es_v221')
   ORDER BY strategy_code, section, sort_order;
   ```

4. **Close the session.**

   ```sh
   kill %1; gcloud sql instances patch waystone-hq --no-assign-ip
   ```

**If a number differs from the workbook:** keep the session date, the strategy and both
values. Then run this to see what the loader stored for that day:

```sql
SELECT l.line_no, l.content
FROM raw.source_line l
JOIN raw.source_file f USING (file_id)
WHERE f.is_current AND f.gcs_uri LIKE '%<strategy>/<YYYY-MM-DD>%'
ORDER BY l.line_no;
```

Once a week of sessions matches, stop updating the Excel workbook and the comparison files.
The dashboard `/hq` pages and the MCP tools are then the source.

## Day-status meanings

| Field | Values |
|---|---|
| paper | `FINAL` (DAILY SUMMARY present and it matches the parsed trades), `PRELIMINARY` (past day, no summary), `INTRADAY` (today, no summary yet), `PARTIAL` (parsed trades disagree with the summary; see `checks`) |
| backtest | `LOADED`, `DATA_INCOMPLETE`, `PENDING` (before 17:30 ET), `MISSING` (none by 17:30 ET), `NOT_APPLICABLE` |
| sync | `OK`, `FLAG` (exit reason or trade count differs, or the delta is over threshold), `N/A` (no replay) |
