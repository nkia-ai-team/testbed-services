#!/usr/bin/env bash
# Tests for assemble-eval-case.sh. Exercises the deterministic VM-export shift
# (--vm-only) and dry-run; the ClickHouse parquet path needs a live CH runtime.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ASSEMBLE="$SCRIPT_DIR/assemble-eval-case.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

case_dir="$TMP/case"
normal="$TMP/normal"
mkdir -p "$case_dir/data" "$normal/data"

# Same-day layout: normal 2026-07-15 00:00-02:00 KST is 2026-07-14T15:00Z..17:00Z;
# scenario capture_start (t1-10m) is 2026-07-15T00:50:00Z. delta shifts the
# normal prefix forward so its end lands on the seam at capture_start.
normal_end="2026-07-14T17:00:00Z"
capture_start="2026-07-15T00:50:00Z"
delta_sec=$(( $(date -u -d "$capture_start" +%s) - $(date -u -d "$normal_end" +%s) ))

# Synthetic VM exports (JSON lines). Normal sample at 16:59:00Z, scenario at
# 00:55:00Z (real time).
normal_ts=$(( $(date -u -d "2026-07-14T16:59:00Z" +%s) * 1000 ))
scenario_ts=$(( $(date -u -d "2026-07-15T00:55:00Z" +%s) * 1000 ))
printf '{"metric":{"__name__":"cpu"},"values":[1],"timestamps":[%s]}\n' "$normal_ts" \
  > "$normal/data/victoriametrics.export"
printf '{"metric":{"__name__":"cpu"},"values":[2],"timestamps":[%s]}\n' "$scenario_ts" \
  > "$case_dir/data/victoriametrics.export"

# dry-run reports the delta and plan.
"$ASSEMBLE" --case-dir "$case_dir" --normal-segment "$normal" --delta-sec "$delta_sec" --dry-run \
  | jq -e --argjson d "$delta_sec" '.mode == "dry-run" and .delta_sec == $d' >/dev/null \
  || fail 'dry-run plan mismatch'

# vm-only assembly: normal timestamp shifted by delta, scenario untouched, concat order.
"$ASSEMBLE" --case-dir "$case_dir" --normal-segment "$normal" --delta-sec "$delta_sec" --vm-only >/dev/null
out="$case_dir/assembled/victoriametrics.export"
[[ -s "$out" ]] || fail 'assembled VM export missing'
[[ "$(wc -l < "$out")" -eq 2 ]] || fail 'assembled VM export should have 2 lines'
shifted=$(sed -n '1p' "$out" | jq '.timestamps[0]')
kept=$(sed -n '2p' "$out" | jq '.timestamps[0]')
expected_shifted=$(( normal_ts + delta_sec * 1000 ))
[[ "$shifted" -eq "$expected_shifted" ]] || fail "normal timestamp not shifted: $shifted != $expected_shifted"
[[ "$kept" -eq "$scenario_ts" ]] || fail "scenario timestamp was altered: $kept != $scenario_ts"

# The shifted normal sample must land at/just before the seam (capture_start).
seam_ms=$(( $(date -u -d "$capture_start" +%s) * 1000 ))
[[ "$shifted" -le "$seam_ms" ]] || fail 'shifted normal sample crossed the seam'

# Re-running must refuse to clobber an existing assembled/ (immutability).
if "$ASSEMBLE" --case-dir "$case_dir" --normal-segment "$normal" --delta-sec "$delta_sec" --vm-only >/dev/null 2>&1; then
  fail 'assembly overwrote existing assembled/'
fi

printf '[PASS] assemble-eval-case VM-shift tests\n'
