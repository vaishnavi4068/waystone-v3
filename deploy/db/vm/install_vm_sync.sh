#!/usr/bin/env bash
# Installs the log -> GCS sync on the trading VM. Run as root on the VM, or from
# a Mac with: deploy/db/bootstrap_gcp.sh vm
#
# The VM only copies folders. It never parses, never talks to the database, and
# runs at idle CPU/IO priority so the trading process is unaffected.
#   paper:    every hour at :25, Mon-Fri (New York time)
#   backtest: 16:20 and 17:20, Mon-Fri (the 17:20 run only catches a late replay)
set -euo pipefail

BUCKET="${BUCKET:-waystone-data}"
ES_PAPER_DIR="${ES_PAPER_DIR:-/root/ES_ALGO/v221_logs}"
NQ_PAPER_DIR="${NQ_PAPER_DIR:-/root/NQ_FUTURE/v221_logs}"
R2_PAPER_DIR="${R2_PAPER_DIR:-/root/R2_MNQ_ALGO/R2_MNQ/v221_logs}"
S5_PAPER_DIR="${S5_PAPER_DIR:-}"
BACKTEST_DIR="${BACKTEST_DIR:-/root/BACK_TEST_DAILY}"
TZ_NAME="America/New_York"
PAPER_CALENDAR="Mon..Fri *-*-* *:25:00 $TZ_NAME"
BACKTEST_CALENDAR="Mon..Fri *-*-* 16,17:20:00 $TZ_NAME"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required." >&2; exit 1; }
GCLOUD="$(command -v gcloud || true)"
[ -n "$GCLOUD" ] || { echo "gcloud not found on this VM." >&2; exit 1; }
systemd-analyze calendar "$PAPER_CALENDAR" >/dev/null 2>&1 \
    || { echo "This systemd does not support time zones in OnCalendar (needs v235+)." >&2; exit 1; }

PAPER_SOURCES=""
add_source() {
    local code="$1" dir="$2"
    [ -n "$dir" ] || return 0
    if [ -d "$dir" ]; then
        PAPER_SOURCES="$PAPER_SOURCES $code=$dir"
        echo "paper: $dir -> gs://$BUCKET/raw/paper/$code/"
    else
        echo "WARNING: $dir not found; $code not synced (re-run with the right path)" >&2
    fi
}
add_source es_v221 "$ES_PAPER_DIR"
add_source nq_v221 "$NQ_PAPER_DIR"
add_source r2_mnq "$R2_PAPER_DIR"
add_source s5_options "$S5_PAPER_DIR"
[ -d "$BACKTEST_DIR" ] && echo "backtest: $BACKTEST_DIR -> gs://$BUCKET/raw/backtest/" \
    || echo "WARNING: $BACKTEST_DIR not found; backtest sync will skip until it exists" >&2

echo "Checking this VM can write to gs://$BUCKET ..."
# gcloud caches access tokens for up to an hour; a token minted before an
# access-scope change still carries the old scopes, so start from a fresh one.
rm -f "${HOME:-/root}/.config/gcloud/access_tokens.db"
if ! write_err="$(printf 'ok\n' | "$GCLOUD" storage cp - "gs://$BUCKET/raw/.vm-write-test" 2>&1)"; then
    scopes="$(curl -s -H 'Metadata-Flavor: Google' \
        http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes || true)"
    echo "ERROR: this VM cannot write to gs://$BUCKET. Nothing was installed." >&2
    echo "gcloud said: $write_err" >&2
    echo "gcloud account: $("$GCLOUD" auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" >&2
    echo "VM access scopes:" >&2
    printf '%s\n' "$scopes" | sed 's/^/  /' >&2
    case "$scopes" in
        *cloud-platform*|*devstorage.full_control*) ;;
        *devstorage.read_write*)
            echo "Storage scope is already Read Write. Hierarchical-namespace buckets may also need the" >&2
            echo "cloud-platform scope, which opens every API to the VM's service account roles." >&2 ;;
        *) echo "Fix: stop the VM, set Storage access to Read Write, start it, then re-run this installer." >&2 ;;
    esac
    exit 1
fi
echo "OK"

cat >/etc/waystone-sync.conf <<EOF
BUCKET="$BUCKET"
GCLOUD="$GCLOUD"
PAPER_SOURCES="${PAPER_SOURCES# }"
BACKTEST_DIR="$BACKTEST_DIR"
EOF

cat >/usr/local/bin/waystone-sync <<'EOF'
#!/usr/bin/env bash
# Copies log folders to GCS. Never deletes anything in the bucket.
set -uo pipefail
MODE="${1:?usage: waystone-sync paper|backtest}"
. /etc/waystone-sync.conf
exec 9>"/run/waystone-sync-$MODE.lock"
flock -n 9 || { echo "previous $MODE sync still running; skipping"; exit 0; }

# Live SQLite files can be mid-write, so they are never copied. The patterns
# must not start with '^': gcloud reads a leading ^...^ as a list delimiter.
# "\.db([.-].*)?" also covers SQLite sidecars and renamed copies like v221_futures.db.old_2026-09-24.
PAPER_EXCLUDE='(.*/)?(state(/.*)?|__pycache__/.*|[^/]*\.db([.-][^/]*)?|[^/]*\.(zip|py|pyc|tmp|swp))$'
BACKTEST_EXCLUDE='(.*/)?(__pycache__/.*|[^/]*\.db([.-][^/]*)?|[^/]*\.(zip|csv|py|pyc|tmp|swp))$'
rc=0
sync_dir() {
    local src="$1" dst="$2" exclude="$3"
    if [ ! -d "$src" ]; then
        echo "skip: $src not found"
        return
    fi
    echo "$(date -Is) $src -> $dst"
    "$GCLOUD" storage rsync --recursive --exclude="$exclude" "$src" "$dst" || rc=1
}

case "$MODE" in
    paper)
        for entry in $PAPER_SOURCES; do
            sync_dir "${entry#*=}" "gs://$BUCKET/raw/paper/${entry%%=*}/" "$PAPER_EXCLUDE"
        done ;;
    backtest)
        sync_dir "$BACKTEST_DIR" "gs://$BUCKET/raw/backtest/" "$BACKTEST_EXCLUDE" ;;
    *) echo "unknown mode $MODE" >&2; exit 2 ;;
esac
exit $rc
EOF
chmod 755 /usr/local/bin/waystone-sync

write_units() {
    local mode="$1" calendar="$2"
    cat >"/etc/systemd/system/waystone-sync-$mode.service" <<EOF
[Unit]
Description=Copy $mode logs to gs://$BUCKET
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=HOME=/root
ExecStart=/usr/local/bin/waystone-sync $mode
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=20min
EOF
    cat >"/etc/systemd/system/waystone-sync-$mode.timer" <<EOF
[Unit]
Description=Schedule for waystone-sync-$mode

[Timer]
OnCalendar=$calendar
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
}
write_units paper "$PAPER_CALENDAR"
write_units backtest "$BACKTEST_CALENDAR"

systemctl daemon-reload
systemctl enable --now waystone-sync-paper.timer waystone-sync-backtest.timer

echo
echo "Test run (first run uploads all existing history):"
systemctl start waystone-sync-paper.service || true
systemctl start waystone-sync-backtest.service || true
journalctl -u waystone-sync-paper.service -u waystone-sync-backtest.service -n 20 --no-pager || true
echo
systemctl list-timers 'waystone-sync-*' --no-pager
echo
echo "Logs:      journalctl -u waystone-sync-paper -u waystone-sync-backtest"
echo "Run now:   sudo systemctl start waystone-sync-paper"
echo "Uninstall: sudo systemctl disable --now waystone-sync-paper.timer waystone-sync-backtest.timer"
