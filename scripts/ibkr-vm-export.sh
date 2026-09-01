#!/usr/bin/env bash
# Manual or cron (6pm ET): collect from TWS and publish the daily dump to GCS.
# Usage on the VM:
#   export IBKR_REPORTS_BUCKET=your-bucket
#   /root/waystone-v3/scripts/ibkr-vm-export.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${IB_HOST:=127.0.0.1}"
: "${IB_PORT:=4001}"
: "${IB_CLIENT_ID:=99}"
: "${IBKR_LEDGER_DIR:=/root/IBKR_MONTH/reports/ledger}"

if [[ -z "${IBKR_REPORTS_BUCKET:-}" ]]; then
  echo "set IBKR_REPORTS_BUCKET to the GCS bucket" >&2
  exit 1
fi

export IB_HOST IB_PORT IB_CLIENT_ID IBKR_LEDGER_DIR IBKR_REPORTS_BUCKET
exec uv run waystone3 ibkr-export
