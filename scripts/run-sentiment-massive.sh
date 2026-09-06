#!/usr/bin/env bash
# Score Massive news -> daily sentiment + events, run shock backtest, sync to GCS.
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
SYNC="${ROOT}/.venv/bin/waystone3"

echo "== pull sentiment from GCS (merge with local)"
"$SYNC" research-sentiment-sync --pull || echo "gcs pull skipped/failed"

echo "== score Massive news -> daily sentiment (S&P 500)"
"$PY" ml/sentiment/finbert_score.py --sp500 --scorer massive --source-filter massive

echo "== classify events (S&P 500)"
"$PY" ml/sentiment/event_classifier.py --sp500

N=$("$PY" -c "from wsbt.data import load_symbol_list; print(len(load_symbol_list()))")
echo "== shock backtest on $N S&P 500 names (Massive tone shock_z gate)"
"$PY" ml/sentiment/sentiment_backtest.py --mode shock --n-symbols "$N" --grid --both-sides --name ml_sent_shock_massive

echo "== push news + sentiment to GCS"
"$SYNC" research-sentiment-sync --push || echo "gcs push skipped/failed"

echo "== done -> results/ml_sent_shock_massive/"
