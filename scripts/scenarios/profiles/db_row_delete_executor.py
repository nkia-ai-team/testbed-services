#!/usr/bin/env python3
"""Delete a few live rows the way an operator's purge script does, and put them back.

F56-R reconstructs Atlassian's 2022-04-05 outage: a cleanup script meant to
remove one kind of object was handed the IDs of another, live kind and deleted
them at once, so the customers those objects belonged to lost service while
everyone else carried on.  Here the purge runs against banking Oracle
BANKING.ACCOUNTS and the IDs it is handed are the commerce partner's two live
settlement accounts (commerce-settlement, commerce-merchant).  Every commerce
checkout settles through a transfer between exactly those two accounts, so
transfer-service answers 400 "Account not found" to every settlement, commerce
payment answers 502 and every checkout fails; banking's own customers, whose
accounts are untouched, see nothing.

Like a careful purge script it copies the rows into an archive table before
deleting them, in the same transaction, after locking them.  That archive is
also the inverse: cleanup re-inserts exactly the archived rows (balances as they
were at deletion; no transfer can move money on an account that does not exist)
and drops the archive.  No schema, constraint, other row, pod or configuration
is touched, and neither the archive name nor any statement carries the
scenario id: it stays on this side of kubectl.
"""
from __future__ import annotations

import re
from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.row_delete"

# Oracle only, one table, the two settlement accounts payment-service hardcodes
# (BankingTransferClient SETTLEMENT_ACCOUNT, MERCHANT_ACCOUNT).  Widening this
# dict is a design change: deleting a customer account turns the incident into
# a banking 400 for that customer alone, and deleting more of the table is a
# different outage.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F56-R": {
        "engine": "oracle",
        "namespace": "rca-testbed-banking",
        "pod": "testbed-oracle-0",
        "schema": "BANKING",
        "table": "ACCOUNTS",
        "key_column": "ID",
        "keys": ["commerce-merchant", "commerce-settlement"],
        "archive_table": "ACCOUNTS_PURGE_ARCHIVE",
    },
}

IDENT = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified row deletion contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified row deletion contract")
    for name in ("schema", "table", "key_column", "archive_table"):
        if not IDENT.fullmatch(params[name]):
            raise ExecutorError(f"{name} is not a plain Oracle identifier")
    if not params["keys"] or not all(KEY.fullmatch(k) for k in params["keys"]):
        raise ExecutorError("keys must be plain lowercase account ids")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    argv = [
        "/usr/bin/bash", "-s", "--", action,
        p["namespace"], p["pod"], p["schema"], p["table"], p["key_column"], p["archive_table"],
        ",".join(p["keys"]),
    ]
    return argv, ORACLE_REMOTE


ORACLE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; schema="$4"; table="$5"; keycol="$6"; archive="$7"; keys_csv="$8"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
for ident in "$schema" "$table" "$keycol" "$archive"; do [[ "$ident" =~ ^[A-Z][A-Z0-9_]{0,29}$ ]] || exit 2; done
[[ "$keys_csv" =~ ^[a-z0-9][a-z0-9-]*(,[a-z0-9][a-z0-9-]*)*$ ]] || exit 2
IFS=, read -r -a keys <<<"$keys_csv"
want="${#keys[@]}"
inlist="$(printf "'%s'," "${keys[@]}")"; inlist="${inlist%,}"

# One sqlplus per call, SYSDBA inside the pod like the other Oracle executors.
# "whenever sqlerror" makes a refused statement fail the action and roll back.
sqlrun() {
  { printf 'set pages 0 feedback off heading off\nwhenever sqlerror exit failure rollback\nalter session set container=FREEPDB1;\n'
    printf '%s\n' "$1"
    printf 'exit;\n'; } | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba
}
num() { sqlrun "$1" | tr -d '[:space:]'; }
# Checks ask only how many of the listed ids exist; Top SQL may keep them.
live_rows() { num "select count(*) from $schema.$table where $keycol in ($inlist);"; }
archive_exists() { num "select count(*) from all_tables where owner = '$schema' and table_name = '$archive';"; }
archived_rows() { num "select count(*) from $schema.$archive where $keycol in ($inlist);"; }

check() {
  command -v kubectl >/dev/null
  [[ "$(live_rows)" == "$want" ]]
  [[ "$(archive_exists)" == 0 ]]
}

case "$action" in
  preflight) check ;;
  run)
    check
    # Archive first (DDL commits on its own), then lock, copy and delete in one
    # transaction so an in-flight transfer either finishes before or sees no row.
    sqlrun "create table $schema.$archive as select * from $schema.$table where 1 = 0;" >/dev/null
    sqlrun "select $keycol from $schema.$table where $keycol in ($inlist) for update;
insert into $schema.$archive select * from $schema.$table where $keycol in ($inlist);
delete from $schema.$table where $keycol in ($inlist);
commit;" >/dev/null
    [[ "$(live_rows)" == 0 ]]
    [[ "$(archived_rows)" == "$want" ]]
    ;;
  cleanup)
    if [[ "$(archive_exists)" != 0 ]]; then
      sqlrun "insert into $schema.$table select a.* from $schema.$archive a where a.$keycol in ($inlist) and not exists (select 1 from $schema.$table t where t.$keycol = a.$keycol);
commit;" >/dev/null
      [[ "$(live_rows)" == "$want" ]]
      sqlrun "drop table $schema.$archive purge;" >/dev/null
    fi
    # Without the archive the rows must already be back; nothing here invents them.
    [[ "$(live_rows)" == "$want" ]]
    [[ "$(archive_exists)" == 0 ]]
    ;;
  recovery)
    [[ "$(live_rows)" == "$want" ]]
    [[ "$(archive_exists)" == 0 ]]
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
