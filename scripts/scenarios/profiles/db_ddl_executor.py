#!/usr/bin/env python3
"""Exact inverse-DDL executor for the verified PostgreSQL product-search index."""
from __future__ import annotations

import shlex
from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.ddl"

CONTRACTS: dict[str, dict[str, Any]] = {
    "F02-R": {
        "engine": "postgresql",
        "db_host": "192.168.122.77",
        "db_port": 30432,
        "db_name": "commerce",
        "db_user": "commerce",
        "application_name": "rca-F02-R-index-ddl",
        "schema": "product_schema",
        "table": "products",
        "index": "idx_products_name",
        "expected_indexdef": "CREATE INDEX idx_products_name ON product_schema.products USING btree (name)",
        "minimum_rows": 2000,
    },
    "F02-P": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "menus", "index": "idx_menus_category",
        "column": "category_id", "minimum_rows": 1000,
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified database client and inverse DDL")
    if params != expected:
        if scenario_id == "F02-P" and not params:
            raise ExecutorError("no verified database client contract matches empty parameters")
        raise ExecutorError("parameters do not exactly match the verified inverse-DDL contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    if p["engine"] == "mysql":
        return ["/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"], p["namespace"], p["pod"], p["database"], p["table"], p["index"], p["column"], str(p["minimum_rows"])], MYSQL_REMOTE
    location = instance["location"]
    if location.get("host") != "192.168.122.206" or location.get("transport") != "ssh":
        raise ExecutorError("db.ddl requires the canonical tb-runner")
    # ssh joins argv with spaces into one remote command line, so every remote
    # argument must be shell-quoted or the DDL's parentheses break remote bash.
    remote_args = [
        action, plan["scenario"]["id"], p["db_host"], str(p["db_port"]),
        p["db_name"], p["db_user"], p["application_name"], p["schema"],
        p["table"], p["index"], p["expected_indexdef"], str(p["minimum_rows"]),
    ]
    argv = [
        "/usr/bin/ssh", "-i", "/root/.ssh/tb_key", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
        f"{location.get('user', 'nkia')}@{location['host']}", "bash", "-s", "--",
        *(shlex.quote(arg) for arg in remote_args),
    ]
    return argv, REMOTE


REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; db_host="$3"; db_port="$4"; db_name="$5"; db_user="$6"
tag="$7"; schema="$8"; table="$9"; index="${10}"; expected="${11}"; minimum_rows="${12}"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-index-definition.sql"
[[ -r "$HOME/.pgpass" ]] || { echo "trusted PostgreSQL credential file is unavailable" >&2; exit 3; }
psql_base=(psql -X -v ON_ERROR_STOP=1 -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name")
sql() { PGAPPNAME="$tag" "${psql_base[@]}" -Atc "$1"; }
current_definition() { sql "SELECT indexdef FROM pg_indexes WHERE schemaname='$schema' AND tablename='$table' AND indexname='$index';"; }
tag_count() { sql "SELECT count(*) FROM pg_stat_activity WHERE application_name='$tag' AND pid <> pg_backend_pid();" | tr -d '[:space:]'; }
check() {
  command -v psql >/dev/null
  sql 'SELECT 1' | grep -qx 1
  [[ "$(tag_count)" == 0 ]]
  [[ "$(sql "SELECT count(*) >= $minimum_rows FROM $schema.$table;")" == t ]]
  [[ "$(current_definition)" == "$expected" ]]
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run)
    check
    mkdir -p "$state_root"; umask 077
    current_definition >"$state.tmp"; mv -T "$state.tmp" "$state"
    sql "DROP INDEX $schema.$index;" >/dev/null
    [[ -z "$(current_definition)" ]]
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    original=$(cat "$state"); [[ "$original" == "$expected" ]]
    current=$(current_definition)
    if [[ -z "$current" ]]; then
      sql "$original;" >/dev/null
    else
      [[ "$current" == "$original" ]]
    fi
    [[ "$(current_definition)" == "$expected" ]]
    rm -f "$state"
    ;;
  recovery)
    [[ ! -e "$state" ]]
    sql 'SELECT 1' | grep -qx 1
    [[ "$(tag_count)" == 0 ]]
    [[ "$(current_definition)" == "$expected" ]]
    ;;
  *) exit 2 ;;
esac
'''

MYSQL_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; db="$5"; table="$6"; index="$7"; column="$8"; minimum="$9"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
mysql() { "${k[@]}" exec "$pod" -- env DB="$db" SQL="$1" sh -lc 'mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB" -e "$SQL" 2>/dev/null'; }
definition() { mysql "SELECT CONCAT(INDEX_NAME,':',COLUMN_NAME) FROM information_schema.statistics WHERE table_schema='$db' AND table_name='$table' AND index_name='$index' ORDER BY seq_in_index;"; }
check() { [[ "$(mysql "SELECT COUNT(*) >= $minimum FROM $table;")" == 1 ]]; [[ "$(definition)" == "$index:$column" ]]; }
case "$action" in
 preflight) check ;;
 run) check; mysql "ALTER TABLE $table DROP INDEX $index;" >/dev/null; [[ -z "$(definition)" ]] ;;
 cleanup) current=$(definition); if [[ -z "$current" ]]; then mysql "CREATE INDEX $index ON $table($column);" >/dev/null; else [[ "$current" == "$index:$column" ]]; fi ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
