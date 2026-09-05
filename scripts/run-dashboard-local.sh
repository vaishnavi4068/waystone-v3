#!/usr/bin/env bash
# One-command local HQCapital dashboard (API :9200 + Next :3001).
# Preview uses staged sample week when IBKR_STAGED=1 (default here).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export WAYSTONE_DB="${WAYSTONE_DB:-$ROOT/arena.db}"
export WAYSTONE_ADMIN_TOKEN="${WAYSTONE_ADMIN_TOKEN:-dev-admin-token}"
export IBKR_STAGED="${IBKR_STAGED:-1}"
export IBKR_PAPER="${IBKR_PAPER:-true}"
export WAYSTONE_BROKER="${WAYSTONE_BROKER:-paper}"
if [[ -z "${IBKR_REPORTS_LOCAL_DIR:-}" && -d "$ROOT/reports/demo" ]]; then
  export IBKR_REPORTS_LOCAL_DIR="$ROOT/reports/demo"
fi

if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  (cd "$ROOT/frontend" && npm install)
fi

if [[ ! -f "$ROOT/frontend/.env.local" ]]; then
  printf '%s\n' "NEXT_PUBLIC_API_BASE=http://127.0.0.1:9200" > "$ROOT/frontend/.env.local"
fi

API_PID=""
cleanup() {
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Bind :: so http://localhost (IPv6) and 127.0.0.1 both work on Linux dual-stack.
uv run waystone3 api-serve --host :: --port 9200 &
API_PID=$!

ok=0
for _ in $(seq 1 50); do
  if curl -sf --max-time 1 http://127.0.0.1:9200/api/health >/dev/null \
    || curl -sf --max-time 1 -g "http://[::1]:9200/api/health" >/dev/null; then
    ok=1
    break
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "api-serve exited before becoming healthy" >&2
    exit 1
  fi
  sleep 0.2
done
if [[ "$ok" -ne 1 ]]; then
  echo "api-serve did not become healthy on :9200" >&2
  exit 1
fi

echo
echo "HQCapital local preview"
echo "  API  http://127.0.0.1:9200/api/health"
echo "  UI   http://127.0.0.1:3001"
echo "  also http://localhost:3001  (now listens on IPv6)"
echo "Sign in with a team username. Prefer 127.0.0.1 if a port-forward hangs."
echo

cd "$ROOT/frontend"
exec npm run dev
