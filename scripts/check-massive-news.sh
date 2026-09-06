#!/usr/bin/env bash
# Verify Massive/Polygon Stocks news + LLM insights on the current API key.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/waystone_backtests"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
if [[ -z "${MASSIVE_API_KEY:-}" && -z "${POLYGON_API_KEY:-}" ]]; then
  echo "ERROR: export MASSIVE_API_KEY or POLYGON_API_KEY" >&2
  exit 1
fi
exec "$PY" ml/sentiment/fetch_free_sentiment.py probe-news "$@"
