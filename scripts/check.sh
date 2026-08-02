#!/usr/bin/env bash
# The gate for alpha-gate. Answers ONE question: can this tree's verdicts be
# trusted?
#
#   scripts/check.sh
#
# Exit: 0 PASS · 1 FAIL · 2 usage · 3 UNKNOWN.
# 3 is NEVER a pass. A missing tool means this gate cannot prove the tree is
# good, and silence must not look like success.
#
# WHY THIS REPO GETS A GATE: it exists to tell you whether a trading strategy
# is real. If the harness itself can be quietly edited -- a seal overwritten, a
# strategy tweaked after a bad week, order-placement code slipped in -- then
# every verdict it has ever issued becomes worthless, and worse, worthless in a
# direction that costs money. This gate protects the meaning of the output, not
# the style of the input.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
REPO=$(pwd)

[ $# -eq 0 ] || { sed -n '2,16p' "$0"; exit 2; }

pass=0; fail=0; unknown=0
ok()      { printf '\033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()     { printf '\033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
unproven(){ printf '\033[33m?\033[0m %s\n' "$1"; unknown=$((unknown+1)); }

PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)

# --- 0. dependencies -------------------------------------------------------- #
# Never check without resolved dependencies, or the gate reports the tool's
# absence as the repo's fault.
have() { command -v "$1" >/dev/null 2>&1; }
[ -x "$PY" ] || unproven "dependency missing: python3 — cannot parse or test"
have git    || unproven "dependency missing: git — cannot enumerate tracked files"
if ! "$PY" -c "import pytest" 2>/dev/null; then
  unproven "dependency missing: pytest — cannot run the honesty suite"
fi
if ! have gitleaks; then
  unproven "dependency missing: gitleaks — cannot prove the tree is secret-free"
fi
if [ "$unknown" -gt 0 ]; then
  printf '\nUNKNOWN — %d dependency check(s) unresolved. Not a pass.\n' "$unknown"
  exit 3
fi

# --- mutation guard --------------------------------------------------------- #
# A gate that rewrites a tracked file would let an unattended loop commit its
# own side effects under a green check.
before=$(git status --porcelain | shasum -a 256 | cut -d' ' -f1)

printf '\n── alpha-gate integrity ──────────────────────────────────────\n\n'

# --- 1. THE STRUCTURAL GUARANTEE: this package cannot trade ----------------- #
# The whole safety argument of this repo is that the capability is absent, not
# merely unused. If someone adds it, that must be a loud, deliberate, reviewed
# event -- never something discovered after an account is drained.
banned_hits=$(grep -rnE \
  "api/v3/order|place_order|create_order|new_order|submit_order|X-MEXC-APIKEY" \
  src/ 2>/dev/null | grep -vE "^\S+:\s*#|never|NEVER|must not|cannot|banned" || true)
if [ -n "$banned_hits" ]; then
  bad "ORDER-PLACEMENT CODE FOUND — this package must never be able to trade:"
  printf '%s\n' "$banned_hits" | sed 's/^/      /'
else
  ok "no order-placement capability anywhere in src/ (structural, not policy)"
fi

# Private-endpoint signing is the other half of the same guarantee.
if grep -rnE "hmac|hashlib\.sha256\(.*secret|signature=" src/ 2>/dev/null \
     | grep -v "prereg.py" | grep -q .; then
  bad "request-signing code found — a read-only harness needs no credentials"
else
  ok "no request signing outside prereg hashing — no credential path exists"
fi

# --- 2. every Python file parses -------------------------------------------- #
# Uses ast.parse, which writes nothing (compileall would create __pycache__ and
# trip the mutation guard).
#
# CAVEATS rule 12, 2026-08-01: covers UNTRACKED-but-not-ignored .py as well.
# `git ls-files` is blind to the newest file in the tree, and in a harness whose
# entire value is that its result cannot be quietly influenced, "the file nobody
# checked" is the wrong thing to have. Python runs what is on disk.
#
# Worth recording for the next audit: check 1 — the structural guarantee that
# this package cannot place an order — was ALREADY safe from this, because it
# greps the filesystem (`grep -rn ... src/`) rather than git. That is why the
# safety-critical half of this gate needed no change and this one did.
parse_out=$( { git ls-files '*.py'; git ls-files --others --exclude-standard '*.py'; } | sort -u | "$PY" -c '
import ast, sys, pathlib
bad = []
for line in sys.stdin:
    p = pathlib.Path(line.strip())
    if not p.exists():
        continue
    try:
        ast.parse(p.read_text())
    except SyntaxError as e:
        bad.append(f"{p}:{e.lineno}: {e.msg}")
print("\n".join(bad))
' 2>&1)
if [ -n "$parse_out" ]; then
  bad "Python files do not parse:"; printf '%s\n' "$parse_out" | sed 's/^/      /'
else
  ok "every Python file parses ($( { git ls-files '*.py'; git ls-files --others --exclude-standard '*.py'; } | sort -u | wc -l | tr -d ' ') files, tracked + untracked)"
fi

# --- 3. seals are intact ---------------------------------------------------- #
# The heart of it. A seal whose strategy code has changed since sealing means
# the strategy was edited after it started being tested -- and any result from
# it is retrospective fitting, not evidence.
seal_out=$("$PY" - <<'PYEOF' 2>&1
import sys
sys.path.insert(0, "src")
from alpha_gate.prereg import load_seals, verify_seal, count_trials

seals = load_seals()
if not seals:
    print("EMPTY")
    sys.exit(0)

problems = []
for s in seals:
    v = verify_seal(s)          # no bars: checks spec + code hashes only
    tag = f"{s.spec['strategy_id']} @ {s.sealed_at_utc[:19]}"
    for x in v.violations:
        problems.append(("VIOLATION", f"{tag}: {x}"))
    for x in getattr(v, "unprovable", []):
        problems.append(("UNPROVABLE", f"{tag}: {x}"))

print(f"COUNT {count_trials()}")
for kind, p in problems:
    print(f"{kind} {p}")
PYEOF
)
if printf '%s' "$seal_out" | grep -q "^EMPTY"; then
  ok "no seals yet — nothing under test (that is a valid state, not a failure)"
elif printf '%s' "$seal_out" | grep -q "^VIOLATION"; then
  bad "SEAL VIOLATED — verdicts from these runs are void:"
  printf '%s\n' "$seal_out" | grep "^VIOLATION" | sed 's/^VIOLATION /      /'
elif printf '%s' "$seal_out" | grep -q "^UNPROVABLE"; then
  # The seal could not be CHECKED. Still blocks a verdict — but this is not an
  # accusation, and must never be printed as one. Before 2026-08-02 both cases
  # shared the "SEAL VIOLATED" line, so a fresh `git clone` without pandas
  # installed reported that the strategy had been edited after sealing. For a
  # harness whose whole product is that an independent party can check the work,
  # accusing yourself of fraud when a library is missing is the worst possible
  # failure mode.
  unproven "SEAL UNVERIFIED here — this is NOT evidence of tampering:"
  printf '%s\n' "$seal_out" | grep "^UNPROVABLE" | sed 's/^UNPROVABLE /      /'
  echo "      Install the runtime deps and re-run; only a HASH MISMATCH means a violation." >&2
else
  n=$(printf '%s' "$seal_out" | grep "^COUNT" | awk '{print $2}')
  ok "all $n seal(s) intact — spec and strategy code unchanged since sealing"
fi

# --- 4. captured data has not been rewritten -------------------------------- #
data_out=$("$PY" - <<'PYEOF' 2>&1
import sys, pathlib
sys.path.insert(0, "src")
from alpha_gate.capture import audit_bars

d = pathlib.Path("data/bars")
files = sorted(d.glob("*.jsonl")) if d.exists() else []
if not files:
    print("EMPTY")
    sys.exit(0)

bad = []
for f in files:
    sym, _, interval = f.stem.rpartition("_")
    a = audit_bars(sym, interval)
    for p in a.get("problems", []):
        bad.append(f"{f.name}: {p}")
print(f"FILES {len(files)}")
for b in bad:
    print(f"PROBLEM {b}")
PYEOF
)
if printf '%s' "$data_out" | grep -q "^EMPTY"; then
  ok "no captured data yet — capture has not started"
elif printf '%s' "$data_out" | grep -q "^PROBLEM"; then
  bad "CAPTURED DATA FAILED AUDIT — the append-only record was modified:"
  printf '%s\n' "$data_out" | grep "^PROBLEM" | sed 's/^PROBLEM /      /'
else
  n=$(printf '%s' "$data_out" | grep "^FILES" | awk '{print $2}')
  ok "$n capture file(s) monotonic, no fabricated timestamps"
fi

# --- 5. the honesty suite --------------------------------------------------- #
if "$PY" -m pytest tests/ -q >/tmp/alpha_gate_pytest.log 2>&1; then
  n=$(grep -oE '[0-9]+ passed' /tmp/alpha_gate_pytest.log | head -1)
  ok "honesty suite green (${n:-passed}) — lookahead, seal, cost and deflation checks"
else
  bad "honesty suite FAILED — the harness cannot be trusted to judge anything:"
  tail -20 /tmp/alpha_gate_pytest.log | sed 's/^/      /'
fi

# --- 6. no secrets ---------------------------------------------------------- #
if gitleaks detect --no-banner --redact -v >/tmp/alpha_gate_leaks.log 2>&1; then
  ok "gitleaks: no secrets in the tree or its history"
else
  bad "gitleaks found secrets:"; tail -15 /tmp/alpha_gate_leaks.log | sed 's/^/      /'
fi

# --- mutation guard, closing -------------------------------------------------#
after=$(git status --porcelain | shasum -a 256 | cut -d' ' -f1)
if [ "$before" != "$after" ]; then
  bad "this gate MUTATED the working tree — a check must never have side effects"
else
  ok "working tree unchanged by this gate"
fi

printf '\n──────────────────────────────────────────────────────────────\n'
if [ "$fail" -gt 0 ]; then
  printf 'FAIL — %d check(s) failed, %d passed.\n' "$fail" "$pass"; exit 1
fi
if [ "$unknown" -gt 0 ]; then
  printf 'UNKNOWN — %d unproven. Not a pass.\n' "$unknown"; exit 3
fi
printf 'PASS — %d check(s). Verdicts from this tree can be trusted.\n' "$pass"
exit 0
