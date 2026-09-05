#!/usr/bin/env bash
# Redeploy the IBKR dashboard (waystone-dash namespace) with the latest code.
#
# Run this from Google Cloud Shell (it already has gcloud + kubectl authenticated).
# It rebuilds both images from source and rolls them into the existing GKE deployment.
#
# IMPORTANT: as of 2026-09-05, the dashboard code (PR #4: DASHBOARD_GKE.md,
# deploy/k8s/dashboard.yaml, src/waystone3/ibkr/*, frontend IBKR pages) is NOT
# on origin/main -- it was reverted/lost from main at some point. It currently
# only exists on the `restore-dashboard-pr4` branch (which also carries two
# bugfixes: QueryGate's optional children prop, and the Next.js HOSTNAME=0.0.0.0
# fix needed for the standalone server to bind correctly inside a pod).
# Someone should open a PR to properly merge restore-dashboard-pr4 back into
# main -- until then, this script deploys from that branch on purpose.
set -euo pipefail

# ---- 1. Fixed values for this stack (edit only if the project/cluster changes) ----
export PROJECT=microdrive-dev
export REGION=us-east1
export CLUSTER=md-dev
export REPO=waystone
export DOMAIN=dash.arqflo.ai
export BUCKET=waystone-data
export BRANCH=restore-dashboard-pr4   # see note above -- switch to "main" once merged

# ---- 2. Point gcloud/kubectl at the right project + cluster ----
gcloud config set project "$PROJECT"
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION"

# ---- 3. Fresh checkout of the branch that has the dashboard code ----
# (re-clone each time so this script is copy-pasteable and idempotent;
#  delete any stale checkout from a previous run first)
rm -rf ~/waystone-v3-deploy
git clone https://github.com/vaishnavi4068/waystone-v3.git ~/waystone-v3-deploy
cd ~/waystone-v3-deploy
git checkout "$BRANCH"

# ---- 4. Build + push both images via Cloud Build (amd64 -- required for GKE) ----
export TAG=$(date +%Y%m%d-%H%M)
export IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/waystone-arena:$TAG"
export FRONTEND_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/waystone-frontend:$TAG"

echo "Building backend image: $IMAGE"
gcloud builds submit --tag "$IMAGE" .

echo "Building frontend image: $FRONTEND_IMAGE"
gcloud builds submit frontend --config=frontend/cloudbuild.yaml \
  --substitutions=_API_BASE="https://$DOMAIN",_IMAGE="$FRONTEND_IMAGE"

# ---- 5. Apply the manifest with the new image tags baked in ----
# (namespace/secret/PVC/ingress/cert already exist from the first deploy --
#  kubectl apply just updates what changed, safely, without recreating them)
sed -e "s|__IMAGE__|$IMAGE|g" \
    -e "s|__FRONTEND_IMAGE__|$FRONTEND_IMAGE|g" \
    -e "s|__DASH_DOMAIN__|$DOMAIN|g" \
    -e "s|__IBKR_BUCKET__|$BUCKET|g" \
    deploy/k8s/dashboard.yaml | kubectl apply -f -

# ---- 6. Wait for both rollouts to finish before declaring success ----
kubectl -n waystone-dash rollout status deploy/waystone-dash-api
kubectl -n waystone-dash rollout status deploy/waystone-dash-ui

# ---- 7. Quick sanity check ----
echo "Checking API health..."
curl -sS "https://$DOMAIN/api/health" && echo
echo "Deployed. Open https://$DOMAIN/ to verify in a browser."
