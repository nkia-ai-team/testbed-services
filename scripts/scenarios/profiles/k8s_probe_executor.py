#!/usr/bin/env python3
"""Exact-snapshot Kubernetes readiness/liveness probe fault executor."""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.probe"
APPROVED_TARGETS = {
    "F05-H": ("rca-testbed-commerce", "testbed-payment", "payment-service", "livenessProbe"),
    "F17-R": ("rca-testbed-banking", "testbed-transfer", "transfer-service", "readinessProbe"),
    "F16-H": ("rca-testbed-commerce", "testbed-user", "user-service", "readinessProbe"),
}
F05_H_PARAMETERS = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-payment",
    "container": "payment-service",
    "probe": "livenessProbe",
    "baseline": {
        "failureThreshold": 5,
        "httpGet": {"path": "/actuator/health", "port": 8083, "scheme": "HTTP"},
        "periodSeconds": 15,
        "successThreshold": 1,
        "timeoutSeconds": 3,
    },
    "fault": {
        "failureThreshold": 5,
        "httpGet": {
            "path": "/actuator/health/f05-h-fail",
            "port": 8083,
            "scheme": "HTTP",
        },
        "periodSeconds": 15,
        "successThreshold": 1,
        "timeoutSeconds": 3,
    },
}
F17_R_PARAMETERS = {
    "namespace": "rca-testbed-banking",
    "deployment": "testbed-transfer",
    "container": "transfer-service",
    "probe": "readinessProbe",
    "baseline": {
        "failureThreshold": 3,
        "httpGet": {"path": "/actuator/health", "port": 8082, "scheme": "HTTP"},
        "periodSeconds": 10,
        "successThreshold": 1,
        "timeoutSeconds": 3,
    },
    "fault": {
        "failureThreshold": 3,
        "httpGet": {
            "path": "/actuator/health/f17-r-fail",
            "port": 8082,
            "scheme": "HTTP",
        },
        "periodSeconds": 10,
        "successThreshold": 1,
        "timeoutSeconds": 3,
    },
}
F16_H_PARAMETERS = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-user",
    "container": "user-service",
    "probe": "readinessProbe",
    "baseline": {
        "failureThreshold": 3,
        "httpGet": {"path": "/actuator/health", "port": 8085, "scheme": "HTTP"},
        "periodSeconds": 10,
        "successThreshold": 1,
        "timeoutSeconds": 3,
    },
    "fault": {
        "failureThreshold": 3,
        "httpGet": {
            "path": "/actuator/health/f16-h-fail",
            "port": 8085,
            "scheme": "HTTP",
        },
        "periodSeconds": 10,
        "successThreshold": 1,
        "timeoutSeconds": 3,
    },
}
APPROVED_PARAMETERS = {
    "F05-H": F05_H_PARAMETERS,
    "F17-R": F17_R_PARAMETERS,
    "F16-H": F16_H_PARAMETERS,
}


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    required = {"namespace", "deployment", "container", "probe", "baseline", "fault"}
    if set(params) != required:
        raise ExecutorError("parameters do not match the approved probe schema")
    target = (params["namespace"], params["deployment"], params["container"], params["probe"])
    if APPROVED_TARGETS.get(scenario_id) != target:
        raise ExecutorError("scenario probe target is not allowlisted")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if params["probe"] not in {"readinessProbe", "livenessProbe"}:
        raise ExecutorError("unsupported probe field")
    if not isinstance(params["baseline"], dict) or not isinstance(params["fault"], dict):
        raise ExecutorError("probe snapshots must be JSON objects")
    if params["baseline"] == params["fault"]:
        raise ExecutorError("fault probe must differ from baseline")
    if params != APPROVED_PARAMETERS[scenario_id]:
        raise ExecutorError(f"parameters do not match the measured {scenario_id} probe contract")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], p["namespace"], p["deployment"], p["container"], p["probe"],
        json.dumps(p["baseline"], sort_keys=True, separators=(",", ":")),
        json.dumps(p["fault"], sort_keys=True, separators=(",", ":")),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; ns="$3"; deploy="$4"; container="$5"; probe="$6"; baseline="$7"; fault="$8"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-${probe}.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get deploy "$deploy" -o json | jq -Sc --arg c "$container" --arg p "$probe" '.spec.template.spec.containers[] | select(.name==$c) | .[$p]'; }
patch() { jq -cn --arg c "$container" --arg p "$probe" --argjson v "$1" '{spec:{template:{spec:{containers:[{name:$c,($p):$v}]}}}}' | "${k[@]}" patch deploy "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
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
