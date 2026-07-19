#!/usr/bin/env python3
"""Bounded, tagged, read-only batch SQL workload for F02-G."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, profile_instance, kubectl_bash_argv

PROFILE_ID = "db.workload"
CONTRACTS: dict[str, dict[str, Any]] = {
    "F02-G": {"namespace": "rca-testbed-commerce", "pod": "testbed-postgres-0", "application_name": "rca-F02-G-batch-heavy-sql", "runtime_seconds": 600, "statement_timeout_seconds": 30}
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    if CONTRACTS.get(scenario_id) != params:
        raise ExecutorError("parameters do not exactly match the verified batch SQL contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    p = profile_instance(plan, PROFILE_ID)["parameters"]
    validate(plan["scenario"]["id"], p, {})
    return kubectl_bash_argv([action, plan["scenario"]["id"], p["namespace"], p["pod"], p["application_name"], str(p["runtime_seconds"]), str(p["statement_timeout_seconds"])]), REMOTE


REMOTE = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario="$2"; ns="$3"; pod="$4"; tag="$5"; runtime="$6"; timeout="$7"
k=(kubectl --kubeconfig /root/tb-kubeconfig -n "$ns")
count() { "${k[@]}" exec "$pod" -- sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM pg_stat_activity WHERE application_name='"'"'$tag'"'"';"' | tr -d '[:space:]'; }
stop() { "${k[@]}" exec "$pod" -- sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='"'"'$tag'"'"' AND pid<>pg_backend_pid();"' >/dev/null; }
case "$action" in
 preflight) [[ "$(count)" == 0 ]]; "${k[@]}" exec "$pod" -- sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*)>0 FROM order_schema.order_items;"' | grep -qx t ;;
 run) [[ "$(count)" == 0 ]]; "${k[@]}" exec "$pod" -- sh -lc 'nohup env PGAPPNAME='"'"'$tag'"'"' psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SET statement_timeout='"'"'${timeout}s'"'"'; SELECT count(*) FROM order_schema.order_items a CROSS JOIN product_schema.products b CROSS JOIN generate_series(1,100) g; SELECT pg_sleep('"'"'$runtime'"'"');" >/tmp/'"'"'$tag'"'"'.log 2>&1 </dev/null &' ;;
 cleanup) stop; "${k[@]}" exec "$pod" -- rm -f "/tmp/$tag.log" ;;
 recovery) [[ "$(count)" == 0 ]] ;;
 *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
