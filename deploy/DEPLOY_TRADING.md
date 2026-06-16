# Waystone v3 — Trading + Read-Only Website + Cliq Alerts on GKE

Deploys the **read-only dashboard website**, trading on **one shared Alpaca paper account**,
with **Zoho Cliq** alerts to a team group whenever a strategy runs / an order fills.

This builds on the base infra in [`DEPLOY.md`](./DEPLOY.md) (cluster, ingress, TLS, Secret
Manager, frontend, read-only API). Read that first for the cluster/ingress/cert steps; this
doc covers the **three deltas** that make the team-trading + alerts story work:

1. a **trader** workload that runs the agent-OS loop against the shared Alpaca paper account,
2. binding the trader **and** the read-only API to **Alpaca** (not the in-process sim),
3. **Zoho Cliq** delivery — webhook secret + the `NotifierAgent → OrderFilled` event wiring.

> **Paper only.** `ALPACA_PAPER=true` throughout. Going to real capital is a deliberate,
> separate step — never flip it as part of this deploy.

---

## Architecture (one cluster, one public host)

```
                Internet → HTTPS (GKE ManagedCertificate)
                          │
                ┌─────────▼──────────┐  GCE Ingress (deploy/k8s/ingress.yaml)
                │  arena.yourco.com  │  path fan-out
                └──┬──────────┬───────┬──────────────┘
          /*       │   /api/* │       │ /mcp*
    ┌──────────────▼─┐ ┌──────▼─────┐ │
    │ frontend (3000)│ │  api (9200)│ │   READ-ONLY website (GET-only API, CORS GET-only)
    │ Next.js, RO    │ │  FastAPI   │ │
    └────────────────┘ └─────┬──────┘ │
    ┌──────────────────────────────────▼──────────────────────────┐
    │  POD waystone-arena:  arena(9100) + api(9200)  [+ CSI sync]  │
    └──────────────────────────────────────────────────────────────┘
    ┌──────────────────────────────────────────────────────────────┐
    │  CronJob waystone-trader [NEW]: `serve --broker alpaca`       │
    │  → trades shared Alpaca PAPER acct on a schedule              │
    │  → emits OrderFilled/StrategySubmitted → NotifierAgent → Cliq │
    └──────────────────────────┬───────────────────────────────────┘
                              │
        ┌─────────────────────┼────────────────────────┐
   Alpaca PAPER (shared)  GCP Secret Manager       Zoho Cliq webhook
   ALPACA_API_KEY/SECRET  (CSI driver → env)       ZOHO_CLIQ_WEBHOOK_URL
```

**Already in `deploy/k8s/`** ✅ — namespace, RWO SQLite PVC, `arena` + `api` containers,
3 Services, ingress fan-out, ManagedCertificate, Secret Manager CSI provider, frontend.

**Added by this doc** 🆕 — the `trader` CronJob, the Alpaca + Cliq + WhatsApp secret keys, and the
broker/alert env. See [§5 Gaps to close](#5-gaps-to-close-the-deltas).

---

## 1. Prerequisites (one-time)

```bash
gcloud config set project PROJECT_ID
gcloud services enable container.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com

gcloud container clusters create-auto waystone --region REGION        # Autopilot
gcloud container clusters get-credentials waystone --region REGION
gcloud artifacts repositories create waystone --repository-format=docker --location=REGION

gcloud compute addresses create waystone-arena-ip --global            # reserved IP
# → DNS A record: arena.yourco.com → <reserved IP>
```

## 2. Secrets — the shared Alpaca account + Cliq live here

Source of truth is **GCP Secret Manager**; the CSI provider syncs them into the
`waystone-arena-secrets` env Secret the pod consumes.

```bash
printf '%s' "<alpaca paper key>"      | gcloud secrets create alpaca-api-key       --data-file=-
printf '%s' "<alpaca paper secret>"   | gcloud secrets create alpaca-api-secret    --data-file=-
printf '%s' "<cliq incoming webhook>" | gcloud secrets create zoho-cliq-webhook    --data-file=-
printf '%s' "<polygon key>"           | gcloud secrets create polygon-api-key      --data-file=-
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create waystone-admin-token --data-file=-
# optional: anthropic-api-key for the sentiment scorer / Claude agents
```

**Getting the Cliq webhook:** in Zoho Cliq open the team **channel → Connect / Integrations →
Incoming Webhook**, copy the URL (the `zapikey` is embedded). That single URL is the whole
config — set it as `ZOHO_CLIQ_WEBHOOK_URL`.

**One shared Alpaca paper account:** one key/secret pair, used by both the `trader`
(executes) and the `api` (reads account/positions/orders). That is the "common account".

## 3. Build & push images

```bash
TAG=$(git rev-parse --short HEAD)
REG=REGION-docker.pkg.dev/PROJECT_ID/waystone

# Backend image — arena, api, and trader all run from it (different CMDs)
gcloud builds submit --tag $REG/waystone-arena:$TAG .

# Frontend image — bake the public API base in at build time (frontend/lib/api.ts:14)
gcloud builds submit --tag $REG/waystone-frontend:$TAG \
  --substitutions=_API_BASE=https://arena.yourco.com frontend
```

## 4. Deploy the base stack (from DEPLOY.md)

```bash
sed "s|__IMAGE__|$REG/waystone-arena:$TAG|g"          deploy/k8s/arena.yaml    | kubectl apply -f -
sed "s|__FRONTEND_IMAGE__|$REG/waystone-frontend:$TAG|g" deploy/k8s/frontend.yaml | kubectl apply -f -
kubectl apply -f deploy/k8s/secret-provider.yaml       # grant KSA secretAccessor first (DEPLOY.md §3)
sed "s|__ARENA_DOMAIN__|arena.yourco.com|g"           deploy/k8s/ingress.yaml  | kubectl apply -f -
```

TLS provisions in 10–30 min once DNS resolves. The **read-only website** is then live at
`https://arena.yourco.com` (it cannot place trades — the API is GET-only by design).

---

## 5. Gaps to close (the deltas)

Steps 1–4 stand up the site + shared account. The pieces below make trades actually run and
ping the team group. **Most are now implemented** — status noted per item.

### 5a. Trader CronJob — `deploy/k8s/trader.yaml`  ✅ added
`waystone3 serve` runs a fixed number of cycles then exits, so the trader is a **CronJob**,
not an always-on sidecar (cleaner, bounds cost; the agent-OS run is stateless so it needs no
SQLite). It runs `serve --broker alpaca --source yfinance` against the shared paper account
and `envFrom`s `waystone-arena-secrets`. Schedule defaults to every 15 min during US market
hours — tune in the manifest.

```bash
sed "s|__IMAGE__|$REG/waystone-arena:$TAG|g" deploy/k8s/trader.yaml | kubectl apply -f -
```

### 5b. Read-only API on the shared Alpaca paper account  ✅ wired
`api-serve` builds its workspace via `build_broker_from_env`, which selects the broker from
env: set **`WAYSTONE_BROKER=alpaca`** (with the ALPACA creds in the secret) and `/api/account`
reflects the shared **paper** account; unset = auto-detect (Alpaca when creds present, else
the in-process sim); `WAYSTONE_BROKER=paper` pins the sim. The choice is logged at startup
(`broker_selected`). Paper-only — there is no live path.

### 5c. Alpaca + Cliq + WhatsApp secret keys  ✅ wired
`deploy/k8s/secret-provider.yaml` already mounts `alpaca-api-key`, `alpaca-api-secret`,
`zoho-cliq-webhook`, and `whatsapp-group-*` from Secret Manager and syncs them to the env
Secret (`ALPACA_*`, `ZOHO_CLIQ_WEBHOOK_URL`, `WHATSAPP_GROUP_*`). `arena.yaml` now mounts the
CSI volume + sets `serviceAccountName: waystone-arena` to trigger the sync. Just create the
GCP secrets and grant the KSA `secretAccessor` (commands in the secret-provider header).

### 5d. Trade/strategy → group message  ✅ wired (P&L pending)
Implemented and tested:
- `NotifierAgent` now reacts to **`OrderFilled`** and a new **`StrategySubmitted`** event;
- both carry an **`actor`** (player display name), so messages read
  "Order filled: NVDA — Mark: BUY 1 NVDA @ 100";
- `build_agent_os` **auto-seeds** a TRADER recipient for whichever channel env is set
  (`ZOHO_CLIQ_WEBHOOK_URL` → `cliq`, `WHATSAPP_GROUP_API_URL` → `whatsapp_group`) — so just
  setting the secret delivers alerts; no recipient YAML needed.

Remaining enhancement: **per-trade realized P&L** in the message (needs cost-basis tracking;
today the message carries side/qty/price). Strategy + fills already reach the group.

---

## 6. Verify

```bash
kubectl -n waystone-arena get pods                 # Running, 3/3 containers ready
curl https://arena.yourco.com/api/health           # ok
curl https://arena.yourco.com/api/account          # is_paper: true, shared account
```
- Browser → `https://arena.yourco.com` renders positions/signals; **cannot** trade (GET-only).
- Trigger a cycle → the Zoho Cliq team channel receives the strategy + P&L message.

---

## 7. Scaling note (when SQLite hurts)

`arena` + `trader` both write the single RWO SQLite file in one pod — fine for a ~5-person
paper desk with WAL mode; keep `replicas: 1`, `strategy: Recreate` (never two writers).
The scale path mirrors v2: move to **AlloyDB / Postgres**, after which `trader`, `api`, and
`arena` can split into separate Deployments and scale independently.
