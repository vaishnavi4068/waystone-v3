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
echo "== build coverage.csv"
"$PY" - <<'PY'
from pathlib import Path
import pandas as pd
from wsbt.data import DATA_DIR, load_symbol_list, _safe_name
rows = []
for sym in load_symbol_list():
    p = DATA_DIR / "sentiment" / f"{_safe_name(sym)}_daily.csv"
    if not p.exists():
        rows.append({"symbol": sym, "days": 0, "shock_days_abs2": 0, "window_coverage_pct": 0.0, "src_sentiment_days": 0})
        continue
    d = pd.read_csv(p, parse_dates=["date"]).set_index("date")
    rows.append({"symbol": sym, "days": len(d), "first": str(d.index.min().date()), "last": str(d.index.max().date()),
                 "shock_days_abs2": int((d["shock_z"].abs() >= 2).sum()) if "shock_z" in d else 0,
                 "window_coverage_pct": round(100 * len(d) / max(1, len(pd.bdate_range(d.index.min(), d.index.max()))), 1),
                 "src_sentiment_days": int(d["score_src"].notna().sum()) if "score_src" in d else 0})
pd.DataFrame(rows).to_csv(DATA_DIR / "sentiment" / "coverage.csv", index=False)
print(f"coverage.csv: {len(rows)} symbols, {sum(r['shock_days_abs2'] for r in rows)} total |shock|>=2 days")
PY

echo "== shock backtest on $N S&P 500 names (structural universe + max_positions cap)"
"$PY" ml/sentiment/sentiment_backtest.py --mode shock --n-symbols "$N" --grid --both-sides \
  --universe structural --max-positions 5 --name ml_sent_shock_massive

echo "== push news + sentiment to GCS"
"$SYNC" research-sentiment-sync --push || echo "gcs push skipped/failed"

echo "== done -> results/ml_sent_shock_massive/"
