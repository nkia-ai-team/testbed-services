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

The MySQL held-table path (F48-R) is a cancelled index backfill whose cleanup removed
the live table instead of its own shadow copy.  The backfill builds a shadow table
(`_vt_vrp_<uuid>_<time>_`) with the new index and copies the rows; the cancel then
"drops" the live table the way Vitess table lifecycle drops tables, by renaming it into
the hold state (`_vt_hld_<uuid>_<hold-until>_`), and leaves the shadow behind.  Every
statement that names the table fails with ER_NO_SUCH_TABLE (1146) until the held table
is renamed back.  No row is lost, so the inverse is the same rename the other way plus
dropping the shadow copy.  F48-P runs the same path on the order event outbox table, which
order-service writes in the same transaction as the order, so order creation is rolled back.

The MySQL dropped-table path (F53-R) is a migration run from a developer's machine whose
connection pointed at production: its drop step removes one live table.  A real DROP would
lose the rows, so the table is moved out of the application schema into the system schema
`mysql` under `<database>_<table>`, which the DPM collector does not report.  To the
application and to the inventory metrics this is the drop itself: the table, its rows and its
indexes leave the schema (table and index counts fall) and every statement that names it fails
with ER_NO_SUCH_TABLE (1146).  Foreign keys follow a rename, so the inverse is the same rename
back and the rows, indexes and constraints return unchanged.  F53-P runs the same path on the
payments table, which only payment-service writes, so order creation fails one hop away: payment
answers 500 on its first write and order turns that into 502 after the courier is dispatched.
F32-P runs it on the dispatches table, which only dispatch-service uses: the courier capacity check
order makes before saving an order answers 500, so order answers 503 before any write of its own.

The PostgreSQL dropped-table path (F23-P) is the same wrong-environment migration against the commerce
PostgreSQL: its drop step removes the live products table.  A real DROP would lose the rows and the foreign key
from product_variants, so a session that pg_stat_statements does not record (PGOPTIONS track=none) first moves the
table into the `public` schema, where the application never looks (product-service names product_schema on every
entity), and zeroes the moved table's statistics counters.  A real drop takes the table's scan counters out of
pg_stat_user_tables, which the DPM collector sums per database (dpm.postgresql.sql.seq_scans, .index_scans), so the
sums step down in the minute of the drop; the moved table would keep its counters under its OID, and zeroing them
leaves the same step (and matches a table restored from backup, whose counters start at zero).  The drop statement
itself is not reproduced: the DPM Top SQL collector only publishes deltas of statements it has seen in an earlier
poll, so a one-off DROP never reaches it.  To the application this is the drop itself: every statement that names
product_schema.products fails with 42P01 (relation does not exist).  Indexes, the owned id sequence and the foreign
keys follow the move, so the inverse is the same move back, again unrecorded.  If product-service restarted in the
window, its startup schema.sql recreated an empty products table; cleanup drops that one only when it holds fewer than
`minimum_rows` rows and nothing references it, and otherwise leaves both tables for a person.

The Oracle dropped-table path (F35-H) is the same wrong-environment migration against the banking
Oracle: its drop step removes the live transfers table.  It is a real DROP TABLE (no PURGE), so
the database does what it does for any drop with the recycle bin on: the table leaves the schema,
every statement that names it fails (ORA-00942, or ORA-04043 where the driver describes the table
for generated keys), and its space counts as free again, so the tablespace usage the DPM collector
reports falls by the size of the table and its indexes.  Its segment stays in the recycle bin.  The
inverse is FLASHBACK TABLE ... TO BEFORE DROP, which brings back the rows, indexes, constraints and
identity sequence but leaves the indexes and constraints under their recycle-bin names (BIN$...);
cleanup gives each its original name back from the contract, keyed by what it covers, so it needs
no state file and finishes a cleanup that stopped between the restore and the renames.

The Oracle identity-rewind path (F57-R) is a table migration that carried the rows but not the
identity generator's state.  Run replays the cut-over: the transfers table goes read-only for
`read_only_seconds` (the old side switched to read-only, so writes fail with ORA-12081 while reads go
on; kept short because run holds the runner's coordinator lock under a 30 s lease), then its identity generator restarts from the snapshot's high-water mark, `rewind_rows` keys
behind the table, and the table takes writes again.  Every new row then gets a key that is already
taken, and the primary key rejects it with ORA-00001 (Oracle 23ai names the table and column and adds
ORA-03301 with the key that exists).  The rows, indexes and constraints are untouched and reads keep
working.  Run refuses unless the primary key named in the contract covers the identity column (without
it the rewind would write duplicates instead of failing) and every key from the restart point up to
1000 below the highest key exists (so no insert lands in a gap and succeeds); if that check fails after the
read-only hold, run makes the table writable again and stops.  The inverse is READ WRITE if needed
and START WITH LIMIT VALUE, which moves the generator past the highest key; DDL is retried a bounded
number of times when it times out behind in-flight transactions, it needs no state file and it is a no-op once the table is
writable and the generator is ahead again.
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
    # 취소된 인덱스 백필: 그림자 표에 backfill_index 를 만들고 행을 옮긴 뒤, 취소 정리가 그림자 대신
    # 원래 표를 보류 이름(_vt_hld_)으로 치운다. cleanup 은 보류 표를 원래 이름으로 되돌리고 그림자를 지운다.
    "F48-R": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "menu_popularity_summary",
        "backfill_index": "idx_menu_popularity_restaurant", "backfill_column": "restaurant_id",
        "minimum_rows": 20,
    },
    # 같은 보류 표 모드를 주문 이벤트 outbox 표에: createOrder 가 같은 트랜잭션에서 이 표에 쓰므로 주문 생성이 1146 으로
    # 되돌려지고, 릴레이 폴링도 같은 오류로 실패한다. 행 약 5만 6천(24시간 보존)이라 그림자 복사는 1~2초다.
    "F48-P": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "order_outbox_events",
        "backfill_index": "idx_order_outbox_aggregate", "backfill_column": "aggregate_id",
        "minimum_rows": 1000,
    },
    # 운영 DB 를 가리킨 로컬 마이그레이션의 표 지우기: orders 를 DPM 이 보지 않는 시스템 스키마 mysql 로
    # 옮겨(이름 fooddelivery_orders) 앱과 DB 목록 지표에는 DROP 과 같게 보이게 한다. 행, 인덱스, 외래 키는
    # 그대로 따라가고 cleanup 이 같은 RENAME 으로 되돌린다. 메타데이터만 바꾸므로 표 크기와 무관하게 즉시 끝난다.
    "F53-R": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "orders", "hold_schema": "mysql",
        "minimum_rows": 1000,
    },
    # 같은 표 지우기를 결제 표에: payments 를 mysql.fooddelivery_payments 로 옮긴다. payment 만 이 표를 쓰므로
    # payment 가 첫 쓰기(결제 PENDING INSERT)에서 500 을 내고, order 는 배차 뒤 그 500 을 502 로 돌려준다.
    "F53-P": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "payments", "hold_schema": "mysql",
        "minimum_rows": 1000,
    },
    # 같은 표 지우기를 배차 표에: dispatches 를 mysql.fooddelivery_dispatches 로 옮긴다. dispatch 만 이 표를 쓰고 외래 키도
    # 없다. order 가 주문 저장 전에 부르는 배달원 용량 확인이 500 이 되어 order 는 자기 쓰기 없이 503 으로 답한다.
    "F32-P": {
        "engine": "mysql", "namespace": "rca-testbed-food", "pod": "testbed-mysql-0",
        "database": "fooddelivery", "table": "dispatches", "hold_schema": "mysql",
        "minimum_rows": 1000,
    },
    # 같은 표 지우기를 commerce PostgreSQL 상품 표에: pg_stat_statements 가 기록하지 않는 세션(track=none)이 products 를
    # public 스키마로 옮기고(앱은 늘 product_schema 를 이름에 붙여 부르므로 찾지 못함) 옮긴 표의 통계 카운터를 0 으로
    # 돌린다(실제 지우기처럼 DPM 이 합산하는 pg_stat_user_tables 의 seq_scan, idx_scan 합이 그 분에 내려간다).
    # 인덱스, id 시퀀스, 외래 키는 원본을 따라가고 cleanup 이 기록되지 않는 세션으로 되돌린다.
    "F23-P": {
        "engine": "postgresql", "namespace": "rca-testbed-commerce", "db_pod": "testbed-postgres-0",
        "schema": "product_schema", "table": "products", "hold_schema": "public",
        "minimum_rows": 1000,
    },
    # 같은 표 지우기를 banking Oracle 이체 표에: 실제 DROP TABLE(휴지통 on, PURGE 없음). 지우면 USERS 사용량이 표와
    # 인덱스만큼(109 실측 784+1600MB) 줄어 DPM 이 보고하는 tablespace used 가 떨어진다. cleanup 은 FLASHBACK TABLE 로
    # 되돌린 뒤 BIN$ 이름으로 돌아온 인덱스와 제약에 아래 원래 이름을 다시 붙인다(덮는 열과 제약 종류로 짝지음).
    "F35-H": {
        "engine": "oracle", "namespace": "rca-testbed-banking", "pod": "testbed-oracle-0",
        "pdb": "FREEPDB1", "schema": "BANKING", "table": "TRANSFERS",
        "indexes": "ID=SYS_C008658,TRANSFER_REF=SYS_C008659,FROM_ACCOUNT=IDX_TRANSFERS_FROM,"
                   "TO_ACCOUNT=IDX_TRANSFERS_TO,ORDER_ID=IDX_TRANSFERS_ORDER,STATUS=IDX_TRANSFERS_STATUS,"
                   "CREATED_AT=IDX_TRANSFERS_CREATED",
        "constraints": "C:ID=SYS_C008652,C:TRANSFER_REF=SYS_C008653,C:FROM_ACCOUNT=SYS_C008654,"
                       "C:TO_ACCOUNT=SYS_C008655,C:AMOUNT=SYS_C008656,C:STATUS=SYS_C008657,"
                       "P:ID=SYS_C008658,U:TRANSFER_REF=SYS_C008659",
        "minimum_rows": 1000, "minimum_free_mb": 2048,
    },
    # 행은 옮겼지만 식별자 생성기 상태는 옮기지 않은 표 이전의 전환: TRANSFERS 를 read_only_seconds 동안 읽기 전용으로 두고(run 이 러너
    # coordinator lock 을 쥔 채 도는데 lease 가 30초라 짧게 둔다)
    # (옛 쪽 쓰기 중지, 이체 ORA-12081), identity 생성기를 표의 최대 키보다 rewind_rows 뒤(이전 스냅숏 시점)에서 다시
    # 시작시킨 뒤 쓰기를 연다. 새 이체마다 이미 있는 키를 받아 기본 키(primary_key)가 ORA-00001 로 거절한다. 다시 시작하는
    # 자리부터 최대 키까지 키가 모두 있어야 한다(빈 키에 떨어져 성공하는 이체가 없게). cleanup 은 READ WRITE 와
    # START WITH LIMIT VALUE 로 생성기를 최대 키 뒤로 보낸다(행, 인덱스, 제약은 손대지 않음).
    "F57-R": {
        "engine": "oracle", "namespace": "rca-testbed-banking", "pod": "testbed-oracle-0",
        "pdb": "FREEPDB1", "schema": "BANKING", "table": "TRANSFERS", "identity_column": "ID",
        "primary_key": "SYS_C008658", "rewind_rows": 400000, "read_only_seconds": 6,
        "minimum_rows": 1000,
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
    if p["engine"] == "mysql" and "backfill_index" in p:
        return ["/usr/bin/bash", "-s", "--", action, p["namespace"], p["pod"], p["database"], p["table"], p["backfill_index"], p["backfill_column"], str(p["minimum_rows"])], MYSQL_HOLD_REMOTE
    if p["engine"] == "oracle" and "identity_column" in p:
        return ["/usr/bin/bash", "-s", "--", action, p["namespace"], p["pod"], p["pdb"], p["schema"], p["table"],
                p["identity_column"], p["primary_key"], str(p["rewind_rows"]), str(p["read_only_seconds"]),
                str(p["minimum_rows"])], ORACLE_IDENTITY_REMOTE
    if p["engine"] == "oracle" and "indexes" in p:
        return ["/usr/bin/bash", "-s", "--", action, p["namespace"], p["pod"], p["pdb"], p["schema"], p["table"],
                p["indexes"], p["constraints"], str(p["minimum_rows"]), str(p["minimum_free_mb"])], ORACLE_DROP_REMOTE
    if p["engine"] == "mysql" and "hold_schema" in p:
        return ["/usr/bin/bash", "-s", "--", action, p["namespace"], p["pod"], p["database"], p["table"], p["hold_schema"], str(p["minimum_rows"])], MYSQL_DROP_REMOTE
    if p["engine"] == "mysql" and "renamed_to" in p:
        return ["/usr/bin/bash", "-s", "--", action, p["namespace"], p["pod"], p["database"], p["table"], p["column"], p["renamed_to"], str(p["minimum_rows"])], MYSQL_RENAME_REMOTE
    if p["engine"] == "postgresql" and "hold_schema" in p:
        return kubectl_bash_argv([
            action, p["namespace"], p["db_pod"], p["schema"], p["table"], p["hold_schema"], str(p["minimum_rows"]),
        ]), PG_DROP_REMOTE
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

MYSQL_HOLD_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; db="$4"; table="$5"; index="$6"; column="$7"; minimum="$8"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# The first MySQL error line (e.g. ERROR 1205 lock wait timeout) goes to stderr so a failed step is readable in the runner log.
mysql() { "${k[@]}" exec "$pod" -- env DB="$db" SQL="$1" sh -lc 'e=$(mktemp); mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB" -e "$SQL" 2>"$e"; rc=$?; grep -v "Using a password" "$e" | head -n 1 >&2; rm -f "$e"; exit $rc'; }
# Table presence is read from information_schema only (no metadata lock on the table).
present() { mysql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$db' AND table_name='$1';"; }
# The backfill tool's own tables: shadow copies (_vt_vrp_) and held tables (_vt_hld_).
tool_tables() { mysql "SELECT table_name FROM information_schema.tables WHERE table_schema='$db' AND LEFT(table_name, 8) = '$1' ORDER BY table_name;"; }
original() { [[ "$(present "$table")" == 1 && -z "$(tool_tables _vt_vrp_)" && -z "$(tool_tables _vt_hld_)" ]]; }
check() { [[ "$(mysql "SELECT COUNT(*) >= $minimum FROM (SELECT 1 FROM $table LIMIT $minimum) t;")" == 1 ]]; original; }
uuid() { tr -d '-' </proc/sys/kernel/random/uuid; }
# lock_wait_timeout bounds the metadata-lock wait behind in-flight transactions (server default is a year).
case "$action" in
 preflight) check ;;
 run)
  check
  shadow="_vt_vrp_$(uuid)_$(date -u +%Y%m%d%H%M%S)_"
  mysql "SET SESSION lock_wait_timeout=10; CREATE TABLE $shadow LIKE $table; ALTER TABLE $shadow ADD INDEX $index ($column); INSERT INTO $shadow SELECT * FROM $table;" >/dev/null
  # The cancel removes the live table instead of the shadow: renamed into the hold state, rows kept.
  held="_vt_hld_$(uuid)_$(date -u -d '+2 days' +%Y%m%d%H%M%S)_"
  mysql "SET SESSION lock_wait_timeout=10; RENAME TABLE $table TO $held;" >/dev/null
  [[ "$(present "$table")" == 0 && "$(present "$held")" == 1 && "$(present "$shadow")" == 1 ]]
  ;;
 cleanup)
  if [[ "$(present "$table")" == 0 ]]; then
    held=$(tool_tables _vt_hld_)
    [[ -n "$held" && "$held" != *$'\n'* ]] || { echo "expected exactly one held table, found: ${held//$'\n'/ }" >&2; exit 3; }
    mysql "SET SESSION lock_wait_timeout=10; RENAME TABLE $held TO $table;" >/dev/null
  fi
  # A held table next to a present original would hold rows nobody restored; leave it for a person.
  [[ -z "$(tool_tables _vt_hld_)" ]] || { echo "held table left beside $table" >&2; exit 3; }
  for shadow in $(tool_tables _vt_vrp_); do
    mysql "DROP TABLE $shadow;" >/dev/null
  done
  original
  ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''

MYSQL_DROP_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; db="$4"; table="$5"; hold="$6"; minimum="$7"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# The first MySQL error line (e.g. ERROR 1205 lock wait timeout) goes to stderr so a failed step is readable in the runner log.
mysql() { "${k[@]}" exec "$pod" -- env DB="$db" SQL="$1" sh -lc 'e=$(mktemp); mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB" -e "$SQL" 2>"$e"; rc=$?; grep -v "Using a password" "$e" | head -n 1 >&2; rm -f "$e"; exit $rc'; }
# The dropped table waits in the system schema under <database>_<table>; presence is read from information_schema only.
held="${db}_${table}"
present() { mysql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$1' AND table_name='$2';"; }
original() { [[ "$(present "$db" "$table")" == 1 && "$(present "$hold" "$held")" == 0 ]]; }
check() { [[ "$(mysql "SELECT COUNT(*) >= $minimum FROM (SELECT 1 FROM $table LIMIT $minimum) t;")" == 1 ]]; original; }
# lock_wait_timeout bounds the metadata-lock wait behind in-flight transactions (server default is a year).
move() { mysql "SET SESSION lock_wait_timeout=10; RENAME TABLE $1 TO $2;" >/dev/null; }
case "$action" in
 preflight) check ;;
 run)
  check
  move "$db.$table" "$hold.$held"
  [[ "$(present "$db" "$table")" == 0 && "$(present "$hold" "$held")" == 1 ]]
  ;;
 cleanup)
  if [[ "$(present "$hold" "$held")" == 1 ]]; then
    # A table back under the original name next to the held one would hold rows nobody restored; leave it for a person.
    [[ "$(present "$db" "$table")" == 0 ]] || { echo "$table exists beside $hold.$held" >&2; exit 3; }
    move "$hold.$held" "$db.$table"
  fi
  original
  ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''

ORACLE_DROP_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; pdb="$4"; schema="$5"; table="$6"; index_map="$7"; constraint_map="$8"; minimum="$9"; free_mb="${10}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# Run one SQL script; every error is fatal and its first ORA- line goes to stderr for the runner log.
# feedback off comes first: while it is on, sqlplus echoes "Session altered." into the output.
sql() {
  local out
  out=$(printf 'set pages 0 lines 400 feedback off heading off trimspool on\nwhenever sqlerror exit failure\nalter session set container=%s;\n%s\nexit;\n' "$pdb" "$1" \
    | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba) || { grep -m1 'ORA-' <<<"$out" >&2 || true; return 1; }
  printf '%s\n' "$out" | sed '/^[[:space:]]*$/d'
}
value() { sql "$1" | tr -d '[:space:]'; }
present() { value "select count(*) from dba_tables where owner='$schema' and table_name='$table';"; }
# The dropped table in the recycle bin, restorable (exactly one, or the restore would be ambiguous).
binned() { value "select count(*) from dba_recyclebin where owner='$schema' and original_name='$table' and type='TABLE' and can_undrop='YES';"; }
# Index and constraint names keyed by what they cover, one line each, so a restored table can be compared to the contract.
inventory() {
  sql "select 'I:'||(select listagg(column_name,',') within group (order by column_position) from dba_ind_columns c where c.index_owner=i.owner and c.index_name=i.index_name)||'='||i.index_name from dba_indexes i where i.owner='$schema' and i.table_name='$table'
union all
select 'C:'||k.constraint_type||':'||(select listagg(column_name,',') within group (order by position) from dba_cons_columns c where c.owner=k.owner and c.constraint_name=k.constraint_name)||'='||k.constraint_name from dba_constraints k where k.owner='$schema' and k.table_name='$table';" | LC_ALL=C sort
}
expected() { { tr ',' '\n' <<<"$index_map" | sed 's/^/I:/'; tr ',' '\n' <<<"$constraint_map" | sed 's/^/C:/'; } | sed 's/;/,/g' | LC_ALL=C sort; }
original() { [[ "$(present)" == 1 && "$(binned)" == 0 && "$(inventory)" == "$(expected)" ]]; }
check() {
  [[ "$(value "select lower(value) from v\$parameter where name='recyclebin';")" == on ]]
  [[ "$(value "select count(*) from (select 1 from $schema.$table where rownum <= $minimum);")" == "$minimum" ]]
  original
}
# Renames take the table lock too, so they wait behind in-flight transactions like the drop.
# Restored indexes and constraints keep their recycle-bin names (BIN$...); give each its contract name back by what it covers.
rename_back() {
  local line key name stmts=""
  declare -A want=()
  while IFS= read -r line; do want["${line%%=*}"]="${line#*=}"; done < <(expected)
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    key="${line%%=*}"; name="${line#*=}"
    [[ "$name" == BIN\$* ]] || continue
    [[ -n "${want[$key]:-}" ]] || { echo "no contract name for $key ($name)" >&2; return 3; }
    if [[ "$key" == I:* ]]; then
      stmts+="alter index $schema.\"$name\" rename to ${want[$key]};"$'\n'
    else
      stmts+="alter table $schema.$table rename constraint \"$name\" to ${want[$key]};"$'\n'
    fi
  done < <(inventory)
  [[ -z "$stmts" ]] || sql "alter session set ddl_lock_timeout = 10;"$'\n'"$stmts" >/dev/null
}
case "$action" in
 preflight)
  check
  # Enough free space that the database never reclaims the dropped table's space from the recycle bin during the window.
  [[ "$(value "select nvl(floor(sum(bytes)/1048576),0) from dba_free_space where tablespace_name=(select tablespace_name from dba_tables where owner='$schema' and table_name='$table');")" -ge "$free_mb" ]]
  ;;
 run)
  check
  # ddl_lock_timeout bounds the wait behind in-flight transactions (the server default 0 fails at once with ORA-00054).
  # No PURGE: with the recycle bin on, the drop renames the table in place and keeps its segment for the restore.
  sql "alter session set ddl_lock_timeout = 10;
drop table $schema.$table;" >/dev/null
  [[ "$(present)" == 0 && "$(binned)" == 1 ]]
  ;;
 cleanup)
  if [[ "$(present)" == 0 ]]; then
    [[ "$(binned)" == 1 ]] || { echo "$schema.$table is gone and not restorable from the recycle bin" >&2; exit 3; }
    sql "alter session set ddl_lock_timeout = 10;
flashback table $schema.$table to before drop;" >/dev/null
  fi
  rename_back
  original
  ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''

ORACLE_IDENTITY_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; pdb="$4"; schema="$5"; table="$6"; column="$7"; pk="$8"; rewind="$9"; hold="${10}"; minimum="${11}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
# Run one SQL script; every error is fatal and its first ORA- line goes to stderr for the runner log.
sql() {
  local out
  out=$(printf 'set pages 0 lines 400 feedback off heading off trimspool on\nwhenever sqlerror exit failure\nalter session set container=%s;\n%s\nexit;\n' "$pdb" "$1" \
    | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba) || { grep -m1 'ORA-' <<<"$out" >&2 || true; return 1; }
  printf '%s\n' "$out" | sed '/^[[:space:]]*$/d'
}
value() { sql "$1" | tr -d '[:space:]'; }
# Run holds the runner's coordinator lock while it runs, and the runner's lease is 30 s, so every wait inside
# run is bounded: each DDL waits at most run_wait seconds behind in-flight transactions (ORA-00054 after that)
# and is tried run_tries times, 1 s apart.  Run issues at most three DDL calls (read only, restart + read write,
# and read write again on failure), so its worst case is read_only_seconds + 3 x (run_tries x run_wait +
# run_tries - 1) plus a few queries, about 21 s.  Cleanup waits a little longer (5 s, twice) per DDL.
run_wait=2; run_tries=2
ddl() {
  local wait=$1 tries=$2 i
  for ((i = 1; i <= tries; i++)); do
    sql "alter session set ddl_lock_timeout = $wait;"$'\n'"$3" >/dev/null && return 0
    (( i < tries )) && sleep 1
  done
  return 1
}
ident="from dba_tab_identity_cols c join dba_sequences s on s.sequence_owner=c.owner and s.sequence_name=c.sequence_name where c.owner='$schema' and c.table_name='$table' and c.column_name='$column'"
# A number or the script stops: an empty answer must never read as "behind" or "ahead".
number() {
  local v
  v=$(value "$1") || { echo "query failed: $1" >&2; exit 5; }
  [[ "$v" =~ ^[0-9]+$ ]] || { echo "not a number ($v): $1" >&2; exit 5; }
  printf '%s' "$v"
}
# last_number is the first key the generator has not handed out (its cache included).
next_key() { number "select s.last_number $ident;"; }
high_key() { number "select nvl(max($column),0) from $schema.$table;"; }
# Highest key first: the generator only moves up, so reading it second cannot fall behind a key handed out
# between the two reads (the other order fails whenever a cache block is taken in between).
ahead() { local n h; h=$(high_key) || exit 5; n=$(next_key) || exit 5; (( n > h )); }
read_only() { value "select read_only from dba_tables where owner='$schema' and table_name='$table';"; }
# The contract's primary key must be the only column of a P constraint on the identity column, so a reused key fails instead of duplicating.
guarded() { [[ "$(value "select count(*) from dba_constraints k join dba_cons_columns c on c.owner=k.owner and c.constraint_name=k.constraint_name where k.owner='$schema' and k.table_name='$table' and k.constraint_type='P' and k.constraint_name='$pk' and c.column_name='$column' and (select count(*) from dba_cons_columns x where x.owner=k.owner and x.constraint_name=k.constraint_name)=1;")" == 1 ]]; }
check() {
  [[ "$(value "select count(*) $ident and c.generation_type='BY DEFAULT';")" == 1 ]]
  [[ "$(value "select count(*) from (select 1 from $schema.$table where rownum <= $minimum);")" == "$minimum" ]]
  guarded
}
# Every key from the restart point up to 1000 below the highest key exists, so no new row lands in a gap and
# succeeds.  The newest 1000 keys are left out: before the table is read-only some of them can belong to
# transactions that have not committed yet (keys are handed out before commit, so the committed set has
# momentary holes there), and the restarted generator never gets near them (a run uses some 14 thousand keys).
dense() {
  local high=$1
  (( high - rewind >= minimum ))
  [[ "$(number "select count(*) from $schema.$table where $column between $((high - rewind + 1)) and $((high - 1000));")" == "$((rewind - 1000))" ]]
}
original() { check; [[ "$(read_only)" == NO ]]; ahead; }
case "$action" in
 preflight)
  original
  high=$(high_key)
  dense "$high"
  ;;
 run)
  original
  high=$(high_key)
  dense "$high"
  # Cut-over: the old side goes read-only first (writes fail, reads go on) for the hold, as in the original switch.
  ddl "$run_wait" "$run_tries" "alter table $schema.$table read only;"
  sleep "$hold"
  # Nothing is written while read-only, so the restart point is final; recheck it before switching.
  high=$(high_key)
  if ! dense "$high"; then
    ddl "$run_wait" "$run_tries" "alter table $schema.$table read write;"
    echo "keys behind the restart point are not all present" >&2
    exit 4
  fi
  # The new side takes writes with its generator at the snapshot's high-water mark.
  if ! ddl "$run_wait" "$run_tries" "alter table $schema.$table modify ($column generated by default as identity (start with $((high - rewind + 1))));
alter table $schema.$table read write;"; then
    ddl "$run_wait" "$run_tries" "alter table $schema.$table read write;"
    echo "could not restart the identity generator" >&2
    exit 4
  fi
  [[ "$(read_only)" == NO ]]
  if ahead; then echo "identity generator did not restart behind the table" >&2; exit 4; fi
  ;;
 cleanup)
  [[ "$(read_only)" == NO ]] || ddl 5 2 "alter table $schema.$table read write;"
  ahead || ddl 5 2 "alter table $schema.$table modify ($column generated by default as identity (start with limit value));"
  original
  ;;
 recovery) original ;;
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


PG_DROP_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; pod="$3"; schema="$4"; table="$5"; hold="$6"; minimum="$7"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
# SQL travels in the environment; the first psql line on stderr (error or notice) goes to stderr for the runner log.
# PGOPTIONS carries per-session settings: every step runs with pg_stat_statements tracking off, so nothing it runs reaches Top SQL.
sql() { "${k[@]}" exec "$pod" -- env SQL="$1" PGOPTIONS="${2:-}" sh -lc 'e=$(mktemp); psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL" 2>"$e"; rc=$?; head -n 1 "$e" >&2; rm -f "$e"; exit $rc'; }
quiet="-c pg_stat_statements.track=none"
# Presence is read from the catalog only (no lock on the table).
present() { sql "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = '$1' AND c.relname = '$2' AND c.relkind = 'r';" "$quiet"; }
original() { [[ "$(present "$schema" "$table")" == 1 && "$(present "$hold" "$table")" == 0 ]]; }
check() { [[ "$(sql "SELECT count(*) >= $minimum FROM (SELECT 1 FROM $schema.$table LIMIT $minimum) t;" "$quiet")" == t ]]; original; }
# lock_timeout bounds the ACCESS EXCLUSIVE wait behind in-flight transactions.
move() { sql "SET lock_timeout = '10s'; ALTER TABLE $1.$table SET SCHEMA $2;" "$quiet" >/dev/null; }
case "$action" in
 preflight) check ;;
 run)
  check
  move "$schema" "$hold"
  # A dropped table takes its scan counters out of pg_stat_user_tables, which the DPM collector sums per
  # database; the held table keeps them under its OID, so they are zeroed to leave the same step.  The table's
  # idx_scan is read from its indexes' counters, so the indexes are zeroed with it.
  sql "SELECT count(pg_stat_reset_single_table_counters(o)) FROM (SELECT '$hold.$table'::regclass::oid AS o UNION ALL SELECT indexrelid FROM pg_index WHERE indrelid = '$hold.$table'::regclass) r;" "$quiet" >/dev/null
  [[ "$(present "$schema" "$table")" == 0 && "$(present "$hold" "$table")" == 1 ]]
  ;;
 cleanup)
  if [[ "$(present "$hold" "$table")" == 1 ]]; then
    if [[ "$(present "$schema" "$table")" == 1 ]]; then
      # A restarted service recreates an empty table from its startup schema.sql; drop only that one.
      small=$(sql "SELECT count(*) < $minimum FROM (SELECT 1 FROM $schema.$table LIMIT $minimum) t;" "$quiet")
      refs=$(sql "SELECT count(*) FROM pg_constraint WHERE confrelid = '$schema.$table'::regclass;" "$quiet")
      [[ "$small" == t && "$refs" == 0 ]] || { echo "$schema.$table exists beside $hold.$table" >&2; exit 3; }
      sql "SET lock_timeout = '10s'; DROP TABLE $schema.$table;" "$quiet" >/dev/null
    fi
    move "$hold" "$schema"
  fi
  original
  ;;
 recovery) check ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
