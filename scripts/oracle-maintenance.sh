#!/usr/bin/env bash
# Banking Oracle (testbed-oracle-0) housekeeping on tb-ops: ADR trace cleanup and a
# watch on the Oracle Free 12GB user-data cap.
#
#   oracle-maintenance.sh purge   delete ADR diagnostic files older than the retention,
#                                 only while no scenario is being recorded
#   oracle-maintenance.sh check   measure user data (segments of non-Oracle-maintained
#                                 accounts in the PDB) and raise an alert file above the
#                                 threshold
#
# Why: 09-29 the bank's Oracle hit the 12GB cap (ORA-12954) mid-recording and every
# write failed. The cap counts user data, not the whole datafile set (user decision
# 11-09: stop line 10.5GB on user data). 66 on 11-09: user segments 6.15GB, datafiles
# 10.83GB, ADR 153MB. The ADR is on the same local-path PVC as the datafiles (node
# root filesystem), and the slim image has no adrci, so the purge is by file age.
#
# purge never runs during a recording: the runner must report no active run and a
# queue phase outside the recording cycle (idle, paused, completed). If the runner
# cannot be asked, it skips. A skipped purge is retried by the next timer tick.
# check is read-only (two queries) and runs regardless of recording. It also watches the
# bank load's transfer pool: its lowest balance against a floor, and that every pool
# account still exists and is ACTIVE.
#
# Install (tb-ops root crontab; the script and its log path are the only state):
#   23 * * * * /opt/testbed/oracle-maintenance.sh purge >> /var/log/oracle-maintenance.log 2>&1
#   7 */6 * * * /opt/testbed/oracle-maintenance.sh check >> /var/log/oracle-maintenance.log 2>&1
#   install -m 644 core-banking/loadgen/transfer-accounts.txt /opt/testbed/banking-transfer-accounts.txt
# A check with any reason (user data above the threshold, a pool account below the floor
# or missing or not ACTIVE) exits 3 and writes one line per reason to $STATE_DIR/ALERT;
# the next clean check removes the file.
set -Eeuo pipefail

KUBECONFIG_PATH="${KUBECONFIG_PATH:-/root/tb-kubeconfig}"
ORACLE_NAMESPACE="${ORACLE_NAMESPACE:-rca-testbed-banking}"
ORACLE_POD="${ORACLE_POD:-testbed-oracle-0}"
ORACLE_PDB="${ORACLE_PDB:-FREEPDB1}"
DIAG_ROOT="${DIAG_ROOT:-/opt/oracle/diag}"
RUNNER_URL="${RUNNER_URL:-http://127.0.0.1:8091}"
# Diagnostic files untouched for this long are deleted (ADR's own short policy is 30
# days and has not kept the directory small: 582 trace files back to 10-18 on 66).
RETENTION_HOURS="${RETENTION_HOURS:-168}"
# The listener writes two single files that only grow (66 11-09: listener.log 38MB,
# log.xml 61MB since 10-15, about 4MB a day). Above this size they are emptied.
LISTENER_LOG_MAX_MB="${LISTENER_LOG_MAX_MB:-256}"
# 10.5 GiB of user data: 1.5 GiB of room below the 12GB cap.
CAPACITY_ALERT_BYTES="${CAPACITY_ALERT_BYTES:-11274289152}"
STATE_DIR="${STATE_DIR:-/var/lib/lucida/oracle-capacity}"
# The bank load's transfer pool (core-banking/loadgen/transfer-accounts.txt), installed
# beside this script. Floor 500,000: ten largest transfers (50,000) of room before the
# first 'Insufficient balance', several days of the pool's normal drift (a heaviest
# companion run moves one account by about 210,000 at one standard deviation; the
# baseline returns what it sends).
POOL_FILE="${POOL_FILE:-/opt/testbed/banking-transfer-accounts.txt}"
POOL_BALANCE_FLOOR="${POOL_BALANCE_FLOOR:-500000}"
# Queue phases outside a recording cycle. Everything else (cycle_reset, cycle_normal,
# cycle_buffer, running, cycle_cooldown, waiting_capture, waiting_clean_window) is
# part of some recording's window or the clean window before the next one.
PURGE_ALLOWED_PHASES="${PURGE_ALLOWED_PHASES:-idle paused completed}"

log() { printf '[oracle-maintenance] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }

k() { kubectl --kubeconfig "$KUBECONFIG_PATH" -n "$ORACLE_NAMESPACE" "$@"; }

# Prints "ok" when a purge may run, otherwise the reason to skip.
recording_state() {
  local active queue
  active=$(curl -fsS -m 10 "$RUNNER_URL/api/active" 2>/dev/null) || { echo "runner unreachable ($RUNNER_URL/api/active)"; return; }
  queue=$(curl -fsS -m 10 "$RUNNER_URL/api/live-queue" 2>/dev/null) || { echo "runner unreachable ($RUNNER_URL/api/live-queue)"; return; }
  ACTIVE="$active" QUEUE="$queue" ALLOWED="$PURGE_ALLOWED_PHASES" python3 - <<'PY'
import json, os
try:
    active = json.loads(os.environ["ACTIVE"])
    queue = json.loads(os.environ["QUEUE"])
except ValueError:
    print("runner answer is not JSON")
    raise SystemExit(0)
allowed = os.environ["ALLOWED"].split()
if active.get("is_active") is not False:
    print(f"run active: {active.get('scenario_id')} {active.get('run_id')}")
elif queue.get("phase") not in allowed:
    print(f"queue phase {queue.get('phase')} (scenario {queue.get('current_scenario_id')})")
else:
    print("ok")
PY
}

# Runs inside the Oracle container as the oracle user. Only files under the ADR
# homes' trace, cdump, incident and hm directories are deleted; alert logs, metadata
# and anything outside DIAG_ROOT are left alone.
PURGE_REMOTE=$(cat <<'SH'
set -euo pipefail
root="$1"; minutes="$2"; listener_max_bytes="$3"
case "$root" in */diag) ;; *) echo "refusing ADR root $root: not a */diag directory" >&2; exit 1 ;; esac
[[ -d "$root" ]] || { echo "no ADR root $root" >&2; exit 1; }
deleted=0; bytes=0
for home in "$root"/*/*/*; do
  [[ -d "$home" ]] || continue
  for sub in trace cdump incident hm; do
    [[ -d "$home/$sub" ]] || continue
    while IFS= read -r -d '' f; do
      case "$f" in
        */trace/listener.log|*/trace/alert_*.log) continue ;;
      esac
      size=$(stat -c %s "$f")
      rm -f -- "$f" && deleted=$((deleted + 1)) && bytes=$((bytes + size))
    done < <(find "$home/$sub" -type f -mmin "+$minutes" -print0)
    # Same age rule for directories: an incident directory being written right now is
    # empty for a moment and must not go. A directory emptied by this run has a fresh
    # mtime and goes on a later run.
    find "$home/$sub" -mindepth 1 -type d -empty -mmin "+$minutes" -delete
  done
done
truncated=""
for f in "$root"/tnslsnr/*/*/trace/listener.log "$root"/tnslsnr/*/*/alert/log.xml; do
  [[ -f "$f" ]] || continue
  if (( $(stat -c %s "$f") > listener_max_bytes )); then
    : > "$f"
    truncated="$truncated ${f#"$root"/}"
  fi
done
echo "deleted=$deleted bytes=$bytes truncated=${truncated:-none}"
SH
)

purge() {
  local state result
  # Deletion is confined to an ADR tree. A mistyped DIAG_ROOT must not become a find
  # over some other directory.
  case "$DIAG_ROOT" in
    */diag) ;;
    *) log "purge refused: DIAG_ROOT $DIAG_ROOT is not a */diag directory"; return 1 ;;
  esac
  state=$(recording_state)
  if [[ "$state" != ok ]]; then
    log "purge skipped: $state"
    return 0
  fi
  if ! result=$(k exec -i "$ORACLE_POD" -- bash -s -- "$DIAG_ROOT" "$((RETENTION_HOURS * 60))" \
    "$((LISTENER_LOG_MAX_MB * 1024 * 1024))" <<<"$PURGE_REMOTE"); then
    log "purge failed in $ORACLE_POD"
    return 1
  fi
  log "purge done (retention ${RETENTION_HOURS}h): $result"
}

# Two lines. CAP|user data bytes|datafile bytes (context only), and for the transfer
# pool POOL|accounts found|lowest balance|account with it|accounts below the floor|
# accounts not ACTIVE. sqlplus -s exits 0 on ORA errors, so `whenever sqlerror` makes
# them a failure and both lines are validated.
capacity_sql() {
  local in_list=$1
  cat <<SQL
whenever sqlerror exit failure
set pages 0 feedback off heading off lines 400
alter session set container=$ORACLE_PDB;
select 'CAP|'||(select nvl(sum(s.bytes),0) from dba_segments s join dba_users u on u.username = s.owner where u.oracle_maintained = 'N')||'|'||(select nvl(sum(bytes),0) from dba_data_files) from dual;
select 'POOL|'||count(*)||'|'||nvl(min(balance),0)||'|'||nvl(min(id) keep (dense_rank first order by balance),'-')||'|'||count(case when balance < $POOL_BALANCE_FLOOR then 1 end)||'|'||count(case when status <> 'ACTIVE' then 1 end) from banking.accounts where id in ($in_list);
exit;
SQL
}

# The transfer pool (core-banking/loadgen/transfer-accounts.txt) as a quoted IN list.
# Ids are checked against the pool's shape before they reach SQL.
pool_in_list() {
  local ids
  [[ -r "$POOL_FILE" ]] || return 1
  ids=$(grep -v '^#' "$POOL_FILE" | grep -v '^[[:space:]]*$') || return 1
  grep -qvE '^ACC-[0-9]{4}$' <<<"$ids" && return 1
  # Twenty ids a line: SQL*Plus refuses very long input lines, a statement may span many.
  awk '{ printf "%s'"'"'%s'"'"'", (NR == 1 ? "" : (NR % 20 == 1 ? ",\n" : ",")), $0 } END { print "" }' <<<"$ids"
}

check() {
  local in_list pool_expected out cap pool user_bytes datafile_bytes now
  local pool_found pool_min pool_min_id pool_below pool_inactive
  if ! in_list=$(pool_in_list); then
    log "check failed: transfer pool file missing or malformed ($POOL_FILE)"
    return 1
  fi
  pool_expected=$(grep -o "'ACC-" <<<"$in_list" | wc -l)
  if ! out=$(k exec -i "$ORACLE_POD" -- sqlplus -s / as sysdba <<<"$(capacity_sql "$in_list")"); then
    log "check failed: query did not run"
    return 1
  fi
  cap=$(tr -d ' \t\r' <<<"$out" | grep '^CAP|' || true)
  pool=$(tr -d ' \t\r' <<<"$out" | grep '^POOL|' || true)
  if [[ ! "$cap" =~ ^CAP\|[0-9]+\|[0-9]+$ || ! "$pool" =~ ^POOL\|[0-9]+\|[0-9.]+\|[A-Za-z0-9-]+\|[0-9]+\|[0-9]+$ ]]; then
    log "check failed: unreadable answer '$(tr '\n' ' ' <<<"$out")'"
    return 1
  fi
  IFS='|' read -r _ user_bytes datafile_bytes <<<"$cap"
  IFS='|' read -r _ pool_found pool_min pool_min_id pool_below pool_inactive <<<"$pool"
  now=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
  mkdir -p "$STATE_DIR"
  printf '{"measured_at":"%s","user_data_bytes":%s,"datafile_bytes":%s,"alert_bytes":%s,"pool_expected":%s,"pool_found":%s,"pool_min_balance":%s,"pool_min_account":"%s","pool_below_floor":%s,"pool_not_active":%s,"pool_floor":%s}\n' \
    "$now" "$user_bytes" "$datafile_bytes" "$CAPACITY_ALERT_BYTES" "$pool_expected" "$pool_found" \
    "$pool_min" "$pool_min_id" "$pool_below" "$pool_inactive" "$POOL_BALANCE_FLOOR" >"$STATE_DIR/last.json.tmp"
  mv -f "$STATE_DIR/last.json.tmp" "$STATE_DIR/last.json"

  local reasons=()
  if (( user_bytes > CAPACITY_ALERT_BYTES )); then
    reasons+=("banking Oracle user data $user_bytes bytes > $CAPACITY_ALERT_BYTES (12GB cap: writes fail with ORA-12954)")
  fi
  if (( pool_below > 0 )); then
    reasons+=("transfer pool: $pool_below accounts below $POOL_BALANCE_FLOOR, lowest $pool_min_id $pool_min (transfers up to 50000 start failing with 'Insufficient balance' 400)")
  fi
  if (( pool_found != pool_expected || pool_inactive > 0 )); then
    reasons+=("transfer pool: $pool_found of $pool_expected accounts found, $pool_inactive not ACTIVE (transfers to or from them are refused)")
  fi
  if (( ${#reasons[@]} > 0 )); then
    local reason
    : >"$STATE_DIR/ALERT.tmp"
    for reason in "${reasons[@]}"; do printf '%s %s\n' "$now" "$reason" >>"$STATE_DIR/ALERT.tmp"; done
    mv -f "$STATE_DIR/ALERT.tmp" "$STATE_DIR/ALERT"
    log "ALERT ${#reasons[@]}: $(paste -sd';' "$STATE_DIR/ALERT")"
    return 3
  fi
  rm -f "$STATE_DIR/ALERT"
  log "check ok: user data ${user_bytes} bytes, threshold ${CAPACITY_ALERT_BYTES} (datafiles ${datafile_bytes}); transfer pool ${pool_found} accounts, lowest ${pool_min_id} ${pool_min}, floor ${POOL_BALANCE_FLOOR}"
}

case "${1:-}" in
  purge) purge ;;
  check) check ;;
  *) echo "usage: $0 purge|check" >&2; exit 2 ;;
esac
