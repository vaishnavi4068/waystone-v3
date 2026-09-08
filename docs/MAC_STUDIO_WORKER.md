# Mac Studio Cursor worker

This Cloud Agent VM cannot reach your Mac. A **self-hosted worker** on the Mac
opens an outbound HTTPS connection to Cursor; then a new cloud agent can be
targeted at that machine and run `research-fetch` / `research-run` / `research-publish`
with local keys.

## One-time on the Mac Studio

```sh
curl https://cursor.com/install -fsS | bash
agent login
cd /path/to/waystone-v3
./scripts/mac-studio-worker.sh
```

Leave that process running (or install it as a LaunchAgent). The worker should
appear as **mac-studio** under Cursor → Cloud Agents → My Machines.

Keep Massive / GCP keys only in the Mac environment (never GKE, never git).
The SA JSON is `waystone-data@microdrive-dev.iam.gserviceaccount.com` —
save it as `$HOME/.config/gcloud/waystone-data.json`, not in the repo.

```sh
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/waystone-data.json"
export IBKR_REPORTS_BUCKET=waystone-data
export MASSIVE_API_KEY=...
export MASSIVE_S3_ACCESS_KEY_ID=...
export MASSIVE_S3_SECRET_ACCESS_KEY=...
export MASSIVE_S3_ENDPOINT=https://files.massive.com
export MASSIVE_S3_BUCKET=flatfiles
# optional — Grok Bot webhook routine (desktop app)
export GROK_BOT_WEBHOOK_URL='https://api2.cursor.sh/automations/webhook/<id>'
export GROK_BOT_WEBHOOK_KEY='crsr_...'
export GROK_BOT_INBOX_TOKEN="$GROK_BOT_WEBHOOK_KEY"
```

Outbound HTTPS required: `api2.cursor.sh`, `api2direct.cursor.sh`.
No inbound ports.

Docs: https://cursor.com/docs/cloud-agent/bring-your-own-machine
