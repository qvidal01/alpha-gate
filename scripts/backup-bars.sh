#!/usr/bin/env bash
# Back up the captured bars off-host, and verify the append-only invariant while
# doing it.
#
#   ./scripts/backup-bars.sh            # back up + verify
#   ./scripts/backup-bars.sh --verify   # verify only, copy nothing
#
# WHY THIS EXISTS
#
# The captured bars are the single irreplaceable asset in this project. They are
# gitignored on purpose (they are observations, not source), they cannot be
# backfilled without destroying the forward-only guarantee the whole harness
# rests on, and until now they existed in exactly ONE place: /aidata on the capture host
# — a VM on the hypervisor, the node with the known failing DIMM. A host loss would not
# have damaged the experiment, it would have ENDED it, with no recovery short of
# restarting the 90-day clock.
#
# THE BACKUP IS ALSO A TAMPER DETECTOR
#
# Bars are append-only, so any legitimate new version of a file must contain the
# previous version as an exact PREFIX. That makes verification cheap and strong:
# compare the first N bytes of the live file against the whole of the backup. If
# they differ, history was rewritten — which `audit_bars` can only catch when the
# rewrite breaks monotonicity or fabricates a timestamp. A careful rewrite that
# preserves ordering would pass the audit and fail this.
#
# So this is not merely a copy. Restoring FROM the backup would be backfilling,
# and is not what it is for; it exists so the record survives the host and so
# rewrites are provable.
#
# Exit: 0 OK · 1 INVARIANT VIOLATED (investigate before trusting any verdict)
#       3 UNKNOWN (could not reach the destination — not a pass)
set -uo pipefail

SRC="${ALPHA_GATE_DATA:-/aidata/projects/alpha-gate/data/bars}"
DEST_HOST="${ALPHA_GATE_BACKUP_HOST:-user@backup-host}"
DEST_DIR="${ALPHA_GATE_BACKUP_DIR:-/path/on/backup-host}"
VERIFY_ONLY=0
[ "${1:-}" = "--verify" ] && VERIFY_ONLY=1

die3() { echo "UNKNOWN: $*" >&2; exit 3; }

# The NAS is a Synology: it ships `sha256sum` and has no `shasum`, while macOS
# and this Ubuntu host have `shasum`. Hardcoding either one makes every remote
# hash come back empty, every comparison fail, and the tool report corruption
# that is not there. Resolve per host instead of assuming.
REMOTE_SHA='f(){ if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d" " -f1; }; f'
local_sha() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

[ -d "$SRC" ] || die3 "source $SRC does not exist — is this the capture host?"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$DEST_HOST" "mkdir -p '$DEST_DIR'" 2>/dev/null \
  || die3 "cannot reach $DEST_HOST — backup NOT taken, do not report success"

# Transport failures and invariant violations are counted SEPARATELY and must
# stay that way. The first version lumped a failed copy in with "append-only
# violated", which told the operator their record had been tampered with when
# the NAS had merely refused an scp. A backup tool that cries wolf about data
# integrity gets ignored, and then it is not a backup tool.
violations=0
errors=0
checked=0

for f in "$SRC"/*.jsonl; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  checked=$((checked + 1))

  # Size of the existing backup, if any. Missing backup => first run for this file.
  bsize=$(ssh -o BatchMode=yes "$DEST_HOST" \
            "[ -f '$DEST_DIR/$name' ] && wc -c < '$DEST_DIR/$name' || echo 0" 2>/dev/null | tr -d ' ')
  bsize=${bsize:-0}
  lsize=$(wc -c < "$f" | tr -d ' ')

  if [ "$bsize" -gt 0 ]; then
    if [ "$lsize" -lt "$bsize" ]; then
      echo "✗ $name: live file SHRANK ($lsize < $bsize bytes) — append-only violated"
      violations=$((violations + 1))
      continue
    fi
    # The prefix check: first bsize bytes of live must equal the whole backup.
    if command -v sha256sum >/dev/null 2>&1; then
      live_prefix=$(head -c "$bsize" "$f" | sha256sum | cut -d' ' -f1)
    else
      live_prefix=$(head -c "$bsize" "$f" | shasum -a 256 | cut -d' ' -f1)
    fi
    backup_hash=$(ssh -o BatchMode=yes "$DEST_HOST" \
                    "$REMOTE_SHA '$DEST_DIR/$name'" 2>/dev/null)
    if [ "$live_prefix" != "$backup_hash" ]; then
      echo "✗ $name: HISTORY REWRITTEN — the backed-up bytes are no longer a prefix"
      echo "    live[0:$bsize] $live_prefix"
      echo "    backup         $backup_hash"
      echo "    A rewrite that preserves ordering passes audit_bars and fails here."
      violations=$((violations + 1))
      continue
    fi
    grew=$((lsize - bsize))
  else
    grew=$lsize
  fi

  if [ "$VERIFY_ONLY" -eq 0 ]; then
    # `cat | ssh 'cat >'` rather than scp: this NAS is a Synology, whose SFTP
    # subsystem refuses an absolute destination path that plainly exists —
    # "dest open ...: No such file or directory" against a directory you just
    # listed. The pipe never touches SFTP and works.
    if ! ssh -o BatchMode=yes "$DEST_HOST" "cat > '$DEST_DIR/$name'" < "$f" 2>/dev/null; then
      echo "! $name: copy FAILED (transport, not integrity)"
      errors=$((errors + 1)); continue
    fi
    # Prove the copy landed intact rather than assuming a zero exit means so.
    src_hash=$(local_sha "$f")
    dst_hash=$(ssh -o BatchMode=yes "$DEST_HOST" "$REMOTE_SHA '$DEST_DIR/$name'" 2>/dev/null)
    if [ "$src_hash" != "$dst_hash" ]; then
      echo "! $name: copy landed CORRUPT (hash mismatch) — backup not usable"
      errors=$((errors + 1)); continue
    fi
    echo "✓ $name: +${grew}B (now ${lsize}B, verified)"
  else
    echo "✓ $name: append-only intact (+${grew}B since last backup)"
  fi
done

[ "$checked" -gt 0 ] || die3 "no .jsonl files found in $SRC — capture may never have run"

echo
if [ "$violations" -gt 0 ]; then
  echo "FAILED — $violations file(s) violated the APPEND-ONLY INVARIANT."
  echo "History was rewritten. Do NOT trust a verdict from this record until"
  echo "you know why. This is an integrity finding, not a transport problem."
  exit 1
fi
if [ "$errors" -gt 0 ]; then
  # Not exit 1: nothing is wrong with the DATA, we just failed to protect it.
  # Not exit 0 either — silence here is how "the backup was running all along"
  # turns out to be false at the worst possible moment.
  echo "UNKNOWN — integrity holds, but $errors file(s) could not be backed up."
  echo "The record is intact and unprotected. Fix transport and re-run."
  exit 3
fi
echo "OK — $checked file(s) backed up and verified, append-only invariant holds"
exit 0
