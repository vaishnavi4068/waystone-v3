#!/usr/bin/env bash
# Daily free-sentiment collection for the ML sleeves (ML_ALGO.md §"Sentiment").
# The free sources only give history going forward, so this runs every day and the
# rows are merged idempotently, then mirrored to GCS so no machine is a single point of loss.
#
#   ./scripts/sentiment-daily.sh            # fetch + sync
#   SENTIMENT_NO_SYNC=1 ./scripts/sentiment-daily.sh
#
# Install (Mac, launchd):  cp scripts/com.waystone.sentiment.plist ~/Library/LaunchAgents/ && \
#                          launchctl load ~/Library/LaunchAgents/com.waystone.sentiment.plist
# Install (Linux, cron):   10 22 * * 1-5  cd /path/to/waystone-v3 && ./scripts/sentiment-daily.sh >> /tmp/sentiment.log 2>&1
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/waystone_backtests"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
export SEC_USER_AGENT="${SEC_USER_AGENT:-waystone-research research@waystone.local}"
LIST="${SENTIMENT_LIST:-data/sentiment_top.csv}"
SYMS=$(tail -n +2 "$LIST" | cut -d, -f1 | tr '\n' ' ')
# GDELT rejects phrases under 4 chars, so it gets the company column (3rd column, quoted names allowed)
mapfile -t COMPANIES < <("$PY" -c "import csv,sys; [print(r.get('company') or r['symbol']) for r in csv.DictReader(open(sys.argv[1]))]" "$LIST")
START="${SENTIMENT_START:-$(date -u -d '-14 days' +%F 2>/dev/null || date -u -v-14d +%F)}"
F=ml/sentiment/fetch_free_sentiment.py

echo "== sentiment-daily $(date -u +%FT%TZ) symbols=$(echo $SYMS | wc -w) start=$START"
"$PY" $F fng                                   || echo "fng failed"
"$PY" $F yahoo-rss --symbols $SYMS             || echo "yahoo-rss failed"
"$PY" $F sec-8k    --symbols $SYMS --start "$START" || echo "sec-8k failed"
"$PY" $F gdelt     --symbols $SYMS --company "${COMPANIES[@]}" --start "$START" || echo "gdelt failed"
# optional, needs POLYGON_API_KEY with a Stocks plan
if [ -n "${POLYGON_API_KEY:-}" ]; then "$PY" $F polygon-news --symbols $SYMS --start "$START" || echo "polygon-news failed"; fi

if [ -z "${SENTIMENT_NO_SYNC:-}" ]; then
  (cd "$ROOT" && "$PY" -m waystone3.cli research-sentiment-sync --push) || echo "gcs sync skipped/failed"
fi
echo "== done $(date -u +%FT%TZ)"
