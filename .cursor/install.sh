#!/usr/bin/env bash
# Idempotent bootstrap for the Waystone v3 dev environment (Python backend + Next.js UI).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- uv (Python package manager) -------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# --- Backend deps (from the committed lockfile) ----------------------------------
uv sync --frozen --extra alpaca

# --- Frontend deps ---------------------------------------------------------------
# Make node/npm available whether provided by nvm or already on PATH.
if [ -s "$HOME/.nvm/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.nvm/nvm.sh"
fi
( cd frontend && npm install )

# Point the UI at the local read-only API (idempotent).
[ -f frontend/.env.local ] || cp frontend/.env.example frontend/.env.local

# --- Seed a local dev workspace (SQLite: one member + a shared strategy) ----------
# Durable state so the dashboard is usable offline. Guarded so it runs only once.
mkdir -p .devdata
export WAYSTONE_DB="$REPO_ROOT/.devdata/arena.db"
export WAYSTONE_ADMIN_TOKEN="dev-admin-token"
if [ ! -f .devdata/arena.db ]; then
  uv run python .cursor/seed_dev.py
fi

echo "Setup complete. Dev login token:"
cat .devdata/token.txt 2>/dev/null || true
