#!/usr/bin/env python3
"""Flip a single Oracle table to READ ONLY and back.

F14-P needs ledger INSERTs to fail *immediately and only for the ledger table*.
A lock will not do it: a blocked INSERT waits instead of raising, so the consumer
stalls (that is F04-P's shape) rather than losing the event.  `READ ONLY` raises
ORA-12081 on the first write, which is what `TransferEventConsumer` swallows.

The blast radius is one table.  `banking.transfers` keeps committing, so the
transfer API stays 200 while the ledger silently stops being written — the
"이체는 성공했는데 원장이 없다" signature.

The inverse is a single statement (`READ WRITE`), which is why this is a profile
of its own rather than a mode bolted onto `db.ddl` (index drop/recreate) or
`db.lock` (hold-and-release).
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.table_readonly"

# Oracle only, and only the ledger table.  Widening this dict is a design change:
# a read-only `transfers` or `accounts` would break the transfer path itself and
# turn F14-P's silent-integrity signature into a plain availability incident.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F14-P": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "pod": "testbed-oracle-0",
        "schema": "BANKING",
        "table": "LEDGER_ENTRIES",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified read-only table contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified read-only contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    scenario_id = plan["scenario"]["id"]
    validate(scenario_id, p, {})
    argv = [
        "/usr/bin/bash", "-s", "--", action, scenario_id,
        p["namespace"], p["pod"], p["schema"], p["table"],
    ]
    return argv, ORACLE_REMOTE


ORACLE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; schema="$5"; table="$6"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")

# ALL_TABLES.READ_ONLY is the authority. Never trust the DDL's exit status alone,
# because a no-op ALTER also succeeds.
state() {
  printf 'alter session set container=FREEPDB1;\nset pages 0 feedback off heading off\nselect read_only from all_tables where owner='"'"'%s'"'"' and table_name='"'"'%s'"'"';\nexit;\n' \
    "$schema" "$table" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]'
}
flip() {
  printf 'alter session set container=FREEPDB1;\nalter table %s.%s %s;\nexit;\n' \
    "$schema" "$table" "$1" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba >/dev/null
}
expect() { [[ "$(state)" == "$1" ]]; }

case "$action" in
  preflight)
    # Table must exist and be writable, or the run would "succeed" against a
    # ledger that was already frozen by an earlier, uncleaned run.
    expect NO
    ;;
  run)
    expect NO
    flip 'READ ONLY'
    expect YES
    ;;
  cleanup)
    flip 'READ WRITE'
    expect NO
    ;;
  recovery)
    # Writability is restored, but the events dropped during the window are gone
    # for good: the consumer already committed their offsets.  That permanence
    # is the scenario, not a cleanup failure.
    expect NO
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
