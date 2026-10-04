#!/usr/bin/env bash
# One-time setup of the Waystone HQ database on GCP. Safe to re-run: every step
# checks what already exists and only creates what is missing.
#
# Usage (from a Mac or Linux shell, after `gcloud auth login`):
#   deploy/db/bootstrap_gcp.sh            # everything: infra, then tables
#   deploy/db/bootstrap_gcp.sh infra      # APIs, network, Cloud SQL, secrets, bucket, IAM
#   deploy/db/bootstrap_gcp.sh sql        # create/upgrade schemas, tables, views, seed data
#   deploy/db/bootstrap_gcp.sh vm         # install the hourly GCS sync on the trading VM
#   deploy/db/bootstrap_gcp.sh summary    # print the connection details to hand over
#
# Every setting below can be overridden from the environment, e.g.
#   VM_ZONE=us-east4-c S5_PAPER_DIR=/root/S5_ALGO/logs deploy/db/bootstrap_gcp.sh vm
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-microdrive-dev}"
REGION="${REGION:-us-east1}"
GKE_CLUSTER="${GKE_CLUSTER:-md-dev}"
GKE_LOCATION="${GKE_LOCATION:-us-east1}"
NETWORK="${NETWORK:-}"
INSTANCE="${INSTANCE:-waystone-hq}"
DB_NAME="${DB_NAME:-waystone}"
DB_TIER="${DB_TIER:-db-custom-2-8192}"
BUCKET="${BUCKET:-waystone-data}"
STRATEGIES="${STRATEGIES:-es_v221 nq_v221 r2_mnq s5_options}"
DASH_SA="${DASH_SA:-waystone-dash@${PROJECT_ID}.iam.gserviceaccount.com}"
LOADER_SA="${LOADER_SA:-}"
VM_NAME="${VM_NAME:-waystone}"
VM_ZONE="${VM_ZONE:-}"
ES_PAPER_DIR="${ES_PAPER_DIR:-/root/ES_ALGO/v221_logs}"
NQ_PAPER_DIR="${NQ_PAPER_DIR:-/root/NQ_FUTURE/v221_logs}"
R2_PAPER_DIR="${R2_PAPER_DIR:-/root/R2_MNQ_ALGO/R2_MNQ/v221_logs}"
S5_PAPER_DIR="${S5_PAPER_DIR:-}"
BACKTEST_DIR="${BACKTEST_DIR:-/root/BACK_TEST_DAILY}"
PROXY_PORT="${PROXY_PORT:-6543}"
KEEP_PUBLIC_IP="${KEEP_PUBLIC_IP:-false}"
ENABLE_VERSIONING="${ENABLE_VERSIONING:-true}"

SECRET_PG="waystone-db-postgres-password"
SECRET_LOAD="waystone-db-load-password"
SECRET_READ="waystone-db-read-password"
VM_SA=""
HERE="$(cd "$(dirname "$0")" && pwd)"
SQL_DIR="$HERE/sql"
PROXY_PID=""
HNS=false

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    WARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
g()    { gcloud --project "$PROJECT_ID" --quiet "$@"; }

cleanup() {
    if [ -n "$PROXY_PID" ]; then kill "$PROXY_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

preflight() {
    log "Checking local tools and gcloud login"
    command -v gcloud >/dev/null || die "gcloud not found. Install: https://cloud.google.com/sdk/docs/install"
    command -v openssl >/dev/null || die "openssl not found"
    local account
    account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
    [ -n "$account" ] || die "No active gcloud account. Run: gcloud auth login"
    info "gcloud account: $account"
    info "project: $PROJECT_ID   region: $REGION"
    g projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null \
        || die "Cannot access project $PROJECT_ID with $account"
}

preflight_sql() {
    command -v psql >/dev/null || die "psql not found. On a Mac: brew install libpq && brew link --force libpq"
    local major
    major="$(psql --version | sed -E 's/[^0-9]*([0-9]+).*/\1/')"
    [ "$major" -ge 15 ] || die "psql $major is too old; need 15+ (brew upgrade libpq)"
    command -v pg_isready >/dev/null || die "pg_isready not found (it ships with libpq)"
    command -v cloud-sql-proxy >/dev/null \
        || die "cloud-sql-proxy not found. Install: gcloud components install cloud-sql-proxy  (or: brew install cloud-sql-proxy)"
}

enable_apis() {
    log "Enabling APIs"
    g services enable sqladmin.googleapis.com servicenetworking.googleapis.com \
        secretmanager.googleapis.com compute.googleapis.com iam.googleapis.com \
        run.googleapis.com cloudscheduler.googleapis.com
}

detect_network() {
    if [ -z "$NETWORK" ]; then
        NETWORK="$(g container clusters describe "$GKE_CLUSTER" --location "$GKE_LOCATION" \
            --format='value(network)' 2>/dev/null || true)"
    fi
    [ -n "$NETWORK" ] || die "Could not read the VPC of GKE cluster $GKE_CLUSTER; set NETWORK=<vpc-name>"
    info "VPC network (same as GKE $GKE_CLUSTER): $NETWORK"
}

private_services_access() {
    log "Private services access on VPC $NETWORK (lets GKE and Cloud Run reach Cloud SQL on a private IP)"
    local ranges
    ranges="$(g services vpc-peerings list --network="$NETWORK" \
        --service=servicenetworking.googleapis.com --format='value(reservedPeeringRanges)' 2>/dev/null || true)"
    if [ -n "$ranges" ]; then
        info "already connected (ranges: $ranges)"
        return
    fi
    if ! g compute addresses describe waystone-psa-range --global >/dev/null 2>&1; then
        g compute addresses create waystone-psa-range --global --purpose=VPC_PEERING \
            --prefix-length=16 --network="$NETWORK"
    fi
    g services vpc-peerings connect --service=servicenetworking.googleapis.com \
        --ranges=waystone-psa-range --network="$NETWORK"
}

# Prints the secret value, creating it with a random password when missing.
ensure_secret() {
    local name="$1" value
    if g secrets describe "$name" >/dev/null 2>&1; then
        g secrets versions access latest --secret="$name"
        return
    fi
    value="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)"
    printf '%s' "$value" | g secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
    printf '%s' "$value"
}

secrets() {
    log "Database passwords in Secret Manager"
    PG_PW="$(ensure_secret "$SECRET_PG")"
    LOAD_PW="$(ensure_secret "$SECRET_LOAD")"
    READ_PW="$(ensure_secret "$SECRET_READ")"
    info "$SECRET_PG, $SECRET_LOAD, $SECRET_READ"
}

load_secrets() {
    PG_PW="$(g secrets versions access latest --secret="$SECRET_PG")" || die "missing secret $SECRET_PG; run: $0 infra"
    LOAD_PW="$(g secrets versions access latest --secret="$SECRET_LOAD")" || die "missing secret $SECRET_LOAD"
    READ_PW="$(g secrets versions access latest --secret="$SECRET_READ")" || die "missing secret $SECRET_READ"
}

cloud_sql() {
    log "Cloud SQL instance $INSTANCE (Postgres 16, $DB_TIER, $REGION)"
    if g sql instances describe "$INSTANCE" >/dev/null 2>&1; then
        info "instance already exists"
    else
        info "creating; this takes 10-15 minutes"
        g sql instances create "$INSTANCE" \
            --database-version=POSTGRES_16 --edition=enterprise --tier="$DB_TIER" \
            --region="$REGION" --availability-type=zonal \
            --storage-type=SSD --storage-size=20GB --storage-auto-increase \
            --network="projects/$PROJECT_ID/global/networks/$NETWORK" --assign-ip \
            --backup-start-time=07:00 --enable-point-in-time-recovery --retained-backups-count=14 \
            --maintenance-window-day=SUN --maintenance-window-hour=8 \
            --deletion-protection --root-password="$PG_PW" || true
    fi
    local state
    for _ in $(seq 1 90); do
        state="$(g sql instances describe "$INSTANCE" --format='value(state)' 2>/dev/null || true)"
        [ "$state" = "RUNNABLE" ] && break
        info "instance state: ${state:-PENDING} (waiting)"
        sleep 20
    done
    [ "$state" = "RUNNABLE" ] || die "instance $INSTANCE did not become RUNNABLE"
    g sql users set-password postgres --instance="$INSTANCE" --password="$PG_PW" >/dev/null
    if g sql databases describe "$DB_NAME" --instance="$INSTANCE" >/dev/null 2>&1; then
        info "database $DB_NAME already exists"
    else
        g sql databases create "$DB_NAME" --instance="$INSTANCE"
    fi
}

bucket_is_hns() {
    g storage buckets describe "gs://$BUCKET" --format=json \
        | tr -d ' \n' | grep -qi '"hierarchical_namespace":{"enabled":true}'
}

ensure_folder() {
    local prefix="$1"
    if [ "$HNS" = "true" ]; then
        g storage folders create --recursive "gs://$BUCKET/$prefix" >/dev/null 2>&1 || true
    else
        printf '' | g storage cp - "gs://$BUCKET/${prefix}.keep" >/dev/null 2>&1
    fi
    info "gs://$BUCKET/$prefix"
}

bucket() {
    log "Bucket gs://$BUCKET: recovery settings and folders"
    g storage buckets describe "gs://$BUCKET" --format='value(name)' >/dev/null \
        || die "bucket gs://$BUCKET not found"
    HNS=false
    local err=""
    if bucket_is_hns; then
        HNS=true
    elif [ "$ENABLE_VERSIONING" = "true" ]; then
        if ! err="$(g storage buckets update "gs://$BUCKET" --versioning 2>&1 >/dev/null)"; then
            case "$err" in
                *ierarchical*) HNS=true ;;
                *) printf '%s\n' "$err" >&2; die "could not enable versioning on gs://$BUCKET" ;;
            esac
        fi
    fi
    if [ "$HNS" = "true" ]; then
        info "hierarchical namespace bucket: object versioning is not supported, so it is skipped"
        local soft_delete
        soft_delete="$(g storage buckets describe "gs://$BUCKET" --format=json \
            | tr -d ' \n' | sed -n 's/.*"retentionDurationSeconds":"\{0,1\}\([0-9]*\).*/\1/p')"
        if [ -n "$soft_delete" ] && [ "$soft_delete" != "0" ]; then
            info "soft delete keeps overwritten or deleted logs for $((soft_delete / 86400)) days"
        else
            warn "could not confirm soft delete. Check Bucket > Protection > Soft delete policy is on (it keeps overwritten logs recoverable)."
        fi
    elif [ "$ENABLE_VERSIONING" = "true" ]; then
        info "object versioning on (overwritten or deleted logs stay recoverable)"
        if g storage buckets describe "gs://$BUCKET" --format=json | grep -q '"lifecycle'; then
            warn "bucket already has lifecycle rules; left unchanged. Add 'delete noncurrent versions after 90 days' in the console if wanted."
        else
            local rules
            rules="$(mktemp)"
            printf '%s\n' '{"rule":[{"action":{"type":"Delete"},"condition":{"isLive":false,"daysSinceNoncurrentTime":90}}]}' >"$rules"
            g storage buckets update "gs://$BUCKET" --lifecycle-file="$rules" >/dev/null
            rm -f "$rules"
            info "lifecycle: old versions deleted after 90 days"
        fi
    fi
    local prefix code
    for code in $STRATEGIES; do
        ensure_folder "raw/paper/$code/"
    done
    for prefix in raw/backtest/ raw/comparison/; do
        ensure_folder "$prefix"
    done
}

vm_lookup() {
    if [ -z "$VM_ZONE" ]; then
        VM_ZONE="$(g compute instances list --filter="name=$VM_NAME" --format='value(zone.basename())' | head -1)"
    fi
    [ -n "$VM_ZONE" ] || die "VM $VM_NAME not found in $PROJECT_ID; set VM_NAME / VM_ZONE"
}

# Sets VM_SA and, unless overridden, LOADER_SA (the loader reuses the VM's account).
resolve_service_accounts() {
    vm_lookup
    VM_SA="$(g compute instances describe "$VM_NAME" --zone="$VM_ZONE" --format='value(serviceAccounts[0].email)')"
    [ -n "$VM_SA" ] || die "VM $VM_NAME has no service account attached"
    LOADER_SA="${LOADER_SA:-$VM_SA}"
}

iam() {
    log "Service accounts and permissions (no new accounts are created)"
    resolve_service_accounts
    g iam service-accounts describe "$LOADER_SA" >/dev/null 2>&1 \
        || die "loader service account $LOADER_SA not found"

    # objectUser (not objectCreator): rsync must overwrite the day's growing log file.
    g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$VM_SA" \
        --role=roles/storage.objectUser >/dev/null
    info "VM $VM_NAME ($VM_ZONE) runs as $VM_SA: bucket read/write"

    g projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$LOADER_SA" \
        --role=roles/cloudsql.client --condition=None >/dev/null
    if [ "$LOADER_SA" != "$VM_SA" ]; then
        g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$LOADER_SA" \
            --role=roles/storage.objectViewer >/dev/null
    fi
    g secrets add-iam-policy-binding "$SECRET_LOAD" --member="serviceAccount:$LOADER_SA" \
        --role=roles/secretmanager.secretAccessor >/dev/null
    info "loader (Cloud Run Job) runs as $LOADER_SA: Cloud SQL client, bucket access, load password"

    if g iam service-accounts describe "$DASH_SA" >/dev/null 2>&1; then
        g projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DASH_SA" \
            --role=roles/cloudsql.client --condition=None >/dev/null
        g secrets add-iam-policy-binding "$SECRET_READ" --member="serviceAccount:$DASH_SA" \
            --role=roles/secretmanager.secretAccessor >/dev/null
        info "$DASH_SA: Cloud SQL client, read-only password"
    else
        warn "dashboard service account $DASH_SA not found; skipped (set DASH_SA)"
    fi

    local scopes
    scopes="$(g compute instances describe "$VM_NAME" --zone="$VM_ZONE" --format='value(serviceAccounts[0].scopes)')"
    case "$scopes" in
        *cloud-platform*|*devstorage.read_write*|*devstorage.full_control*) info "VM API scopes allow Storage writes" ;;
        *) warn "VM API scopes do not allow Storage writes. Stop the VM outside trading hours, then Edit > Access scopes > Storage: Read Write (or 'Allow full access'). Current: $scopes" ;;
    esac
}

ensure_public_ip_for_setup() {
    local ips
    ips="$(g sql instances describe "$INSTANCE" --format='value(settings.ipConfiguration.ipv4Enabled)')"
    if [ "$ips" != "True" ]; then
        info "enabling a temporary public IP (no authorized networks; only the IAM-authenticated proxy can connect)"
        g sql instances patch "$INSTANCE" --assign-ip >/dev/null
    fi
}

start_proxy() {
    local conn
    conn="$(g sql instances describe "$INSTANCE" --format='value(connectionName)')"
    cloud-sql-proxy --port "$PROXY_PORT" --gcloud-auth "$conn" >/tmp/waystone-sql-proxy.log 2>&1 &
    PROXY_PID=$!
    for _ in $(seq 1 30); do
        if pg_isready -h 127.0.0.1 -p "$PROXY_PORT" >/dev/null 2>&1; then
            info "proxy ready on 127.0.0.1:$PROXY_PORT ($conn)"
            return
        fi
        sleep 1
    done
    cat /tmp/waystone-sql-proxy.log >&2
    die "Cloud SQL proxy did not start"
}

psql_as() {
    local user="$1" pw="$2"
    shift 2
    PGPASSWORD="$pw" psql "host=127.0.0.1 port=$PROXY_PORT dbname=$DB_NAME user=$user sslmode=disable" \
        -v ON_ERROR_STOP=1 -q "$@"
}

run_sql() {
    log "Creating users, schemas, tables, views and seed data in $DB_NAME"
    preflight_sql
    load_secrets
    ensure_public_ip_for_setup
    start_proxy
    WAYSTONE_DB="$DB_NAME" WAYSTONE_LOAD_PW="$LOAD_PW" WAYSTONE_READ_PW="$READ_PW" \
        psql_as postgres "$PG_PW" -f "$SQL_DIR/01_roles.sql"
    info "01_roles.sql (as postgres)"
    local f out
    out="$(mktemp)"
    for f in 02_schemas.sql 03_ref.sql 04_raw_ops.sql 05_core.sql 06_kpi.sql 07_api_views.sql 08_seed.sql; do
        if ! psql_as waystone_load "$LOAD_PW" -f "$SQL_DIR/$f" >"$out" 2>&1; then
            cat "$out" >&2
            die "$f failed"
        fi
        grep -v 'already exists, skipping' "$out" || true
        info "$f (as waystone_load)"
    done
    rm -f "$out"
    log "Check"
    psql_as waystone_read "$READ_PW" -c "SELECT strategy_code, display_name, asset_class FROM api.v_strategy ORDER BY 1" \
        -c "SELECT table_schema AS schema, count(*) AS tables_and_views FROM information_schema.tables WHERE table_schema IN ('ref','raw','ops','core','kpi','api') GROUP BY 1 ORDER BY 1"
    kill "$PROXY_PID" 2>/dev/null || true
    PROXY_PID=""
    if [ "$KEEP_PUBLIC_IP" != "true" ]; then
        info "removing the public IP again (private IP only)"
        g sql instances patch "$INSTANCE" --no-assign-ip >/dev/null
    fi
}

install_vm_sync() {
    log "Installing the GCS sync timers on VM $VM_NAME"
    vm_lookup
    g compute ssh "$VM_NAME" --zone="$VM_ZONE" --command \
        "sudo env BUCKET='$BUCKET' ES_PAPER_DIR='$ES_PAPER_DIR' NQ_PAPER_DIR='$NQ_PAPER_DIR' R2_PAPER_DIR='$R2_PAPER_DIR' S5_PAPER_DIR='$S5_PAPER_DIR' BACKTEST_DIR='$BACKTEST_DIR' bash -s" \
        <"$HERE/vm/install_vm_sync.sh"
}

summary() {
    log "Hand-over details (no passwords; those stay in Secret Manager)"
    resolve_service_accounts
    local out="$HERE/waystone-db-connection.txt"
    {
        echo "project:              $PROJECT_ID"
        echo "region:               $REGION"
        echo "instance:             $INSTANCE"
        echo "connection name:      $(g sql instances describe "$INSTANCE" --format='value(connectionName)')"
        g sql instances describe "$INSTANCE" --format='yaml(ipAddresses)'
        echo "database:             $DB_NAME"
        echo "load user / secret:   waystone_load / $SECRET_LOAD"
        echo "read user / secret:   waystone_read / $SECRET_READ"
        echo "admin user / secret:  postgres / $SECRET_PG"
        echo "VM / loader SA:       $VM_SA / $LOADER_SA"
        echo "dashboard SA:         $DASH_SA"
        echo "bucket:               gs://$BUCKET/raw/{paper/<strategy>,backtest,comparison}/"
        echo "vpc network:          ${NETWORK:-(run infra to detect)}"
    } | tee "$out"
    info "saved to $out"
}

main() {
    local step="${1:-all}"
    preflight
    case "$step" in
        all)
            enable_apis; detect_network; private_services_access; secrets; cloud_sql; bucket; iam
            run_sql; summary ;;
        infra)
            enable_apis; detect_network; private_services_access; secrets; cloud_sql; bucket; iam; summary ;;
        sql) run_sql ;;
        vm) install_vm_sync ;;
        summary) summary ;;
        *) die "unknown step '$step' (use: all | infra | sql | vm | summary)" ;;
    esac
    log "Done: $step"
}

main "$@"
