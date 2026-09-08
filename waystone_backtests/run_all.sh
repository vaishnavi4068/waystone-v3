#!/usr/bin/env bash
# Runs every strategy.  `./run_all.sh --synthetic` needs no data (mechanics check);
# `./run_all.sh` runs on whatever CSVs exist under data/ and skips the ones that are missing data.
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
MODE="${1:-}"
S=strategies
run() { echo; echo "### $*"; $PY "$@" $MODE 2>&1 | grep -vE "RuntimeWarning|c /= stddev" || true; }

run $S/01_mean_reversion/backtest.py --mode bb
run $S/01_mean_reversion/backtest.py --mode nr7
run $S/02_gex_dealer_gamma/backtest.py --mode gap_fade
run $S/02_gex_dealer_gamma/backtest.py --mode wall_revert
run $S/03_vol_carry_put_spreads/backtest.py
run $S/04_calendar_effects/backtest.py --mode tom
run $S/04_calendar_effects/backtest.py --mode fomc
run $S/04_calendar_effects/backtest.py --mode opex
run $S/05_orderflow_cvd_mnq/backtest.py
run $S/06_sector_rotation/backtest.py
run $S/07_breadth_regime/backtest.py
run $S/08_pead_implied_move/backtest.py

echo; echo "results/ now holds metrics.json, trades.csv and equity.csv per strategy."
