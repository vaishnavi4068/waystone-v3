#!/usr/bin/env bash
# Start a Cursor self-hosted worker on the Mac Studio so Cloud Agents can run here.
# https://cursor.com/docs/cloud-agent/bring-your-own-machine
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${CURSOR_WORKER_NAME:-mac-studio}"
if ! command -v agent >/dev/null 2>&1; then
  echo "Install the Cursor CLI first: curl https://cursor.com/install -fsS | bash" >&2
  echo "Then: agent login" >&2
  exit 1
fi
exec agent worker start --name "$NAME" --worker-dir "$ROOT"
