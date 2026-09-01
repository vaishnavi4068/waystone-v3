#!/usr/bin/env bash
# Manual or cron: pull TWS session fills into the local execId ledger.
# Usage on the VM (after cloning this repo and setting env):
#   /root/waystone-v3/scripts/ibkr-vm-collect.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${IB_HOST:=127.0.0.1}"
: "${IB_PORT:=4001}"
: "${IB_CLIENT_ID:=99}"
: "${IBKR_LEDGER_DIR:=/root/IBKR_MONTH/reports/ledger}"

export IB_HOST IB_PORT IB_CLIENT_ID IBKR_LEDGER_DIR
exec uv run waystone3 ibkr-collect
