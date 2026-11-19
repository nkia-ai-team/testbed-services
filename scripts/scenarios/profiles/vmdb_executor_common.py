#!/usr/bin/env python3
"""Shared body of the db01 executors vmdb.lock and vmdb.workload (docs/spec-testbed-vm-3tier.md §6-1).

The VM counterparts of the db.* executors, through psql on db01 itself. The session runs as the
postgres OS account (setpriv, no PAM session and no log line) inside a named transient unit (the
contract's unit name and description, a maintenance job's plain words), and it names itself with the
contract's application_name so the undo can end exactly its own backends: stopping psql does not end a
backend that sits in pg_sleep or a long statement (no client_connection_check_interval).

What reaches 119 (no DPM here, design §1): the unit's journal lines, the processes in the process
sample, db01's CPU, memory and IO series, and server log lines that match the log monitor patterns
(lock waits past deadlock_timeout, statement errors). The connection lines are LOG level and stay in
db01's local server log (§6-4).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError
import vm_executor_common as vm

DB_NAME = "petition"
TABLES = ("petition", "petition_history", "petition_answer", "petition_attachment", "app_user",
          "department", "stats_dept_monthly", "batch_run")
LOCK_MODES = ("ACCESS EXCLUSIVE", "EXCLUSIVE", "SHARE ROW EXCLUSIVE", "SHARE")
# Read statements a workload may repeat. Each scans a large table the application reads with an index.
STATEMENTS = {
    "petition-title-scan": "SELECT count(*) FROM petition WHERE title LIKE '%' || md5(random()::text) || '%'",
    "history-note-scan": "SELECT count(*) FROM petition_history WHERE note LIKE '%' || md5(random()::text) || '%'",
    "petition-body-scan": "SELECT count(*) FROM petition WHERE body LIKE '%' || md5(random()::text) || '%'",
}
APP_NAME_KEYS = ("app_name",)


def app_name(value: Any) -> str:
    return vm.neutral_name(value, "app_name")


def common_args(p: dict[str, Any]) -> list[str]:
    if vm.vm_named(p, ("db",)) != "db01":
        raise ExecutorError("vmdb executors run on db01")
    return [vm.neutral_name(p["unit"], "unit"), vm.description(p["description"]), app_name(p["app_name"])]


# set -- <act> <unit> <description> <app name> <kind> <sql> <sessions> <runtime>
NODE_BODY = vm.UNIT_FUNCTIONS + br'''act="$1"; unit="$2"; description="$3"; app="$4"; kind="$5"; sql="$6"; sessions="$7"; runtime="$8"
job="$unit"
db="${PETITION_DB:-petition}"
pg_setpriv="${PG_SETPRIV:-setpriv}"
as_postgres() { "$pg_setpriv" --reuid=postgres --regid=postgres --init-groups --reset-env -- "$@"; }
backends() {
  as_postgres psql -X -q -At -d "$db" -c "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$app' AND pid <> pg_backend_pid()"
}
end_backends() {
  as_postgres psql -X -q -At -d "$db" -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = '$app' AND pid <> pg_backend_pid()" >/dev/null
}
state() {
  if unit_active || [[ "$(backends)" != 0 ]]; then echo ours; return 0; fi
  echo default
}
check() {
  command -v psql >/dev/null
  [[ "$(as_postgres psql -X -q -At -d "$db" -c 'SELECT 1')" == 1 ]] || { echo "psql cannot reach $db" >&2; exit 1; }
  if unit_active; then echo "$unit is already running" >&2; exit 1; fi
}
undo_body() {
  unit_undo
  printf '%q --reuid=postgres --regid=postgres --init-groups --reset-env -- psql -X -q -At -d %q -c %q >/dev/null || true\n' \
    "$pg_setpriv" "$db" "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = '$app' AND pid <> pg_backend_pid()"
}
apply() {
  if [[ "$kind" == lock ]]; then
    unit_start /usr/bin/env PGAPPNAME="$app" "$pg_setpriv" --reuid=postgres --regid=postgres --init-groups -- \
      psql -X -q -d "$db" -c "BEGIN; $sql; SELECT pg_sleep($runtime); COMMIT;"
  else
    unit_start /usr/bin/env PGAPPNAME="$app" "$pg_setpriv" --reuid=postgres --regid=postgres --init-groups -- \
      /bin/bash -c 'end=$(( SECONDS + $2 )); for _ in $(seq 1 "$3"); do ( while (( SECONDS < end )); do psql -X -q -d "$4" -c "$1" >/dev/null || sleep 1; done ) & done; wait' \
      workload "$sql" "$runtime" "$sessions" "$db"
  fi
  local _
  for _ in $(seq 1 25); do if unit_active; then return 0; fi; sleep 0.2; done
  unit_active
}
remove() { unit_stop; end_backends; }
'''
