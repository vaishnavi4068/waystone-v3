# Waystone Arena — GKE Deployment Runbook (Platform Engineer)

Hosts the team trading workspace as a remote MCP server with TLS, keys in **GCP Secret
Manager**, live Polygon data, and **real execution on one shared Alpaca paper account**.
Five members operate the same account via Claude; tools are attributed to the caller.
**Alpaca paper** (no real money) — going live is a deliberate later step.

---

## 1. What you're deploying (endpoint inventory)

**One public host, fanned out by path to three backends.** The MCP server and the read-only
API run as **two containers in one pod** (so they share the single ReadWriteOnce SQLite
volume); the frontend is its own Deployment. Two images: the Python image (Arena + API) and
the Next.js frontend image.

| K8s object | Count | Notes |
|---|---|---|
| Deployment `waystone-arena` | 1 | 1 replica, `Recreate`; **2 containers** — `arena` (MCP, :9100) + `api` (dashboard, :9200) sharing the PVC |
| Deployment `waystone-frontend` | 1 | Next.js UI (:3000), stateless |
| Service `waystone-arena` / `waystone-api` / `waystone-frontend` | 3 | ClusterIP + NEG, one per backend port |
| Ingress `waystone-arena` | 1 | external HTTPS, GKE ManagedCertificate, path routing |
| PersistentVolumeClaim | 1 | 1Gi, `/data/arena.db` (auto-provisioned) |

**Public routes on the single host (e.g. `https://arena.example.com`):**

| Route | Backend | Auth |
|---|---|---|
| `/mcp` | Arena MCP server | per-user `Authorization: Bearer <token>` |
| `/api/*` | dashboard API | per-user bearer (except `/api/health`) |
| `/*` | dashboard frontend | open page; calls the API with the player's token |

So players add **`https://<host>/mcp`** in Claude, and open **`https://<host>/`** in a browser
for the read-only dashboard (signing in with the same token).

---

## 2. Prerequisites
- `gcloud`, `kubectl`, authenticated (`gcloud auth login`).
- A domain you control.
- `POLYGON_API_KEY`, `ANTHROPIC_API_KEY` in hand.
- Run from repo root: `cd .../waystone/waystone-v3`.

```sh
export PROJECT=your-gcp-project
export REGION=us-central1
export CLUSTER=waystone-cluster
export REPO=waystone
export DOMAIN=arena.example.com
export IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/waystone-arena:$(date +%Y%m%d-%H%M)"
gcloud config set project "$PROJECT"
gcloud services enable container.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com
```

---

## 3. Store ALL secrets in Secret Manager (source of truth)

Five infra secrets + four player tokens. Create the infra ones now:

```sh
printf '%s' "$POLYGON_API_KEY"        | gcloud secrets create polygon-api-key       --data-file=-
printf '%s' "$ANTHROPIC_API_KEY"      | gcloud secrets create anthropic-api-key     --data-file=-
printf '%s' "$(openssl rand -hex 24)" | gcloud secrets create waystone-admin-token  --data-file=-
```

Player tokens are created **after seeding** (Step 8) — one secret per user:
`arena-user-mark`, `arena-user-manoj`, `arena-user-brent`, `arena-user-akash`,
`arena-user-kole`.

### How the pod reads them — pick one

**Option A — native Secret Manager access (recommended).** GKE Secret Manager add-on +
Secrets Store CSI driver mounts the secrets and syncs them into the env the app reads.
Requires Workload Identity (on by default in Autopilot) and an IAM grant:

```sh
# Enable the Secret Manager add-on / CSI driver on the cluster (confirm the flag for your
# GKE version; on recent GKE this is `--enable-secret-manager`).
gcloud container clusters update "$CLUSTER" --region "$REGION" --enable-secret-manager

# Grant the Arena's Kubernetes SA access to each secret via Workload Identity.
PROJECT_NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
for S in polygon-api-key anthropic-api-key waystone-admin-token; do
  gcloud secrets add-iam-policy-binding "$S" \
    --role=roles/secretmanager.secretAccessor \
    --member="principal://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$PROJECT.svc.id.goog/subject/ns/waystone-arena/sa/waystone-arena"
done
```

Then apply [deploy/k8s/secret-provider.yaml](k8s/secret-provider.yaml) (a `SecretProviderClass`
that mounts the three secrets and **syncs them into the `waystone-arena-secrets` k8s Secret**
the Deployment already `envFrom`s). Add the CSI volume to the Deployment (snippet at the
bottom of that file). Nothing else changes — the app still reads plain env vars.

**Option B — sync at deploy (simplest, zero app/infra change).** Pull from Secret Manager and
create the k8s Secret directly. Re-run + `rollout restart` to rotate.

```sh
kubectl create namespace waystone-arena
kubectl -n waystone-arena create secret generic waystone-arena-secrets \
  --from-literal=POLYGON_API_KEY="$(gcloud secrets versions access latest --secret=polygon-api-key)" \
  --from-literal=ALPACA_API_KEY="$(gcloud secrets versions access latest --secret=alpaca-api-key)" \
  --from-literal=ALPACA_API_SECRET="$(gcloud secrets versions access latest --secret=alpaca-api-secret)" \
  --from-literal=ALPACA_PAPER="true" \
  --from-literal=ANTHROPIC_API_KEY="$(gcloud secrets versions access latest --secret=anthropic-api-key)" \
  --from-literal=WAYSTONE_ADMIN_TOKEN="$(gcloud secrets versions access latest --secret=waystone-admin-token)"
```

**This is the shared team account.** With `ALPACA_API_KEY`/`ALPACA_API_SECRET` set, `run_cycle`
places **real orders on one shared Alpaca paper account**; without them it falls back to the
in-process simulator. Keep `ALPACA_PAPER=true` — going live (real money) is a deliberate later
step, not a flag flip. Add the Alpaca secrets to Secret Manager alongside the others:
```sh
printf '%s' "$ALPACA_API_KEY"    | gcloud secrets create alpaca-api-key    --data-file=-
printf '%s' "$ALPACA_API_SECRET" | gcloud secrets create alpaca-api-secret --data-file=-
```

Either way, the Deployment consumes `waystone-arena-secrets` via `envFrom` — unchanged.

> **Prefer a Console click-through?** See [deploy/CONSOLE_DEPLOY.md](CONSOLE_DEPLOY.md) — the
> same deploy done from the GCP Console UI (with Cloud Shell only where the UI can't help).

### Use Claude via Vertex AI instead of an Anthropic API key (keyless)

Claude-on-GCP runs through **Vertex AI**, which authenticates with **GCP IAM, not an API
key**. So you don't store an Anthropic key at all — you grant the pod's identity access.

```sh
# 1) Run the pod as the waystone-arena KSA (set serviceAccountName on the Deployment —
#    already present if you applied secret-provider.yaml; otherwise add it).
# 2) Grant that identity Vertex access via Workload Identity:
PROJECT_NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT" \
  --role=roles/aiplatform.user \
  --member="principal://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/$PROJECT.svc.id.goog/subject/ns/waystone-arena/sa/waystone-arena"

# 3) Point the app at Vertex (non-secret config — set as env on the Deployment):
kubectl -n waystone-arena set env deploy/waystone-arena \
  WAYSTONE_LLM_PROVIDER=vertex \
  VERTEX_REGION=us-east5 \
  VERTEX_PROJECT="$PROJECT" \
  WAYSTONE_AGENT_MODEL="<opus model id from Vertex Model Garden>" \
  WAYSTONE_SCORER_MODEL="<haiku model id from Vertex Model Garden>"
```

With `WAYSTONE_LLM_PROVIDER=vertex` you can **omit the `ANTHROPIC_API_KEY` secret entirely**.
Confirm the exact model IDs in your region's Vertex Model Garden (newest models reach the
first-party API before Vertex). The image already includes the Vertex client
(`anthropic[vertex]`). Note: the Agent-OS agents use a first-party structured-output feature
that may need adjustment on Vertex; the sentiment scorer (tool-use) works on Vertex as-is.

---

## 4. Build & push BOTH images (use Cloud Build — amd64)

Building locally on Apple Silicon yields an arm64 image that crash-loops on GKE
(`exec format error`). Cloud Build produces amd64 and needs no local Docker. Build two
images — the Python backend (Arena + API) and the Next.js frontend:

```sh
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" || true

# Backend (Arena MCP + dashboard API) — from repo root
gcloud builds submit --tag "$IMAGE" .

# Frontend — NEXT_PUBLIC_API_BASE is baked at build; point it at the public host so the
# browser calls https://$DOMAIN/api/*  (routed to the API by the Ingress).
FRONTEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/waystone-frontend:$(date +%Y%m%d-%H%M)"
gcloud builds submit frontend --config=frontend/cloudbuild.yaml \
  --substitutions=_API_BASE="https://$DOMAIN",_IMAGE="$FRONTEND_IMAGE"
# (Local Docker alternative: docker buildx build --platform linux/amd64 \
#   --build-arg NEXT_PUBLIC_API_BASE="https://$DOMAIN" -t "$FRONTEND_IMAGE" --push frontend)
```

---

## 5. Cluster (skip if you have one)

```sh
gcloud container clusters create-auto "$CLUSTER" --region "$REGION"   # Autopilot: WI on by default
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"
```

---

## 6. Networking — expose the endpoint with TLS

```sh
gcloud compute addresses create waystone-arena-ip --global
gcloud compute addresses describe waystone-arena-ip --global --format='value(address)'
# Create an A record:  arena.example.com -> <that IP>
```

The Ingress ([k8s/ingress.yaml](k8s/ingress.yaml)) creates an external L7 LB + a
ManagedCertificate. TLS provisions automatically once DNS resolves to the IP (10–30 min).

---

## 7. Deploy

```sh
kubectl create namespace waystone-arena 2>/dev/null || true
# (do Step 3 secret creation now if using Option B)

sed "s#__IMAGE__#$IMAGE#g"                deploy/k8s/arena.yaml    | kubectl apply -f -
sed "s#__FRONTEND_IMAGE__#$FRONTEND_IMAGE#g" deploy/k8s/frontend.yaml | kubectl apply -f -
sed "s#__ARENA_DOMAIN__#$DOMAIN#g"        deploy/k8s/ingress.yaml  | kubectl apply -f -

kubectl -n waystone-arena rollout status deploy/waystone-arena      # arena + api containers
kubectl -n waystone-arena rollout status deploy/waystone-frontend
```

The Ingress fans out by path: `/mcp` → Arena, `/api/*` → API, `/*` → frontend.

---

## 8. Seed the fixed players + store their tokens in Secret Manager

The server loads players from the DB **at startup**, so seed → restart → store tokens.

```sh
# 1) Seed (writes the five users; default passwords are mark1234, manoj1234, …).
#    -c arena: the pod now has two containers (arena + api); seed via arena.
#    The API also creates these users on startup if the DB is empty.
kubectl -n waystone-arena exec deploy/waystone-arena -c arena -- \
  uv run waystone3 arena-seed --players "Mark,Manoj,Brent,Akash,Kole"

# 2) Reload so BOTH the MCP server and the API pick up the new players (each loads at start).
kubectl -n waystone-arena rollout restart deploy/waystone-arena
kubectl -n waystone-arena rollout status deploy/waystone-arena

# 3) Store each printed token in Secret Manager (replace the values with the printed tokens):
printf '%s' "<mark-token>"  | gcloud secrets create arena-user-mark  --data-file=-
printf '%s' "<manoj-token>" | gcloud secrets create arena-user-manoj --data-file=-
printf '%s' "<brent-token>" | gcloud secrets create arena-user-brent --data-file=-
printf '%s' "<akash-token>" | gcloud secrets create arena-user-akash --data-file=-
printf '%s' "<kole-token>"  | gcloud secrets create arena-user-kole  --data-file=-
```

Each player gets a **unique dashboard password** (username = their member name) and an
**MCP bearer token**. Both are per-user, never shared, sent only over TLS. Hand each
user theirs over a private channel.

---

## 9. How players use it

- **Dashboard (read-only):** open `https://$DOMAIN/` and sign in with your **member name
  + unique password** — the shared account, positions, orders, the shared strategy, team,
  activity log, signals, charts, backtests, news. See [docs/UI.md](../docs/UI.md).
- **Claude (operate the account):** add the remote MCP server `https://$DOMAIN/mcp` with
  header `Authorization: Bearer <token>` in **Claude Code / Desktop**. Tools:
  `set_strategy` / `get_strategy`, `run_cycle` (submits real orders to the shared Alpaca
  paper account), `account` / `positions` / `orders`, `backtest`, `halt` / `resume`,
  `activity`. The **claude.ai web** connector needs OAuth (not built) — see
  [CLAUDE_CONNECTOR.md](CLAUDE_CONNECTOR.md).

Dashboard login uses name + password; Claude still uses the per-member bearer token.
Never share either credential; send only over TLS.

---

## 10. Verify

```sh
curl -s https://$DOMAIN/api/health                              # -> {"ok":true}  (dashboard API)
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/       # -> 200  (frontend)
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/mcp    # -> 401  (MCP, no token)
TOKEN=$(gcloud secrets versions access latest --secret=arena-user-manoj)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  https://$DOMAIN/api/me                                        # -> 200  (per-user dashboard data)
kubectl -n waystone-arena logs deploy/waystone-arena -c api -f  # API logs (or -c arena for MCP)
```

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `exec format error`, CrashLoop | arm64 image — rebuild with Cloud Build (Step 4) |
| `ImagePullBackOff` | `__IMAGE__` not substituted, or AR perms |
| Player 401 after seeding | missed the `rollout restart` (Step 8) |
| Cert stuck `Provisioning` | DNS A-record not resolving to the static IP yet (`dig $DOMAIN`) |
| `POLYGON_API_KEY is not set` in logs | secret not populated / Option A IAM grant missing → `rollout restart` after fixing |
| Reset competition | `kubectl -n waystone-arena delete pvc waystone-arena-data` + `rollout restart` |

## Rotating a secret
Option B: re-create the k8s secret from Secret Manager + `rollout restart`. Option A: add a new
secret version; the CSI driver picks it up (or `rollout restart` to force).
