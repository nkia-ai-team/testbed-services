#!/usr/bin/env bash
# Runs the real commerce and core-banking baseline k6 scripts as k6 runs them (iterations at a
# fixed rate handed to VUs in turn, a fresh process every hour) against an in-memory bank
# (scripts/loadgen/simulate-seed-balances.mjs), and checks both what their transfers do to the
# seven seed accounts ACC-1001~1007 and that pairing them does not change the transfer request
# shape. Needs node and jq.
#
# It also checks the commerce cart journey against the gateway's write auth (v4 E5c): cart writes
# carry the token the checkout journey uses, so the baseline sends no 401, with the same requests,
# and the standing load monitor (loadgen_monitor.py, through scripts/loadgen/monitor-windows.py)
# counts a cart write the gateway refuses. That part needs python3 as well.
#
#   COMMERCE, BANKING   scripts under test (default: the repository's)
#   OLD_COMMERCE, OLD_BANKING  optional: scripts that must show the drain this test exists for
#   OLD_COMMERCE also: a cart journey without tokens must show the 401s, and the scripts under test
#   must send the same requests as it apart from logins
#
# And the commerce scenario load scripts surge.js and capacity-probe.js (v4 E5d): their cart journey
# adds to the cart with the token their checkout journey caches per VU and user, like the baseline.
#
#   SURGE, CAPACITY_PROBE           scripts under test (default: the repository's)
#   OLD_SURGE, OLD_CAPACITY_PROBE   optional: the same scripts with a cart add without a token, which
#                                   must show the 401s and otherwise send the same requests

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

# A bank outage over an hour's last ten minutes must leave the transfer request count of the
# outage as it was: no retry storm (no minute above an ordinary minute plus one per VU) and no
# shortfall (the ten minutes together at least 85% of ten ordinary minutes; a failed send while
# closing must not count as a paired turn). Refused connections and 503 are the answers a return
# is retried on; 502 is not.
for kind in 503 '"refused"' 502; do
  out=$(run "$COMMERCE" "$BANKING" "{\"hours\":4,\"commerce\":{\"rate\":10,\"vus\":20},\"banking\":{\"rate\":4,\"vus\":10},\"fault\":{\"hour\":2,\"from\":50,\"to\":60,\"kind\":$kind}}")
  jq -e 'def fault_ok($k): .shape[$k] as $s
           | $s.faultHourMaxPerMinute <= $s.profileHeadMean + .load[$k].vus
             and ($s.faultHourPerMinute | add) >= $s.profileHeadMean * (.fault.to - .fault.from) * 0.85;
         .stats.scriptErrors == 0 and fault_ok("commerce") and fault_ok("banking")' <<<"$out" >/dev/null \
    || fail "outage request count with $kind: $(jq -c '{c: .shape.commerce.faultHourPerMinute, cmean: .shape.commerce.profileHeadMean, b: .shape.banking.faultHourPerMinute, bmean: .shape.banking.profileHeadMean}' <<<"$out")"
done

# The unpaired scripts drain the seven (the defect this test exists for).
if [[ -n "${OLD_COMMERCE:-}" && -n "${OLD_BANKING:-}" ]]; then
  out=$(run "$OLD_COMMERCE" "$OLD_BANKING" '{"hours":48}')
  jq -e '(.stats.failed + .stats.rejected400) > 0 and .maxDriftFromSeedAtEnd > 1000000' <<<"$out" >/dev/null \
    || fail "old scripts did not show the drain: $(jq -c '{stats, maxDriftFromSeedAtEnd}' <<<"$out")"
fi

# ---- cart writes and the gateway's write auth (v4 E5c) ----
command -v python3 >/dev/null || fail 'python3 is required'
WINDOWS="$SCRIPT_DIR/loadgen/monitor-windows.py"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# Monitor documents of a run, summed: windows, windows without a check, failures by check name.
monitor_sum() {
  python3 "$WINDOWS" "$1" checkout list | jq -s '{windows: length,
    uncounted: (map(select(.check_count == 0)) | length),
    failed: (map(.check_failed // {} | to_entries[]) | group_by(.key)
             | map({key: .[0].key, value: (map(.value) | add)}) | from_entries)}'
}

# Day and night: no 401 anywhere, every cart write carries a token, one write per cart journey,
# logins at most once a VU and user an hour (the token cache is reused across journeys), no check
# fails, and the monitor sees checks in every window and no failure.
for load in '"rate":10,"vus":20' '"rate":2,"vus":20'; do
  out=$(run "$COMMERCE" "$BANKING" "{\"hours\":2,\"commerce\":{$load},\"gateway\":true,\"k6json\":\"$work/k6.json\"}")
  jq -e '
    def n($re): [.requests | to_entries[] | select(.key | test($re)) | .value] | add // 0;
    .stats.scriptErrors == 0
    and n(" 401 ") == 0
    and n("^cart (add|remove) [A-Z]+ [0-9]+ none$") == 0
    and n("^cart (add|remove) ") == n("^cart get ")
    and n("^cart (add|remove) ") > 0
    and n(" login POST ") <= .hours * .load.commerce.vus * 20
    and .tokenOwnerMismatch == 0
    and ([.checks[] | .fail] | add) == 0' <<<"$out" >/dev/null \
    || fail "cart writes at {$load}: $(jq -c '{stats, tokenOwnerMismatch, requests, checks}' <<<"$out")"
  sum=$(monitor_sum "$work/k6.json")
  jq -e '.windows > 200 and .uncounted == 0 and .failed == {}' <<<"$sum" >/dev/null \
    || fail "monitor at {$load} with tokens: $sum"
done

# A refused cart write must show in the monitor: with every token refused (expired), the cart
# writes of both journeys (cart add, remove; checkout clear-cart, add-to-cart) answer 401 and the
# monitor counts them by check name (the monitor-windows buckets do not overlap, so never more
# than the run's failures). Two rates, because the sample clock grid differs by rate.
for rate in 3 5; do
out=$(run "$COMMERCE" "$BANKING" "{\"hours\":1,\"commerce\":{\"rate\":$rate,\"vus\":20},\"gateway\":\"expired\",\"k6json\":\"$work/k6.json\"}")
sum=$(monitor_sum "$work/k6.json")
jq -e --argjson sum "$sum" '
  def counted($name): ($sum.failed[$name] // 0) >= 0.97 * .checks[$name].fail
                      and ($sum.failed[$name] // 0) <= .checks[$name].fail;
  def all_cart_writes: ["cart add 200", "cart remove 200", "checkout clear-cart 200", "checkout add-to-cart 200"];
  all(all_cart_writes[]; . as $name | $sum.failed | has($name))
  and all(all_cart_writes[] as $name | .checks[$name].fail > 0 and counted($name); .)
  and .checks["checkout clear-cart 200"].fail == ([.requests | to_entries[] | select(.key | startswith("checkout clear-cart ")) | .value] | add)
  and .checks["checkout add-to-cart 200"].fail == ([.requests | to_entries[] | select(.key | startswith("checkout add-to-cart ")) | .value] | add)' <<<"$out" >/dev/null \
  || fail "monitor did not count refused cart writes at $rate/s: run $(jq -c .checks <<<"$out"), monitor $sum"
done

# Login down (every login answers 500): a cart journey still reads the cart, then sends no write,
# as the checkout journey does. No cart write at all, no write without a token, and the cart reads
# keep the journey share (10% of iterations, within 5%).
out=$(run "$COMMERCE" "$BANKING" '{"hours":1,"commerce":{"rate":5,"vus":20},"gateway":"login-down"}')
jq -e '
  def n($re): [.requests | to_entries[] | select(.key | test($re)) | .value] | add // 0;
  .stats.scriptErrors == 0
  and n(" login POST 500 ") > 0
  and n("^cart (add|remove) ") == 0
  and n("^[^ ]+ [^ ]+ (POST|PUT|DELETE|PATCH) [0-9]+ none$") == n(" login POST ")
  and (n("^cart get ") - .hours * 3600 * .load.commerce.rate * 0.10 | fabs)
      <= 0.05 * .hours * 3600 * .load.commerce.rate * 0.10' <<<"$out" >/dev/null \
  || fail "cart journey with login down: $(jq -c '{stats, requests}' <<<"$out")"

# Against the cart journey without tokens: that one shows the 401s and the monitor counts them, and
# the scripts under test send the same requests (every step but login, minute by minute) to the same
# users.
if [[ -n "${OLD_COMMERCE:-}" ]]; then
  for load in '"rate":10,"vus":20' '"rate":2,"vus":20'; do
    old=$(run "$OLD_COMMERCE" "$BANKING" "{\"hours\":2,\"commerce\":{$load},\"gateway\":true,\"k6json\":\"$work/old.json\"}")
    new=$(run "$COMMERCE" "$BANKING" "{\"hours\":2,\"commerce\":{$load},\"gateway\":true}")
    sum=$(monitor_sum "$work/old.json")
    jq -e --argjson sum "$sum" '.checks["cart add 200"].fail > 0
      and ($sum.failed["cart add 200"] // 0) >= 0.97 * .checks["cart add 200"].fail
      and ($sum.failed["cart remove 200"] // 0) >= 0.97 * .checks["cart remove 200"].fail' <<<"$old" >/dev/null \
      || fail "old cart journey at {$load} did not show the 401s: $(jq -c .checks <<<"$old"), monitor $sum"
    jq -e -n --argjson old "$old" --argjson new "$new" '
      def steps($r): $r.perMinute | with_entries(select(.key | endswith("/login") | not));
      steps($old) == steps($new) and $old.cartUsers == $new.cartUsers' >/dev/null \
      || fail "cart journey with tokens changed the request shape at {$load}"
    jq -n --argjson old "$old" --argjson new "$new" --arg load "$load" -r '
      def logins($r): [$r.requests | to_entries[] | select(.key | test(" login POST ")) | .value] | add;
      "[INFO] {\($load)} logins over 2 hours: without tokens \(logins($old)), with tokens \(logins($new))"'
  done
fi

# ---- cart writes of the scenario load scripts surge.js and capacity-probe.js (v4 E5d) ----
SURGE=${SURGE:-$REPO/commerce/loadgen/surge.js}
CAPACITY_PROBE=${CAPACITY_PROBE:-$REPO/commerce/loadgen/capacity-probe.js}
# One hour at a constant rate with 100 VUs (surge.js preallocates 100), the scripts pointed at the
# modelled gateway. $1 the exported function an iteration runs, $2 iterations a second, $3 gateway,
# $4 the script, $5 optional extra config members.
surge_run() {
  run "$4" "$BANKING" "{\"hours\":1,\"commerce\":{\"rate\":$2,\"vus\":100},\"gateway\":$3,\"commerceEnv\":{\"GATEWAY_URL\":\"http://testbed-gateway:8089\"},\"commerceEntry\":\"$1\"${5:+,$5}}"
}
for spec in "surge.js|$SURGE|default|${OLD_SURGE:-}" "capacity-probe.js|$CAPACITY_PROBE|mix|${OLD_CAPACITY_PROBE:-}"; do
  IFS='|' read -r name script entry old_script <<<"$spec"

  # The lowest companion rate on file (3) and a common one (35): no 401 anywhere, the only writes
  # without a token are logins, one cart add per cart journey and each with a token, the token is
  # the cart user's own (the cache is per user), logins at most once a VU and user.
  for rate in 3 35; do
    out=$(surge_run "$entry" "$rate" true "$script")
    jq -e '
      def n($re): [.requests | to_entries[] | select(.key | test($re)) | .value] | add // 0;
      .stats.scriptErrors == 0
      and n(" 401 ") == 0
      and n("^surge-cart add [A-Z]+ [0-9]+ none$") == 0
      and n("^[^ ]+ [^ ]+ (POST|PUT|DELETE|PATCH) [0-9]+ none$") == n(" login POST ")
      and n("^surge-cart add ") == n("^surge-cart get ")
      and n("^surge-cart add ") > 0
      and n(" login POST ") <= .hours * .load.commerce.vus * 20
      and .tokenOwnerMismatch == 0' <<<"$out" >/dev/null \
      || fail "$name cart add at $rate/s: $(jq -c '{stats, tokenOwnerMismatch, requests}' <<<"$out")"
  done

  # Login down (every login answers 500): the cart journey still reads the cart, then sends no add,
  # as the checkout journey does. No cart add at all, no write without a token but the logins, and
  # the cart reads keep the journey share (15% of iterations, within 5%).
  out=$(surge_run "$entry" 5 '"login-down"' "$script")
  jq -e '
    def n($re): [.requests | to_entries[] | select(.key | test($re)) | .value] | add // 0;
    .stats.scriptErrors == 0
    and n(" login POST 500 ") > 0
    and n("^surge-cart add ") == 0
    and n("^[^ ]+ [^ ]+ (POST|PUT|DELETE|PATCH) [0-9]+ none$") == n(" login POST ")
    and (n("^surge-cart get ") - .hours * 3600 * .load.commerce.rate * 0.15 | fabs)
        <= 0.05 * .hours * 3600 * .load.commerce.rate * 0.15' <<<"$out" >/dev/null \
    || fail "$name cart journey with login down: $(jq -c '{stats, requests}' <<<"$out")"

  # Against the same script with the cart add without a token: that one shows the 401s, and the
  # script under test sends the same requests (every step but login, minute by minute) to the same
  # users, and the same cart adds (product and quantity) in the same order.
  if [[ -n "$old_script" ]]; then
    for rate in 3 35; do
      # Files, not --argjson: the cart writes of an hour are larger than one argument may be.
      surge_run "$entry" "$rate" true "$old_script" '"cartWrites":true' > "$work/surge-old.json"
      surge_run "$entry" "$rate" true "$script" '"cartWrites":true' > "$work/surge-new.json"
      jq -e '[.requests | to_entries[] | select(.key | test("^surge-cart add POST 401 none$")) | .value] | add // 0 > 0' \
        "$work/surge-old.json" >/dev/null || fail "old $name at $rate/s did not show the 401s: $(jq -c .requests "$work/surge-old.json")"
      jq -e -n --slurpfile o "$work/surge-old.json" --slurpfile w "$work/surge-new.json" '
        def steps($r): $r.perMinute | with_entries(select(.key | endswith("/login") | not));
        $o[0] as $old | $w[0] as $new
        | steps($old) == steps($new) and $old.cartUsers == $new.cartUsers and ($new.cartUsers | length) > 0
          and $old.cartWrites == $new.cartWrites and ($new.cartWrites | length) == ($new.cartUsers | length)' >/dev/null \
        || fail "$name cart add with tokens changed the request shape at $rate/s"
      jq -n --slurpfile o "$work/surge-old.json" --slurpfile w "$work/surge-new.json" --arg name "$name" --arg rate "$rate" -r '
        def logins($r; $m): [$r.perMinute | to_entries[] | select(.key | endswith("/login")) | .value
                              | to_entries[] | select((.key | tonumber) < $m) | .value] | add // 0;
        $o[0] as $old | $w[0] as $new |
        "[INFO] \($name) at \($rate)/s, 100 VUs: logins in the first 15 minutes without tokens \(logins($old; 15)), with tokens \(logins($new; 15))"'
    done
  fi
done

printf '[PASS] loadgen transfer pair simulation\n'
