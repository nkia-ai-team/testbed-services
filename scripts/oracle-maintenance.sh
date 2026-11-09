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
# check is a single read-only query and runs regardless of recording.
#
# Install (tb-ops root crontab; the script and its log path are the only state):
#   23 * * * * /opt/testbed/oracle-maintenance.sh purge >> /var/log/oracle-maintenance.log 2>&1
#   7 */6 * * * /opt/testbed/oracle-maintenance.sh check >> /var/log/oracle-maintenance.log 2>&1
# A check above the threshold exits 3 and leaves $STATE_DIR/ALERT; the next check
# below it removes the file.
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
    find "$home/$sub" -mindepth 1 -type d -empty -delete
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

# One line: user data bytes | datafile bytes (context only). sqlplus -s exits 0 on
# ORA errors, so `whenever sqlerror` makes them a failure and the line is validated.
CAPACITY_SQL=$(cat <<SQL
whenever sqlerror exit failure
set pages 0 feedback off heading off lines 200
alter session set container=$ORACLE_PDB;
select (select nvl(sum(s.bytes),0) from dba_segments s join dba_users u on u.username = s.owner where u.oracle_maintained = 'N')||'|'||(select nvl(sum(bytes),0) from dba_data_files) from dual;
exit;
SQL
)

check() {
  local line user_bytes datafile_bytes now
  if ! line=$(k exec -i "$ORACLE_POD" -- sqlplus -s / as sysdba <<<"$CAPACITY_SQL" | tr -d '[:space:]'); then
    log "check failed: query did not run"
    return 1
  fi
  if [[ ! "$line" =~ ^[0-9]+\|[0-9]+$ ]]; then
    log "check failed: unreadable answer '$line'"
    return 1
  fi
  IFS='|' read -r user_bytes datafile_bytes <<<"$line"
  now=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
  mkdir -p "$STATE_DIR"
  printf '{"measured_at":"%s","user_data_bytes":%s,"datafile_bytes":%s,"alert_bytes":%s}\n' \
    "$now" "$user_bytes" "$datafile_bytes" "$CAPACITY_ALERT_BYTES" >"$STATE_DIR/last.json.tmp"
  mv -f "$STATE_DIR/last.json.tmp" "$STATE_DIR/last.json"
  if (( user_bytes > CAPACITY_ALERT_BYTES )); then
    printf '%s banking Oracle user data %s bytes > %s (12GB cap: writes fail with ORA-12954)\n' \
      "$now" "$user_bytes" "$CAPACITY_ALERT_BYTES" >"$STATE_DIR/ALERT"
    log "ALERT user data ${user_bytes} bytes above ${CAPACITY_ALERT_BYTES} (datafiles ${datafile_bytes})"
    return 3
  fi
  rm -f "$STATE_DIR/ALERT"
  log "check ok: user data ${user_bytes} bytes, threshold ${CAPACITY_ALERT_BYTES} (datafiles ${datafile_bytes})"
}

case "${1:-}" in
  purge) purge ;;
  check) check ;;
  *) echo "usage: $0 purge|check" >&2; exit 2 ;;
esac
