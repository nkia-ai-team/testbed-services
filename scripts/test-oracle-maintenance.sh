#!/usr/bin/env bash
# Side-effect-free tests for oracle-maintenance.sh. kubectl and curl are fakes on PATH:
# kubectl runs the purge script locally against a temporary ADR tree and answers the
# capacity query from $FAKE_CAPACITY; curl answers the runner API from $FAKE_ACTIVE and
# $FAKE_PHASE (or fails when $FAKE_RUNNER_DOWN is set).

set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MAINT="$SCRIPT_DIR/oracle-maintenance.sh"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT

fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/kubectl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_LOG"
while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
shift
if [[ "$1" == sqlplus ]]; then
  sql=$(cat)
  grep -q "oracle_maintained = 'N'" <<<"$sql" || exit 9
  grep -q "whenever sqlerror exit failure" <<<"$sql" || exit 9
  [[ -n "${FAKE_SQL_FAIL:-}" ]] && exit 1
  printf '\n%s\n\n' "$FAKE_CAPACITY"
  exit 0
fi
[[ "$1 $2" == "bash -s" ]] || exit 9
shift 2
exec bash -s "$@"
SH
cat >"$TMP_ROOT/bin/curl" <<'SH'
#!/usr/bin/env bash
[[ -n "${FAKE_RUNNER_DOWN:-}" ]] && exit 7
for a in "$@"; do url=$a; done
case "$url" in
  */api/active) printf '{"is_active":%s,"scenario_id":"F01-R","run_id":"r1"}' "$FAKE_ACTIVE" ;;
  */api/live-queue) printf '{"phase":"%s","current_scenario_id":null}' "$FAKE_PHASE" ;;
  *) exit 22 ;;
esac
SH
chmod +x "$TMP_ROOT/bin/kubectl" "$TMP_ROOT/bin/curl"

export PATH="$TMP_ROOT/bin:$PATH" FAKE_LOG="$TMP_ROOT/kubectl.log"
export STATE_DIR="$TMP_ROOT/state" RUNNER_URL="http://runner.invalid:8091"
export FAKE_ACTIVE=false FAKE_PHASE=completed FAKE_CAPACITY='6602620928|9017688064'

# A small ADR tree: one database home and one listener home.
reset_tree() {
  rm -rf "$TMP_ROOT/diag"
  export DIAG_ROOT="$TMP_ROOT/diag"
  local db="$DIAG_ROOT/rdbms/free/FREE" lsnr="$DIAG_ROOT/tnslsnr/testbed-oracle-0/listener"
  mkdir -p "$db/trace" "$db/cdump" "$db/incident/incdir_7" "$db/hm" "$db/alert" "$db/metadata" "$lsnr/trace" "$lsnr/alert"
  for f in "$db/trace/FREE_ora_1.trc" "$db/trace/FREE_ora_1.trm" "$db/cdump/core_1" \
           "$db/incident/incdir_7/FREE_ora_1_i7.trc" "$db/hm/HMREPORT_1.hm" \
           "$db/trace/alert_FREE.log" "$db/alert/log.xml" "$db/metadata/ADR_CONTROL.ams"; do
    printf 'old\n' >"$f"; touch -d '10 days ago' "$f"
  done
  printf 'new\n' >"$db/trace/FREE_ora_2.trc"; touch -d '2 days ago' "$db/trace/FREE_ora_2.trc"
  printf 'listener\n' >"$lsnr/trace/listener.log"; touch -d '10 days ago' "$lsnr/trace/listener.log"
  printf 'xml\n' >"$lsnr/alert/log.xml"
}

# --- purge: deletes only old diagnostic files, never alert logs or metadata ---
reset_tree
out=$("$MAINT" purge)
grep -q 'purge done (retention 168h): deleted=5 bytes=20 truncated=none' <<<"$out" || fail "purge result: $out"
db="$DIAG_ROOT/rdbms/free/FREE"
for gone in trace/FREE_ora_1.trc trace/FREE_ora_1.trm cdump/core_1 incident/incdir_7 hm/HMREPORT_1.hm; do
  [[ ! -e "$db/$gone" ]] || fail "old $gone survived the purge"
done
for kept in trace/FREE_ora_2.trc trace/alert_FREE.log alert/log.xml metadata/ADR_CONTROL.ams; do
  [[ -f "$db/$kept" ]] || fail "purge removed $kept"
done
[[ -s "$DIAG_ROOT/tnslsnr/testbed-oracle-0/listener/trace/listener.log" ]] || fail 'listener.log under the cap was touched'
grep -q 'exec -i testbed-oracle-0 -- bash -s --' "$FAKE_LOG" || fail 'purge did not run in the Oracle pod'

# --- purge: the listener's single growing files are emptied above the cap ---
reset_tree
head -c $((2 * 1024 * 1024)) /dev/zero >"$DIAG_ROOT/tnslsnr/testbed-oracle-0/listener/trace/listener.log"
out=$(LISTENER_LOG_MAX_MB=1 "$MAINT" purge)
grep -q 'truncated= tnslsnr/testbed-oracle-0/listener/trace/listener.log$' <<<"$out" || fail "listener truncate: $out"
[[ -f "$DIAG_ROOT/tnslsnr/testbed-oracle-0/listener/trace/listener.log" && ! -s "$DIAG_ROOT/tnslsnr/testbed-oracle-0/listener/trace/listener.log" ]] \
  || fail 'listener.log above the cap was not emptied in place'

# --- purge never runs while a recording may be in progress ---
expect_skip() {
  local reason=$1; shift
  reset_tree; : >"$FAKE_LOG"
  out=$(env "$@" "$MAINT" purge)
  grep -q "purge skipped: $reason" <<<"$out" || fail "expected skip ($reason): $out"
  [[ -f "$DIAG_ROOT/rdbms/free/FREE/trace/FREE_ora_1.trc" ]] || fail "purge deleted files while skipping ($reason)"
  [[ ! -s "$FAKE_LOG" ]] || fail "purge reached the pod while skipping ($reason)"
}
expect_skip 'run active: F01-R r1' FAKE_ACTIVE=true
for phase in cycle_reset cycle_normal cycle_buffer running cycle_cooldown waiting_capture waiting_clean_window; do
  expect_skip "queue phase $phase" FAKE_PHASE="$phase"
done
expect_skip 'runner unreachable' FAKE_RUNNER_DOWN=1
for phase in idle paused completed; do
  reset_tree
  FAKE_PHASE=$phase "$MAINT" purge | grep -q 'purge done' || fail "purge refused in phase $phase"
done

# --- check: user data against the 10.5 GiB line, alert file raised and cleared ---
out=$("$MAINT" check)
grep -q 'check ok: user data 6602620928 bytes, threshold 11274289152 (datafiles 9017688064)' <<<"$out" || fail "check: $out"
jq -e '.user_data_bytes == 6602620928 and .datafile_bytes == 9017688064 and .alert_bytes == 11274289152' \
  "$STATE_DIR/last.json" >/dev/null || fail 'last.json content'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'ALERT present below the threshold'
# The datafile sum alone above the line is not an alert (user decision: user data).
FAKE_CAPACITY='6602620928|12000000000' "$MAINT" check >/dev/null || fail 'datafile sum raised an alert'
rc=0; FAKE_CAPACITY='11274289153|12000000000' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 ]] || fail "over-threshold check exited $rc, want 3"
grep -q 'banking Oracle user data 11274289153 bytes > 11274289152' "$STATE_DIR/ALERT" || fail 'ALERT content'
"$MAINT" check >/dev/null || fail 'check back under the threshold failed'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'ALERT not cleared after recovery'
rc=0; FAKE_CAPACITY='ORA-01017|x' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "unreadable answer exited $rc, want 1"
rc=0; FAKE_SQL_FAIL=1 "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "failed query exited $rc, want 1"

rc=0; "$MAINT" >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 ]] || fail "no subcommand exited $rc, want 2"

printf '[PASS] oracle maintenance tests\n'
