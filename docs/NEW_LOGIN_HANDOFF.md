# New Cursor login — handoff for a fresh Cloud Agent chat

Paste this file (or link to it in the repo) when starting a **new Cloud Agent** on a **different Cursor account**. It preserves what was built, where it lives, and how to wire secrets so work continues without re-doing research.

**Repo:** https://github.com/vaishnavi4068/waystone-v3  
**Primary branch for sentiment work:** `cursor/massive-sentiment-gate-2a31`  
**GCS bucket:** `gs://waystone-data` (project `microdrive-dev`)

---

## 1. What is already saved (you will NOT lose this)

| Asset | Location | Notes |
|--------|----------|--------|
| All code changes | GitHub branches + open PRs | See §6 |
| Massive news + sentiment | `gs://waystone-data/research/v1/sentiment/` | 503 SP500 names backfilled |
| Published backtest scorecards | `gs://waystone-data/research/v1/{strategy_id}/dt=YYYY-MM-DD/` | 16 variants published (see §7) |
| Grok ops log | `gs://waystone-data/research/v1/ops/status.json` + inbox | Inbox polls run every 15 min |
| Local backtest CSVs | `waystone_backtests/results/` on any machine after clone + run | Reproducible from Git + GCS data |

## 2. What is NOT tied to your Cursor login

| Lost when switching login | Workaround |
|---------------------------|------------|
| Old Cloud Agent chat transcript | Use this doc + GitHub PRs |
| Old agent URL (`cursor.com/agents/bc-…`) | Start a **new** agent on correct account |
| Port forwarding to old VM | Run dashboard **locally on Mac** (§5) or forward ports on **your** new agent |
| Secrets in old account’s Environment panel | Re-enter in new account (§4) |

The previous agent run was owned by **manoj@arqflo.ai**. Another Cursor login sees “Cloud Agent not found” for that URL — expected.

---

## 3. First steps on the new login

### 3.1 Clone and checkout

```bash
git clone https://github.com/vaishnavi4068/waystone-v3.git
cd waystone-v3
git fetch origin
git checkout cursor/massive-sentiment-gate-2a31   # latest sentiment + v3 merge
uv sync --extra research
cd frontend && npm install && cd ..
```

### 3.2 Start a new Cloud Agent (optional)

1. Cursor Desktop → **Agents** → **New Agent**
2. Repo: **vaishnavi4068/waystone-v3**
3. Branch: **`cursor/massive-sentiment-gate-2a31`**
4. First message: *“Read `docs/NEW_LOGIN_HANDOFF.md` and continue from §8.”*

### 3.3 Do NOT commit secrets

Never put keys, webhook URLs, or SA JSON in git. Use `.env` (local) and Cursor **Environment → Secrets** (cloud).

---

## 4. Secrets checklist (copy same values from old setup)

### 4.1 Mac Studio — `waystone-v3/.env`

Copy from `.env.example` and fill:

```bash
# Massive / Polygon REST (Stocks news + bars)
MASSIVE_API_KEY=<same key as before>
POLYGON_API_KEY=<same as MASSIVE_API_KEY>
POLYGON_BASE_URL=https://api.massive.com

# Optional: Massive flat-file S3
MASSIVE_S3_ACCESS_KEY_ID=...
MASSIVE_S3_SECRET_ACCESS_KEY=...
MASSIVE_S3_ENDPOINT=https://files.massive.com
MASSIVE_S3_BUCKET=flatfiles

# GCP
GOOGLE_APPLICATION_CREDENTIALS=$HOME/.config/gcloud/waystone-data.json
GOOGLE_CLOUD_PROJECT=microdrive-dev
IBKR_REPORTS_BUCKET=waystone-data

# Grok Bot (optional)
GROK_BOT_WEBHOOK_URL=https://api2.cursor.sh/automations/webhook/<routine-id>
GROK_BOT_WEBHOOK_KEY=crsr_...
GROK_BOT_INBOX_TOKEN=crsr_...

# Dashboard
WAYSTONE_DB=$PWD/arena.db
IBKR_STAGED=0
```

**GCP service account file (Mac, one-time):**

```bash
mkdir -p ~/.config/gcloud
# Save JSON key as ~/.config/gcloud/waystone-data.json
# SA: waystone-data@microdrive-dev.iam.gserviceaccount.com
chmod 600 ~/.config/gcloud/waystone-data.json
```

### 4.2 Cursor Cloud Agent — Environment secrets

Cursor → **Cloud Agents** → **Environments** → **waystone-v3** → **Secrets** → add the **same names** as above.

| Secret name | Required for |
|-------------|----------------|
| `MASSIVE_API_KEY` | Sentiment fetch, Massive news |
| `POLYGON_API_KEY` | Same (optional if MASSIVE set) |
| `IBKR_REPORTS_BUCKET` | `waystone-data` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to SA JSON **on the agent VM** |
| `GROK_BOT_WEBHOOK_URL` | Status posts to Grok |
| `GROK_BOT_WEBHOOK_KEY` | Webhook auth |
| `GROK_BOT_INBOX_TOKEN` | Inbound approve-fetch/run/publish |

Click **Save** after adding secrets.

**Do not set** `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` unless Tailscale is running — breaks Cloud Agents.

### 4.3 Verify secrets

```bash
set -a && source .env && set +a

uv run waystone3 research-inbox --pending          # GCS OK if no error
./scripts/check-massive-news.sh                    # Massive OK if PASS
uv run waystone3 research-status --phase note \
  --title "handoff test" --body "new login wired"  # Grok OK if message in bot
```

---

## 5. Local HQ dashboard (easiest — no port forwarding)

On Mac (not in browser-only cursor.com):

```bash
cd waystone-v3
source .env   # or export vars from §4.1
./scripts/run-dashboard-local.sh
```

Open **http://127.0.0.1:3001/strategies**  
Login: **Mark** / **mark1234**

Cloud Agent port forwarding only works when **you** open **your** agent in Cursor Desktop → plug icon → port **3001**.

---

## 6. Open pull requests (code history)

| PR | Branch | Topic |
|----|--------|--------|
| [#9](https://github.com/vaishnavi4068/waystone-v3/pull/9) | `cursor/massive-sentiment-gate-2a31` | Massive sentiment, full SP500, GCS sync, v3 universe/cap |
| [#8](https://github.com/vaishnavi4068/waystone-v3/pull/8) | `cursor/massive-news-probe-2a31` | Massive news probe |
| [#7](https://github.com/vaishnavi4068/waystone-v3/pull/7) | `cursor/research-backtests-2a31` | Scorecards, research backtests |
| [#6](https://github.com/vaishnavi4068/waystone-v3/pull/6) | `cursor/ibkr-daily-dashboard-2a31` | Dashboard IPv6 fix |

**Latest commit theme (PR #9):** Merged `waystone_backtests_3.zip` — universe filters, `max_positions=5` cap, `score_src`/`shock_src_z`, `build_history.py`, `ml/universe.py`.

---

## 7. Research / backtest state (as of 2026-09-07)

### Data on GCS

```
gs://waystone-data/research/v1/sentiment/
  news/       ~514 files
  sentiment/  ~505 files (<SYM>_daily.csv)
  events/     ~550 files
  lists/sp500.csv

gs://waystone-data/research/v1/{strategy_id}/dt=YYYY-MM-DD/{variant}/
  metrics.json, equity.csv, trades.csv, kpi.json, scorecard.json, scorecard.html
```

**Published via `research-publish`:** 16 catalog variants (01 pullback, 04 calendar, 06 sector, 08 PEAD, ML meta overlays, etc.).

**Not yet in catalog (not auto-published):** `ml_sent_shock_massive` — add to `waystone_backtests/catalog.json` then re-run `research-publish` if HQ should show it.

### Key backtest result — sentiment shock (v3)

| Metric | Value |
|--------|--------|
| Run name | `ml_sent_shock_massive` |
| Net PnL | **-$93,375** on $100k NAV |
| Sharpe | -1.06 (was -1.41 before v3 cap/universe fix) |
| Trades | 2,551 from 6,069 signals (cap 5 names) |
| Gate | **FAIL** (PBO 71%, DSR 0) |
| Local path | `waystone_backtests/results/ml_sent_shock_massive/` |

Pipeline works; tone-shock has no tradable edge on full universe. Next experiments: `event-study` mode, **reactive universe** (top-50), or **event-filter overlay** on pullback/PEAD (not standalone shock).

### Positive sleeves (local + published)

| Sleeve | Approx net PnL | Sharpe |
|--------|----------------|--------|
| `01_mean_reversion_pullback` | +$90,882 | 1.42 |
| `08_pead_implied_move` | +$54,478 | 1.31 |
| `ml_meta_01_pullback` | +$71,532 | 2.32 |
| `ml_meta_08_pead` | +$76,513 | 1.60 |

### Blocked / negative

| Sleeve | Issue |
|--------|--------|
| **02 GEX** | Only 1 day SPX chain snapshot — needs historical option chain |
| **05 orderflow** | -$38k; uses CVD **proxy** not real bid/ask volume |
| **ml_sent_shock_massive** | No edge after costs |

---

## 8. Commands the new agent should know

```bash
# Pull sentiment from GCS
uv run waystone3 research-sentiment-sync --pull

# Score + backtest sentiment (full SP500)
./scripts/run-sentiment-massive.sh

# Publish results to GCS for HQ dashboard
uv run waystone3 research-publish

# Grok inbox poll (every 15 min timer)
uv run waystone3 research-inbox --pending
# If approve-publish: research-publish, ack, post_status

# Post status to Grok + GCS
uv run waystone3 research-status --phase run_done --title "..." --body "..."

# Local dashboard
./scripts/run-dashboard-local.sh
```

**Grok inbox workflow:** If inbox empty → do nothing. Do not republish or rerun backtests on empty polls.

---

## 9. Grok Bot setup (reference)

Full detail: [docs/GROK_BOT.md](GROK_BOT.md)

1. Grok Bot desktop → routine → **When a webhook fires**
2. Copy POST URL + `crsr_…` key into env (§4)
3. Inbound approvals: POST to `/api/research/ops/inbox` with `X-Grok-Bot-Key`, or `waystone3 research-inbox-add --action approve-publish`

Actions: `approve-fetch`, `approve-run`, `approve-publish`.

---

## 10. Environment config in repo

| File | Purpose |
|------|---------|
| `.cursor/environment.json` | Cloud Agent install, ports 3001/9200, terminal commands |
| `.env.example` | Template for local secrets |
| `scripts/run-dashboard-local.sh` | One-command HQ UI + API |
| `scripts/run-sentiment-massive.sh` | Score + backtest + GCS push |
| `scripts/sentiment-daily.sh` | Daily cron-style sentiment update |

Draft environment build (if using Builds): `bld-20260907-f02fb236-cda9-4fc8-952c-db78c89d151f` on branch `cursor/massive-sentiment-gate-2a31`.

---

## 11. Prompt to paste into the new agent chat

```
Read docs/NEW_LOGIN_HANDOFF.md in full.

Context: waystone-v3 research backtests on vaishnavi4068/waystone-v3.
Branch: cursor/massive-sentiment-gate-2a31 (PR #9).

Already done:
- Full SP500 Massive news + sentiment on GCS
- v3 sentiment code (universe, position cap, build_history)
- ml_sent_shock_massive backtest run (gate FAIL, Sharpe -1.06)
- research-publish pushed 16 strategy variants to gs://waystone-data/research/v1/

Secrets should be in Environment panel + .env (MASSIVE_API_KEY, GCP SA, IBKR_REPORTS_BUCKET=waystone-data, optional Grok webhook).

Continue from handoff §8. Do not rerun backtests unless inbox approve-run or I ask.
Pending if I want: add ml_sent_shock_massive to catalog.json and republish; event-study on real data; reactive universe top-50.
02 GEX still blocked on SPX historical chain.
```

---

## 12. Architecture reminder

- **ML overlays** (meta_label) filter primary trades — they do not generate trades.
- **Sentiment shock** is a primary sleeve — currently fails gates; consider event-filter overlay instead.
- **HQ dashboard** reads published GCS scorecards (`IBKR_STAGED=0`), not local `results/` unless you publish.
- **Mac Studio** is the intended compute host; Cloud Agent is for automation. See [docs/MAC_STUDIO_WORKER.md](MAC_STUDIO_WORKER.md).

---

## 13. Related docs

- [RESEARCH_BACKTESTS.md](RESEARCH_BACKTESTS.md) — GCS layout, fetch/run/publish
- [GROK_BOT.md](GROK_BOT.md) — webhook + inbox
- [MAC_STUDIO_WORKER.md](MAC_STUDIO_WORKER.md) — self-hosted worker
- [IBKR_DAILY.md](IBKR_DAILY.md) — daily reports bucket layout
- `waystone_backtests/ML_ALGO.md` — sentiment + ML gate spec
