#!/usr/bin/env python3
"""Turn a whole MySQL instance read-only and writable again.

F38-R reconstructs a production database that a client session switched to
read-only mode (Vapi 2025-01-21): reads keep working, every write in every
service fails at once.  `SET GLOBAL read_only = ON` is that state on MySQL 8.0.
Sessions without SUPER or CONNECTION_ADMIN get ER_OPTION_PREVENTS_STATEMENT
(1290, "running with the --read-only option") on the first INSERT, UPDATE or
DELETE, while SELECT, connection checks (ping) and readiness stay green.  The
rejected statement is not counted in Com_insert / Com_update (verified on a
local mysql:8.0.46), so DPM sees writes stop while reads go on.

The blast radius is one instance and one variable.  No table, row, account or
connection is touched, and nothing is persisted (`SET GLOBAL`, not
`SET PERSIST`): a MySQL restart also clears it.  The inverse is a single
statement, which is why this is a profile of its own rather than a mode bolted
onto `db.table_readonly` (one Oracle table, F14-P) or `db.ddl` (schema).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.instance_readonly"

# MySQL only, and only the food instance.  `app_account` is the account whose
# writes must be refused: preflight proves it cannot bypass read_only, or the
# run would flip the flag and nothing would fail.  Widening this dict is a
# design change.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F38-R": {
        "engine": "mysql",
        "namespace": "rca-testbed-food",
        "pod": "testbed-mysql-0",
        "app_account": "fooddelivery",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified read-only instance contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified read-only instance contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    scenario_id = plan["scenario"]["id"]
    validate(scenario_id, p, {})
    # The scenario id stays on this side of kubectl: nothing that reaches the
    # database carries it.
    argv = [
        "/usr/bin/bash", "-s", "--", action,
        p["namespace"], p["pod"], p["app_account"],
    ]
    return argv, MYSQL_REMOTE


MYSQL_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; app="$4"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# The first MySQL error line goes to stderr so a failed flip is readable in the runner log.
mysql() { "${k[@]}" exec "$pod" -- env SQL="$1" sh -lc 'e=$(mktemp); mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" -e "$SQL" 2>"$e"; rc=$?; grep -v "Using a password" "$e" | head -n 1 >&2; rm -f "$e"; exit $rc'; }

# @@global.read_only and @@global.super_read_only are the authority ("00" writable,
# "10" read-only for ordinary accounts). Never trust the SET's exit status alone:
# setting an already set variable also succeeds.
state() { mysql "SELECT CONCAT(@@global.read_only, @@global.super_read_only);" | tr -d '[:space:]'; }
expect() { [[ "$(state)" == "$1" ]]; }
# An account with SUPER or CONNECTION_ADMIN ignores read_only; then the flag would
# change and no write would fail.
bypass() {
  mysql "SELECT (SELECT COUNT(*) FROM mysql.user WHERE user='$app' AND super_priv='Y') + (SELECT COUNT(*) FROM mysql.global_grants WHERE user='$app' AND priv IN ('CONNECTION_ADMIN','SUPER'));" | tr -d '[:space:]'
}
# lock_wait_timeout bounds the global read lock wait behind table locks (server default is a year).
flip() { mysql "SET SESSION lock_wait_timeout=10; SET GLOBAL read_only=$1;" >/dev/null; }

case "$action" in
  preflight)
    # Writable now, and the application account cannot bypass the flag, or the
    # run would "succeed" against an instance an earlier, uncleaned run left
    # read-only, or change nothing at all.
    expect 00
    [[ "$(bypass)" == 0 ]]
    ;;
  run)
    expect 00
    [[ "$(bypass)" == 0 ]]
    flip ON
    expect 10
    ;;
  cleanup)
    flip OFF
    expect 00
    ;;
  recovery)
    # Writes work again at once; nothing was queued or lost on the database side.
    expect 00
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
