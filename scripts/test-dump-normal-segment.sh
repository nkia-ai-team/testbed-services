#!/usr/bin/env bash
# Side-effect-free tests for dump-normal-segment.sh. Only --dry-run is used.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DUMP="$SCRIPT_DIR/dump-normal-segment.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

expect_rejected() {
  local name=$1; shift
  if "$DUMP" "$@" --output-root "$TMP_ROOT/seg" --dry-run >/dev/null 2>&1; then
    fail "$name was accepted"
  fi
}

# 2026-07-15 00:00 KST == 2026-07-14T15:00:00Z; +2h == 2026-07-14T17:00:00Z.
output=$("$DUMP" --domain commerce --date 2026-07-15 --output-root "$TMP_ROOT/seg" --dry-run)
jq -e '
  .mode == "dry-run" and .domain == "commerce" and .time_basis == "UTC" and
  .tod_phase == "00:00-02:00 KST" and
  .segment_start == "2026-07-14T15:00:00Z" and
  .segment_end == "2026-07-14T17:00:00Z" and
  .segment_start_kst == "2026-07-15T00:00:00+09:00" and
  (.final_dir | endswith("/commerce/2026-07-15")) and
  (.stores | index("victoriametrics.export") != null)
' <<<"$output" >/dev/null || fail 'unexpected dry-run plan'

expect_rejected unknown-domain --domain social-feed --date 2026-07-15
expect_rejected bad-date --domain commerce --date 2026-7-5
expect_rejected missing-domain --date 2026-07-15

printf '[PASS] normal segment dump dry-run tests\n'
