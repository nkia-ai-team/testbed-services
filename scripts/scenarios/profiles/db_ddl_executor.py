#!/usr/bin/env python3
"""Exact inverse-DDL executor for the verified PostgreSQL product-search index.

The MySQL path drops and recreates one verified index (F02-P, F33-R) or renames
one verified column and renames it back (F36-R).  A column rename is the
"migration applied before the code that reads the new name" shape: the running
service keeps selecting the old column and every such query fails with
ER_BAD_FIELD_ERROR (1054) until the rename is reverted.  MySQL 8.0 renames a
column in place (ALGORITHM=INSTANT, metadata only), so no row is rewritten and
the inverse is a single statement.

The PostgreSQL swap path (F33-P) runs an index-consolidation migration on one
verified table: a concurrent rebuild under the migration session's statement
timeout, then the old unique constraint and plain index are dropped and the
rebuild is renamed into the plain index's name.  A cancelled CREATE INDEX
CONCURRENTLY leaves its catalog entry behind as INVALID, so after the swap the
only index on the column is one the planner never uses, and the owning
service's startup `CREATE INDEX IF NOT EXISTS` skips it because the name exists.
Cleanup drops the invalid index, rebuilds the plain index first (which ends the scans)
and then the unique index it reattaches as the original constraint.
"""
from __future__ import annotations

import shlex
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

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
    # 복합 인덱스는 column 에 열을 순서대로 쉼표로 잇는다(CREATE INDEX 열 목록 그대로).
    "F33-R": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "dispatches", "index": "idx_dispatches_status_assigned",
        "column": "status,assigned_at", "minimum_rows": 1000,
    },
    # 열 이름 바꾸기: column 을 renamed_to 로 바꾸고 cleanup 에서 되돌린다(인덱스 계약과 키가 달라 섞이지 않는다).
    "F36-R": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "restaurants", "column": "region",
        "renamed_to": "delivery_region", "minimum_rows": 20,
    },
    # 인덱스 교체 마이그레이션: rebuild_index 를 statement_timeout 안에서 동시 생성하다 끊겨 INVALID 로 남은 채
    # constraint 와 index 를 지우고 rebuild_index 를 index 이름으로 바꾼다. cleanup 은 원래 둘을 다시 만든다.
    "F33-P": {
        "engine": "postgresql", "namespace": "rca-testbed-commerce", "db_pod": "testbed-postgres-0",
        "schema": "user_schema", "table": "auth_tokens", "column": "token",
        "constraint": "auth_tokens_token_key", "index": "idx_auth_tokens_token",
        "rebuild_index": "idx_auth_tokens_token_new", "statement_timeout": "2s",
        "minimum_rows": 100000,
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
    if p["engine"] == "mysql" and "renamed_to" in p:
        return ["/usr/bin/bash", "-s", "--", action, p["namespace"], p["pod"], p["database"], p["table"], p["column"], p["renamed_to"], str(p["minimum_rows"])], MYSQL_RENAME_REMOTE
    if p["engine"] == "postgresql" and "rebuild_index" in p:
        return kubectl_bash_argv([
            action, p["namespace"], p["db_pod"], p["schema"], p["table"], p["column"],
            p["constraint"], p["index"], p["rebuild_index"], p["statement_timeout"], str(p["minimum_rows"]),
        ]), PG_SWAP_REMOTE
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
# A multi-column index prints as one line (index:col1,col2); a single column prints as before.
definition() { mysql "SELECT CONCAT(INDEX_NAME,':',GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX)) FROM information_schema.statistics WHERE table_schema='$db' AND table_name='$table' AND index_name='$index' GROUP BY INDEX_NAME;"; }
# Read at most $minimum rows: a full COUNT of a multi-million-row table would itself be a heavy query in the recording window.
check() { [[ "$(mysql "SELECT COUNT(*) >= $minimum FROM (SELECT 1 FROM $table LIMIT $minimum) t;")" == 1 ]]; [[ "$(definition)" == "$index:$column" ]]; }
case "$action" in
 preflight) check ;;
 run) check; mysql "ALTER TABLE $table DROP INDEX $index;" >/dev/null; [[ -z "$(definition)" ]] ;;
 cleanup) current=$(definition); if [[ -z "$current" ]]; then mysql "CREATE INDEX $index ON $table($column);" >/dev/null; else [[ "$current" == "$index:$column" ]]; fi ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''

MYSQL_RENAME_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; db="$4"; table="$5"; column="$6"; renamed="$7"; minimum="$8"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# The first MySQL error line (e.g. ERROR 1205 lock wait timeout) goes to stderr so a failed rename is readable in the runner log.
mysql() { "${k[@]}" exec "$pod" -- env DB="$db" SQL="$1" sh -lc 'e=$(mktemp); mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB" -e "$SQL" 2>"$e"; rc=$?; grep -v "Using a password" "$e" | head -n 1 >&2; rm -f "$e"; exit $rc'; }
# Column presence is read from information_schema only (no table access, no metadata lock on the table).
has_column() { mysql "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='$db' AND table_name='$table' AND column_name='$1';"; }
# lock_wait_timeout bounds the metadata-lock wait behind in-flight transactions (server default is a year).
rename() { mysql "SET SESSION lock_wait_timeout=10; ALTER TABLE $table RENAME COLUMN $1 TO $2, ALGORITHM=INSTANT;" >/dev/null; }
original() { [[ "$(has_column "$column")" == 1 && "$(has_column "$renamed")" == 0 ]]; }
check() { [[ "$(mysql "SELECT COUNT(*) >= $minimum FROM (SELECT 1 FROM $table LIMIT $minimum) t;")" == 1 ]]; original; }
case "$action" in
 preflight) check ;;
 run) check; rename "$column" "$renamed"; [[ "$(has_column "$column")" == 0 && "$(has_column "$renamed")" == 1 ]] ;;
 cleanup) if [[ "$(has_column "$renamed")" == 1 ]]; then rename "$renamed" "$column"; fi; original ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''

PG_SWAP_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; schema="$4"; table="$5"; column="$6"
constraint="$7"; index="$8"; rebuild="$9"; timeout="${10}"; minimum="${11}"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
# SQL travels in the environment; the first psql error line goes to stderr for the runner log.
# PGOPTIONS carries a per-session setting (the migration's statement_timeout) without a multi-statement
# string, because CREATE INDEX CONCURRENTLY refuses to run inside the implicit transaction of one.
sql() { "${k[@]}" exec "$pod" -- env SQL="$1" PGOPTIONS="${2:-}" sh -lc 'e=$(mktemp); psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL" 2>"$e"; rc=$?; head -n 1 "$e" >&2; rm -f "$e"; exit $rc'; }
rel="$schema.$table"
# Every index on the table except the primary key, one line each: name|valid|definition.
indexes() { sql "SELECT c.relname || '|' || i.indisvalid || '|' || pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = '$rel'::regclass AND NOT i.indisprimary ORDER BY c.relname;"; }
has_constraint() { sql "SELECT count(*) FROM pg_constraint WHERE conrelid = '$rel'::regclass AND conname = '$constraint' AND contype = 'u';"; }
expected="$constraint|true|CREATE UNIQUE INDEX $constraint ON $rel USING btree ($column)
$index|true|CREATE INDEX $index ON $rel USING btree ($column)"
swapped="$index|false|CREATE UNIQUE INDEX $index ON $rel USING btree ($column)"
original() { [[ "$(indexes)" == "$expected" && "$(has_constraint)" == 1 ]]; }
# Read at most $minimum rows: a full count of a multi-million-row table is itself a heavy query in the recording window.
check() { [[ "$(sql "SELECT count(*) >= $minimum FROM (SELECT 1 FROM $rel LIMIT $minimum) t;")" == t ]]; original; }
case "$action" in
 preflight) check ;;
 run)
  check
  # The rebuild must be cancelled by the timeout and leave an INVALID entry; if it finished, undo it and stop.
  sql "CREATE UNIQUE INDEX CONCURRENTLY $rebuild ON $rel ($column);" "-c statement_timeout=$timeout" >/dev/null || true
  state=$(sql "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = '$rel'::regclass AND c.relname = '$rebuild';")
  if [[ "$state" != f ]]; then
    [[ -z "$state" ]] || sql "DROP INDEX $schema.$rebuild;" >/dev/null
    echo "concurrent rebuild did not end invalid (state='$state')" >&2
    exit 4
  fi
  sql "SET lock_timeout = '10s'; ALTER TABLE $rel DROP CONSTRAINT $constraint; DROP INDEX $schema.$index; ALTER INDEX $schema.$rebuild RENAME TO $index;" >/dev/null
  [[ "$(indexes)" == "$swapped" && "$(has_constraint)" == 0 ]]
  ;;
 cleanup)
  original && exit 0
  for name in $(sql "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = '$rel'::regclass AND NOT i.indisvalid ORDER BY 1;"); do
    [[ "$name" == "$index" || "$name" == "$rebuild" ]] || { echo "unexpected invalid index $name" >&2; exit 3; }
    sql "DROP INDEX $schema.$name;" >/dev/null
  done
  # The plain index first: the moment it is valid the hot lookup stops scanning, so the
  # unique rebuild that follows no longer competes with a saturated server.
  sql "CREATE INDEX IF NOT EXISTS $index ON $rel ($column);" >/dev/null
  if [[ "$(has_constraint)" == 0 ]]; then
    sql "CREATE UNIQUE INDEX IF NOT EXISTS $constraint ON $rel ($column);" >/dev/null
    sql "ALTER TABLE $rel ADD CONSTRAINT $constraint UNIQUE USING INDEX $constraint;" >/dev/null
  fi
  original
  ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
