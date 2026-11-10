#!/usr/bin/env bash
# Runs the real commerce and core-banking baseline k6 scripts against an in-memory bank
# (scripts/loadgen/simulate-seed-balances.mjs) and checks what their transfers do to the seven
# seed accounts ACC-1001~1007. The scripts before v4 E5b (origin's versions given as
# $OLD_COMMERCE and $OLD_BANKING, optional) must show the drain this test exists for.
# Needs node.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SCRIPT_DIR/.." && pwd)
SIM="$SCRIPT_DIR/loadgen/simulate-seed-balances.mjs"
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
command -v node >/dev/null || fail 'node is required'

run() { node "$SIM" "$@"; }

# Three days with hourly k6 restarts: no short-balance answer at all, and at every hour end
# each of the seven is exactly back at its seed balance (every pair returned, none lost).
out=$(run "$REPO/commerce/loadgen/script.js" "$REPO/core-banking/loadgen/script.js" 72)
jq -e '.stats.failed == 0 and .stats.rejected400 == 0 and .stats.completed > 100000
       and .lostAtRestart == 0 and .maxDriftFromSeedAtEnd == 0
       and .minSevenBalanceAtHourEnds == 450000' <<<"$out" >/dev/null || fail "paired scripts: $out"

# Transfers answering 503 for twenty minutes: returns are retried, nothing is lost.
out=$(run "$REPO/commerce/loadgen/script.js" "$REPO/core-banking/loadgen/script.js" 24 reversal-5xx)
jq -e '.stats.server5xx > 0 and .stats.failed == 0 and .stats.rejected400 == 0
       and .lostAtRestart == 0 and .maxDriftFromSeedAtEnd == 0' <<<"$out" >/dev/null || fail "5xx retry: $out"

# The unpaired scripts drain the seven within the same three days (the defect).
if [[ -n "${OLD_COMMERCE:-}" && -n "${OLD_BANKING:-}" ]]; then
  out=$(run "$OLD_COMMERCE" "$OLD_BANKING" 72)
  jq -e '(.stats.failed + .stats.rejected400) > 0 and .minSevenBalanceAtHourEnds < 50000' <<<"$out" >/dev/null \
    || fail "old scripts did not show the drain: $out"
fi

printf '[PASS] loadgen transfer pair simulation\n'
