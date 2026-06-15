# Waystone Arena — Manual Deploy from the GCP Console

Click-through deploy using the GCP Console UI. Two steps genuinely need a terminal (building
the container image, and seeding players) — both run in **Cloud Shell**, the terminal built
into the Console (top-right `>_` icon), so you never leave the browser.

Throughout, replace: **PROJECT** (your project id), **REGION** (e.g. `us-central1`),
**DOMAIN** (e.g. `arena.example.com`).

---

## 1. Enable APIs
**Console → APIs & Services → Enable APIs and Services.** Enable each:
- Kubernetes Engine API
- Artifact Registry API
- Cloud Build API
- Secret Manager API
- Vertex AI API *(only if using Claude via Vertex)*

## 2. Create the Artifact Registry repo
**Console → Artifact Registry → Create Repository.**
- Name: `waystone` · Format: **Docker** · Mode: Standard · Region: **REGION** → Create.

## 3. Store secrets in Secret Manager
**Console → Security → Secret Manager → Create Secret.** Create these (one per secret, paste
the value as the secret value, leave defaults):
- `polygon-api-key` → your Polygon key
- `anthropic-api-key` → your Anthropic key *(skip if using Vertex)*
- `waystone-admin-token` → a long random string you generate
- (player token secrets `arena-user-manoj` … `arena-user-cole` come **after** seeding, Step 10)

## 4. Create the GKE cluster
**Console → Kubernetes Engine → Clusters → Create → "GKE Autopilot" → Configure.**
- Name: `waystone-cluster` · Region: **REGION** → Create. (Autopilot enables Workload
  Identity automatically.) Wait until the cluster is green (~5 min).

## 5. Build & push the image (Cloud Shell)
Open **Cloud Shell** (`>_` top-right). Upload the project (or `git clone` it), then build
**both** images (backend = Arena + dashboard API; frontend = the UI):
```sh
cd waystone-v3
PROJECT=$(gcloud config get-value project); REGION=us-central1; DOMAIN=arena.example.com
IMAGE="$REGION-docker.pkg.dev/$PROJECT/waystone/waystone-arena:v1"
FRONTEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT/waystone/waystone-frontend:v1"

gcloud builds submit --tag "$IMAGE" .
gcloud builds submit frontend --config=frontend/cloudbuild.yaml \
  --substitutions=_API_BASE="https://$DOMAIN",_IMAGE="$FRONTEND_IMAGE"
echo "$IMAGE"; echo "$FRONTEND_IMAGE"   # paste these into the manifests in Step 9
```
*(Cloud Build produces amd64 images — required for GKE. A local Mac `docker build` would
produce arm64 and crash-loop. The frontend bakes `NEXT_PUBLIC_API_BASE=https://$DOMAIN` so
the browser calls the public `/api/*` path.)*

## 6. Reserve a static IP
**Console → VPC network → IP addresses → Reserve external static address.**
- Name: `waystone-arena-ip` · Type: **Global** → Reserve. Copy the assigned IP.

## 7. Point DNS
In your DNS provider, create an **A record**: `DOMAIN` → the IP from Step 6. (TLS won't
provision until this resolves.)

## 8. (Optional) Grant Vertex AI access — keyless Claude
Skip if using the Anthropic API key. Otherwise the pod's identity needs Vertex access:
**Console → IAM & Admin → IAM → Grant Access.**
- New principal:
  `principal://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/PROJECT.svc.id.goog/subject/ns/waystone-arena/sa/waystone-arena`
  (PROJECT_NUMBER is on the Console home dashboard.)
- Role: **Vertex AI User** (`roles/aiplatform.user`) → Save.

Also grant that same principal **Secret Manager Secret Accessor** on each secret if you use
the CSI mount (Option A); for the simple path (Step 9 secret YAML) you can skip it.

## 9. Deploy the workload (GKE Console — paste YAML)
**Console → Kubernetes Engine → Workloads → Deploy → "YAML" tab.** Paste the manifests
below **with `__IMAGE__` and `__ARENA_DOMAIN__` replaced** by your values. You can paste them
one at a time (Deploy after each) or all together separated by `---`.

1. **Namespace + Secret** (paste real secret values, or use Secret Manager CSI instead):
   ```yaml
   apiVersion: v1
   kind: Namespace
   metadata: { name: waystone-arena }
   ---
   apiVersion: v1
   kind: Secret
   metadata: { name: waystone-arena-secrets, namespace: waystone-arena }
   type: Opaque
   stringData:
     POLYGON_API_KEY: "PASTE_OR_OMIT_IF_VERTEX"
     ANTHROPIC_API_KEY: "PASTE_OR_OMIT_IF_VERTEX"
     WAYSTONE_ADMIN_TOKEN: "PASTE_A_LONG_RANDOM_STRING"
   ```
   *(Cleaner: skip the literal secret and use the Secret Manager CSI driver —
   [k8s/secret-provider.yaml](k8s/secret-provider.yaml). The literal Secret is the fastest
   path for a one-day event.)*

2. **The app** — paste the contents of [k8s/arena.yaml](k8s/arena.yaml) with `__IMAGE__`
   replaced by your Step 5 image. (Add `serviceAccountName: waystone-arena` under
   `spec.template.spec` if you're using Vertex/CSI Workload Identity.)

3. **The public endpoint** — paste [k8s/ingress.yaml](k8s/ingress.yaml) with
   `__ARENA_DOMAIN__` replaced by **DOMAIN** (both places).

Watch **Workloads → waystone-arena** until the pod is green (Running, 1/1).

## 10. Seed the 5 players (Cloud Shell)
```sh
gcloud container clusters get-credentials waystone-cluster --region "$REGION"
kubectl -n waystone-arena exec deploy/waystone-arena -- \
  uv run waystone3 arena-seed --players "Manoj,Mark,Brent,Akash,Cole"
# copy each printed token
kubectl -n waystone-arena rollout restart deploy/waystone-arena   # reload players from DB
```
Then store each token in Secret Manager (Console → Secret Manager → Create Secret):
`arena-user-manoj` … `arena-user-cole`. Hand each player their token privately.

## 11. (Optional) Turn on Vertex for Claude
**Console → Kubernetes Engine → Workloads → waystone-arena → Edit → YAML**, add under the
container `env:` (or run in Cloud Shell `kubectl -n waystone-arena set env deploy/waystone-arena ...`):
```yaml
- { name: WAYSTONE_LLM_PROVIDER, value: "vertex" }
- { name: VERTEX_REGION, value: "us-east5" }
- { name: VERTEX_PROJECT, value: "PROJECT" }
- { name: WAYSTONE_AGENT_MODEL, value: "<opus model id from Vertex Model Garden>" }
- { name: WAYSTONE_SCORER_MODEL, value: "<haiku model id from Vertex Model Garden>" }
```

## 12. Verify
- **Workloads → waystone-arena**: 1/1 Running, no restarts.
- **Services & Ingress → waystone-arena**: note the Ingress; the **ManagedCertificate**
  shows `Provisioning` then `Active` (10–30 min after DNS resolves).
- In Cloud Shell:
  ```sh
  curl -s https://DOMAIN/healthz                                   # -> ok
  curl -s -o /dev/null -w '%{http_code}\n' https://DOMAIN/mcp      # -> 401 (gated)
  ```

## 13. Players connect from Claude
See [CLAUDE_CONNECTOR.md](CLAUDE_CONNECTOR.md). Each player adds `https://DOMAIN/mcp` in
Claude Code/Desktop with header `Authorization: Bearer <their token>`. (claude.ai *web*
connector needs OAuth — see the caveat there.)

---

### Console vs CLI — what needs Cloud Shell and why
| Step | Console UI? | Notes |
|---|---|---|
| APIs, Artifact Registry, Secret Manager, cluster, IAM, static IP | ✅ pure UI | |
| Build image (Step 5) | ⚠️ Cloud Shell | pure-UI build needs a connected Git repo + trigger; Cloud Shell is faster |
| Deploy manifests (Step 9) | ✅ Workloads → Deploy → YAML | paste the YAML |
| Seed players (Step 10) | ⚠️ Cloud Shell | `kubectl exec` into the pod |
| Verify / edit env (11–12) | ✅ UI | Workloads → Edit |
