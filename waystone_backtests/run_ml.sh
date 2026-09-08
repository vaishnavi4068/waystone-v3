#!/usr/bin/env bash
# Runs the ML / sentiment layer.  `./run_ml.sh --synthetic` needs no data (mechanics + controls, ~2-3 min);
# without the flag it runs on whatever is under data/ and stops at the first missing input.
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
MODE="${1:-}"
DASH="${KPI_DASHBOARD:-}"            # export KPI_DASHBOARD="/path/to/Options Strategy KPI Dashboard.html" to get dashboard.html per run
D=""; [ -n "$DASH" ] && D="--dashboard $DASH"
run() { echo; echo "### $*"; $PY "$@" 2>&1 | grep -vE "RuntimeWarning|c /= stddev|UserWarning" || true; }

if [ "$MODE" = "--synthetic" ]; then
  run ml/meta_label.py --primary vwap --synthetic --days 120 --n-symbols 20 --tune --no-sens $D
  run ml/meta_label.py --primary v221 --synthetic --n-splits 4 --tune --no-sens $D
  run ml/regime_hmm.py --synthetic --trades-csv results/ml_meta_vwap_syn/primary_trades.csv --gate-train-days 40 --gate-min-trades 10 --warmup 200
  run ml/direction_gbdt.py --synthetic $D
  run ml/sentiment/finbert_score.py --synthetic --symbols S00 S01 --days 200
  run ml/sentiment/event_classifier.py --symbols S00 S01
  run ml/sentiment/sentiment_backtest.py --mode shock --synthetic --plant 0.6 --grid --both-sides --name ml_sent_shock_syn_planted
  run ml/sentiment/sentiment_backtest.py --mode shock --synthetic --grid --both-sides
  run ml/sentiment/sentiment_backtest.py --mode event-filter --synthetic --trades-csv results/ml_meta_vwap_syn/primary_trades.csv
  run ml/sentiment/sentiment_backtest.py --mode macro --synthetic
else
  run ml/regime_hmm.py --symbol "${MKT:-SPY}" --vol-symbol "${VOL:-I:VIX}" --name mkt
  run ml/meta_label.py --primary v221 --symbol MNQ --vol-symbol "${VXN:-I:VXN}" --regime mkt --tune $D
  run ml/meta_label.py --primary vwap --n-symbols "${N:-45}" --vol-symbol "${VOL:-I:VIX}" --regime mkt --tune $D
  run ml/direction_gbdt.py --symbol "${MKT:-SPY}" --vol-symbol "${VOL:-I:VIX}" --regime mkt --tune $D
  run ml/sentiment/finbert_score.py --sp500 --scorer massive --source-filter massive
  run ml/sentiment/event_classifier.py --sp500
  SP500_N=$($PY -c "from wsbt.data import load_symbol_list; print(len(load_symbol_list()))")
  run ml/sentiment/sentiment_backtest.py --mode shock --n-symbols "$SP500_N" --grid --both-sides \
    --universe structural --max-positions 5 --name ml_sent_shock_massive $D
  run ml/sentiment/sentiment_backtest.py --mode macro --symbol "${MKT:-SPY}" $D
fi
echo; echo "results/ml_*/ hold metrics.json, trades.csv, equity.csv, kpi.json (+ dashboard.html when KPI_DASHBOARD is set); results/trial_log.csv is the trial log."
