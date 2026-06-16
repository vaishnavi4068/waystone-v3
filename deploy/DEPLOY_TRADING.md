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
                             │ shares RWO SQLite + Alpaca creds (same pod)
    ┌────────────────────────▼─────────▼──────────────────────────┐
    │  POD waystone-arena:  arena(9100) + api(9200) + trader[NEW]  │
    │  trader = `waystone3 serve` → trades shared Alpaca PAPER acct │
    │           → emits OrderFilled → NotifierAgent → Zoho Cliq     │
    └──────────────────────────┬───────────────────────────────────┘
                              │
        ┌─────────────────────┼────────────────────────┐
   Alpaca PAPER (shared)  GCP Secret Manager       Zoho Cliq webhook
   ALPACA_API_KEY/SECRET  (CSI driver → env)       ZOHO_CLIQ_WEBHOOK_URL
```

**Already in `deploy/k8s/`** ✅ — namespace, RWO SQLite PVC, `arena` + `api` containers,
3 Services, ingress fan-out, ManagedCertificate, Secret Manager CSI provider, frontend.

**Added by this doc** 🆕 — the `trader` sidecar, the Alpaca + Cliq secret keys, and the
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

Steps 1–4 stand up the site + shared account. To make trades actually run and ping Cliq:

### 5a. Add the `trader` sidecar (executes on the shared account, emits events)
Add a third container to the `waystone-arena` pod in `deploy/k8s/arena.yaml`, alongside
`arena` and `api` (same pod ⇒ shares the RWO SQLite volume and the Alpaca creds, and is the
event source for alerts):

```yaml
        - name: trader
          image: __IMAGE__
          command: ["uv", "run", "waystone3", "serve"]   # the agent-OS loop
          env:
            - name: WAYSTONE_DB
              value: /data/arena.db
            - name: WAYSTONE_BROKER          # bind to the shared Alpaca PAPER account
              value: alpaca
          envFrom:
            - secretRef: { name: waystone-arena-secrets }
          volumeMounts:
            - name: data
              mountPath: /data
          resources:
            requests: { cpu: "100m", memory: "256Mi" }
            limits:   { cpu: "500m", memory: "512Mi" }
```

### 5b. Point the read-only API at Alpaca too
So `/api/account` reflects the real shared paper account, set on the `api` container env:
`WAYSTONE_BROKER=alpaca`, `ALPACA_PAPER=true`.

### 5c. Wire the Cliq secret keys into the env Secret
Add to `deploy/k8s/secret-provider.yaml` (`parameters.secrets` + `secretObjects.data`):

```yaml
      - resourceName: "projects/PROJECT_ID/secrets/alpaca-api-key/versions/latest"
        path: "alpaca-api-key"
      - resourceName: "projects/PROJECT_ID/secrets/alpaca-api-secret/versions/latest"
        path: "alpaca-api-secret"
      - resourceName: "projects/PROJECT_ID/secrets/zoho-cliq-webhook/versions/latest"
        path: "zoho-cliq-webhook"
```
```yaml
        - { objectName: "alpaca-api-key",    key: ALPACA_API_KEY }
        - { objectName: "alpaca-api-secret", key: ALPACA_API_SECRET }
        - { objectName: "zoho-cliq-webhook", key: ZOHO_CLIQ_WEBHOOK_URL }
```
Also grant the `waystone-arena` KSA `roles/secretmanager.secretAccessor` on the new secrets.

`ZohoCliqChannel` reads `ZOHO_CLIQ_WEBHOOK_URL` automatically (it's registered as the `cliq`
channel in `agent_os.py`). Seed a recipient so alerts route to it, e.g. in startup config:
`store.create("Team channel", Role.TRADER, channel="cliq", min_severity=Severity.INFO)`.

### 5d. Code wiring — make trades/strategies actually emit to Cliq  ⚠️ required
The `cliq` channel is the delivery half. For "Mark ran Momentum-v2 — BUY 1 NVDA, P&L +$240"
to appear, the agent layer still needs:
- `NotifierAgent.subscribes_to` to include **`OrderFilled`** (today: StrongSignal /
  OrderBlocked / AgentAction / AgentError), plus a `_to_alert(OrderFilled)` branch;
- a new **`StrategySubmitted`** event published from the competition/workspace submit path;
- a **`user_id`** (and P&L) field on those events so the message names *who* and the profit.

This is a small, tested code change (see `src/waystone3/agents/notifier.py`,
`bus/events.py`). Until it lands, Cliq still fires for strong signals and risk blocks, but
not for routine fills.

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
