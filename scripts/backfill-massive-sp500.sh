#!/usr/bin/env bash
# Backfill Massive/Polygon news for the full S&P 500 (~503 names) and score daily sentiment.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi
cd "$ROOT/waystone_backtests"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
START="${MASSIVE_NEWS_START:-2021-01-01}"
LOG="${MASSIVE_BACKFILL_LOG:-/tmp/massive-sp500-backfill.log}"

echo "== refresh S&P 500 list" | tee "$LOG"
"$PY" tools/refresh_sp500.py | tee -a "$LOG"

echo "== polygon-news --sp500 start=$START (this takes a while)" | tee -a "$LOG"
"$PY" ml/sentiment/fetch_free_sentiment.py polygon-news --sp500 --start "$START" 2>&1 | tee -a "$LOG"

echo "== score Massive LLM insights -> daily sentiment" | tee -a "$LOG"
"$PY" ml/sentiment/finbert_score.py --sp500 --scorer massive --source-filter massive 2>&1 | tee -a "$LOG"

echo "== classify events" | tee -a "$LOG"
"$PY" ml/sentiment/event_classifier.py --sp500 2>&1 | tee -a "$LOG"

echo "== done. log: $LOG" | tee -a "$LOG"
