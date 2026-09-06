#!/usr/bin/env bash
# One-command local HQCapital dashboard (API :9200 + Next :3001).
# Preview uses staged sample week when IBKR_STAGED=1 (default here).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load repo .env when present; GCS keys are often injected by the shell/Cloud Agent
# (they are not always checked into .env).
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

export WAYSTONE_DB="${WAYSTONE_DB:-$ROOT/arena.db}"
export WAYSTONE_ADMIN_TOKEN="${WAYSTONE_ADMIN_TOKEN:-dev-admin-token}"
# IBKR_STAGED=0 → read IBKR + research from GCS when IBKR_REPORTS_BUCKET is set.
export IBKR_STAGED="${IBKR_STAGED:-0}"
export IBKR_PAPER="${IBKR_PAPER:-true}"
export WAYSTONE_BROKER="${WAYSTONE_BROKER:-paper}"
if [[ -z "${IBKR_REPORTS_LOCAL_DIR:-}" && -d "$ROOT/reports/demo" ]]; then
  export IBKR_REPORTS_LOCAL_DIR="$ROOT/reports/demo"
fi
if [[ -z "${IBKR_REPORTS_BUCKET:-}" ]]; then
  echo "WARN: IBKR_REPORTS_BUCKET is unset — /strategies will show the built-in preview only." >&2
  echo "      Export GCS credentials (GOOGLE_APPLICATION_CREDENTIALS, IBKR_REPORTS_BUCKET) and retry." >&2
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

# API stays on IPv4. Next rewrites /api to 127.0.0.1:9200; the UI binds :: so
# browsers that resolve localhost to ::1 still load the page.
uv run waystone3 api-serve --host 0.0.0.0 --port 9200 &
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
echo "  UI   http://127.0.0.1:3001/strategies"
echo "  also http://localhost:3001/strategies"
if [[ "${CURSOR_AGENT:-}" == "1" ]]; then
  echo
  echo "Cloud Agent: 127.0.0.1 is inside the remote VM, not your Mac."
  echo "  1. In Cursor Desktop, open this agent run."
  echo "  2. Click the plug icon (top-right) → forward port 3001 (and 9200 if needed)."
  echo "  3. Open http://localhost:3001/strategies in your browser."
  echo "  Agent run: https://cursor.com/agents/${CURSOR_CONVERSATION_ID:-}"
fi
echo "Sign in: Mark / mark1234 (or your team username)."
echo

cd "$ROOT/frontend"
exec npm run dev
