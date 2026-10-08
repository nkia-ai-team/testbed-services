#!/usr/bin/env python3
"""Lock a single application database account and unlock it again.

F35-R reconstructs a credential rotation that disabled the old database user
while the services still logged in with it (Harness 2026-01-08).  `ACCOUNT LOCK`
refuses every *new* login (ORA-28000) but leaves the sessions already open alone,
so the services keep working until their Hikari pools retire those connections
(max-lifetime 10 min) and cannot replace them.  That delay is the scenario: the
change happens once, the damage arrives as the pools drain.

The blast radius is one account.  The database stays up, the DPM monitoring
account (LUCIDA_MON) and SYS keep logging in, and no table, row or password is
touched.  The inverse is a single statement (`ACCOUNT UNLOCK`), which is why this
is a profile of its own rather than a mode bolted onto `db.table_readonly` (one
table) or `app.control` (a control row the services read).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.account"

# Oracle only, and only the banking application account.  Widening this dict is
# a design change: LUCIDA_MON is the observer, and locking it would blind DPM
# instead of breaking the services.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F35-R": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "pod": "testbed-oracle-0",
        "pdb": "FREEPDB1",
        "account": "BANKING",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified database account contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified account contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    scenario_id = plan["scenario"]["id"]
    validate(scenario_id, p, {})
    # The scenario id stays on this side of kubectl: nothing that reaches the
    # database carries it.
    argv = [
        "/usr/bin/bash", "-s", "--", action,
        p["namespace"], p["pod"], p["pdb"], p["account"],
    ]
    return argv, ORACLE_REMOTE


ORACLE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; pdb="$4"; account="$5"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")

# DBA_USERS.ACCOUNT_STATUS is the authority. Never trust the ALTER's exit status
# alone: locking an already locked account also succeeds.
state() {
  # feedback off first, or "Session altered." folds into the value (see
  # db_table_readonly_executor).
  printf 'set pages 0 feedback off heading off\nalter session set container=%s;\nselect account_status from dba_users where username='"'"'%s'"'"';\nexit;\n' \
    "$pdb" "$account" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]'
}
flip() {
  printf 'alter session set container=%s;\nalter user %s account %s;\nexit;\n' \
    "$pdb" "$account" "$1" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba >/dev/null
}
expect() { [[ "$(state)" == "$1" ]]; }

case "$action" in
  preflight)
    # The account must exist and be plainly OPEN, or the run would "succeed"
    # against an account an earlier, uncleaned run left locked.
    expect OPEN
    ;;
  run)
    expect OPEN
    flip lock
    expect LOCKED
    ;;
  cleanup)
    flip unlock
    expect OPEN
    ;;
  recovery)
    # Logins work again; the pools refill on their own and readiness follows.
    expect OPEN
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
