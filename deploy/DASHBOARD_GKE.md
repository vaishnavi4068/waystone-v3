# Deploy only the IBKR dashboard on GKE (from `main`)

This ships **two things**: the FastAPI dashboard API and the Next.js UI.
It does **not** deploy Arena MCP, the trader CronJob, Alpaca, Polygon, or Claude.

You do **not** apply `deploy/k8s/arena.yaml`, `trader.yaml`, or `secret-provider.yaml`.

On `main` today the UI has Daily, Options KPIs, Account, Positions, Orders, and the
other read-only tabs. Compare / Futures KPIs / default passwords land after PR #4
merges — same deploy steps.

---

## What you skip

| Leave out | Why |
|---|---|
| `waystone3 arena-serve` / `/mcp` | MCP for Claude, not the dashboard |
| `deploy/k8s/trader.yaml` | live cycle runner |
| Alpaca / Polygon / Anthropic keys | dashboard reads **GCS IBKR dumps** + SQLite users |
| Frontend local port 3001 | the container listens on **3000** |

---

## 0. Values

```sh
export PROJECT=microdrive-dev          # your GCP project
export REGION=us-central1
export CLUSTER=waystone-cluster        # existing Autopilot cluster is fine
export REPO=waystone
export DOMAIN=dash.example.com         # hostname you control
export BUCKET=waystone-data            # gs://waystone-data IBKR dumps
export TAG=$(date +%Y%m%d-%H%M)
export IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/waystone-arena:$TAG"
export FRONTEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/waystone-frontend:$TAG"

gcloud config set project "$PROJECT"
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"
```

Enable APIs if this project is new:

```sh
gcloud services enable \
  container.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com
```

Create the Artifact Registry repo once:

```sh
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" || true
```

---

## 1. Build images from `main`

```sh
git clone https://github.com/vaishnavi4068/waystone-v3.git
cd waystone-v3
git checkout main

# Python image (same Dockerfile; we only run api-serve)
gcloud builds submit --tag "$IMAGE" .

# Next.js — bake the public origin so the browser calls https://$DOMAIN/api/*
gcloud builds submit frontend --config=frontend/cloudbuild.yaml \
  --substitutions=_API_BASE="https://$DOMAIN",_IMAGE="$FRONTEND_IMAGE"
```

Use Cloud Build (amd64). A local Mac `docker build` is arm64 and will crash-loop on GKE.

---

## 2. DNS + static IP

```sh
gcloud compute addresses create waystone-dash-ip --global || true
gcloud compute addresses describe waystone-dash-ip --global --format='value(address)'
```

Create an **A record**: `$DOMAIN` → that IP. Wait until it resolves before you expect TLS.

---

## 3. Namespace, admin token, apply manifests

```sh
kubectl create namespace waystone-dash

kubectl -n waystone-dash create secret generic waystone-dash-secrets \
  --from-literal=WAYSTONE_ADMIN_TOKEN="$(openssl rand -hex 24)"

# Save the token; you need it to seed users (step 5).
kubectl -n waystone-dash get secret waystone-dash-secrets \
  -o jsonpath='{.data.WAYSTONE_ADMIN_TOKEN}' | base64 -d; echo
```

Fill placeholders and apply [k8s/dashboard.yaml](k8s/dashboard.yaml):

```sh
sed -e "s|__IMAGE__|$IMAGE|g" \
    -e "s|__FRONTEND_IMAGE__|$FRONTEND_IMAGE|g" \
    -e "s|__DASH_DOMAIN__|$DOMAIN|g" \
    -e "s|__IBKR_BUCKET__|$BUCKET|g" \
    deploy/k8s/dashboard.yaml | kubectl apply -f -
```

Do **not** apply `arena.yaml` / `ingress.yaml` / `trader.yaml` for this stack.

---

## 4. Let the API read GCS (`gs://waystone-data`)

The dashboard never opens TWS. It only reads published prefixes after `_SUCCESS`.

Bind the Kubernetes SA to a GCP SA that can **read** the bucket
(Workload Identity; Autopilot has this on):

```sh
PROJECT_NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud iam service-accounts create waystone-dash --display-name="waystone dashboard" || true

gcloud iam service-accounts add-iam-policy-binding \
  "waystone-dash@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:$PROJECT.svc.id.goog[waystone-dash/waystone-dash]"

kubectl -n waystone-dash annotate serviceaccount waystone-dash \
  iam.gke.io/gcp-service-account="waystone-dash@$PROJECT.iam.gserviceaccount.com" \
  --overwrite

gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:waystone-dash@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/storage.objectViewer
```

Dump writes stay on the GCE VM (`waystone3 ibkr-export`). That VM SA needs
`objectAdmin` (or similar) — the dashboard SA is **read-only**.

---

## 5. Seed the five users (`main` prints passwords)

On `main`, passwords are generated at seed time. Copy the table.

```sh
kubectl -n waystone-dash exec deploy/waystone-dash-api -- \
  env WAYSTONE_DB=/data/arena.db \
      WAYSTONE_ADMIN_TOKEN="$(kubectl -n waystone-dash get secret waystone-dash-secrets \
        -o jsonpath='{.data.WAYSTONE_ADMIN_TOKEN}' | base64 -d)" \
  uv run waystone3 arena-seed --players "Mark,Manoj,Brent,Akash,Kole"
```

Then bounce the API so it reloads the SQLite roster:

```sh
kubectl -n waystone-dash rollout restart deploy/waystone-dash-api
kubectl -n waystone-dash rollout status deploy/waystone-dash-api
```

After PR #4 is on `main`, the API also creates Mark / Manoj / Brent / Akash / Kole
with the fixed default passwords on startup; seed is then optional.

---

## 6. Check it

```sh
kubectl -n waystone-dash get pods,svc,ingress
kubectl -n waystone-dash logs deploy/waystone-dash-api -c api --tail=50
curl -sS "https://$DOMAIN/api/health"
```

Open `https://$DOMAIN/`. Sign in. Daily is `/ibkr`, Options KPIs is `/options-kpis`.

TLS via Managed Certificate takes 10–30 minutes after DNS is correct
(`kubectl -n waystone-dash describe managedcertificate waystone-dash-cert`).

---

## 7. Update later (still dashboard-only)

```sh
git checkout main && git pull
# rebuild IMAGE + FRONTEND_IMAGE (step 1)
kubectl -n waystone-dash set image deploy/waystone-dash-api api="$IMAGE"
kubectl -n waystone-dash set image deploy/waystone-dash-ui frontend="$FRONTEND_IMAGE"
```

Rebuild the frontend whenever `$DOMAIN` changes (`NEXT_PUBLIC_API_BASE` is baked in).

---

## Env the API actually needs

| Env | Value |
|---|---|
| `WAYSTONE_DB` | `/data/arena.db` (PVC) |
| `WAYSTONE_ADMIN_TOKEN` | from the Secret (seed only on `main`) |
| `IBKR_REPORTS_BUCKET` | `waystone-data` |
| `IBKR_PAPER` | `true` |
| `WAYSTONE_BROKER` | `paper` (no Alpaca) |
| `IBKR_STAGED` | `0` in prod (omit / `1` only if you want the sample week overlay) |

`IBKR_REPORTS_LOCAL_DIR` must stay **unset** in the cluster so GCS is used.
