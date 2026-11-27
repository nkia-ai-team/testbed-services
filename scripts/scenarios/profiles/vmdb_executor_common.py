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

import re
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
    node = vm.vm_named(p, ("db",))
    if node != vm.testbed_of(p).role_vm("db"):
        raise ExecutorError("vmdb executors run on the testbed's database VM")
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


# vmdb.lock mode drop-index (an operator's DDL, design 6-1 change cause through the operator path, 6-2 Q35): the operator
# logs in and drops one index of an application table with `sudo -u postgres psql` (ALTER TABLE ... DROP CONSTRAINT when a
# constraint owns the index). The sudo COMMAND= line in auth.log is the change's own trace. The undo builds the same
# definition again, read from the catalog right before the change (pg_get_constraintdef or pg_get_indexdef), so the
# undo cannot drift from what the server had.
#
# Preflight measures whether the change will hurt (principle 9): how often the application reads through the index now
# (the index's idx_scan over a few seconds) times what one read costs without it (one serial full scan of the table with
# the index's first column compared, timed), against the server's cores. Below the contract's percent the run is refused:
# at a quiet hour the scans fit in the cores and only slow the reads a little.
INDEX_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
PROBE_SECONDS = 6


def index_name(value: Any) -> str:
    if not isinstance(value, str) or not INDEX_NAME.fullmatch(value):
        raise ExecutorError("index must be a lowercase PostgreSQL identifier")
    words = set(re.split(r"_", value))
    for word in vm.FORBIDDEN_WORDS:
        if word in words or (len(word) > 3 and word in value):
            raise ExecutorError(f"index must not carry the word {word}")
    return value


# set -- <act> <table> <index> <load percent> <probe seconds>
DDL_BODY = vm.OPERATOR_FUNCTIONS + br'''act="$1"; table="$2"; index="$3"; load_pct="$4"; probe="$5"
job="ddl-$index"
db="${PETITION_DB:-petition}"
pg_setpriv="${PG_SETPRIV:-setpriv}"
state_root="${OPSJOB_STATE_ROOT:-/var/lib/opsjobs}"
deffile="$state_root/$job.sql"
as_postgres() { "$pg_setpriv" --reuid=postgres --regid=postgres --init-groups --reset-env -- "$@"; }
pq() { as_postgres psql -X -q -At -d "$db" -c "$1"; }
# The index, valid, on the contract's table (a constraint's index included).
present() {
  [[ "$(pq "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = '$index' AND i.indrelid = '$table'::regclass AND i.indisvalid")" == 1 ]]
}
# The statement that builds it again: the owning constraint's when there is one, else the index's own.
definition() {
  pq "SELECT coalesce((SELECT format('ALTER TABLE %s ADD CONSTRAINT %I %s', con.conrelid::regclass, con.conname, pg_get_constraintdef(con.oid)) FROM pg_constraint con WHERE con.conindid = c.oid AND con.conrelid = '$table'::regclass), pg_get_indexdef(c.oid)) FROM pg_class c WHERE c.relname = '$index' AND c.relkind = 'i'"
}
owned_by_constraint() {
  [[ "$(pq "SELECT count(*) FROM pg_constraint WHERE conname = '$index' AND conrelid = '$table'::regclass")" == 1 ]]
}
state() {
  if present; then echo default; return 0; fi
  if [[ -s "$deffile" ]]; then echo ours; return 0; fi
  echo missing
}
# One serial full read of the table comparing the index's first column, the way a read that lost the index runs.
scan_ms() {
  local col t0 t1
  col=$(pq "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = '$index'")
  [[ -n "$col" ]] || { echo "cannot read the first column of $index" >&2; return 1; }
  t0=$(date +%s%N)
  pq "SET max_parallel_workers_per_gather = 0; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_indexonlyscan = off; SELECT count(*) FROM $table WHERE $col::text = ''" >/dev/null
  t1=$(date +%s%N)
  echo $(( (t1 - t0) / 1000000 ))
}
reads() { pq "SELECT coalesce(sum(idx_scan), 0) FROM pg_stat_user_indexes WHERE indexrelname = '$index'"; }
check() {
  local a b ms t0 t1 cores need have
  command -v psql >/dev/null
  [[ "$(pq 'SELECT 1')" == 1 ]] || { echo "psql cannot reach $db" >&2; exit 1; }
  if [[ ! -s "$deffile" ]] && ! present; then echo "$index is not on $table" >&2; exit 1; fi
  if ! present; then return 0; fi
  if [[ -z "$op_home" || ! -d "$op_home" ]]; then echo "no home for $operator" >&2; exit 1; fi
  cores="${DB_CORES:-$(nproc)}"
  t0=$(date +%s%N); a=$(reads)
  ms=$(scan_ms)
  t1=$(date +%s%N); sleep "$(awk -v p="$probe" -v d="$(( (t1 - t0) / 1000000 ))" 'BEGIN { s = p - d / 1000; if (s < 0) s = 0; printf "%.2f", s }')"
  b=$(reads); t1=$(date +%s%N)
  # demand in milli-cores: reads per second x one scan in ms; it has to reach cores x load percent.
  have=$(( (b - a) * ms * 1000000000 / (t1 - t0) ))
  need=$(( cores * 10 * load_pct ))
  echo "reads $(( b - a )) in $(( (t1 - t0) / 1000000 )) ms, one scan ${ms} ms, demand ${have} of ${need} milli-cores (${cores} cores x ${load_pct} %)" >&2
  if (( have < need )); then echo "the reads through $index would not fill the cores now; try at a busier hour" >&2; exit 1; fi
}
prepare() {
  mkdir -p "$state_root"; chmod 700 "$state_root"
  definition >"$deffile.tmp"
  [[ -s "$deffile.tmp" ]] || { echo "cannot read the definition of $index" >&2; exit 1; }
  mv -T "$deffile.tmp" "$deffile"
  boot_files+=("$deffile")
}
undo_body() {
  local def
  def=$(cat "$deffile")
  printf 'if [[ "$(%q --reuid=postgres --regid=postgres --init-groups --reset-env -- psql -X -q -At -d %q -c %q)" != 1 ]]; then\n' \
    "$pg_setpriv" "$db" "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = '$index' AND i.indisvalid"
  printf '  %q --reuid=postgres --regid=postgres --init-groups --reset-env -- psql -X -q -d %q -c %q\n' "$pg_setpriv" "$db" "$def"
  printf 'fi\n'
  printf 'rm -f %q\n' "$deffile"
}
apply() { echo "an operator change goes through the operator session" >&2; exit 2; }
# What is left after the operator's undo (or all of it when the operator session could not run): root builds it again.
remove() {
  if ! present && [[ -s "$deffile" ]]; then pq "$(cat "$deffile")"; fi
  if present; then rm -f "$deffile"; fi
}
# The operator's commands: psql as the database's own account, one statement each.
op_apply_script() {
  if owned_by_constraint; then
    op_line sudo -u postgres psql -X -d "$db" -c "ALTER TABLE $table DROP CONSTRAINT $index"
  else
    op_line sudo -u postgres psql -X -d "$db" -c "DROP INDEX $index"
  fi
}
op_remove_script() {
  if ! present && [[ -s "$deffile" ]]; then op_line sudo -u postgres psql -X -d "$db" -c "$(cat "$deffile")"; fi
}
'''
