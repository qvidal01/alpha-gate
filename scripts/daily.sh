#!/usr/bin/env bash
# Hourly capture + daily status for alpha-gate.
#
#   scripts/daily.sh capture     # every hour, on the hour (cron/launchd)
#   scripts/daily.sh report      # once a day: status + provisional evaluate
#
# Exit: 0 ok · 1 error · 2 usage.
#
# WHY CAPTURE MUST RUN ON A SCHEDULE AND CANNOT BE BACKFILLED: capture.py
# refuses to write a bar more than ~1.5 intervals stale. If this cron dies for
# two days you get a two-day HOLE in the record, and status will show it. That
# is deliberate. A hole is information -- it tells you the run is compromised.
# A silent backfill would tell you nothing while quietly turning a forward-only
# test back into an ordinary backtest.
#
# Timezone note: America/Chicago per house standard, though nothing here is
# TZ-sensitive -- every timestamp written is UTC unix epoch.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
REPO=$(pwd)
export TZ="America/Chicago"

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3) || { echo "no python3" >&2; exit 1; }

LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y-%m).log"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$1" | tee -a "$LOG"; }

usage() { sed -n '2,20p' "$0"; exit 2; }
[ $# -eq 1 ] || usage

case "$1" in
  capture)
    out=$("$PY" -m alpha_gate.cli capture 2>&1)
    rc=$?
    log "capture rc=$rc"
    printf '%s\n' "$out" | sed 's/^/    /' >> "$LOG"
    if [ $rc -ne 0 ]; then
      log "CAPTURE FAILED — a gap is now forming in the forward-only record"
      "$PY" - <<'PYEOF' 2>/dev/null || true
import sys
sys.path.insert(0, "src")
try:
    from alpha_gate.notify import send
    send("alpha-gate capture failed",
         "A gap is forming in the forward-only record. "
         "Gaps cannot be backfilled; a long enough one invalidates the run.",
         cls="ticket", domain="alpha-gate")
except Exception:
    pass
PYEOF
      exit 1
    fi
    exit 0
    ;;

  report)
    log "daily report"
    "$PY" -m alpha_gate.cli status   2>&1 | tee -a "$LOG"
    echo | tee -a "$LOG"
    # evaluate returns 3 (UNKNOWN) while the run is still INCONCLUSIVE, which
    # is the expected state for the first 90 days. Do not treat it as an error.
    "$PY" -m alpha_gate.cli evaluate 2>&1 | tee -a "$LOG"
    exit 0
    ;;

  *) usage ;;
esac
