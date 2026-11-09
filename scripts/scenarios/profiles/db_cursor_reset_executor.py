#!/usr/bin/env python3
"""Empty one statement's shared cursors after a run and prove its plan came back.

F74-R saturates the banking Oracle with the transfer history page count
(sql_id byb2y2hv7p4ag).  While it runs, the arrival poll binds that statement
with a different null shape, Oracle builds a new child cursor for it with the
poll's peeked binds, and that child carries another plan (847607780, CPU about
1.4 to 3 seconds per execute instead of about 0.5 for 2603328921).  Nothing
removes the child when the poll stops: the steady load keeps sharing it.  After
the 2026-10-24 05:35Z run the bank stayed slow for about 33 hours, until the
automatic statistics job invalidated the cursor on 10-25 13:02Z
(runbook-testbed-move-109-to-66 §8-11).  That residue is not part of the
scenario; it poisoned the normal window of every bank recording after it.

So this profile rides along as a companion and owns the residue:

- preflight and run are read-only.  They refuse to start when an executed
  child of the statement already carries a plan other than the expected one,
  which is the "plan is back before the next bank recording" check.
- cleanup empties the statement's cursors with DBMS_SHARED_POOL.PURGE (flag
  'C', the parent's address and hash value, every child at once) and checks
  that no child with another plan is left.
- recovery waits for the steady load to parse and execute the statement again
  and passes only when every executed child carries the expected plan.  A
  failure here fails the profile's cleanup (profile-control), so a run never
  ends clean with the plan still stuck.

Why a purge and not the alternatives:
- Statistics with no_invalidate=>FALSE also drop the cursors, but they rewrite
  the table statistics (the optimizer's input for every statement on the
  table) and rescan about 7 million rows on a two-CPU database.  The purge
  touches one statement's cursors and nothing else.
- SQL plan management (a baseline) is ruled out by the 2026-11-09 decision: it
  would pin the plan for every later scenario, including the ones whose point
  is a plan change.

The production runner cleans the primary profile first and the companions
after it, so the poll has stopped when the purge runs; otherwise the poll would
parse the bad plan again and recovery would fail (closed, not silently).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.cursor_reset"

# Oracle only, one statement per scenario.  The expected plan is a measured
# value, not a snapshot taken at run time: a snapshot would record a plan that
# is already stuck as the one to come back to.  2603328921 is the plan on 109
# (before 10-24 and after 10-25 15Z) and on 66 (2026-11-09, one child, 215,897
# executions since 10-17, 0.527 s CPU per execute).  If a legitimate change
# (new statistics after a golden rebuild) moves it, preflight refuses and the
# contract is measured again.  Widening this dict is a design change.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F74-R": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "pod": "testbed-oracle-0",
        "pdb": "FREEPDB1",
        "sql_id": "byb2y2hv7p4ag",
        "expected_plan_hash_value": 2603328921,
        # The steady load runs the statement about 6.5 times a minute (66,
        # 215,897 executions over 23 days), so a re-parse is due within a
        # minute or two.  The bound stays well inside the controller's 10m
        # cleanup budget after the load profile's own cleanup.
        "reparse_wait_seconds": 300,
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified cursor reset contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified cursor reset contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    # The scenario id stays on this side of kubectl: nothing that reaches the
    # database carries it.
    argv = [
        "/usr/bin/bash", "-s", "--", action,
        p["namespace"], p["pod"], p["pdb"], p["sql_id"],
        str(p["expected_plan_hash_value"]), str(p["reparse_wait_seconds"]),
    ]
    return argv, ORACLE_CURSOR_REMOTE


ORACLE_CURSOR_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; pdb="$4"; sql_id="$5"; expected="$6"; wait_s="$7"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
q="'"
[[ "$sql_id" =~ ^[a-z0-9]{13}$ && "$expected" =~ ^[0-9]+$ && "$wait_s" =~ ^[0-9]+$ ]] || exit 2

sqlplus_in() { "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba; }

# v$sql is the authority. One line: children | executed children | children on
# another plan | executed children on another plan | the other plans. sqlplus -s
# exits 0 on ORA errors, so `whenever sqlerror` turns them into a failure and an
# unparsable line fails the caller too.
state() {
  printf '%s\n' 'whenever sqlerror exit failure' 'set pages 0 feedback off heading off lines 400' \
    "alter session set container=$pdb;" \
    "select count(*)||'|'||count(case when executions>0 then 1 end)||'|'||count(case when plan_hash_value<>$expected then 1 end)||'|'||count(case when executions>0 and plan_hash_value<>$expected then 1 end)||'|'||nvl(listagg(distinct case when plan_hash_value<>$expected then to_char(plan_hash_value) end, ',') within group (order by 1),'-') from v\$sql where sql_id=$q$sql_id$q;" \
    'exit;' | sqlplus_in | tr -d '[:space:]'
}
read_state() {
  local line
  line=$(state)
  [[ "$line" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[-0-9,]+$ ]] || { echo "unreadable cursor state: '$line'" >&2; return 1; }
  IFS='|' read -r children executed other executed_other other_plans <<<"$line"
}

# Every child of the parent goes in one call (flag C, "address,hash_value").
# Nothing to purge when the parent is not loaded.
purge() {
  printf '%s\n' 'whenever sqlerror exit failure' 'set feedback off' \
    "alter session set container=$pdb;" \
    "begin for c in (select address||','||hash_value as name from v\$sqlarea where sql_id=$q$sql_id$q) loop sys.dbms_shared_pool.purge(c.name, 'C'); end loop; end;" \
    '/' 'exit;' | sqlplus_in >/dev/null
}

package_ready() {
  local status
  status=$(printf '%s\n' 'whenever sqlerror exit failure' 'set pages 0 feedback off heading off' \
    "select status from dba_objects where owner='SYS' and object_name='DBMS_SHARED_POOL' and object_type='PACKAGE BODY';" \
    'exit;' | sqlplus_in | tr -d '[:space:]')
  [[ "$status" == VALID ]] || { echo "DBMS_SHARED_POOL is not usable: '$status'" >&2; return 1; }
}

# The plan already in use must be the expected one. A cursor that is not loaded
# at all (aged out) is no evidence of a stuck plan.
not_stuck() {
  read_state
  if (( executed_other > 0 )); then
    echo "sql_id $sql_id runs on plan $other_plans, expected $expected" >&2
    return 1
  fi
}

case "$action" in
  preflight)
    package_ready
    not_stuck
    ;;
  run)
    # Read-only: the poll (the primary profile) is the fault. This only proves
    # the run starts from the expected plan.
    not_stuck
    ;;
  cleanup)
    package_ready
    # A child still pinned by an execution leaves the pool when it is released,
    # so look again before calling it stuck; a re-parse onto another plan is
    # purged once more and then reported.
    for attempt in 1 2 3; do
      purge
      sleep 5
      read_state
      (( other == 0 )) && exit 0
      echo "attempt $attempt: sql_id $sql_id still has a child on plan $other_plans" >&2
    done
    exit 1
    ;;
  recovery)
    # Read-only. Wait for the steady load to parse and run the statement again.
    deadline=$((SECONDS + wait_s))
    while :; do
      read_state
      if (( executed_other > 0 )); then
        echo "sql_id $sql_id came back on plan $other_plans, expected $expected" >&2
        exit 1
      fi
      (( executed > 0 )) && exit 0
      if (( SECONDS >= deadline )); then
        echo "sql_id $sql_id was not executed again within ${wait_s}s (children=$children)" >&2
        exit 1
      fi
      sleep 5
    done
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
