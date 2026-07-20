#!/usr/bin/env python3
"""Exact-snapshot Kubernetes deployment environment/config executor."""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.env"
APPROVED_TARGETS = {
    "F03-P": ("rca-testbed-commerce", "testbed-payment", "payment-service"),
    "F09-H": ("rca-testbed-commerce", "testbed-order", "order-service"),
    "F08-P": ("rca-testbed-commerce", "testbed-order", "order-service"),
}
APPROVED_KEYS = {
    "F03-P": {"SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE"},
    "F09-H": {"JAVA_TOOL_OPTIONS"},
    "F08-P": {"SPRING_APPLICATION_JSON"},
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    required = {"namespace", "deployment", "container", "baseline", "fault"}
    if set(params) != required:
        raise ExecutorError("parameters do not match the approved environment schema")
    target = (params["namespace"], params["deployment"], params["container"])
    if APPROVED_TARGETS.get(scenario_id) != target:
        raise ExecutorError("scenario environment target is not allowlisted")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    for name in ("baseline", "fault"):
        value = params[name]
        if not isinstance(value, list) or not all(isinstance(row, dict) and set(row) >= {"name"} for row in value):
            raise ExecutorError(f"{name} environment must be a Kubernetes env array")
    baseline = {row["name"]: row for row in params["baseline"]}
    fault = {row["name"]: row for row in params["fault"]}
    changed = {key for key in baseline | fault if baseline.get(key) != fault.get(key)}
    if not changed or not changed <= APPROVED_KEYS[scenario_id]:
        raise ExecutorError("environment change touches a non-allowlisted key")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"], p["container"],
        json.dumps(p["baseline"], sort_keys=True, separators=(",", ":")),
        json.dumps(p["fault"], sort_keys=True, separators=(",", ":")),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; container="$5"; baseline="$6"; fault="$7"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-container-env.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o json | jq -Sc --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | (.env // [])'; }
patch() { jq -cn --arg c "$container" --argjson e "$1" '{spec:{template:{spec:{containers:[{name:$c,env:$e}]}}}}' | "${k[@]}" patch deploy "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
healthy() { "${k[@]}" rollout status deploy/"$deploy" --timeout="$1" >/dev/null; }
check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch deployments | grep -qx yes; [[ "$(current)" == "$baseline" ]]; healthy 1s; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run) check; mkdir -p "$state_root"; current >"$state.tmp"; mv -T "$state.tmp" "$state"; patch "$fault" ;;
  cleanup) [[ -e "$state" ]] || exit 0; original=$(cat "$state"); [[ "$original" == "$baseline" ]]; patch "$original"; healthy 180s; rm -f "$state" ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
