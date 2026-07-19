#!/usr/bin/env python3
"""Tagged PostgreSQL row-lock executor for F01-R."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance

PROFILE_ID = "db.lock"

CONTRACTS: dict[str, dict[str, Any]] = {
    "F01-P": {"engine": "oracle", "namespace": "rca-testbed-banking", "pod": "testbed-oracle-0", "schema": "BANKING", "table": "accounts", "key_column": "id", "key_value": "commerce-settlement", "client_identifier": "rca-F01-P-oracle-lock", "hold_seconds": 600},
    "F06-H": {"engine": "postgresql", "namespace": "rca-testbed-commerce", "pod": "testbed-postgres-0", "schema": "payment_schema", "table": "payments", "key_column": "id", "key_value": 1, "application_name": "rca-F06-H-payment-lock", "hold_seconds": 600},
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    if scenario_id in CONTRACTS:
        if params != CONTRACTS[scenario_id]:
            raise ExecutorError("parameters do not exactly match the verified row-lock contract")
        return
    if scenario_id not in profile["parameter_contract"]["allowed_scenarios"]:
        raise ExecutorError("scenario is not allowlisted")
    expected = profile["scenario_parameters"][scenario_id]
    if params != expected:
        raise ExecutorError("parameters do not match the approved scenario profile")
    if params["application_name"] != f"rca-{scenario_id}-inventory-lock":
        raise ExecutorError("database session tag does not bind the scenario")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    if p.get("engine") == "oracle":
        return ["/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"], p["namespace"], p["pod"], p["schema"], p["table"], p["key_column"], p["key_value"], p["client_identifier"], str(p["hold_seconds"])], ORACLE_REMOTE
    if p.get("engine") == "postgresql":
        return ["/usr/bin/bash", "-s", "--", action, plan["scenario"]["id"], p["namespace"], p["pod"], p["schema"], p["table"], p["key_column"], str(p["key_value"]), p["application_name"], str(p["hold_seconds"])], POSTGRES_POD_REMOTE
    location = instance["location"]
    if location.get("host") != "192.168.122.206" or location.get("transport") != "ssh":
        raise ExecutorError("db.lock requires the canonical tb-runner")
    argv = [
        "/usr/bin/ssh", "-i", "/root/.ssh/tb_key", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10", f"{location.get('user', 'nkia')}@{location['host']}",
        "bash", "-s", "--", action, plan["scenario"]["id"], p["db_host"],
        str(p["db_port"]), p["db_name"], p["db_user"], p["application_name"],
        str(p["product_id"]), str(p["hold_seconds"]),
    ]
    return argv, REMOTE


REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; db_host="$3"; db_port="$4"; db_name="$5"; db_user="$6"
tag="$7"; product_id="$8"; hold_seconds="$9"
[[ -r "$HOME/.pgpass" ]] || { echo "trusted PostgreSQL credential file is unavailable" >&2; exit 3; }
psql_base=(psql -X -v ON_ERROR_STOP=1 -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name")
tag_count() { "${psql_base[@]}" -tAc "SELECT count(*) FROM pg_stat_activity WHERE application_name='$tag';" | tr -d '[:space:]'; }
check() {
  command -v psql >/dev/null
  "${psql_base[@]}" -tAc 'SELECT 1' | grep -qx '1'
  [[ "$(tag_count)" == "0" ]]
  "${psql_base[@]}" -tAc "SELECT count(*) FROM inventory_schema.inventory WHERE product_id=$product_id;" | grep -qx '1'
}
cleanup_tag() {
  "${psql_base[@]}" -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='$tag' AND pid <> pg_backend_pid();" >/dev/null
}
case "$action" in
  preflight) check ;;
  run)
    check
    nohup env PGAPPNAME="$tag" psql -X -v ON_ERROR_STOP=1 \
      -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" \
      -c "BEGIN; SELECT product_id FROM inventory_schema.inventory WHERE product_id=$product_id FOR UPDATE; SELECT pg_sleep($hold_seconds); ROLLBACK;" \
      >"/tmp/${tag}.log" 2>&1 </dev/null &
    ;;
  cleanup) cleanup_tag; rm -f -- "/tmp/${tag}.log" ;;
  recovery) "${psql_base[@]}" -tAc 'SELECT 1' | grep -qx '1'; [[ "$(tag_count)" == "0" ]] ;;
  *) exit 2 ;;
esac
'''

ORACLE_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; schema="$5"; table="$6"; keycol="$7"; key="$8"; tag="$9"; hold="${10}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns"); state="/tmp/${tag}.pid"
alive() { "${k[@]}" exec "$pod" -- sh -lc 'test -s '"'"'$state'"'"' && kill -0 "$(cat '"'"'$state'"'"')" 2>/dev/null'; }
stop() { "${k[@]}" exec "$pod" -- sh -lc 'if test -s '"'"'$state'"'"'; then kill "$(cat '"'"'$state'"'"')" 2>/dev/null || true; rm -f '"'"'$state'"'"' /tmp/'"'"'$tag'"'"'.sql /tmp/'"'"'$tag'"'"'.log; fi'; }
check_row() { printf 'alter session set container=FREEPDB1;\nalter session set current_schema=%s;\nset pages 0 feedback off heading off\nselect count(*) from %s where %s='"'"'%s'"'"';\nexit;\n' "$schema" "$table" "$keycol" "$key" | "${k[@]}" exec -i "$pod" -- sqlplus -s / as sysdba | tr -d '[:space:]' | grep -qx 1; }
case "$action" in preflight) check_row; ! alive;; run) check_row; ! alive; "${k[@]}" exec "$pod" -- sh -lc 'cat > /tmp/'"'"'$tag'"'"'.sql <<EOF
alter session set container=FREEPDB1;
alter session set current_schema='"'"'$schema'"'"';
begin dbms_session.set_identifier('"'"'$tag'"'"'); end;
/
select '"'"'$keycol'"'"' from '"'"'$table'"'"' where '"'"'$keycol'"'"'='"'"''"'"'$key'"'"''"'"' for update;
host sleep '"'"'$hold'"'"'
rollback;
exit;
EOF
nohup sqlplus -s / as sysdba @/tmp/'"'"'$tag'"'"'.sql >/tmp/'"'"'$tag'"'"'.log 2>&1 </dev/null & echo $! >/tmp/'"'"'$tag'"'"'.pid' ;; cleanup) stop;; recovery) ! alive; check_row;; *) exit 2;; esac
'''

POSTGRES_POD_REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; schema="$5"; table="$6"; keycol="$7"; key="$8"; tag="$9"; hold="${10}"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
envs=(env TAG="$tag" SCHEMA="$schema" TABLE="$table" KEYCOL="$keycol" KEY="$key" HOLD="$hold")
count() { "${k[@]}" exec "$pod" -- "${envs[@]}" sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM pg_stat_activity WHERE application_name='"'"'$TAG'"'"';"' | tr -d '[:space:]'; }
row() { "${k[@]}" exec "$pod" -- "${envs[@]}" sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM $SCHEMA.$TABLE WHERE $KEYCOL=$KEY;"' | grep -qx 1; }
stop() { "${k[@]}" exec "$pod" -- "${envs[@]}" sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='"'"'$TAG'"'"' AND pid<>pg_backend_pid();"' >/dev/null; }
case "$action" in
 preflight) row; [[ "$(count)" == 0 ]] ;;
 run) row; [[ "$(count)" == 0 ]]; "${k[@]}" exec "$pod" -- "${envs[@]}" sh -lc 'nohup env PGAPPNAME="$TAG" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "BEGIN; SELECT $KEYCOL FROM $SCHEMA.$TABLE WHERE $KEYCOL=$KEY FOR UPDATE; SELECT pg_sleep($HOLD); ROLLBACK;" >"/tmp/$TAG.log" 2>&1 </dev/null &' ;;
 cleanup) stop; "${k[@]}" exec "$pod" -- rm -f "/tmp/$tag.log" ;;
 recovery) [[ "$(count)" == 0 ]]; row ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
