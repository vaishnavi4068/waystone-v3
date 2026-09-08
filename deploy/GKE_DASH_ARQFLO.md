# End-to-end: HQ dashboard on GKE (`dash.arqflo.ai`)

This is the developer redeploy path, plus the extra steps that actually wire
`gs://waystone-data` into the API pods.

It ships **only** the FastAPI dashboard API and the Next.js UI in namespace
`waystone-dash`. It does **not** deploy Arena MCP, the trader CronJob, or Alpaca.

Run this in **Google Cloud Shell** (or any machine with `gcloud` + `kubectl`
logged into `microdrive-dev`). A Mac `docker build` is arm64 and will crash-loop
on GKE — use Cloud Build.

---

## What you get

| Piece | Value |
|--------|--------|
| Project | `microdrive-dev` |
| Cluster | `md-dev` (`us-east1`) |
| Namespace | `waystone-dash` |
| URL | https://dash.arqflo.ai |
| Bucket | `gs://waystone-data` |
| GCP SA | `waystone-dash@microdrive-dev.iam.gserviceaccount.com` |
| K8s SA | `waystone-dash` / `waystone-dash` |

The UI never talks to GCS. Ingress sends `/api/*` to the API pod; that pod reads
the bucket via **Workload Identity** (no JSON key in the cluster).

---

## Pick a branch

| Branch | What the live site will show after GCS is wired |
|--------|--------------------------------------------------|
| `restore-dashboard-pr4` | Developer default. IBKR Daily / Options / Account / etc. **No** `/strategies` research scorecards. |
| `cursor/massive-sentiment-gate-2a31` | Same IBKR pages **plus** `/strategies` from `gs://waystone-data/research/v1/`. Use this if you want the HQ you saw locally. |

Set `BRANCH` in step 1. Do not deploy `main` until the dashboard + research
code is merged there.

---

## 1. Set variables (every session)

```sh
export PROJECT_ID=microdrive-dev
export REGION=us-east1
export AR=waystone
export DOMAIN=dash.arqflo.ai
export BUCKET=waystone-data
export CLUSTER=md-dev
export BRANCH=cursor/massive-sentiment-gate-2a31   # or restore-dashboard-pr4

export TAG=$(date +%Y%m%d-%H%M)
export API_IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$AR/waystone-arena:$TAG
export FRONTEND_IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$AR/waystone-frontend:$TAG

gcloud config set project "$PROJECT_ID"
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"
```

---

## 2. Get the code

```sh
rm -rf ~/waystone-v3-deploy
git clone -b "$BRANCH" https://github.com/vaishnavi4068/waystone-v3.git ~/waystone-v3-deploy
cd ~/waystone-v3-deploy
```

---

## 3. Build + push the API image

```sh
gcloud builds submit --tag "$API_IMAGE" .
```

---

## 4. Build + push the frontend image

`NEXT_PUBLIC_API_BASE` is baked in at build time. Rebuild the frontend if
`$DOMAIN` changes.

```sh
gcloud builds submit frontend --config=frontend/cloudbuild.yaml \
  --substitutions=_API_BASE="https://$DOMAIN",_IMAGE="$FRONTEND_IMAGE"
```

---

## 5. Roll the Kubernetes deployments

If this stack already exists (it does on `md-dev`):

```sh
kubectl set image deployment/waystone-dash-api api=$API_IMAGE -n waystone-dash
kubectl set image deployment/waystone-dash-ui frontend=$FRONTEND_IMAGE -n waystone-dash
```

`set image` does **not** change env. Do step 6 even on a redeploy if Daily or
Strategies still look staged or return 500.

First time on a new cluster only — apply the manifest (namespace, PVC, ingress,
cert already exist on `md-dev`; `kubectl apply` updates in place):

```sh
sed -e "s|__IMAGE__|$API_IMAGE|g" \
    -e "s|__FRONTEND_IMAGE__|$FRONTEND_IMAGE|g" \
    -e "s|__DASH_DOMAIN__|$DOMAIN|g" \
    -e "s|__IBKR_BUCKET__|$BUCKET|g" \
    deploy/k8s/dashboard.yaml | kubectl apply -f -
```

On older `dashboard.yaml` (empty bucket + `IBKR_STAGED=1`), skip the
`__IBKR_BUCKET__` line and use step 6 to set env.

Do **not** apply `arena.yaml`, `trader.yaml`, or `secret-provider.yaml`.

---

## 6. Wire `gs://waystone-data` (the missing developer steps)

Without this, `/api/health` can be fine while every page that reads IBKR or
research returns **API error 500**.

### 6a. API env — real bucket, not staged preview

```sh
kubectl -n waystone-dash set env deploy/waystone-dash-api \
  IBKR_REPORTS_BUCKET="$BUCKET" \
  IBKR_STAGED=0 \
  IBKR_REPORTS_LOCAL_DIR-
```

Leave `IBKR_REPORTS_LOCAL_DIR` unset. That path is for a Mac checkout only.

Do **not** mount a service-account JSON into the pod.

### 6b. Workload Identity (once; safe to re-run)

Developer step 7 was only the annotate. You need all four:

```sh
# A. GCP SA
gcloud iam service-accounts create waystone-dash \
  --display-name="waystone dashboard" || true

# B. K8s SA may impersonate that GCP SA
gcloud iam service-accounts add-iam-policy-binding \
  "waystone-dash@$PROJECT_ID.iam.gserviceaccount.com" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:$PROJECT_ID.svc.id.goog[waystone-dash/waystone-dash]"

# C. Annotate the Kubernetes SA (developer step 7)
kubectl -n waystone-dash annotate serviceaccount waystone-dash \
  iam.gke.io/gcp-service-account="waystone-dash@$PROJECT_ID.iam.gserviceaccount.com" \
  --overwrite

# D. Read objects in the bucket
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:waystone-dash@$PROJECT_ID.iam.gserviceaccount.com" \
  --role=roles/storage.objectViewer
```

Pods only pick up a new SA annotation after a restart:

```sh
kubectl -n waystone-dash rollout restart deploy/waystone-dash-api
kubectl -n waystone-dash rollout status deploy/waystone-dash-api
kubectl -n waystone-dash rollout status deploy/waystone-dash-ui
```

Skip A–D on later deploys if they already succeeded. Always keep 6a + a restart
if you changed env.

---

## 7. Verify

```sh
kubectl get pods -n waystone-dash
kubectl logs deployment/waystone-dash-api -n waystone-dash --tail=20
curl -sS "https://$DOMAIN/api/health"
```

Expect `{"ok":true}` (or similar). Then confirm GCS from inside the API pod:

```sh
kubectl -n waystone-dash exec deploy/waystone-dash-api -- env | grep -E 'IBKR_|GOOGLE_'

# Expect: IBKR_REPORTS_BUCKET=waystone-data  IBKR_STAGED=0
# No GOOGLE_APPLICATION_CREDENTIALS (WI, not a key file)

kubectl -n waystone-dash exec deploy/waystone-dash-api -- \
  python -c "from google.cloud import storage; print([b.name for b in storage.Client().list_blobs('waystone-data', prefix='research/v1/', max_results=8)])"
```

Browser:

1. Open https://dash.arqflo.ai/
2. Sign in (Mark / `mark1234` if this stack still uses the default roster)
3. Daily / Options KPIs should show **published IBKR dumps**, not the
   “STAGED DATA” week of 10 Aug 2026
4. `/strategies` exists only if you deployed `cursor/massive-sentiment-gate-2a31`
   (or later). First list can take 20–30s (GCS listing)

---

## Checklist

| Step | Needed |
|------|--------|
| 1–5 Build + `set image` | Every deploy |
| 6a Env `BUCKET` + `STAGED=0` | If pods still have empty bucket or `IBKR_STAGED=1` |
| 6b A–D Workload Identity | Once per project (or if 500s persist) |
| Restart API | After annotate or env change |
| New frontend image | If `DOMAIN` or UI code changed |

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `/api/health` OK, other pages 500 | WI missing (6b) or env still staged/empty (6a); restart API |
| STAGED DATA banner / date `2026-08-14` | `IBKR_STAGED=1` or no `_SUCCESS` objects in the bucket |
| `/strategies` 404 | Wrong branch (`restore-dashboard-pr4` has no research UI) |
| Pod crash-loop, exec format error | Image built on a Mac (arm64). Rebuild with Cloud Build |
| Frontend calls localhost / wrong host | Frontend image baked with the wrong `_API_BASE` — rebuild step 4 |
| TLS not ready | Wait on `kubectl -n waystone-dash describe managedcertificate waystone-dash-cert` |

---

## Local vs this deploy

Local `./scripts/run-dashboard-local.sh` uses
`GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/waystone-data.json` on the Mac.

GKE must **not** use that file. The pod identity is
`waystone-dash@microdrive-dev` via Workload Identity, reading the **same**
bucket the Mac already published to (`research-publish` / IBKR export).
