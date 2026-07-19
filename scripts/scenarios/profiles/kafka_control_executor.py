#!/usr/bin/env python3
"""Exact-replica Kafka consumer lifecycle executor for F04-R."""
from __future__ import annotations

from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "kafka.control"

CONTRACTS: dict[str, dict[str, Any]] = {
    "F04-R": {
        "namespace": "rca-testbed-commerce",
        "deployment": "testbed-shipping",
        "baseline_replicas": 1,
        "kafka_pod": "testbed-kafka-0",
        "bootstrap_server": "localhost:9092",
        "consumer_group": "shipping-service",
        "drain_timeout_seconds": 600,
    }
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    del profile
    expected = CONTRACTS.get(scenario_id)
    if expected is None:
        raise ExecutorError("scenario has no verified independent Kafka control surface")
    if params != expected:
        raise ExecutorError("parameters do not exactly match the verified Kafka contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    location = instance["location"]
    if location.get("transport") != "kubectl" or location.get("namespace") != p["namespace"]:
        raise ExecutorError("kafka.control requires the canonical Kubernetes namespace")
    return kubectl_bash_argv([
        action,
        plan["scenario"]["id"],
        p["namespace"],
        p["deployment"],
        str(p["baseline_replicas"]),
        p["kafka_pod"],
        p["bootstrap_server"],
        p["consumer_group"],
        str(p["drain_timeout_seconds"]),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deployment="$4"; baseline="$5"
kafka_pod="$6"; bootstrap="$7"; group="$8"; drain_timeout="$9"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-kafka-consumer-replicas"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current_replicas() { "${k[@]}" get deploy "$deployment" -o 'jsonpath={.spec.replicas}'; }
available_replicas() {
  value=$("${k[@]}" get deploy "$deployment" -o 'jsonpath={.status.availableReplicas}')
  echo "${value:-0}"
}
lag() {
  "${k[@]}" exec "$kafka_pod" -- /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server "$bootstrap" --describe --group "$group" 2>/dev/null |
    awk -v group="$group" '$1 == group {sum += $6; rows++} END {if (rows == 0) exit 2; print sum + 0}'
}
wait_available() {
  deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    [[ "$(available_replicas)" == "$baseline" ]] && return 0
    sleep 5
  done
  return 1
}
wait_lag_zero() {
  deadline=$((SECONDS + drain_timeout))
  while (( SECONDS < deadline )); do
    current_lag=$(lag) && [[ "$current_lag" == "0" ]] && return 0
    sleep 5
  done
  return 1
}
check() {
  command -v kubectl >/dev/null
  "${k[@]}" auth can-i patch deployments | grep -qx yes
  [[ "$(current_replicas)" == "$baseline" ]]
  [[ "$(available_replicas)" == "$baseline" ]]
  lag >/dev/null
}
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run)
    check
    mkdir -p "$state_root"; umask 077
    current_replicas >"$state.tmp"; mv -T "$state.tmp" "$state"
    "${k[@]}" scale deploy "$deployment" --replicas=0 >/dev/null
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    original=$(cat "$state"); [[ "$original" == "$baseline" ]]
    "${k[@]}" scale deploy "$deployment" --replicas="$original" >/dev/null
    wait_available
    wait_lag_zero
    rm -f "$state"
    ;;
  recovery)
    [[ ! -e "$state" ]]
    [[ "$(current_replicas)" == "$baseline" ]]
    [[ "$(available_replicas)" == "$baseline" ]]
    [[ "$(lag)" == "0" ]]
    ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
