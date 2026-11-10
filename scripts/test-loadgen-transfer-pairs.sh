#!/usr/bin/env bash
# Runs the real commerce and core-banking baseline k6 scripts as k6 runs them (iterations at a
# fixed rate handed to VUs in turn, a fresh process every hour) against an in-memory bank
# (scripts/loadgen/simulate-seed-balances.mjs), and checks both what their transfers do to the
# seven seed accounts ACC-1001~1007 and that pairing them does not change the transfer request
# shape. Needs node and jq.
#
#   COMMERCE, BANKING   scripts under test (default: the repository's)
#   OLD_COMMERCE, OLD_BANKING  optional: scripts that must show the drain this test exists for

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SCRIPT_DIR/.." && pwd)
SIM="$SCRIPT_DIR/loadgen/simulate-seed-balances.mjs"
COMMERCE=${COMMERCE:-$REPO/commerce/loadgen/script.js}
BANKING=${BANKING:-$REPO/core-banking/loadgen/script.js}
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
command -v node >/dev/null || fail 'node is required'

run() { node "$SIM" "$1" "$2" "$3"; }

# Day and night rates (66 baselines: commerce 2~10, banking 1~4 iterations a second), two days
# each with hourly restarts:
#  - no short-balance answer, no pair lost at a restart beyond 10% of VU hours, no script error
#  - a transfer count per hour within 5% of the journey share (pairing adds no transfers; the
#    measured bias is about +3% at night, a third of one hour's Poisson spread)
#  - per-minute transfer counts in an hour's last 20 minutes (where pairs are closed) no higher
#    than in its first 40: 99th percentile at most one above, maximum at most two above
shape_ok='
  def kind_ok($k): .shape[$k] as $s
    | ($s.transfersPerHour - $s.expectedPerHour | fabs) <= 0.05 * $s.expectedPerHour
      and $s.tailMinuteP99 <= $s.headMinuteP99 + 1
      and $s.tailMinuteMax <= $s.headMinuteMax + 2;
  .stats.failed == 0 and .stats.rejected400 == 0 and .stats.scriptErrors == 0
  and .lostAtRestart.commerce <= 0.1 * .hours * .load.commerce.vus
  and .lostAtRestart.banking <= 0.1 * .hours * .load.banking.vus
  and kind_ok("commerce") and kind_ok("banking")'
for load in '"commerce":{"rate":5,"vus":20},"banking":{"rate":3,"vus":10}' \
            '"commerce":{"rate":2,"vus":20},"banking":{"rate":1,"vus":10}'; do
  out=$(run "$COMMERCE" "$BANKING" "{\"hours\":48,$load}")
  jq -e "$shape_ok" <<<"$out" >/dev/null || fail "pairs at {$load}: $(jq -c '{stats, lostAtRestart, shape}' <<<"$out")"
done

# A bank outage over an hour's last ten minutes must not turn the returns VUs still owe into a
# retry storm: no minute of the outage sees more transfer requests than an ordinary minute plus
# one per VU. Refused connections and 503 are the answers a return is retried on; 502 is not.
for kind in 503 '"refused"' 502; do
  out=$(run "$COMMERCE" "$BANKING" "{\"hours\":4,\"commerce\":{\"rate\":10,\"vus\":20},\"banking\":{\"rate\":4,\"vus\":10},\"fault\":{\"hour\":2,\"from\":50,\"to\":60,\"kind\":$kind}}")
  jq -e '.stats.scriptErrors == 0
         and .shape.commerce.faultHourMaxPerMinute <= .shape.commerce.profileHeadMean + .load.commerce.vus
         and .shape.banking.faultHourMaxPerMinute <= .shape.banking.profileHeadMean + .load.banking.vus' <<<"$out" >/dev/null \
    || fail "retry storm with $kind: $(jq -c '{c: .shape.commerce.faultHourPerMinute, b: .shape.banking.faultHourPerMinute}' <<<"$out")"
done

# The unpaired scripts drain the seven (the defect this test exists for).
if [[ -n "${OLD_COMMERCE:-}" && -n "${OLD_BANKING:-}" ]]; then
  out=$(run "$OLD_COMMERCE" "$OLD_BANKING" '{"hours":48}')
  jq -e '(.stats.failed + .stats.rejected400) > 0 and .maxDriftFromSeedAtEnd > 1000000' <<<"$out" >/dev/null \
    || fail "old scripts did not show the drain: $(jq -c '{stats, maxDriftFromSeedAtEnd}' <<<"$out")"
fi

printf '[PASS] loadgen transfer pair simulation\n'
