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
  grep -q "whenever sqlerror exit failure" <<<"$sql" || exit 9
  if grep -q "select 'BAL|'" <<<"$sql"; then
    # Seed balances from the fake bank file ("ID|balance|status"), only the ids asked for.
    while IFS='|' read -r id bal st; do
      grep -q "'$id'" <<<"$sql" && printf '\nBAL|%s|%s|%s\n' "$id" "$bal" "$st"
    done <"$FAKE_BANK"
    exit 0
  fi
  if grep -q "FROM banking.ledger_entries" <<<"$sql"; then
    printf '\n%s\n' "${FAKE_IMBALANCE:-0}"
    exit 0
  fi
  grep -q "oracle_maintained = 'N'" <<<"$sql" || exit 9
  grep -q "whenever sqlerror exit failure" <<<"$sql" || exit 9
  grep -q "from banking.accounts where id in ('ACC-5002','ACC-5003'," <<<"$sql" || exit 9
  [[ $(grep -o "'ACC-5" <<<"$sql" | wc -l) -eq 733 ]] || exit 9
  [[ -n "${FAKE_SQL_FAIL:-}" ]] && exit 1
  grep -q "where id in ('ACC-1001','ACC-1002','ACC-1003','ACC-1004','ACC-1005','ACC-1006','ACC-1007');" <<<"$sql" || exit 9
  printf '\nCAP|%s\nPOOL|%s\nSEED|%s\n\n' "$FAKE_CAPACITY" "$FAKE_POOL" "$FAKE_SEED"
  exit 0
fi
[[ "$1 $2" == "bash -s" ]] || exit 9
shift 2
exec bash -s "$@"
SH
cat >"$TMP_ROOT/bin/curl" <<'SH'
#!/usr/bin/env bash
[[ -n "${FAKE_RUNNER_DOWN:-}" ]] && exit 7
body=""; prev=""
for a in "$@"; do [[ "$prev" == -d ]] && body=$a; prev=$a; url=$a; done
if [[ "$url" == "$TRANSFER_URL/api/transfers" ]]; then
  # The transfer API over the fake bank file: moves money or answers FAILED.
  n=$(( $(cat "$FAKE_BANK.calls" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$FAKE_BANK.calls"
  printf '%s\n' "$body" >>"$FAKE_BANK.posts"
  [[ "${FAKE_TRANSFER_FAIL_AT:-0}" -eq "$n" ]] && exit 22
  BODY="$body" BANK="$FAKE_BANK" N="$n" python3 - <<'PY'
import json, os
from decimal import Decimal
b = json.loads(os.environ["BODY"])
rows = [l.split("|") for l in open(os.environ["BANK"]).read().split()]
bal = {r[0]: Decimal(r[1]) for r in rows}
amount = Decimal(str(b["amount"]))
if bal[b["fromAccount"]] < amount:
    print(json.dumps({"transferId": "t" + os.environ["N"], "status": "FAILED"}))
else:
    bal[b["fromAccount"]] -= amount
    bal[b["toAccount"]] += amount
    open(os.environ["BANK"], "w").write("".join(f"{r[0]}|{bal[r[0]]}|{r[2]}\n" for r in rows))
    print(json.dumps({"transferId": "t" + os.environ["N"], "status": "COMPLETED"}))
PY
  exit 0
fi
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
export FAKE_SEED='7|450000|ACC-1006|0|0'
export POOL_FILE="$SCRIPT_DIR/../core-banking/loadgen/transfer-accounts.txt"
export TRANSFER_URL="http://transfer.invalid:30282" FAKE_BANK="$TMP_ROOT/bank"

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
grep -q 'check ok: user data 6602620928 bytes, threshold 11274289152 (datafiles 9017688064); transfer pool 733 accounts, lowest ACC-5310 2004992.47, floor 500000; seed accounts 7, lowest ACC-1006 450000, floor 150000' <<<"$out" || fail "check: $out"
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
# --- check: the seven seed accounts against their 150,000 floor (v4 E5b) ---
jq -e '.seed_expected == 7 and .seed_found == 7 and .seed_min_balance == 450000 and .seed_min_account == "ACC-1006"
       and .seed_below_floor == 0 and .seed_not_active == 0 and .seed_floor == 150000' "$STATE_DIR/last.json" >/dev/null \
  || fail 'seed fields in last.json'
rc=0; FAKE_SEED='7|110157.69|ACC-1007|2|0' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 ]] || fail "seed below the floor exited $rc, want 3"
[[ $(wc -l <"$STATE_DIR/ALERT") -eq 1 ]] || fail 'seed ALERT is not one line'
grep -q 'seed accounts: 2 below 150000, lowest ACC-1007 110157.69' "$STATE_DIR/ALERT" || fail 'seed ALERT reason'
rc=0; FAKE_SEED='6|450000|ACC-1006|0|1' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 3 ]] && grep -q 'seed accounts: 6 of 7 found, 1 not ACTIVE' "$STATE_DIR/ALERT" || fail 'seed membership reason'
"$MAINT" check >/dev/null || fail 'clean check after the seed alert failed'
[[ ! -e "$STATE_DIR/ALERT" ]] || fail 'seed ALERT not cleared'
SEED_BALANCE_FLOOR=90000 "$MAINT" check >/dev/null || fail 'check with a configured seed floor failed'
grep -q "count(case when balance < 90000 then 1 end).*where id in ('ACC-1001'" "$FAKE_LOG.sql" || fail 'seed floor not in the query'
rc=0; SEED_ACCOUNTS="ACC-1001 x');drop" "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "malformed seed account exited $rc, want 1"
rc=0; FAKE_SEED='bad' "$MAINT" check >/dev/null || rc=$?
[[ $rc -eq 1 ]] || fail "unreadable seed line exited $rc, want 1"

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

# --- seed-plan, seed-restore, seed-undo (v4 E5b) ---
# The seed targets are the init.sql seed balances.
init_seeds=$(grep -oE "SELECT 'ACC-100[1-7]'( AS id)?, '[^']*'( AS holder)?, [0-9.]+" "$SCRIPT_DIR/../core-banking/db/init.sql" \
  | LC_ALL=C sed -E "s/SELECT '(ACC-100[1-7])'.* ([0-9]+)\.[0-9]+$/\1=\2/" | sort | paste -sd' ' -)
targets=$(grep -oE 'SEED_TARGETS:-[^}]*' "$MAINT" | sed 's/SEED_TARGETS:-//' | tr ' ' '\n' | sort | paste -sd' ' -)
[[ -n "$init_seeds" && "$init_seeds" == "$targets" ]] || fail "SEED_TARGETS ($targets) differ from init.sql ($init_seeds)"
seed_ids=$(grep -oE 'SEED_ACCOUNTS:-[^}]*' "$MAINT" | sed 's/SEED_ACCOUNTS:-//')
[[ "$seed_ids" == "$(tr ' ' '\n' <<<"$targets" | cut -d= -f1 | paste -sd' ' -)" ]] || fail 'SEED_ACCOUNTS and SEED_TARGETS differ'

drained_bank() {
  # 66 on 11-10 14:55 KST: the money sits in ACC-1003.
  cat >"$FAKE_BANK" <<'BANK'
ACC-1001|261469.03|ACTIVE
ACC-1002|58011.28|ACTIVE
ACC-1003|21935084.25|ACTIVE
ACC-1004|397829.86|ACTIVE
ACC-1005|3239129.03|ACTIVE
ACC-1006|353518.09|ACTIVE
ACC-1007|175782.69|ACTIVE
commerce-settlement|577230000000.00|ACTIVE
BANK
  rm -f "$FAKE_BANK.calls" "$FAKE_BANK.posts"
}
bank_of() { awk -F'|' -v id="$1" '$1 == id { print $2 }' "$FAKE_BANK"; }

drained_bank
plan=$("$MAINT" seed-plan)
jq -e '.shortfall == "0" and .missing == [] and .inactive == [] and (.transfers | length) == 6
       and all(.transfers[]; .from == "ACC-1003")
       and (.transfers[] | select(.to == "ACC-1002") | .amount) == "3141988.72"' <<<"$plan" >/dev/null || fail "seed-plan: $plan"
[[ ! -e "$FAKE_BANK.calls" ]] || fail 'seed-plan moved money'

rm -rf "$STATE_DIR"/seed-restore-*
out=$("$MAINT" seed-restore) || fail "seed-restore: $out"
grep -q 'restore done: 6 transfers (orderId seed-restore-[0-9TZ]*, journal .*), ledger imbalance 0' <<<"$out" || fail "seed-restore log: $out"
for kv in ACC-1001=5000000.00 ACC-1002=3200000.00 ACC-1004=800000.00 ACC-1005=12000000.00 ACC-1006=450000.00 ACC-1007=2750000.00; do
  [[ "$(bank_of "${kv%%=*}")" == "${kv#*=}" ]] || fail "after restore ${kv%%=*} is $(bank_of "${kv%%=*}")"
done
[[ "$(bank_of ACC-1003)" == "2220824.23" ]] || fail "ACC-1003 keeps the rest: $(bank_of ACC-1003)"
journal=$(ls "$STATE_DIR"/seed-restore-*.journal)
[[ $(wc -l <"$journal") -eq 6 ]] || fail 'journal lines'
grep -q '"orderId":"seed-restore-' "$FAKE_BANK.posts" || fail 'restore transfers are not tagged'
# A second run has nothing to do.
"$MAINT" seed-restore | grep -q 'seed-restore: nothing to do' || fail 'second restore moved money'

# Undo puts every balance back where it was.
out=$("$MAINT" seed-undo "$journal") || fail "seed-undo: $out"
grep -q 'undo done: 6 transfers' <<<"$out" || fail "seed-undo log: $out"
[[ "$(bank_of ACC-1007)" == "175782.69" && "$(bank_of ACC-1003)" == "21935084.25" ]] || fail 'undo did not restore balances'

# Never during a recording, never with a ledger out of balance, stop on the first failed transfer.
drained_bank
rc=0; out=$(FAKE_PHASE=cycle_normal "$MAINT" seed-restore) || rc=$?
[[ $rc -eq 1 ]] && grep -q 'seed-restore refused: queue phase cycle_normal' <<<"$out" || fail "restore during recording: $out"
[[ ! -e "$FAKE_BANK.calls" ]] || fail 'restore during recording moved money'
rc=0; out=$(FAKE_IMBALANCE=-36000 "$MAINT" seed-restore) || rc=$?
[[ $rc -eq 1 ]] && grep -q 'ledger imbalance -36000' <<<"$out" || fail "imbalance not caught: $out"
drained_bank
rc=0; out=$(FAKE_TRANSFER_FAIL_AT=3 "$MAINT" seed-restore) || rc=$?
[[ $rc -eq 1 ]] && grep -q 'restore stopped: transfer ACC-1003 -> ACC-1004 .* after 2 done' <<<"$out" || fail "failed transfer: $out"
# Less money in the seven than their seed total: refused unless a fund account is named.
drained_bank
sed -i 's/^ACC-1003|21935084.25/ACC-1003|1000000.00/' "$FAKE_BANK"
rc=0; out=$("$MAINT" seed-restore) || rc=$?
[[ $rc -eq 1 ]] && grep -q 'seed-restore refused: .*"shortfall"' <<<"$out" || fail "shortfall not refused: $out"
out=$(SEED_FUND_ACCOUNT=commerce-settlement "$MAINT" seed-restore) || fail "funded restore: $out"
for kv in ACC-1001=5000000.00 ACC-1002=3200000.00 ACC-1003=1500000.00 ACC-1004=800000.00 ACC-1005=12000000.00 ACC-1006=450000.00 ACC-1007=2750000.00; do
  [[ "$(bank_of "${kv%%=*}")" == "${kv#*=}" ]] || fail "after funded restore ${kv%%=*} is $(bank_of "${kv%%=*}")"
done
grep -q '"fromAccount":"commerce-settlement"' "$FAKE_BANK.posts" || fail 'fund account not used'

rc=0; "$MAINT" >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 ]] || fail "no subcommand exited $rc, want 2"

printf '[PASS] oracle maintenance tests\n'
