#!/usr/bin/env bash
# Daily free-sentiment collection for the ML sleeves (ML_ALGO.md §"Sentiment").
# News source: Massive/Polygon REST for all S&P 500 constituents (replaces Yahoo RSS).
#
#   ./scripts/sentiment-daily.sh            # fetch + sync
#   SENTIMENT_NO_SYNC=1 ./scripts/sentiment-daily.sh
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
[ -x "$PY" ] || PY=python3
export SEC_USER_AGENT="${SEC_USER_AGENT:-waystone-research research@waystone.local}"
START="${SENTIMENT_START:-$(date -u -d '-14 days' +%F 2>/dev/null || date -u -v-14d +%F)}"
F=ml/sentiment/fetch_free_sentiment.py
N=$("$PY" -c "from wsbt.data import load_symbol_list; print(len(load_symbol_list()))")

echo "== sentiment-daily $(date -u +%FT%TZ) sp500=$N start=$START"
"$PY" $F fng                                   || echo "fng failed"
"$PY" $F sec-8k --sp500 --start "$START"       || echo "sec-8k failed"
# GDELT: company names from sp500.csv when available
mapfile -t COMPANIES < <("$PY" -c "
import pandas as pd
from wsbt.data import DATA_DIR, load_symbol_list
p=DATA_DIR/'sp500.csv'
df=pd.read_csv(p)
syms=load_symbol_list()
name_col='company' if 'company' in df.columns else None
by={str(r['symbol']).upper(): (r.get('company') or r['symbol']) for _,r in df.iterrows()} if name_col else {}
for s in syms: print(by.get(s,s))
")
SYMS=$("$PY" -c "from wsbt.data import load_symbol_list; print(' '.join(load_symbol_list()))")
"$PY" $F gdelt --symbols $SYMS --company "${COMPANIES[@]}" --start "$START" || echo "gdelt failed"
MASSIVE_KEY="${MASSIVE_API_KEY:-${POLYGON_API_KEY:-}}"
if [ -n "$MASSIVE_KEY" ]; then
  export POLYGON_API_KEY="$MASSIVE_KEY"
  "$PY" $F probe-news --symbol AAPL --limit 1 >/tmp/massive-news-probe.json 2>&1 || echo "probe-news: $(cat /tmp/massive-news-probe.json 2>/dev/null | head -3)"
  "$PY" $F polygon-news --sp500 --start "$START" || echo "polygon-news failed"
  "$PY" ml/sentiment/finbert_score.py --sp500 --scorer massive --source-filter massive || echo "finbert_score failed"
  "$PY" ml/sentiment/event_classifier.py --sp500 || echo "event_classifier failed"
else
  echo "MASSIVE_API_KEY not set — skipping polygon-news + Massive sentiment scoring"
fi

if [ -z "${SENTIMENT_NO_SYNC:-}" ]; then
  (cd "$ROOT" && "$PY" -m waystone3.cli research-sentiment-sync --push) || echo "gcs sync skipped/failed"
fi
echo "== done $(date -u +%FT%TZ)"
