#!/usr/bin/env python3
"""Exact-snapshot Kubernetes container resource fault executor."""
from __future__ import annotations

import json
from typing import Any

from executor_common import ExecutorError, cli, kubectl_bash_argv, profile_instance

PROFILE_ID = "k8s.resource"

# Only targets named by the scenario catalog/execution matrix are admitted.
APPROVED_TARGETS = {
    "F05-R": ("rca-testbed-commerce", "testbed-payment", "payment-service", "memory"),
    "F09-P": ("rca-testbed-commerce", "testbed-inventory", "inventory-service", "cpu"),
    "F25-H": ("rca-testbed-commerce", "testbed-postgres", "postgres", "memory"),
}
APPROVED_KINDS = {
    "F05-R": "deploy",
    "F09-P": "deploy",
    "F25-H": "statefulset",
}
F05_R_BASELINE = {
    "limits": {"cpu": "500m", "memory": "1Gi"},
    "requests": {"cpu": "200m", "memory": "512Mi"},
}
F05_R_LEVELS = tuple(
    {
        "namespace": "rca-testbed-commerce",
        "deployment": "testbed-payment",
        "container": "payment-service",
        "resource": "memory",
        "baseline": F05_R_BASELINE,
        "fault": {
            "limits": {"cpu": "500m", "memory": limit},
            "requests": {"cpu": "200m", "memory": "512Mi"},
        },
    }
    for limit in ("768Mi", "640Mi", "576Mi")
)


def validate(scenario_id: str, params: dict[str, Any], profile: dict[str, Any]) -> None:
    required = {"namespace", "deployment", "container", "resource", "baseline", "fault"}
    if set(params) != required:
        raise ExecutorError("parameters do not match the approved resource schema")
    target = (params["namespace"], params["deployment"], params["container"], params["resource"])
    if APPROVED_TARGETS.get(scenario_id) != target:
        raise ExecutorError("scenario resource target is not allowlisted")
    allowed = profile.get("parameter_contract", {}).get("allowed_scenarios")
    if allowed is not None and scenario_id not in allowed:
        raise ExecutorError("scenario is not enabled by the profile registry")
    if not isinstance(params["baseline"], dict) or not isinstance(params["fault"], dict):
        raise ExecutorError("baseline and fault resources must be JSON objects")
    key = params["resource"]
    for name in ("baseline", "fault"):
        value = params[name]
        if set(value) - {"requests", "limits"}:
            raise ExecutorError(f"{name} contains unsupported resource fields")
        if key not in value.get("limits", {}):
            raise ExecutorError(f"{name} must declare the selected resource limit")
    if params["baseline"] == params["fault"]:
        raise ExecutorError("fault resources must differ from baseline")
    if scenario_id == "F05-R" and params not in F05_R_LEVELS:
        raise ExecutorError("parameters do not match the measured F05-R memory ladder")


def build_invocation(plan: dict[str, Any], action: str) -> tuple[list[str], bytes]:
    instance = profile_instance(plan, PROFILE_ID)
    p = instance["parameters"]
    validate(plan["scenario"]["id"], p, {})
    kind = APPROVED_KINDS[plan["scenario"]["id"]]
    return kubectl_bash_argv([
        action, plan["scenario"]["id"], kind, p["namespace"], p["deployment"], p["container"],
        json.dumps(p["baseline"], sort_keys=True, separators=(",", ":")),
        json.dumps(p["fault"], sort_keys=True, separators=(",", ":")),
    ]), SCRIPT


SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
action="$1"; scenario_id="$2"; kind="$3"; ns="$4"; deploy="$5"; container="$6"; baseline="$7"; fault="$8"
state_root="${SCENARIO_PROFILE_STATE_ROOT:-/var/lib/lucida/scenario-profile-state}"
state="$state_root/${scenario_id}-container-resources.json"
k=(kubectl --kubeconfig=/root/tb-kubeconfig -n "$ns")
current() { "${k[@]}" get "$kind" "$deploy" -o json | jq -Sc --arg c "$container" '.spec.template.spec.containers[] | select(.name==$c) | (.resources // {})'; }
patch() { jq -cn --arg c "$container" --argjson r "$1" '{spec:{template:{spec:{containers:[{name:$c,resources:$r}]}}}}' | "${k[@]}" patch "$kind" "$deploy" --type=strategic --patch-file=/dev/stdin >/dev/null; }
# Not `kubectl rollout-status` -- ProgressDeadlineExceeded freezes onto a Deployment
# whose pods an injection held unready past progressDeadlineSeconds, and
# the rollout verifier then re-reads that stale verdict after a successful restore
# (batch #17 class). Compute completion from live status; readyReplicas is
# the statefulset spelling of availableReplicas.
healthy() {
  local deadline=$((SECONDS + ${1%s}))
  while :; do
    if "${k[@]}" get "$kind" "$deploy" -o json | jq -e '
        .status.observedGeneration >= .metadata.generation
        and ((.status.updatedReplicas // 0) == .spec.replicas)
        and ((.status.availableReplicas // .status.readyReplicas // 0) == .spec.replicas)
        and ((.status.replicas // 0) == .spec.replicas)' >/dev/null; then return 0; fi
    (( SECONDS < deadline )) || return 1
    sleep 2
  done
}
# `${kind}s` built "deploys" from the "deploy" kind. That is not a resource
# type, so kubectl warned on stderr while still answering yes. Harmless to the
# check itself, but profile-control reports a failed preflight's stderr as the
# reason, so every real failure here arrived labelled with a warning about the
# wrong thing (2026-08-04, F05-R). auth can-i takes the spelling get/patch use.
check() { command -v kubectl >/dev/null; command -v jq >/dev/null; "${k[@]}" auth can-i patch "$kind" | grep -qx yes; [[ "$(current)" == "$baseline" ]]; healthy 1s; }
case "$action" in
  preflight) check; [[ ! -e "$state" ]] ;;
  run)
    mkdir -p "$state_root"; umask 077
    if [[ -e "$state" ]]; then
      original=$(jq -Sc '.original' "$state"); applied=$(jq -Sc '.applied' "$state")
      [[ "$original" == "$baseline" ]]; [[ "$(current)" == "$applied" ]]
    else
      check; original=$(current)
    fi
    patch "$fault"
    jq -cn --argjson original "$original" --argjson applied "$fault" \
      '{original:$original,applied:$applied}' >"$state.tmp"
    mv -T "$state.tmp" "$state"
    ;;
  cleanup)
    [[ -e "$state" ]] || exit 0
    original=$(jq -Sc '.original' "$state"); [[ "$original" == "$baseline" ]]
    patch "$original"; healthy 180s; rm -f "$state"
    ;;
  recovery) [[ ! -e "$state" ]]; [[ "$(current)" == "$baseline" ]]; healthy 1s ;;
  *) exit 2 ;;
esac
'''


if __name__ == "__main__":
    cli(PROFILE_ID, build_invocation, validate)
