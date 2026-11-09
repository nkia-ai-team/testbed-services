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
  printf '%s\n' "$sql" >"$FAKE_LOG.sql"
  grep -q "oracle_maintained = 'N'" <<<"$sql" || exit 9
  grep -q "whenever sqlerror exit failure" <<<"$sql" || exit 9
  grep -q "from banking.accounts where id in ('ACC-5002','ACC-5003'," <<<"$sql" || exit 9
  [[ $(grep -o "'ACC-5" <<<"$sql" | wc -l) -eq 733 ]] || exit 9
  [[ -n "${FAKE_SQL_FAIL:-}" ]] && exit 1
  printf '\nCAP|%s\nPOOL|%s\n\n' "$FAKE_CAPACITY" "$FAKE_POOL"
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
export FAKE_POOL='733|2004992.47|ACC-5310|0|0'
export POOL_FILE="$SCRIPT_DIR/../core-banking/loadgen/transfer-accounts.txt"

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
  # Empty directories: one past the retention, one just created (an incident being written).
  mkdir -p "$db/incident/incdir_old" "$db/incident/incdir_new"
  touch -d '10 days ago' "$db/incident/incdir_old"
  printf 'listener\n' >"$lsnr/trace/listener.log"; touch -d '10 days ago' "$lsnr/trace/listener.log"
  printf 'xml\n' >"$lsnr/alert/log.xml"
}

# --- purge: deletes only old diagnostic files, never alert logs or metadata ---
reset_tree
out=$("$MAINT" purge)
grep -q 'purge done (retention 168h): deleted=5 bytes=20 truncated=none' <<<"$out" || fail "purge result: $out"
db="$DIAG_ROOT/rdbms/free/FREE"
for gone in trace/FREE_ora_1.trc trace/FREE_ora_1.trm cdump/core_1 incident/incdir_7/FREE_ora_1_i7.trc hm/HMREPORT_1.hm incident/incdir_old; do
  [[ ! -e "$db/$gone" ]] || fail "old $gone survived the purge"
done
for kept in trace/FREE_ora_2.trc trace/alert_FREE.log alert/log.xml metadata/ADR_CONTROL.ams; do
  [[ -f "$db/$kept" ]] || fail "purge removed $kept"
done
# A freshly created empty directory stays; so does one this run just emptied (fresh mtime).
[[ -d "$db/incident/incdir_new" ]] || fail 'purge removed a just-created empty directory'
[[ -d "$db/incident/incdir_7" ]] || fail 'purge removed a directory it emptied in the same run'

# --- purge refuses a root that is not an ADR tree, before touching anything ---
reset_tree; calls_before=$(wc -l <"$FAKE_LOG")
rc=0; out=$(DIAG_ROOT="$TMP_ROOT" "$MAINT" purge) || rc=$?
[[ $rc -ne 0 ]] || fail 'purge accepted a root that is not */diag'
grep -q "purge refused: DIAG_ROOT $TMP_ROOT is not a \*/diag directory" <<<"$out" || fail "wrong-root message: $out"
[[ $(wc -l <"$FAKE_LOG") -eq $calls_before ]] || fail 'purge reached the pod with a wrong root'
[[ -f "$DIAG_ROOT/rdbms/free/FREE/trace/FREE_ora_1.trc" ]] || fail 'purge deleted files under a wrong root'
# The script inside the pod refuses it too.
remote=$(sed -n "/^PURGE_REMOTE=\$(cat <<'SH'$/,/^SH$/p" "$MAINT" | sed '1d;$d')
rc=0; bash -c "$remote" _ "$TMP_ROOT" 10080 1048576 >/dev/null 2>&1 || rc=$?
[[ $rc -ne 0 ]] || fail 'pod-side purge accepted a root that is not */diag'
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
grep -q 'check ok: user data 6602620928 bytes, threshold 11274289152 (datafiles 9017688064); transfer pool 733 accounts, lowest ACC-5310 2004992.47, floor 500000' <<<"$out" || fail "check: $out"
jq -e '.user_data_bytes == 6602620928 and .datafile_bytes == 9017688064 and .alert_bytes == 11274289152
       and .pool_expected == 733 and .pool_found == 733 and .pool_min_balance == 2004992.47
       and .pool_min_account == "ACC-5310" and .pool_below_floor == 0 and .pool_floor == 500000' \
  "$STATE_DIR/last.json" >/dev/null || fail 'last.json content'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'ALERT present below the threshold'
# The datafile sum alone above the line is not an alert (user decision: user data).
FAKE_CAPACITY='6602620928|12000000000' "$MAINT" check >/dev/null || fail 'datafile sum raised an alert'
rc=0; FAKE_CAPACITY='11274289153|12000000000' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 ]] || fail "over-threshold check exited $rc, want 3"
grep -q 'banking Oracle user data 11274289153 bytes > 11274289152' "$STATE_DIR/ALERT" || fail 'ALERT content'
"$MAINT" check >/dev/null || fail 'check back under the threshold failed'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'ALERT not cleared after recovery'
# --- check: the transfer pool's lowest balance against the 500,000 floor ---
rc=0; FAKE_POOL='733|432100.5|ACC-5421|2|0' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 ]] || fail "pool below the floor exited $rc, want 3"
[[ $(wc -l <"$STATE_DIR/ALERT") -eq 1 ]] || fail 'pool ALERT is not one line'
grep -q 'transfer pool: 2 accounts below 500000, lowest ACC-5421 432100.5' "$STATE_DIR/ALERT" || fail 'pool ALERT reason'
"$MAINT" check >/dev/null || fail 'clean check after the pool alert failed'
grep -q 'count(case when balance < 500000 then 1 end)' "$FAKE_LOG.sql" || fail 'default floor not in the query'
POOL_BALANCE_FLOOR=400000 "$MAINT" check >/dev/null || fail 'check with a configured floor failed'
grep -q 'count(case when balance < 400000 then 1 end)' "$FAKE_LOG.sql" || fail 'configured floor not in the query'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'ALERT not cleared once the pool is above the floor'
# A pool account that vanished or is no longer ACTIVE is a reason of its own.
rc=0; FAKE_POOL='732|2004992.47|ACC-5310|0|1' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 ]] || fail "pool account missing exited $rc, want 3"
grep -q 'transfer pool: 732 of 733 accounts found, 1 not ACTIVE' "$STATE_DIR/ALERT" || fail 'pool membership reason'
# Two reasons at once give two lines.
rc=0; FAKE_CAPACITY='11274289153|12000000000' FAKE_POOL='733|10|ACC-5002|1|0' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 && $(wc -l <"$STATE_DIR/ALERT") -eq 2 ]] || fail 'two reasons did not give two ALERT lines'
"$MAINT" check >/dev/null || fail 'clean check after two reasons failed'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'ALERT not cleared after two reasons'
# Without the pool file the check refuses instead of passing silently.
rc=0; POOL_FILE="$TMP_ROOT/missing.txt" "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "missing pool file exited $rc, want 1"
printf 'ACC-5002\nACC-5003); drop table x; --\n' >"$TMP_ROOT/bad-pool.txt"
rc=0; POOL_FILE="$TMP_ROOT/bad-pool.txt" "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "malformed pool file exited $rc, want 1"

rc=0; FAKE_CAPACITY='ORA-01017|x' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "unreadable answer exited $rc, want 1"
rc=0; FAKE_SQL_FAIL=1 "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "failed query exited $rc, want 1"

rc=0; "$MAINT" >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 ]] || fail "no subcommand exited $rc, want 2"

printf '[PASS] oracle maintenance tests\n'
