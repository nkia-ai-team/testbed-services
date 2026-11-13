#!/usr/bin/env bash
# Side-effect-free tests for restore-golden-state.sh. docker and curl are fakes on
# PATH (RUNTIME=docker): docker answers inspect/stop/start and swallows the psql and
# pg_restore sidecar runs; curl answers the observer health check. A temporary golden
# root holds one golden (meta.json + data/golden-pg-state.dump).
#
# What is checked: the restore result JSON carries golden_stream_generation and
# golden_stream_boot_load_until read after the data restore (null when unreadable,
# never failing the restore), and golden_sha256 = sha256 of the dump
# that was restored (the runner threads it into phases.json trainer_reset, the capture
# carries it into the recording meta), in both the real and the dry-run output, and a
# dump that does not match meta.pg_ai_state.sha256 is still refused.

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RESTORE="$SCRIPT_DIR/restore-golden-state.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_LOG"
case "$1" in
  inspect)
    if [[ "${2:-}" == -f ]]; then printf 'true\n'; fi
    exit 0 ;;
  stop|start) exit 0 ;;
  run)
    case "$*" in
      *--version*) [[ "$*" == *fake-pg16* ]] && echo 'pg_dump (PostgreSQL) 16.15' || echo 'pg_dump (PostgreSQL) 17.11' ;;
      *'SHOW server_version_num'*) echo "${FAKE_PG_SERVER_NUM:-170011}" ;;
      *trainer_stream_generations*)
        [[ "${FAKE_STREAM_FAIL:-}" == 1 ]] && exit 2
        printf '%s\n' "${FAKE_STREAM_ROW-1985|1795105440999}" ;;
    esac
    exit 0 ;;
  *) exit 9 ;;
esac
SH
cat >"$TMP_ROOT/bin/curl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"$TMP_ROOT/bin/kubectl" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod +x "$TMP_ROOT/bin/docker" "$TMP_ROOT/bin/curl" "$TMP_ROOT/bin/kubectl"

export PATH="$TMP_ROOT/bin:$PATH" FAKE_LOG="$TMP_ROOT/docker.log"
export RUNTIME=docker KUBECTL=false PG_PASSWORD=unused-test-value

golden_root="$TMP_ROOT/goldens"
golden="$golden_root/golden-0900"
mkdir -p "$golden/data"
printf 'fake pg custom dump bytes\n' >"$golden/data/golden-pg-state.dump"
dump_sha=$(sha256sum "$golden/data/golden-pg-state.dump" | cut -d' ' -f1)
jq -n --arg sha "$dump_sha" \
  '{kind:"golden", tod_phase_kst:"09:00",
    pg_ai_state:{sha256:$sha, tables:["detector_seen_signatures","coverage_signal_state"]}}' \
  >"$golden/meta.json"

# 1. Real restore: result JSON keeps its existing keys and adds golden_sha256.
out=$("$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
  fail "restore failed: $(cat "$TMP_ROOT/err")"
jq -e --arg sha "$dump_sha" --arg dir "$golden" '
  .action == "restore-golden" and .status == "ok" and
  .golden_dir == $dir and .golden_tod_kst == "09:00" and
  .pg_tables_restored == 2 and
  .golden_sha256 == $sha and (.golden_sha256 | test("^[0-9a-f]{64}$"))
' <<<"$out" >/dev/null || fail "restore result must carry golden_sha256 of the restored dump: $out"
grep -q '^run ' "$FAKE_LOG" || fail 'restore did not run the pg sidecar'

# 1b. G2META (Q19 B): the result names the restored golden's active stream generation
#     and the earliest valid_until of its artifacts (ms floored to the second), read
#     after the data restore and before the observer starts.
jq -e '.golden_stream_generation == 1985 and .golden_stream_boot_load_until == "2026-11-19T16:24:00Z"' \
  <<<"$out" >/dev/null || fail "restore result must carry the golden stream generation and deadline: $out"
query_line=$(grep -n 'trainer_stream_generations' "$FAKE_LOG" | head -1 | cut -d: -f1)
restore_line=$(grep -n 'pg_restore --data-only' "$FAKE_LOG" | head -1 | cut -d: -f1)
start_line=$(grep -n '^start ' "$FAKE_LOG" | head -1 | cut -d: -f1)
[[ -n "$query_line" && -n "$restore_line" && -n "$start_line" ]] || fail "missing sidecar or start lines: $(cat "$FAKE_LOG")"
(( restore_line < query_line && query_line < start_line )) ||
  fail "stream generation must be read after pg_restore and before the observer starts: $(cat "$FAKE_LOG")"
grep 'trainer_stream_generations' "$FAKE_LOG" | grep -q 'm.active' ||
  fail 'stream generation read must pick the active generation'
grep -q 'min(t.valid_until_ms) FROM public.trainer_stream_threshold' "$FAKE_LOG" &&
  grep -q 'min(d.valid_until_ms) FROM public.trainer_stream_distribution_profile' "$FAKE_LOG" ||
  fail 'deadline must be the earliest valid_until of both the threshold and distribution rows'
for row_case in 'none:' 'fail:' 'nodeadline:1985|' 'garbage:1985|abc'; do
  label=${row_case%%:*} row=${row_case#*:}
  : >"$FAKE_LOG"
  if [[ "$label" == fail ]]; then
    out=$(FAKE_STREAM_FAIL=1 "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
      fail "restore must not fail when the stream read fails: $(cat "$TMP_ROOT/err")"
  else
    out=$(FAKE_STREAM_ROW="$row" "$RESTORE" --golden-root "$golden_root" --for-time now --ready-timeout-sec 5 2>"$TMP_ROOT/err") ||
      fail "restore must not fail on stream row '$row': $(cat "$TMP_ROOT/err")"
  fi
  case "$label" in
    nodeadline) want='.golden_stream_generation == 1985 and .golden_stream_boot_load_until == null' ;;
    *) want='.golden_stream_generation == null and .golden_stream_boot_load_until == null' ;;
  esac
  jq -e ".status == \"ok\" and .golden_sha256 == \"$dump_sha\" and $want" <<<"$out" >/dev/null ||
    fail "stream row case $label: $out"
  grep -q '^start ' "$FAKE_LOG" || fail "stream row case $label must still start the observer"
done

# 2. Dry run reports the same digest without touching anything.
: >"$FAKE_LOG"
out=$("$RESTORE" --golden-dir "$golden" --dry-run 2>"$TMP_ROOT/err") ||
  fail "dry-run failed: $(cat "$TMP_ROOT/err")"
jq -e --arg sha "$dump_sha" '.mode == "dry-run" and .golden_sha256 == $sha' <<<"$out" >/dev/null ||
  fail "dry-run result must carry golden_sha256: $out"
! grep -qE '^(run|stop|start) ' "$FAKE_LOG" || fail 'dry-run must not stop, start or run anything'

# 3. A dump that no longer matches meta is refused before anything is stopped.
: >"$FAKE_LOG"
printf 'tampered\n' >>"$golden/data/golden-pg-state.dump"
if "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
  fail 'restore must refuse a dump whose sha256 differs from meta'
fi
grep -q 'golden dump sha256 mismatch' "$TMP_ROOT/err" || fail "unexpected refusal: $(cat "$TMP_ROOT/err")"
! grep -qE '^(run|stop|start) ' "$FAKE_LOG" || fail 'a refused restore must not touch the stack'

# 4. PG 도구 이미지의 메이저가 서버보다 낮으면 아무것도 멈추기 전에 거절한다(119 PG 17, 도구 16).
#    기본 이미지는 postgres:17-alpine 이다.
printf 'fake pg custom dump bytes\n' >"$golden/data/golden-pg-state.dump"
grep -q 'PG_DUMP_IMAGE="${PG_DUMP_IMAGE:-postgres:17-alpine}"' "$RESTORE" || fail 'default PG tool image is not 17'
: >"$FAKE_LOG"
if PG_DUMP_IMAGE=fake-pg16 "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 >"$TMP_ROOT/out" 2>"$TMP_ROOT/err"; then
  fail 'restore must refuse a PG 16 tool against a PG 17 server'
fi
grep -q 'PG 도구 16 은 서버 17 보다 오래돼' "$TMP_ROOT/err" || fail "unexpected refusal: $(cat "$TMP_ROOT/err")"
! grep -qE '^(stop|start) ' "$FAKE_LOG" || fail 'a refused restore must not stop or start anything'
! grep -q 'pg_restore' "$FAKE_LOG" || fail 'pg_restore ran despite the major mismatch'
PG_DUMP_IMAGE=fake-pg16 FAKE_PG_SERVER_NUM=160015 "$RESTORE" --golden-dir "$golden" --ready-timeout-sec 5 \
  >/dev/null 2>"$TMP_ROOT/err" || fail "restore refused a PG 16 tool against a PG 16 server: $(cat "$TMP_ROOT/err")"

printf '[PASS] restore-golden-state.sh golden_sha256 tests\n'
