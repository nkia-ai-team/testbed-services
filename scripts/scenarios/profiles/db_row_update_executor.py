#!/usr/bin/env python3
"""Re-open a window of finished rows the way an operator's correction tool does, and put them back.

F51-P reconstructs AWS S3's 2017-02-28 outage: an operator ran an established
playbook command meant for a few targets, one of its inputs was entered wrongly,
and the command acted on a much larger set than intended, taking away capacity
the live service depended on (Atlassian 2022-04-05 is the same shape: the script
trusted the ids it was handed).  Here the command is a food re-dispatch
correction: it should have put back on the road a handful of deliveries whose
couriers' app had wrongly marked them delivered, but the order-id filter was
left empty and the tool applied only its time window, so every delivery
finished in the last four hours in fooddelivery.dispatches is set back to
ASSIGNED with a fresh assignment time.  dispatch-service counts ASSIGNED rows
as busy couriers (dispatch.max-capacity 2000), so the courier pool reads full
at once and order-service rejects every new order with 503 at its capacity
check, while browsing, order lookups and payment keep working.

Like a careful correction tool it first copies the affected rows' id, status and
assignment time into a backup table in the same database, then updates exactly
the backed-up rows.  That backup is also the inverse: cleanup writes the saved
status and assignment time back onto the same ids and drops the backup.  No
schema of the live table, other row, pod or configuration is touched, and
neither the backup name nor any statement carries the scenario id: it stays on
this side of kubectl.  Run and cleanup must finish inside the shortest ETA
(15 minutes) so dispatch's expiry batch never delivers a re-opened row.
"""
from __future__ import annotations

import re
from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.row_update"

# MySQL only, the food dispatches table, one status transition and one window.
# Widening this dict is a design change: re-opening more than the window turns
# the incident into a longer one the expiry batch then "delivers" (thousands of
# duplicate delivery events), and touching another table is a different outage.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F51-P": {
        "engine": "mysql",
        "namespace": "rca-testbed-food",
        "pod": "testbed-mysql-0",
        "database": "fooddelivery",
        "table": "dispatches",
        "backup_table": "dispatches_redispatch_backup",
        "from_status": "DELIVERED",
        "to_status": "ASSIGNED",
        "window_hours": 4,
        "minimum_rows": 2500,
        "capacity": 2000,
    },
}

IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
STATUS = re.compile(r"^[A-Z]{1,16}$")


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified row update contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified row update contract")
    for name in ("database", "table", "backup_table"):
        if not IDENT.fullmatch(params[name]):
            raise ExecutorError(f"{name} is not a plain MySQL identifier")
    if params["backup_table"] == params["table"]:
        raise ExecutorError("backup table must differ from the live table")
    for name in ("from_status", "to_status"):
        if not STATUS.fullmatch(params[name]):
            raise ExecutorError(f"{name} is not a plain status value")
    if params["from_status"] == params["to_status"]:
        raise ExecutorError("the update must change the status")
    for name in ("window_hours", "minimum_rows", "capacity"):
        if not isinstance(params[name], int) or params[name] <= 0:
            raise ExecutorError(f"{name} must be a positive integer")
    if params["minimum_rows"] <= params["capacity"]:
        raise ExecutorError("the re-opened rows alone must exceed the capacity")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    argv = [
        "/usr/bin/bash", "-s", "--", action,
        p["namespace"], p["pod"], p["database"], p["table"], p["backup_table"],
        p["from_status"], p["to_status"],
        str(p["window_hours"]), str(p["minimum_rows"]), str(p["capacity"]),
    ]
    return argv, MYSQL_REMOTE


MYSQL_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; db="$4"; table="$5"; backup="$6"
from="$7"; to="$8"; hours="$9"; minimum="${10}"; capacity="${11}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
for ident in "$db" "$table" "$backup"; do [[ "$ident" =~ ^[a-z][a-z0-9_]{0,63}$ ]] || exit 2; done
for s in "$from" "$to"; do [[ "$s" =~ ^[A-Z]{1,16}$ ]] || exit 2; done
for n in "$hours" "$minimum" "$capacity"; do [[ "$n" =~ ^[1-9][0-9]*$ ]] || exit 2; done
# The first MySQL error line goes to stderr so a failed step is readable in the runner log.
mysql() { "${k[@]}" exec "$pod" -- env SQL="$1" sh -lc 'e=$(mktemp); mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" -e "$SQL" 2>"$e"; rc=$?; grep -v "Using a password" "$e" | head -n 1 >&2; rm -f "$e"; exit $rc'; }
num() { mysql "$1" | tr -d '[:space:]'; }

# Checks only count rows; none of them names the scenario.
window_rows() { num "SELECT COUNT(*) FROM $db.$table WHERE status = '$from' AND assigned_at >= NOW() - INTERVAL $hours HOUR;"; }
busy_rows() { num "SELECT COUNT(*) FROM $db.$table WHERE status = '$to';"; }
backup_exists() { num "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '$db' AND table_name = '$backup';"; }
backup_rows() { num "SELECT COUNT(*) FROM $db.$backup;"; }
reopened_rows() { num "SELECT COUNT(*) FROM $db.$table d JOIN $db.$backup b ON b.id = d.id WHERE d.status = '$to';"; }
unrestored_rows() { num "SELECT COUNT(*) FROM $db.$table d JOIN $db.$backup b ON b.id = d.id WHERE NOT (d.status <=> b.status AND d.assigned_at <=> b.assigned_at);"; }

check() {
  command -v kubectl >/dev/null
  # No backup left by an earlier, uncleaned run; enough finished rows in the
  # window that they alone fill the pool; and the pool not already full.
  [[ "$(backup_exists)" == 0 ]]
  (( $(window_rows) >= minimum ))
  (( $(busy_rows) < capacity ))
}

case "$action" in
  preflight) check ;;
  run)
    check
    # Backup first (CREATE ... SELECT commits on its own), then re-open exactly
    # the backed-up rows with a fresh assignment time, in one statement.
    mysql "CREATE TABLE $db.$backup AS SELECT id, status, assigned_at FROM $db.$table WHERE status = '$from' AND assigned_at >= NOW() - INTERVAL $hours HOUR;" >/dev/null
    mysql "UPDATE $db.$table d JOIN $db.$backup b ON b.id = d.id SET d.status = '$to', d.assigned_at = NOW() WHERE d.status = '$from';" >/dev/null
    saved="$(backup_rows)"
    (( saved >= minimum ))
    [[ "$(reopened_rows)" == "$saved" ]]
    (( $(busy_rows) >= capacity ))
    ;;
  cleanup)
    if [[ "$(backup_exists)" != 0 ]]; then
      mysql "UPDATE $db.$table d JOIN $db.$backup b ON b.id = d.id SET d.status = b.status, d.assigned_at = b.assigned_at;" >/dev/null
      [[ "$(unrestored_rows)" == 0 ]]
      mysql "DROP TABLE $db.$backup;" >/dev/null
    fi
    # Without the backup the rows must already be back; nothing here invents them.
    [[ "$(backup_exists)" == 0 ]]
    (( $(busy_rows) < capacity ))
    ;;
  recovery)
    [[ "$(backup_exists)" == 0 ]]
    (( $(busy_rows) < capacity ))
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
