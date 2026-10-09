#!/usr/bin/env python3
"""Put one business-configuration row in front of the service that reads it, and take it back.

F40-R reconstructs a policy row pushed without validation into the table a
serving path reads (Google Cloud 2025-06-12: a quota policy with unintended
blank fields was inserted into the Spanner tables Service Control reads, and
every request that consulted it failed).  Here the table is commerce
pricing_schema.promotions and the reader is pricing-service, which keeps the
active promotions in memory (filled at startup and at the top of every hour)
and applies the largest discount to every checkout quote without a range check.
An operator meant "10% off" and entered 100.00: every quote total becomes 0,
the settlement transfer core-banking refuses with 400 "amount must be
positive", and every checkout fails with 502.

The row is the whole change.  Restarting pricing is how the operator makes a
new promotion take effect before the next hourly refresh; it changes no image,
env or spec field other than the restart annotation `kubectl rollout restart`
writes.  The inverse is one DELETE of exactly that row plus the same restart.
The scenario id never reaches the database or the cluster: it stays on this
side of kubectl.
"""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "db.config_row"

# One verified row in one verified table.  `discount_percent` is the typo (10.00
# intended); `description` keeps the intended "10%" as a human would have typed
# it.  Row id 4 is the first id after the seed (data.sql inserts 1~3 with
# explicit ids and ON CONFLICT DO NOTHING, so pricing restarts never touch it).
# Widening this dict is a design change.
CONTRACTS: dict[str, dict[str, Any]] = {
    "F40-R": {
        "engine": "postgresql",
        "namespace": "rca-testbed-commerce",
        "db_pod": "testbed-postgres-0",
        "table": "pricing_schema.promotions",
        "row_id": 4,
        "name": "가을 정기 세일",
        "description": "전 품목 10% 할인",
        "discount_percent": "100.00",
        "consumer_deployment": "testbed-pricing",
    },
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified configuration row contract")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified configuration row contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    return kubectl_bash_argv([
        action, p["namespace"], p["db_pod"], p["table"], str(p["row_id"]),
        p["name"], p["description"], p["discount_percent"], p["consumer_deployment"],
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; ns="$2"; db_pod="$3"; table="$4"; row_id="$5"; name="$6"; desc="$7"; pct="$8"; deploy="$9"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
[[ "$row_id" =~ ^[0-9]+$ && "$pct" =~ ^[0-9]+\.[0-9]{2}$ && "$table" =~ ^[a-z_]+\.[a-z_]+$ ]] || exit 2
# SQL travels in the environment, not on the psql command line. The first psql
# error line goes to stderr so a refused statement is readable in the runner log.
sql() { "${k[@]}" exec "$db_pod" -- env SQL="$1" sh -lc 'e=$(mktemp); psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL" 2>"$e"; rc=$?; head -n 1 "$e" >&2; rm -f "$e"; exit $rc'; }
q() { printf "'%s'" "${1//\'/\'\'}"; }
# Every check asks only whether row $row_id exists. DPM Top SQL can keep the
# executor's own statements, and a check that named discount_percent or
# "active" would point straight at the answer. Preflight proves the id is free
# before run, so whatever sits at that id afterwards is ours.
row_count() { sql "SELECT count(*) FROM $table WHERE id = $row_id;" | tr -d '[:space:]'; }
# The row is valid from a day ago for 30 days, so it is active whatever time
# zone the JVM (LocalDateTime.now()) and the database session each use.
insert() {
  sql "INSERT INTO $table (id, name, description, discount_percent, starts_at, ends_at, active) VALUES ($row_id, $(q "$name"), $(q "$desc"), $pct, date_trunc('day', now()) - interval '1 day', date_trunc('day', now()) + interval '30 days', true);" >/dev/null
}
remove() { sql "DELETE FROM $table WHERE id = $row_id;" >/dev/null; }
restart() { "${k[@]}" rollout restart deployment "$deploy" >/dev/null; }
# Same readiness test as k8s.env: a whole, current, available Deployment.
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if "${k[@]}" get deploy "$deploy" -o json | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // 0) == .spec.replicas)
        and ((.status.replicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
check() {
  command -v kubectl >/dev/null; command -v jq >/dev/null
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  [[ "$(row_count)" == 0 ]]
  healthy 1s
}
case "$action" in
  preflight) check ;;
  # Like k8s.env, run does not wait for the rollout it starts: the coordinator
  # lock is held for the whole apply and a long wait expires the runner lease.
  # The new pricing pod loads the row at startup; the controller's settle and
  # success ticks cover the ~1 minute until it is Ready.
  run) check; insert; [[ "$(row_count)" == 1 ]]; restart ;;
  cleanup)
    if [[ "$(row_count)" != 0 ]]; then remove; [[ "$(row_count)" == 0 ]]; restart; fi
    healthy 180s
    ;;
  recovery) [[ "$(row_count)" == 0 ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
